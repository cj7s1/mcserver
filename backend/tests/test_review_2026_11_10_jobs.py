# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Jobs (review of 2026-11-10): memory admission and where jobs skip it (findings 7 and 13), the
modpack rollback's progress (6c), what jobs remember about a server they stopped (14), worker
stops (15), create_failed of older agents (16) and a restore cut off by a panel stop (17)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.backups.models import Backup, BackupKind
from apps.instances import scheduling
from apps.instances.models import Instance, Schedule
from apps.jobs import runner, shutdown
from apps.jobs.models import Job, JobStatus
from apps.nodes.models import Node

from .test_audit_job_endings import clock, keep_running, no_stop_flag  # noqa: F401 (fixtures)
from .test_audit_memory_images import run, server  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db

# fake node: 16384 MB RAM, 4096 MB reserve -> 12288 MB for servers. Container = heap + max(768, 30 %):
# 8192 MB heap -> 10649 MB, 2048 MB -> 2816 MB, 4096 MB -> 5324 MB, 1024 MB -> 1792 MB.


def log_of(job: Job) -> str:
    return "\n".join(job.log)


def overcommitted(server_factory: Any, **fields: Any) -> Instance:
    """A running server that does not fit next to "Big" any more (both run: the node is overcommitted)."""
    server_factory("Big", memory_mb=8192)
    return server_factory("Running", memory_mb=2048, **fields)


def a_backup(inst: Instance, capture: Any, **meta: Any) -> Backup:
    job = run("backup", inst, capture)
    assert job.status == JobStatus.SUCCEEDED, job.error
    backup = Backup.objects.get(job=job)
    if meta:
        backup.meta = {**(backup.meta or {}), **meta}
        backup.save(update_fields=["meta"])
    return backup


# --- finding 7: the paths that start a server again without the memory check ---------------------------------
def test_a_restore_starts_the_server_again_without_the_memory_check(server, fake_agent, django_capture_on_commit_callbacks):
    inst = overcommitted(server)
    backup = a_backup(inst, django_capture_on_commit_callbacks)
    job = run("restore", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"]


def test_a_software_update_of_a_running_server_starts_it_again_without_the_check(
    server, fake_agent, django_capture_on_commit_callbacks
):
    inst = overcommitted(server)
    job = run(
        "software_update",
        inst,
        django_capture_on_commit_callbacks,
        minecraft_version="1.21.1",
        loader_version="",
        java_version=21,
        backup=False,
    )
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"]
    assert fake_agent.containers[str(inst.id)]["spec"]["env"]["VERSION"] == "1.21.1"


def _modpack(server_factory: Any, *, running: bool = True) -> Instance:
    server_factory("Big", memory_mb=8192)
    return server_factory(
        "Pack",
        running=running,
        memory_mb=2048,
        server_type="MODRINTH",
        minecraft_version="1.20.1",
        modpack_provider="modrinth",
        modpack_project_id="P1",
        modpack_slug="pack",
        modpack_name="Pack",
        modpack_version_id="V1",
        modpack_version_name="V1",
        modpack_loader="forge",
    )


@pytest.fixture
def plan_v2(monkeypatch):
    from apps.modpacks import handlers as mp

    def plan(instance: Instance, version_id: str) -> dict[str, Any]:
        current = mp.current_pin(instance)
        return {
            "current": current,
            "target": {**current, "modpack_version_id": version_id, "modpack_version_name": version_id},
            "minecraft_changed": False,
            "loader_changed": False,
            "same_release": version_id == instance.modpack_version_id,
            "warnings": [],
        }

    monkeypatch.setattr(mp, "plan_update", plan)


def test_a_modpack_update_of_a_running_server_starts_it_again_without_the_check(
    server, fake_agent, plan_v2, django_capture_on_commit_callbacks
):
    inst = _modpack(server)
    job = run("modpack_update", inst, django_capture_on_commit_callbacks, version_id="V2", backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"]
    assert fake_agent.containers[str(inst.id)]["spec"]["env"]["MODRINTH_VERSION"] == "V2"


def test_a_recreating_restart_checks_memory_before_it_stops_the_server(server, fake_agent, django_capture_on_commit_callbacks):
    inst = overcommitted(server)
    Instance.objects.filter(pk=inst.pk).update(memory_mb=4096)  # changed settings: the restart recreates
    calls = len(fake_agent.calls)
    job = run("restart", inst, django_capture_on_commit_callbacks)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "insufficient_memory")
    assert "stop" not in [name for name, _a, _k in fake_agent.calls[calls:]]
    assert fake_agent.containers[str(inst.id)]["running"]  # still running as it was


def test_a_job_marked_as_failed_after_its_stop_leaves_the_server_stopped(
    server, fake_agent, clock, django_capture_on_commit_callbacks
):
    inst = server("Running", memory_mb=2048)
    backup = a_backup(inst, django_capture_on_commit_callbacks)
    Instance.objects.filter(pk=inst.pk).update(resume_after_boot=True, resume_flagged_at=timezone.now() - timedelta(hours=1))
    keep_running(fake_agent, "backup")  # the safety backup takes long; meanwhile someone marks the job as failed
    polls = {"n": 0}

    def abandon_on_second_poll(op_id: str) -> None:
        polls["n"] += 1
        if polls["n"] == 2:
            Job.objects.filter(kind="restore").update(status=JobStatus.FAILED, error_code="abandoned")

    fake_agent.hooks["operation"] = abandon_on_second_poll
    with django_capture_on_commit_callbacks(execute=True):
        job = runner.enqueue("restore", title="Restore", instance=inst, params={"backup_id": str(backup.id)})
    job.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert "the server it had stopped was left stopped" in log_of(job)
    assert not fake_agent.containers.get(str(inst.id), {}).get("running")
    inst.refresh_from_db()
    assert inst.resume_after_boot is False  # the job no longer owned the server: the panel does not start it


# --- finding 6c: after the restore of a rollback, nothing counts as untouched ------------------------------------
def test_a_rollback_that_fails_after_its_restore_leaves_the_server_stopped(
    server, fake_agent, django_capture_on_commit_callbacks
):
    inst = _modpack(server)
    before = {"modpack_version_id": "V0", "modpack_version_name": "V0", "minecraft_version": "1.20.1"}
    backup = a_backup(inst, django_capture_on_commit_callbacks, modpack_before=before)
    Backup.objects.filter(pk=backup.pk).update(kind=BackupKind.PRE_UPDATE)

    def then_apply_fails(*_a: Any, **_k: Any) -> None:
        # a failure code that, before the restore, would prove that nothing was changed
        fake_agent.failed_operations["apply"] = ("Another operation is in progress", "instance_busy")

    fake_agent.hooks["restore_backup"] = then_apply_fails
    job = run("modpack_rollback", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id))
    assert (job.status, job.error_code) == (JobStatus.FAILED, "instance_busy")
    assert "may have changed the server's files" in log_of(job)
    inst.refresh_from_db()
    assert inst.desired_state == "stopped"


