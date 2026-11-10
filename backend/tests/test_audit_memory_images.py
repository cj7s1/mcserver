"""Memory admission, runtime images, spec hashes, player sampling, status writes, clones and
adopting backups (audit 2026-11-09)."""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.backups.models import Backup, BackupStatus
from apps.core.models import SystemSetting
from apps.instances import reconcile, scheduling
from apps.instances.models import Instance, Schedule
from apps.instances.spec import build_spec, current_spec_hash, legacy_spec_hash, restart_required, spec_outdated
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.metrics.models import MetricSample, PlayerSeen
from apps.metrics.tasks import sample_all

pytestmark = pytest.mark.django_db
REPO = Path(__file__).resolve().parents[2]
GIB = 1024**3


@pytest.fixture
def server(make_instance, fake_agent):
    def _make(name: str, *, running: bool = True, **fields):
        fields.setdefault("desired_state", "running" if running else "stopped")
        inst = make_instance(name=name, **fields)
        fake_agent.apply(inst.id, build_spec(inst), start=running)
        fake_agent.put_file(inst.id, "server.properties", "motd=x\n")
        Instance.objects.filter(pk=inst.pk).update(
            container_spec_hash=current_spec_hash(inst),
            installed_at=timezone.now(),
            status="running" if running else "stopped",
            container_state="running" if running else "exited",
        )
        inst.refresh_from_db()
        return inst

    return _make


def run(kind, inst, capture, **params) -> Job:
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    return job


# --- backend-lifecycle#2 / scenario-boot-shutdown#2: memory admission ---------------------------------------
# fake node: 16 GiB = 16384 MB, reserve 4096 MB -> 12288 MB for servers; 8192 MB heap = 8192 + 2457 = 10649 MB
def test_a_start_that_does_not_fit_is_refused_with_the_numbers(server, fake_agent, clients, django_capture_on_commit_callbacks):
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048)  # 2816 MB: 10649 + 2816 > 12288
    resp = clients["owner"].post(f"/api/instances/{other.id}/power", {"action": "start"}, format="json")
    assert resp.status_code == 409, resp.content
    error = resp.json()["error"]
    assert error["code"] == "insufficient_memory"
    assert "2816 MB" in error["message"] and "10649 MB" in error["message"] and "16384 MB" in error["message"]
    assert error["details"]["reserve_mb"] == 4096 and error["details"]["running"][0]["name"] == "Big"
    assert not Job.objects.filter(instance=other).exists()

    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{other.id}/power", {"action": "start", "allow_overcommit": True}, format="json"
        )
    assert resp.status_code == 202, resp.content
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED and job.params == {"allow_overcommit": True}
    assert any("It is started anyway, as requested" in line for line in job.log)
    assert fake_agent.containers[str(other.id)]["running"]


def test_the_reserve_is_a_system_setting(server, clients):
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048)
    SystemSetting.set("memory_reserve_mb", 1024)
    resp = clients["owner"].post(f"/api/instances/{other.id}/power", {"action": "start"}, format="json")
    assert resp.status_code == 202, resp.content


def test_a_restart_of_a_running_server_and_a_stop_are_never_refused(server, fake_agent, clients):
    fake_agent.memory_bytes = 4 * GIB  # already overcommitted
    inst = server("Running")
    for action in ("restart", "stop"):
        Job.objects.filter(instance=inst).update(status=JobStatus.SUCCEEDED)
        Instance.objects.filter(pk=inst.pk).update(active_job=None)
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": action}, format="json")
        assert resp.status_code == 202, (action, resp.content)


def test_a_start_job_checks_memory_too(server, fake_agent, django_capture_on_commit_callbacks):
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048)
    job = run("start", other, django_capture_on_commit_callbacks)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "insufficient_memory")
    assert not fake_agent.containers[str(other.id)]["running"]


def test_resume_after_boot_waits_for_memory_and_gives_up_after_ten_minutes(server, fake_agent, monkeypatch):
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048, desired_state="running")
    Instance.objects.filter(pk=other.pk).update(resume_after_boot=True, resume_flagged_at=timezone.now() - timedelta(hours=8))
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=2))
    result = scheduling.resume_flagged_servers()
    assert result["started"] is None and result["waiting"] == 1
    assert Instance.objects.get(pk=other.pk).resume_after_boot is True
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=11))
    result = scheduling.resume_flagged_servers()
    assert str(other.id) in result["cleared"]
    event = AuditEvent.objects.get(action="instance.not_resumed_after_boot", target_id=str(other.id))
    assert event.details["reason"] == "insufficient_memory" and event.details["need_mb"] == 2816


