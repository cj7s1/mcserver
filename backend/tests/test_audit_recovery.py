"""Jobs that stop a running server and then fail (audit 2026-11-09): after a failure that provably
changed nothing the server is started again; after an unclear one it is recorded as stopped. And
restoring a "Before update" backup restores the software together with the files."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.backups.models import Backup, BackupKind, BackupStatus
from apps.instances.models import Instance
from apps.instances.spec import build_spec, current_spec_hash, restart_required
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentError, AgentUnavailable

pytestmark = pytest.mark.django_db
STARTED_AGAIN = "Nothing was changed; the server was started again"
LEFT_STOPPED = "so the server was left stopped"


@pytest.fixture
def server(make_instance, fake_agent):
    inst = make_instance(name="Survival", desired_state="running")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.put_file(inst.id, "server.properties", "motd=x\n")
    fake_agent.put_file(inst.id, "world/level.dat", b"current")
    Instance.objects.filter(pk=inst.pk).update(
        container_spec_hash=current_spec_hash(inst), installed_at=timezone.now(), status="running", container_state="running"
    )
    inst.refresh_from_db()
    return inst


def run(kind, inst, capture, **params) -> Job:
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    return job


def running(fake_agent, inst) -> bool:
    return bool(fake_agent.containers.get(str(inst.id), {}).get("running"))


def log(job) -> str:
    return "\n".join(job.log)


def backup_of(inst, fake_agent, capture, **params) -> Backup:
    job = run("backup", inst, capture, **params)
    assert job.status == JobStatus.SUCCEEDED, job.error
    return Backup.objects.get(job=job)


# --- backend-backups#4 / scenario-boot-shutdown#7 -------------------------------------------------------
def test_a_restore_whose_safety_backup_fails_starts_the_server_again(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    fake_agent.failed_operations["backup"] = ("repository is locked", "repo_locked")
    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(backup.id))
    assert (job.status, job.error_code) == (JobStatus.FAILED, "repo_locked")
    assert not fake_agent.called("restore_backup")
    assert running(fake_agent, server)
    assert STARTED_AGAIN in log(job)
    server.refresh_from_db()
    assert server.desired_state == "running"


def test_a_restore_that_restic_refused_starts_the_server_again(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    fake_agent.failed_operations["restore"] = ("restic failed: wrong password", "restic_failed")
    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.FAILED
    assert running(fake_agent, server) and STARTED_AGAIN in log(job)


def test_a_restore_with_an_unknown_outcome_leaves_the_server_stopped(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    fake_agent.failed_operations["restore"] = ("the agent restarted", "operation_lost")
    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.FAILED
    assert not running(fake_agent, server)
    assert LEFT_STOPPED in log(job)
    server.refresh_from_db()
    assert server.desired_state == "stopped"


def test_a_single_path_restore_that_fails_starts_the_server_again(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    job = run("restore_path", server, django_capture_on_commit_callbacks, backup_id=str(backup.id), path="nope")
    assert job.error_code == "not_found"
    assert job.status == JobStatus.FAILED
    assert running(fake_agent, server) and STARTED_AGAIN in log(job)


def test_a_snapshot_rollback_whose_safety_snapshot_fails_starts_the_server_again(
    server, fake_agent, django_capture_on_commit_callbacks
):
    fake_agent.failures["create_snapshot"] = AgentError("Snapshots are not available", "snapshots_unavailable", 409)
    job = run("snapshot_restore", server, django_capture_on_commit_callbacks, name="20261103T000001Z")
    assert job.status == JobStatus.FAILED
    assert running(fake_agent, server) and STARTED_AGAIN in log(job)


def test_a_snapshot_conversion_without_an_answer_leaves_the_server_stopped(
    server, fake_agent, django_capture_on_commit_callbacks
):
    fake_agent.failures["convert_snapshots"] = AgentUnavailable("timed out")
    job = run("snapshot_convert", server, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED
    assert not running(fake_agent, server) and LEFT_STOPPED in log(job)


# --- backend-lifecycle#4 -------------------------------------------------------------------------------
def test_a_software_update_whose_backup_fails_starts_the_server_again(
    server, fake_agent, mojang_java, django_capture_on_commit_callbacks
):
    fake_agent.failed_operations["backup"] = ("no space left", "restic_failed")
    job = run("software_update", server, django_capture_on_commit_callbacks, minecraft_version="1.21.1", java_version=21)
    assert job.status == JobStatus.FAILED
    server.refresh_from_db()
    assert server.minecraft_version == "1.20.4"
    assert running(fake_agent, server) and STARTED_AGAIN in log(job)


def test_a_software_update_whose_image_pull_fails_puts_the_versions_back(
    server, fake_agent, mojang_java, django_capture_on_commit_callbacks
):
    fake_agent.failed_operations["apply"] = ("Image pull failed: toomanyrequests", "pull_failed")
    job = run(
        "software_update", server, django_capture_on_commit_callbacks, minecraft_version="1.21.1", java_version=21, backup=False
    )
    assert (job.status, job.error_code) == (JobStatus.FAILED, "pull_failed")
    server.refresh_from_db()
    assert (server.minecraft_version, server.java_version, server.image) == ("1.20.4", 17, "itzg/minecraft-server:stable-java17")
    assert running(fake_agent, server) and STARTED_AGAIN in log(job)
    assert not restart_required(server)


def test_a_restart_whose_new_setting_the_agent_refuses_starts_the_old_container_again(
    server, fake_agent, django_capture_on_commit_callbacks
):
    Instance.objects.filter(pk=server.pk).update(memory_mb=server.memory_mb + 512)
    fake_agent.failures["apply"] = AgentError("Memory limit exceeds agent maximum of 2048 MB", "memory_limit", 400)
    job = run("restart", server, django_capture_on_commit_callbacks)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "memory_limit")
    assert running(fake_agent, server) and STARTED_AGAIN in log(job)


def test_a_recreate_without_an_answer_from_the_agent_says_so(server, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.failures["apply"] = AgentUnavailable("timed out")
    job = run("recreate", server, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED
    assert LEFT_STOPPED in log(job)
    server.refresh_from_db()
    assert server.desired_state == "stopped"


# --- backend-modpacks#1 --------------------------------------------------------------------------------
@pytest.fixture
def modpack(make_instance, fake_agent, monkeypatch):
    inst = make_instance(
        name="Pack",
        desired_state="running",
        server_type="MODRINTH",
        source="modrinth",
        modpack_provider="modrinth",
        modpack_project_id="P1",
        modpack_version_id="V1",
        modpack_version_name="1.0",
        modpack_loader="fabric",
    )
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.put_file(inst.id, "world/level.dat", b"v1 world")
    Instance.objects.filter(pk=inst.pk).update(
        container_spec_hash=current_spec_hash(inst), installed_at=timezone.now(), status="running", container_state="running"
    )
    from apps.modpacks import handlers as mp

    def plan(instance, version_id):
        return {
            "current": mp.current_pin(instance),
            "target": {**mp.current_pin(instance), "modpack_version_id": "V2", "modpack_version_name": "2.0"},
            "minecraft_changed": False,
            "loader_changed": False,
            "same_release": False,
            "warnings": [],
        }

    monkeypatch.setattr(mp, "plan_update", plan)
    inst.refresh_from_db()
    return inst


def test_a_modpack_update_whose_backup_fails_starts_the_server_again(modpack, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.failed_operations["backup"] = ("repository is locked", "repo_locked")
    job = run("modpack_update", modpack, django_capture_on_commit_callbacks, version_id="V2", backup=True)
    assert job.status == JobStatus.FAILED
    modpack.refresh_from_db()
    assert modpack.modpack_version_id == "V1"
    assert running(fake_agent, modpack) and STARTED_AGAIN in log(job)


def test_a_modpack_update_whose_container_is_refused_pins_the_old_release_again(
    modpack, fake_agent, django_capture_on_commit_callbacks
):
    fake_agent.failed_operations["apply"] = ("Image pull failed", "pull_failed")
    job = run("modpack_update", modpack, django_capture_on_commit_callbacks, version_id="V2", backup=False)
    assert job.status == JobStatus.FAILED
    modpack.refresh_from_db()
    assert modpack.modpack_version_id == "V1"
    assert not [h for h in (modpack.modpack_meta or {}).get("history", []) if h.get("job") == str(job.id)]
    assert running(fake_agent, modpack) and STARTED_AGAIN in log(job)


# --- scenario-data#4: the software of a backup -----------------------------------------------------------
def test_every_backup_records_the_software_it_belongs_to(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    software = backup.meta["software"]
    assert software["server_type"] == "VANILLA" and software["minecraft_version"] == "1.20.4"
    assert software["java_version"] == 17 and software["image"] == "itzg/minecraft-server:stable-java17"
    assert "modpack_version_id" in software


def test_restoring_a_before_update_backup_puts_the_old_version_back(
    server, fake_agent, mojang_java, django_capture_on_commit_callbacks
):
    fake_agent.put_file(server.id, "world/level.dat", b"saved by 1.20.4")
    job = run("software_update", server, django_capture_on_commit_callbacks, minecraft_version="1.21.1", java_version=21)
    assert job.status == JobStatus.SUCCEEDED, job.error
    before_update = Backup.objects.get(pk=job.result["backup_id"])
    assert before_update.kind == BackupKind.PRE_UPDATE
    fake_agent.put_file(server.id, "world/level.dat", b"saved by 1.21.1")
    revision = Instance.objects.get(pk=server.pk).config_revision

    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(before_update.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    server.refresh_from_db()
    assert (server.minecraft_version, server.java_version) == ("1.20.4", 17)
    assert server.config_revision > revision
    container = fake_agent.containers[str(server.id)]
    assert container["running"] and container["spec"]["env"]["VERSION"] == "1.20.4"
    assert container["image"] == "itzg/minecraft-server:stable-java17"
    assert fake_agent.get_file(server.id, "world/level.dat") == b"saved by 1.20.4"
    assert "the server's software was set back to it" in log(job)
    assert job.result["software_restored"]["minecraft_version"] == ["1.21.1", "1.20.4"]


def test_a_backup_from_before_the_software_record_uses_software_before(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    backup.meta = {"node_id": server.node_id, "software_before": {"minecraft_version": "1.19.2", "java_version": 17}}
    backup.kind = BackupKind.PRE_UPDATE
    backup.save()
    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    server.refresh_from_db()
    assert server.minecraft_version == "1.19.2"


def test_restoring_a_backup_of_the_same_software_changes_no_version(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    revision = Instance.objects.get(pk=server.pk).config_revision
    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED and job.result["software_restored"] is None
    assert Instance.objects.get(pk=server.pk).config_revision == revision
    assert not [c for c in fake_agent.called("apply")[1:]]  # started, not recreated


def test_backup_rows_marked_failed_or_deleted_have_their_snapshots_pruned(server, fake_agent, django_capture_on_commit_callbacks):
    given_up = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    deleted = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    Backup.objects.filter(pk=given_up.pk).update(status=BackupStatus.FAILED, snapshot_id="")
    Backup.objects.filter(pk=deleted.pk).update(status=BackupStatus.DELETED)
    kept = backup_of(server, fake_agent, django_capture_on_commit_callbacks)
    ids = {s["id"] for s in fake_agent.repos[str(server.id)]}
    assert given_up.snapshot_id not in ids and deleted.snapshot_id not in ids
    assert kept.snapshot_id in ids