# --- finding 14: a recreate remembers that it stopped the server --------------------------------------------------
def test_a_recreating_restart_remembers_the_stop(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server("Running", memory_mb=2048)
    Instance.objects.filter(pk=inst.pk).update(memory_mb=3072)
    seen: list[Any] = []
    fake_agent.hooks["stop"] = lambda *a, **k: seen.append(Job.objects.get(kind="restart").result)
    job = run("restart", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert seen == [{"stopped_server": True}]


# --- finding 15: a worker stop is an interruption, and a start on its way is not undone -----------------------------
def test_a_worker_stop_before_the_start_again_is_reported_as_an_interruption(
    server, fake_agent, clock, django_capture_on_commit_callbacks
):
    inst = server("Running", memory_mb=2048)
    backup = a_backup(inst, django_capture_on_commit_callbacks)
    # the stop is requested while the restore finishes: the next step (starting the server) sees it
    fake_agent.hooks["operation"] = lambda op_id: (
        shutdown.request_stop() if fake_agent.operations[op_id]["kind"] == "restore" else None
    )
    job = run("restore", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    text = log_of(job)
    assert "The backup was restored. The panel was being stopped" in text
    assert "marked as failed" not in text
    inst.refresh_from_db()
    assert inst.desired_state == "stopped"  # it had stopped the server and did not start it (settle_interrupted)


def test_a_server_the_node_is_starting_again_is_not_recorded_as_stopped(
    server, fake_agent, clock, django_capture_on_commit_callbacks
):
    inst = server("Running", memory_mb=2048)
    backup = a_backup(inst, django_capture_on_commit_callbacks)
    # the backup was made with another version: the restore sets it back and recreates the container
    Instance.objects.filter(pk=inst.pk).update(minecraft_version="1.21.1")
    keep_running(fake_agent, "apply")
    fake_agent.hooks["operation"] = lambda op_id: (
        shutdown.request_stop() if fake_agent.operations[op_id]["kind"] == "apply" else None
    )
    job = run("restore", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    assert "the node finishes that start" in log_of(job)
    assert job.result.get("stopped_server") is False
    inst.refresh_from_db()
    assert inst.desired_state == "running"


def test_a_worker_stop_while_an_update_starts_the_server_again_does_not_record_it_stopped(
    server, fake_agent, clock, django_capture_on_commit_callbacks
):
    inst = server("Running", memory_mb=2048)
    keep_running(fake_agent, "apply")  # the update's new container: the node works on it while the panel stops
    fake_agent.hooks["operation"] = lambda op_id: (
        shutdown.request_stop() if fake_agent.operations[op_id]["kind"] == "apply" else None
    )
    job = run(
        "software_update",
        inst,
        django_capture_on_commit_callbacks,
        minecraft_version="1.21.1",
        loader_version="",
        java_version=21,
        backup=False,
    )
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    assert "the node finishes that start" in log_of(job)
    assert "was left stopped" not in log_of(job)
    inst.refresh_from_db()
    assert inst.desired_state == "running"


# --- finding 16: create_failed proves nothing with an agent that removes the old container first --------------------
@pytest.mark.parametrize(("create_first", "started_again"), [(True, True), (False, False)])
def test_create_failed_is_untouched_only_with_an_agent_that_creates_first(
    create_first, started_again, server, fake_agent, django_capture_on_commit_callbacks
):
    if not create_first:
        fake_agent.features = [f for f in fake_agent.features if f != "pull_newer"]
    inst = server("Running", memory_mb=2048)
    # once the server is stopped, Docker refuses to create the new container
    fake_agent.hooks["stop"] = lambda *a, **k: fake_agent.failed_operations.__setitem__(
        "apply", ("Docker refused to create the container", "create_failed")
    )
    job = run(
        "software_update",
        inst,
        django_capture_on_commit_callbacks,
        minecraft_version="1.21.1",
        loader_version="",
        java_version=21,
        backup=False,
    )
    assert (job.status, job.error_code) == (JobStatus.FAILED, "create_failed")
    inst.refresh_from_db()
    text = log_of(job)
    if started_again:
        assert "The update was not applied: the previous versions were put back" in text
        assert inst.minecraft_version == "1.20.4" and inst.desired_state == "running"
    else:
        assert "may have changed the server's files or configuration" in text
        assert inst.minecraft_version == "1.21.1" and inst.desired_state == "stopped"


# --- finding 17: a restore cut off by a panel stop says that files and software may differ ---------------------------
def test_a_restore_cut_off_by_a_panel_stop_warns_about_the_software(
    server, fake_agent, clock, django_capture_on_commit_callbacks
):
    inst = server("Running", memory_mb=2048)
    backup = a_backup(inst, django_capture_on_commit_callbacks)
    Instance.objects.filter(pk=inst.pk).update(minecraft_version="1.21.1")  # updated since the backup
    keep_running(fake_agent, "restore")
    fake_agent.hooks["operation"] = lambda op_id: (
        shutdown.request_stop() if fake_agent.operations[op_id]["kind"] == "restore" else None
    )
    job = run("restore", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    for text in (job.error, log_of(job)):
        assert "may now be the backup's, made with Vanilla 1.20.4" in text, text
        assert "still set to Vanilla 1.21.1" in text
        assert "restore the same backup again" in text
    inst.refresh_from_db()
    assert inst.minecraft_version == "1.21.1" and inst.desired_state == "stopped"


# --- finding 13: no start without a memory check, and an override where a user asks -----------------------------
def test_creating_a_server_that_starts_checks_memory(
    server, fake_agent, clients, monkeypatch, django_capture_on_commit_callbacks
):
    server("Big", memory_mb=8192)
    body = {"name": "New", "accept_eula": True, "server_type": "VANILLA", "minecraft_version": "1.20.4", "memory_mb": 1024}
    resp = clients["owner"].post("/api/instances/", body, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "insufficient_memory", resp.content
    assert not Instance.objects.filter(name="New").exists()  # nothing was created
    # not started: no check
    assert clients["owner"].post("/api/instances/", {**body, "name": "Idle", "start": False}, format="json").status_code == 201
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", {**body, "allow_overcommit": True}, format="json")
    assert resp.status_code == 201, resp.content
    job = Job.objects.get(kind="install", instance__name="New")
    assert job.params["allow_overcommit"] is True and job.status == JobStatus.SUCCEEDED, job.error
    assert any("It is started anyway, as requested" in line for line in job.log)


def test_a_software_update_that_starts_a_stopped_server_checks_memory(
    server, fake_agent, clients, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(
        "apps.catalog.providers.game_versions",
        lambda st, include_snapshots=False: [{"version": v, "stable": True} for v in ("1.21.1", "1.20.4")],
    )
    monkeypatch.setattr(
        "apps.instances.views.resolve_java",
        lambda mc, st: {"java_version": 21, "required": 21, "minecraft_version": mc, "reason": "t"},
    )
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048)
    url = f"/api/instances/{other.id}/software"
    body = {"minecraft_version": "1.21.1", "backup": False, "start": True}
    resp = clients["owner"].post(url, body, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "insufficient_memory", resp.content
    assert clients["owner"].post(url, {**body, "start": False}, format="json").status_code == 202
    Job.objects.filter(instance=other).update(status=JobStatus.SUCCEEDED)
    Instance.objects.filter(pk=other.pk).update(active_job=None)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(url, {**body, "minecraft_version": "1.20.4", "allow_overcommit": True}, format="json")
    assert resp.status_code == 202, resp.content
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.params["allow_overcommit"] is True and job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(other.id)]["running"]


def test_a_modpack_update_that_starts_a_stopped_server_checks_memory(server, fake_agent, clients, plan_v2):
    pack = _modpack(server, running=False)
    url = f"/api/instances/{pack.id}/modpack/update"
    resp = clients["owner"].post(url, {"version_id": "V2", "start": True}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "insufficient_memory", resp.content
    resp = clients["owner"].post(url, {"version_id": "V2", "start": True, "allow_overcommit": True}, format="json")
    assert resp.status_code == 202, resp.content
    assert Job.objects.get(pk=resp.json()["id"]).params["allow_overcommit"] is True


def test_recreating_a_running_server_checks_memory(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = overcommitted(server)
    Instance.objects.filter(pk=inst.pk).update(memory_mb=4096)
    resp = clients["owner"].post(f"/api/instances/{inst.id}/recreate", {}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "insufficient_memory", resp.content
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/recreate", {"allow_overcommit": True}, format="json")
    assert resp.status_code == 202, resp.content
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert any("It is started anyway, as requested" in line for line in job.log)


@pytest.fixture
def other_node(db):
    return Node.objects.create(name="n2", agent_url="http://agent2.invalid:8700", agent_token="t" * 30, use_settings_token=False)


def test_a_migration_checks_memory_on_the_target_before_it_copies(
    server, fake_agent, clients, other_node, monkeypatch, django_capture_on_commit_callbacks
):
    from apps.nodes.client import AgentClient

    # one fake agent plays both nodes: the target lists none of the source's containers
    listed = AgentClient.list_containers
    monkeypatch.setattr(AgentClient, "list_containers", lambda c: [] if c.node.pk == other_node.pk else listed(c), raising=True)
    inst = server("Huge", memory_mb=10240)  # 13312 MB: more than the target node has for servers
    resp = clients["owner"].post(f"/api/instances/{inst.id}/migrate", {"node": other_node.id, "confirm": True}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "insufficient_memory", resp.content
    assert "node 'n2'" in resp.json()["error"]["message"]
    # the job checks too, before anything is stopped or copied (a job queued another way)
    calls = len(fake_agent.calls)
    job = run("migrate", inst, django_capture_on_commit_callbacks, target_node=other_node.pk, delete_source=False)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "insufficient_memory")
    names = [name for name, _a, _k in fake_agent.calls[calls:]]
    assert "stop" not in names and "prepare" not in names and "download" not in names
    assert fake_agent.containers[str(inst.id)]["running"]
    inst.refresh_from_db()
    assert inst.node_id != other_node.pk


def test_resume_after_boot_notifies_when_it_gives_up_for_memory(server, fake_agent, monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr("apps.notifications.service.notify", lambda e, t, m, **kw: sent.append((e, t, m)))
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048, desired_state="running")
    Instance.objects.filter(pk=other.pk).update(resume_after_boot=True, resume_flagged_at=timezone.now() - timedelta(hours=8))
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=11))
    scheduling.resume_flagged_servers()
    assert [(e, t) for e, t, _m in sent] == [("job.failed", "Other was not started again after the host restart")]
    assert "2816 MB" in sent[0][2]
    sent.clear()
    scheduling.resume_flagged_servers()  # the mark is gone: no second notification
    assert sent == []


def test_a_scheduled_start_that_never_fits_is_notified(server, fake_agent, monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("apps.notifications.service.notify", lambda e, t, m, **kw: sent.append((e, m)))
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(days=1))
    server("Big", memory_mb=8192)
    other = server("Other", running=False, memory_mb=2048)
    sched = Schedule.objects.create(
        instance=other,
        name="Start",
        action="start",
        cron="0 17 * * *",
        only_when_running=False,
        next_run_at=timezone.now() - timedelta(seconds=30),
    )
    scheduling.run_due_schedules(timezone.now())
    Schedule.objects.filter(pk=sched.pk).update(deferred_at=timezone.now() - timedelta(minutes=61))
    scheduling.run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("Failed: not run for 60 minutes")
    assert [e for e, _m in sent] == ["job.failed"] and "Not enough memory on node" in sent[0][1]
    assert not Job.objects.filter(instance=other).exists()
