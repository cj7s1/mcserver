"""Job runner + lifecycle/backup handlers + reconcile, against the fake agent."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.backups.models import Backup, BackupKind, BackupStatus, RetentionPolicy
from apps.core.errors import ApiError, ConflictError
from apps.instances.models import Instance
from apps.instances.reconcile import reconcile_instance, reconcile_node
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentUnavailable

pytestmark = pytest.mark.django_db


@pytest.fixture
def inst(make_instance, fake_agent):
    instance = make_instance(name="Jobbed")
    fake_agent.prepare(instance.id, {"server.properties": "motd=hi\n"})
    return instance


@pytest.fixture
def temp_handlers():
    added: list[str] = []

    def _register(kind, fn):
        runner.HANDLERS[kind] = fn
        added.append(kind)

    yield _register
    for kind in added:
        runner.HANDLERS.pop(kind, None)


def run(job_kind, instance, capture, **params):
    with capture(execute=True):
        job = runner.enqueue(job_kind, title=job_kind, instance=instance, params=params)
    job.refresh_from_db()
    return job


# --- runner semantics -------------------------------------------------------------------------
def test_enqueue_unknown_kind():
    with pytest.raises(ValueError, match="No handler"):
        runner.enqueue("does-not-exist", title="x")


def test_exclusive_lock_and_non_exclusive_jobs(inst):
    job = runner.enqueue("stop", title="Stop", instance=inst)
    inst.refresh_from_db()
    assert inst.active_job_id == job.id
    with pytest.raises(ConflictError) as exc:
        runner.enqueue("start", title="Start", instance=inst)
    assert exc.value.error_code == "instance_busy"
    assert exc.value.details == {"job_id": str(job.id), "kind": "stop"}
    side = runner.enqueue("backup_verify", title="Verify", instance=inst, exclusive=False)
    assert side.exclusive is False
    inst.refresh_from_db()
    assert inst.active_job_id == job.id


def test_stale_lock_from_finished_job_is_ignored(inst):
    old = Job.objects.create(kind="stop", title="old", instance=inst, status=JobStatus.SUCCEEDED)
    Instance.objects.filter(pk=inst.pk).update(active_job=old)
    job = runner.enqueue("start", title="Start", instance=inst)
    inst.refresh_from_db()
    assert inst.active_job_id == job.id


def test_successful_job(inst, temp_handlers, django_capture_on_commit_callbacks):
    def handler(job, ctx):
        ctx.progress(0.5, "halfway", force=True)
        return {"answer": 42}

    temp_handlers("t_ok", handler)
    job = run("t_ok", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED
    assert job.result == {"answer": 42}
    assert job.progress == 1.0
    assert any("halfway" in line for line in job.log)
    assert job.started_at and job.finished_at
    inst.refresh_from_db()
    assert inst.active_job_id is None
    assert inst.status_updated_at is not None  # post-job reconcile ran


def test_api_error_marks_failed_and_releases_lock(inst, temp_handlers, django_capture_on_commit_callbacks):
    def handler(job, ctx):
        raise ApiError("Nope", code="custom_code")

    temp_handlers("t_api", handler)
    job = run("t_api", inst, django_capture_on_commit_callbacks)
    assert (job.status, job.error, job.error_code) == (JobStatus.FAILED, "Nope", "custom_code")
    assert job.log[-1].endswith("ERROR: Nope")
    inst.refresh_from_db()
    assert inst.active_job_id is None


def test_crash_marks_failed_with_internal_error(inst, temp_handlers, django_capture_on_commit_callbacks):
    def handler(job, ctx):
        raise KeyError("boom")

    temp_handlers("t_crash", handler)
    job = run("t_crash", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED
    assert job.error_code == "internal_error"
    assert "boom" in job.error
    inst.refresh_from_db()
    assert inst.active_job_id is None


def test_interrupted_job_is_not_rerun(inst, temp_handlers):
    calls = []
    temp_handlers("t_once", lambda job, ctx: calls.append(job.id))
    job = runner.enqueue("t_once", title="once", instance=inst)
    Job.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING)
    runner.run_job(str(job.id))
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.error_code == "interrupted"
    assert calls == []
    inst.refresh_from_db()
    assert inst.active_job_id is None
    # finished jobs are ignored when redelivered
    runner.run_job(str(job.id))
    assert calls == []
    runner.run_job("00000000-0000-0000-0000-000000000000")  # unknown id is a no-op


# --- lifecycle handlers -----------------------------------------------------------------------------
def test_install_start_restart_recreate_stop(inst, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    job = run("install", inst, capture, server_properties={"motd": "Fresh"}, start=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.text(inst.id, "server.properties") == "motd=hi\n"  # existing file never overwritten
    assert fake_agent.containers[str(inst.id)]["running"] is False

    assert run("start", inst, capture).status == JobStatus.SUCCEEDED
    c = fake_agent.containers[str(inst.id)]
    assert c["running"] is True
    inst.refresh_from_db()
    assert inst.status == "running" and inst.installed_at is not None
    first_container = c["id"]

    # unchanged configuration: restart restarts in place
    assert run("restart", inst, capture).status == JobStatus.SUCCEEDED
    assert fake_agent.containers[str(inst.id)]["id"] == first_container

    # changed configuration: restart recreates the container from the new spec
    Instance.objects.filter(pk=inst.pk).update(memory_mb=3072)
    assert run("restart", inst, capture).status == JobStatus.SUCCEEDED
    c = fake_agent.containers[str(inst.id)]
    assert c["id"] != first_container and c["spec"]["env"]["MEMORY"] == "3072M" and c["running"]

    assert run("recreate", inst, capture).status == JobStatus.SUCCEEDED
    assert fake_agent.containers[str(inst.id)]["running"] is True  # was running => restarted

    assert run("stop", inst, capture).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    assert inst.status == "stopped" and inst.desired_state == "stopped"
    assert fake_agent.get_file(inst.id, "server.properties") is not None  # data untouched


def test_start_reapplies_when_container_missing(inst, fake_agent, django_capture_on_commit_callbacks):
    Instance.objects.filter(pk=inst.pk).update(container_spec_hash="stale", installed_at=timezone.now())
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED
    assert fake_agent.called("apply")


def test_delete_failure_keeps_instance(inst, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.failed_operations["delete_data"] = ("Permission denied", "internal_error")
    job = run("delete", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED
    assert Instance.objects.filter(pk=inst.pk).exists()


def test_clone_requires_stopped_source(inst, fake_agent, make_instance, django_capture_on_commit_callbacks):
    target = make_instance(name="Clone")
    fake_agent.put_file(inst.id, "world/level.dat", b"\x00world")
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    job = run("clone", inst, django_capture_on_commit_callbacks, target_id=str(target.id))
    assert job.status == JobStatus.FAILED and job.error_code == "source_running"
    fake_agent.stop(inst.id)
    job = run("clone", inst, django_capture_on_commit_callbacks, target_id=str(target.id), exclude_worlds=True)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.get_file(target.id, "server.properties") == b"motd=hi\n"
    assert fake_agent.get_file(target.id, "world/level.dat") is None
    assert str(target.id) in fake_agent.containers


# --- backups / restore -----------------------------------------------------------------------------
def test_backup_then_restore_round_trip(inst, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    fake_agent.put_file(inst.id, "world/region/r.0.0.mca", b"original")
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)

    job = run("backup", inst, capture, name="Before the dragon", kind="manual")
    assert job.status == JobStatus.SUCCEEDED, job.error
    backup = Backup.objects.get(pk=job.result["backup_id"])
    assert backup.status == BackupStatus.SUCCEEDED and backup.snapshot_id
    assert backup.consistent is True
    assert [c for _, c in fake_agent.commands] == ["save-off", "save-all flush", "save-on"]

    fake_agent.put_file(inst.id, "world/region/r.0.0.mca", b"griefed")
    fake_agent.put_file(inst.id, "new.txt", "created after the backup")

    job = run("restore", inst, capture, backup_id=str(backup.id), pre_restore_backup=True)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.get_file(inst.id, "world/region/r.0.0.mca") == b"original"
    assert fake_agent.get_file(inst.id, "new.txt") is None
    assert fake_agent.containers[str(inst.id)]["running"] is True  # was running => started again
    safety = Backup.objects.get(pk=job.result["safety_backup"])
    assert safety.kind == BackupKind.PRE_RESTORE and safety.status == BackupStatus.SUCCEEDED
    # the safety backup holds the pre-restore state
    snap = next(s for s in fake_agent.repos[str(inst.id)] if s["id"] == safety.snapshot_id)
    assert snap["tree"]["files"]["world/region/r.0.0.mca"] == b"griefed"


def test_failed_backup_is_recorded(inst, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.failed_operations["backup"] = ("restic backup failed: disk full", "restic_failed")
    job = run("backup", inst, django_capture_on_commit_callbacks, kind="manual")
    assert job.status == JobStatus.FAILED and job.error_code == "restic_failed"
    backup = Backup.objects.get(instance=inst)
    assert backup.status == BackupStatus.FAILED and "disk full" in backup.error


def test_backup_retention_forgets_old_snapshots(inst, fake_agent, django_capture_on_commit_callbacks):
    policy = RetentionPolicy.objects.create(instance=inst, keep_last=10, keep_daily=0, keep_weekly=0, keep_monthly=0)
    capture = django_capture_on_commit_callbacks
    ids = [run("backup", inst, capture, kind="manual").result["backup_id"] for _ in range(4)]
    for age, bid in enumerate(reversed(ids), start=1):  # ids[-1] is 1h old ... ids[0] 4h old
        Backup.objects.filter(pk=bid).update(created_at=timezone.now() - timedelta(hours=age))
    Backup.objects.filter(pk=ids[0]).update(pinned=True)
    policy.keep_last = 2
    policy.save()
    job = run("backup", inst, capture, kind="manual")
    assert job.status == JobStatus.SUCCEEDED, job.error
    doomed = Backup.objects.filter(pk__in=[ids[1], ids[2]])
    assert sorted(job.result["retention_removed"]) == sorted(b.snapshot_id for b in doomed)
    alive = {str(b) for b in Backup.objects.filter(instance=inst, status=BackupStatus.SUCCEEDED).values_list("id", flat=True)}
    assert alive == {ids[0], ids[3], job.result["backup_id"]}
    assert {s["id"] for s in fake_agent.repos[str(inst.id)]} == set(
        Backup.objects.filter(pk__in=alive).values_list("snapshot_id", flat=True)
    )


def test_backup_api_flow(clients, inst, fake_agent, django_capture_on_commit_callbacks):
    owner = clients["owner"]
    with django_capture_on_commit_callbacks(execute=True):
        resp = owner.post(f"/api/instances/{inst.id}/backups", {"name": "API backup"}, format="json")
    assert resp.status_code == 202
    listing = owner.get(f"/api/instances/{inst.id}/backups").json()
    assert [b["name"] for b in listing["backups"]] == ["API backup"]
    bid = listing["backups"][0]["id"]
    unconfirmed = owner.post(f"/api/instances/{inst.id}/backups/{bid}/restore", {}, format="json")
    assert unconfirmed.status_code == 400 and unconfirmed.json()["error"]["code"] == "confirmation_required"
    fake_agent.put_file(inst.id, "server.properties", "motd=changed\n")
    with django_capture_on_commit_callbacks(execute=True):
        resp = owner.post(
            f"/api/instances/{inst.id}/backups/{bid}/restore", {"confirm": True, "pre_restore_backup": False}, format="json"
        )
    assert resp.status_code == 202
    assert Job.objects.get(pk=resp.json()["id"]).status == JobStatus.SUCCEEDED
    assert fake_agent.text(inst.id, "server.properties") == "motd=hi\n"
    assert owner.patch(f"/api/instances/{inst.id}/backups/{bid}", {"pinned": True}, format="json").status_code == 200
    pinned_delete = owner.delete(f"/api/instances/{inst.id}/backups/{bid}")
    assert pinned_delete.status_code == 409 and pinned_delete.json()["error"]["code"] == "pinned"
    bad_policy = owner.put(f"/api/instances/{inst.id}/backups/policy", {"excludes": ["../etc"]}, format="json")
    assert bad_policy.status_code == 400


# --- reconcile -----------------------------------------------------------------------------------------
def test_reconcile_marks_running_and_installed(inst, fake_agent):
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert inst.status == "running"
    assert inst.installed_at is not None and inst.ready_at is not None
    assert inst.container_state == "running" and inst.health == "healthy"
    assert inst.container_spec_hash  # adopted from the container label


def test_reconcile_agent_down_is_unknown(inst, fake_agent):
    fake_agent.failures["status"] = AgentUnavailable("agent down")
    reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert inst.status == "unknown"
    assert inst.status_detail.startswith("Node agent unreachable")


def test_reconcile_docker_down_is_unknown_with_reason(inst, fake_agent):
    from apps.nodes.client import AgentError

    fake_agent.failures["status"] = AgentError("The Docker daemon on this node is not reachable", "docker_unavailable", 503)
    reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert inst.status == "unknown"
    assert "Docker is not reachable" in inst.status_detail


def test_reconcile_node_counts_orphans(inst, fake_agent, node):
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    fake_agent.apply("11111111-1111-1111-1111-111111111111", {"image": "itzg/minecraft-server:java17", "ports": []})
    result = reconcile_node(node)
    assert result == {"instances": 1, "changed": 1, "orphans": 1}
    node.refresh_from_db()
    assert node.last_seen_at is not None and node.last_error == ""
    fake_agent.failures["list_containers"] = AgentUnavailable("down")
    reconcile_node(node)
    node.refresh_from_db()
    assert "down" in node.last_error
    inst.refresh_from_db()
    assert inst.status == "unknown"


def test_job_log_lines_are_stamped_in_the_panel_time_zone(monkeypatch):
    """With TZ=America/Los_Angeles the log must not show UTC times (every other time in the panel is local)."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 11, 10, 1, 30, 5, tzinfo=UTC))
    job = Job(kind="start", title="Start server")
    with timezone.override(ZoneInfo("America/Los_Angeles")):
        job.append_log("Starting container")
    with timezone.override(ZoneInfo("Europe/Berlin")):
        job.append_log("Done")
    assert job.log == ["17:30:05 Starting container", "02:30:05 Done"]