def test_a_scheduled_start_that_does_not_fit_waits(server, monkeypatch):
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048)
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(days=1))
    sched = Schedule.objects.create(
        instance=other,
        name="Start",
        action="start",
        cron="0 17 * * *",
        only_when_running=False,
        next_run_at=timezone.now() - timedelta(seconds=30),
    )
    scheduling.run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("Waiting: Not enough memory on node")
    assert not Job.objects.filter(instance=other).exists()


def test_a_container_limit_above_the_node_ram_is_refused(server, clients):
    inst = server("Survival")
    url = f"/api/instances/{inst.id}"
    resp = clients["owner"].patch(url, {"memory_mb": 16384}, format="json")
    assert resp.status_code == 400 and "has only 16384 MB of RAM" in str(resp.json())
    resp = clients["owner"].patch(url, {"memory_overhead_mb": 100000}, format="json")
    assert resp.status_code == 400
    assert clients["owner"].patch(url, {"memory_mb": 4096, "memory_overhead_mb": 1024}, format="json").status_code == 200


# --- frontend-create-admin#1 / agent-docker#4: runtime images -----------------------------------------------
def test_a_start_asks_for_a_newer_image_and_logs_the_change(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server("Survival")
    fake_agent.stop(inst.id)
    fake_agent.retire(inst.id, settle_seconds=0)  # stopped: the agent keeps the record of the last run
    fake_agent.features = ["retire", "pull_newer"]
    fake_agent.registry_image_ids["itzg/minecraft-server:stable-java17"] = "sha256:" + "b" * 64
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.called("apply")[-1][1]["pull"] == "newer"
    text = "\n".join(job.log)
    assert "Downloaded the runtime image" in text and "The runtime image is a newer build now" in text


def test_an_older_agent_gets_pull_missing(server, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.features = ["retire"]  # an agent from before pull="newer"
    inst = server("Survival", running=False)
    fake_agent.retire(inst.id, settle_seconds=0)
    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    assert fake_agent.called("apply")[-1][1]["pull"] == "missing"


def test_the_runtime_image_channel_reaches_existing_servers(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server("Survival")
    SystemSetting.set("runtime_image_channel", "pinned:2026.10.1")
    assert build_spec(inst)["image"] == "itzg/minecraft-server:2026.10.1-java17"
    assert restart_required(inst)
    job = run("restart", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    inst.refresh_from_db()
    assert inst.image == "itzg/minecraft-server:2026.10.1-java17"
    assert fake_agent.containers[str(inst.id)]["image"] == inst.image
    assert not restart_required(inst)


# --- backend-lifecycle#8 + renaming ---------------------------------------------------------------------
def test_renaming_a_server_does_not_make_it_restart_required(server, clients):
    inst = server("Survival")
    assert clients["owner"].patch(f"/api/instances/{inst.id}", {"name": "Creative"}, format="json").status_code == 200
    inst.refresh_from_db()
    assert not restart_required(inst)


def test_a_hash_of_an_older_release_is_taken_over_once(server):
    inst = server("Survival")
    Instance.objects.filter(pk=inst.pk).update(container_spec_hash=legacy_spec_hash(build_spec(inst)))
    inst.refresh_from_db()
    assert not spec_outdated(inst)
    assert Instance.objects.get(pk=inst.pk).container_spec_hash == current_spec_hash(inst)


def test_the_container_label_is_never_taken_as_the_panels_hash(server, fake_agent):
    inst = server("Survival")
    Instance.objects.filter(pk=inst.pk).update(container_spec_hash="")
    fake_agent.containers[str(inst.id)]["spec_hash"] = "agent-own-hash1"
    reconcile.reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert inst.container_spec_hash == "" and not restart_required(inst)


# --- live-ops#8: status loop writes ---------------------------------------------------------------------
def test_an_unchanged_observation_writes_nothing(server, fake_agent):
    inst = server("Survival")
    reconcile.reconcile_instance(inst.id)
    first = Instance.objects.get(pk=inst.pk).status_updated_at
    assert first is not None
    reconcile.reconcile_instance(inst.id)
    assert Instance.objects.get(pk=inst.pk).status_updated_at == first
    fake_agent.stop(inst.id)
    reconcile.reconcile_instance(inst.id)
    assert Instance.objects.get(pk=inst.pk).status_updated_at > first


# --- backend-jobs#7 / live-ops#8: players by status ping -------------------------------------------------
def test_players_are_counted_with_the_status_ping_when_the_agent_can(server, fake_agent):
    inst = server("Survival")
    fake_agent.features = ["retire", "ping"]
    fake_agent.ping_answer = {"online": 2, "max": 20, "sample": [{"name": "Alex", "id": "x"}, {"name": "Steve", "id": "y"}]}
    assert sample_all() == 1
    sample = MetricSample.objects.get(instance=inst)
    assert (sample.players_online, sample.players_max) == (2, 20)
    assert set(PlayerSeen.objects.filter(instance=inst).values_list("name", flat=True)) == {"Alex", "Steve"}
    assert not [c for c in fake_agent.commands if c[1] == "list"]  # no RCON connection, no log lines


def test_players_fall_back_to_rcon_without_the_ping_feature(server, fake_agent):
    fake_agent.features = ["retire"]  # an agent from before the ping
    inst = server("Survival")
    sample_all()
    assert (str(inst.id), "list") in fake_agent.commands
    assert MetricSample.objects.get(instance=inst).players_online == 0


# --- clone without worlds ------------------------------------------------------------------------------
def test_a_clone_without_worlds_names_the_world_folders(server, fake_agent, make_instance, django_capture_on_commit_callbacks):
    src = server("Source", running=False)
    fake_agent.put_file(src.id, "server.properties", "level-name=survival\n")
    fake_agent.put_file(src.id, "survival/level.dat", b"w")
    fake_agent.put_file(src.id, "config/a.toml", "x")
    target = make_instance(name="Copy")
    job = run("clone", src, django_capture_on_commit_callbacks, target_id=str(target.id), exclude_worlds=True)
    assert job.status == JobStatus.SUCCEEDED, job.error
    _args, kwargs = fake_agent.called("clone")[-1]
    assert kwargs["level_name"] == "survival" and kwargs["layout"] == "vanilla"
    assert fake_agent.get_file(target.id, "survival/level.dat") is None
    assert fake_agent.get_file(target.id, "config/a.toml") == b"x"


# --- backend-backups#5 / deploy#5: adopt_backups ----------------------------------------------------------
def test_adopt_backups_creates_rows_for_unknown_snapshots(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server("Survival")
    job = run("backup", inst, django_capture_on_commit_callbacks)
    known = Backup.objects.get(job=job)
    fake_agent.create_backup(inst.id, "3f0e1c2a-0000-4000-8000-000000000001", tags=["kind:scheduled"])
    lost = Backup.objects.filter(pk=known.pk)
    out = io.StringIO()
    call_command("adopt_backups", "--instance", str(inst.id), "--dry-run", stdout=out)
    assert "1 unknown to the panel" in out.getvalue() and "would be taken over" in out.getvalue()
    assert Backup.objects.filter(instance=inst).count() == 1
    call_command("adopt_backups", "--all", stdout=out)
    adopted = Backup.objects.get(pk="3f0e1c2a-0000-4000-8000-000000000001")
    assert adopted.status == BackupStatus.SUCCEEDED and adopted.kind == "scheduled"
    assert adopted.snapshot_id == fake_agent.repos[str(inst.id)][-1]["id"] and adopted.total_bytes is not None
    assert adopted.meta["adopted"] is True and adopted.meta["node_id"] == inst.node_id
    assert lost.get().status == BackupStatus.SUCCEEDED
    call_command("adopt_backups", "--all", stdout=out)  # a second run finds nothing new
    assert Backup.objects.filter(instance=inst).count() == 2
    assert AuditEvent.objects.filter(action="backup.adopted").count() == 1


def test_adopt_backups_needs_a_known_server():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="no server with the id"):
        call_command("adopt_backups", "--instance", "3f0e1c2a-0000-4000-8000-000000000009", stdout=io.StringIO())


# --- live-ops#5: restart policies ---------------------------------------------------------------------
def test_panel_agent_and_postgres_come_back_after_a_stop_through_the_docker_api():
    compose = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())
    for service in ("panel", "agent", "postgres"):
        assert compose["services"][service]["restart"] == "always", service
