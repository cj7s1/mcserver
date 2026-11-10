"""Regression tests for the reliability / data-integrity audit (jobs, Celery, agent client,
migration, backups, snapshots, reconcile)."""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.backups.handlers import local_backups, repository_node_id
from apps.backups.models import Backup, BackupStatus, RetentionPolicy
from apps.instances.models import Instance, Status
from apps.instances.reconcile import AUTO_STOP_PREFIX, apply_observation, reconcile_instance
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.jobs.tasks import watchdog
from apps.nodes import client as client_mod
from apps.nodes.client import AgentClient, AgentError, AgentUnavailable
from apps.nodes.models import Node

from .conftest import FakeAgent

pytestmark = pytest.mark.django_db


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


@pytest.fixture
def inst(make_instance, fake_agent):
    instance = make_instance(name="Reliable")
    fake_agent.prepare(instance.id, {"server.properties": "motd=hi\n"})
    return instance


def _running_job(instance, **fields):
    fields.setdefault("kind", "backup")
    job = Job.objects.create(title="long backup", instance=instance, status=JobStatus.RUNNING, **fields)
    Instance.objects.filter(pk=instance.pk).update(active_job=job)
    return job


# =====================================================================================
# 1. Redelivered job messages / heartbeat / watchdog
# =====================================================================================
def test_visibility_timeout_is_far_above_the_default():
    from mcpanel.celery import app

    assert app.conf.broker_transport_options["visibility_timeout"] >= 24 * 3600
    assert settings.MCPANEL_JOB_STALE_SECONDS >= 3 * settings.MCPANEL_JOB_HEARTBEAT_SECONDS


def test_redelivered_message_of_a_live_job_is_ignored(inst, temp_handlers):
    calls = []
    temp_handlers("t_long", lambda job, ctx: calls.append(job.id))
    started = timezone.now() - timedelta(hours=3)  # longer than kombu's 1 h default visibility timeout
    job = _running_job(inst, kind="t_long", started_at=started, heartbeat_at=timezone.now() - timedelta(seconds=20))
    runner.run_job(str(job.id))
    job.refresh_from_db()
    inst.refresh_from_db()
    assert job.status == JobStatus.RUNNING and job.error_code == ""
    assert inst.active_job_id == job.id  # the lock stays with the worker that is still running it
    assert calls == []


def test_redelivered_message_of_a_dead_job_marks_it_interrupted(inst, temp_handlers):
    calls = []
    temp_handlers("t_dead", lambda job, ctx: calls.append(job.id))
    stale = timezone.now() - timedelta(seconds=settings.MCPANEL_JOB_STALE_SECONDS + 60)
    job = _running_job(inst, kind="t_dead", started_at=stale, heartbeat_at=stale)
    runner.run_job(str(job.id))
    job.refresh_from_db()
    inst.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    assert inst.active_job_id is None
    assert calls == []


def test_legacy_running_row_without_heartbeat_uses_started_at(inst):
    fresh = _running_job(inst, started_at=timezone.now() - timedelta(seconds=30))
    runner.run_job(str(fresh.id))
    fresh.refresh_from_db()
    assert fresh.status == JobStatus.RUNNING
    Job.objects.filter(pk=fresh.pk).update(started_at=timezone.now() - timedelta(hours=1))
    runner.run_job(str(fresh.id))
    fresh.refresh_from_db()
    assert fresh.error_code == "interrupted"


def test_claiming_a_job_sets_its_heartbeat_and_progress_refreshes_it(inst, temp_handlers, django_capture_on_commit_callbacks):
    seen = {}

    def handler(job, ctx):
        seen["claimed"] = Job.objects.get(pk=job.pk).heartbeat_at
        Job.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now() - timedelta(hours=1))
        ctx.progress(0.5, "working", force=True)
        seen["after_progress"] = Job.objects.get(pk=job.pk).heartbeat_at
        Job.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now() - timedelta(hours=1))
        ctx.log("a line")
        seen["after_log"] = Job.objects.get(pk=job.pk).heartbeat_at

    temp_handlers("t_hb", handler)
    job = run("t_hb", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    recent = timezone.now() - timedelta(minutes=1)
    assert seen["claimed"] and seen["claimed"] > recent
    assert seen["after_progress"] > recent and seen["after_log"] > recent


def test_heartbeat_thread_beats_while_the_handler_blocks(inst, temp_handlers, monkeypatch, django_capture_on_commit_callbacks):
    beats: list[str] = []
    enough = threading.Event()

    def fake_touch(job_id):
        beats.append(str(job_id))
        if len(beats) >= 3:
            enough.set()
        return True

    monkeypatch.setattr(runner, "touch_heartbeat", fake_touch)
    monkeypatch.setattr(settings, "MCPANEL_JOB_HEARTBEAT_SECONDS", 0.01)

    def handler(job, ctx):
        # a long blocking agent call without any progress callback
        assert enough.wait(5), "the heartbeat thread did not beat"

    temp_handlers("t_block", handler)
    job = run("t_block", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert set(beats) == {str(job.id)}
    count = len(beats)
    time.sleep(0.05)
    assert len(beats) == count  # the thread stopped with the handler


def test_heartbeat_touch_only_updates_running_jobs(inst):
    job = _running_job(inst, started_at=timezone.now())
    assert runner.touch_heartbeat(job.pk) is True
    Job.objects.filter(pk=job.pk).update(status=JobStatus.CANCELLED)
    assert runner.touch_heartbeat(job.pk) is False


def test_watchdog_fails_dead_jobs_but_never_live_long_ones(inst, make_instance, monkeypatch):
    now = timezone.now()
    other = make_instance(name="Other")
    third = make_instance(name="Third")
    live = _running_job(inst, started_at=now - timedelta(hours=20), heartbeat_at=now - timedelta(seconds=10))
    dead = _running_job(other, started_at=now - timedelta(minutes=30), heartbeat_at=now - timedelta(minutes=20))
    hung = _running_job(third, started_at=now - timedelta(hours=60), heartbeat_at=now)
    never = Job.objects.create(kind="backup", title="never", instance=None)
    Job.objects.filter(pk=never.pk).update(created_at=now - timedelta(hours=3))
    lost = Job.objects.create(kind="backup", title="lost message", instance=None)
    Job.objects.filter(pk=lost.pk).update(created_at=now - timedelta(minutes=20))
    fresh = Job.objects.create(kind="backup", title="just queued", instance=None)
    dispatched: list[str] = []
    monkeypatch.setattr("apps.jobs.tasks.run_job.delay", lambda job_id: dispatched.append(job_id))

    assert watchdog() == 3
    for job in (live, dead, hung, never, lost, fresh):
        job.refresh_from_db()
    assert live.status == JobStatus.RUNNING
    assert (dead.status, dead.error_code) == (JobStatus.FAILED, "interrupted")
    assert (hung.status, hung.error_code) == (JobStatus.FAILED, "timeout")
    assert (never.status, never.error_code) == (JobStatus.FAILED, "not_started")
    assert lost.status == fresh.status == JobStatus.QUEUED
    assert dispatched == [str(lost.id)]  # a possibly lost message is sent again...
    watchdog()
    assert dispatched == [str(lost.id)]  # ...but not on every watchdog tick
    other.refresh_from_db()
    assert other.active_job_id is None


# --- the Backup row of a job that was cut off ------------------------------------------------------
def _cut_off_backup(instance, *, kind: str = "manual") -> tuple[Job, Backup]:
    """A backup job whose worker died (the panel container was restarted): the job still reads
    RUNNING without a heartbeat, and so does the row run_backup created for it."""
    now = timezone.now()
    job = _running_job(instance, exclusive=True, started_at=now - timedelta(minutes=30), heartbeat_at=now - timedelta(minutes=20))
    backup = Backup.objects.create(
        instance=instance, name="Nightly", kind=kind, job=job, meta={"node_id": instance.node_id, "node_name": instance.node.name}
    )
    return job, backup


def test_a_cut_off_backup_the_agent_finished_is_taken_over(inst, fake_agent, clients):
    job, backup = _cut_off_backup(inst)
    fake_agent.put_file(inst.id, "world/level.dat", b"x" * 1000)
    # the agent went on while the panel was down: its snapshot carries the id of the backup
    fake_agent.create_backup(inst.id, str(backup.id), tags=["kind:manual"])
    snapshot = fake_agent.repos[str(inst.id)][-1]

    assert watchdog() == 1  # the job is failed as interrupted ...
    job.refresh_from_db()
    backup.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    # ... and its backup is not left "running": the finished snapshot is taken over
    assert backup.status == BackupStatus.SUCCEEDED and backup.snapshot_id == snapshot["id"]
    assert backup.total_bytes == snapshot["summary"]["total_bytes_processed"] > 0
    assert backup.files_count == 2 and backup.finished_at is not None and backup.error == ""
    assert backup.consistent is False and any("took it over" in warning for warning in backup.warnings)
    listed = clients["owner"].get(f"/api/instances/{inst.id}/backups").json()["backups"]
    assert [(row["id"], row["status"]) for row in listed] == [(str(backup.id), "succeeded")]
    assert [b.pk for b in local_backups(inst)] == [backup.pk]  # it can be restored and counts for retention
    watchdog()
    backup.refresh_from_db()
    assert backup.status == BackupStatus.SUCCEEDED and len(backup.warnings) == 1


def test_a_cut_off_backup_without_a_snapshot_is_marked_failed(inst, fake_agent):
    _job, backup = _cut_off_backup(inst, kind="pre_update")
    other = Backup.objects.create(instance=inst, name="Of another job", kind="manual")  # no job, just created
    assert watchdog() == 1
    backup.refresh_from_db()
    other.refresh_from_db()
    assert backup.status == BackupStatus.FAILED and backup.finished_at is not None and backup.snapshot_id == ""
    assert "was not finished" in backup.error and "ended as failed" in backup.error and "stopped" in backup.error
    assert other.status == BackupStatus.RUNNING


def test_a_backup_of_a_job_that_was_marked_as_failed_is_settled_too(inst, fake_agent):
    job, backup = _cut_off_backup(inst)
    runner.finish(job, JobStatus.FAILED, error="Marked as failed by owner", error_code="abandoned")
    watchdog()
    backup.refresh_from_db()
    assert backup.status == BackupStatus.FAILED and "Marked as failed by owner" in backup.error


def test_a_backup_the_agent_is_still_writing_waits_for_it(inst, fake_agent):
    _job, backup = _cut_off_backup(inst)
    fake_agent.active_operations[str(inst.id)] = {"id": "op1", "kind": "backup", "status": "running", "finished_at": None}
    assert watchdog() == 1
    backup.refresh_from_db()
    assert backup.status == BackupStatus.RUNNING  # restic is still running on the node
    fake_agent.create_backup(inst.id, str(backup.id), tags=["kind:manual"])
    del fake_agent.active_operations[str(inst.id)]
    watchdog()
    backup.refresh_from_db()
    assert backup.status == BackupStatus.SUCCEEDED and backup.snapshot_id


def test_a_backup_whose_agent_cannot_be_asked_is_given_an_hour(inst, fake_agent):
    job, backup = _cut_off_backup(inst)
    fake_agent.failures["list_backups"] = AgentUnavailable()
    assert watchdog() == 1
    backup.refresh_from_db()
    assert backup.status == BackupStatus.RUNNING
    Job.objects.filter(pk=job.pk).update(finished_at=timezone.now() - timedelta(hours=2))
    watchdog()
    backup.refresh_from_db()
    assert backup.status == BackupStatus.FAILED and "could not be asked" in backup.error


def test_the_backup_of_a_job_that_is_alive_is_left_alone(inst, fake_agent):
    now = timezone.now()
    job = _running_job(inst, started_at=now - timedelta(hours=5), heartbeat_at=now - timedelta(seconds=5))
    backup = Backup.objects.create(instance=inst, name="Big", kind="manual", job=job)
    assert watchdog() == 0
    backup.refresh_from_db()
    assert backup.status == BackupStatus.RUNNING and not fake_agent.called("list_backups")


# =====================================================================================
# 2. Periodic tasks: own queue, expiry, no overlapping runs
# =====================================================================================
def test_periodic_tasks_use_their_own_queue_and_jobs_stay_on_the_default():
    from mcpanel.celery import app

    router = app.amqp.router
    for name in (
        "apps.instances.tasks.reconcile_all",
        "apps.instances.tasks.scheduler_tick",
        "apps.jobs.tasks.watchdog",
        "apps.metrics.tasks.sample_all",
        "apps.metrics.tasks.cleanup",
        "apps.modpacks.tasks.check_updates_due",
        "apps.modpacks.tasks.check_update",
        "apps.notifications.deliver",
    ):
        assert router.route({}, name)["queue"].name == "periodic", name
    assert router.route({}, "apps.jobs.runner.run_job")["queue"].name == "celery"
    queues = app.amqp.queues
    # a worker without -Q consumes both; distinct bindings so no message lands in both queues
    assert set(queues) == {"celery", "periodic"}
    assert queues["celery"].routing_key != queues["periodic"].routing_key


def test_every_beat_entry_expires_before_it_can_pile_up():
    for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
        expires = entry.get("options", {}).get("expires")
        assert expires and 0 < expires <= entry["schedule"] * 6, name


def test_single_flight_tasks_skip_overlapping_runs(monkeypatch):
    from mcpanel import celery as celery_mod

    monkeypatch.setitem(celery_mod.SINGLE_FLIGHT, "tests.reliability.tick", 60)
    runs = []

    @celery_mod.app.task(name="tests.reliability.tick")
    def tick():
        runs.append(1)
        return "ran"

    key = "celery:single-flight:tests.reliability.tick"
    cache.set(key, "someone-else", 60)  # a previous run is still in progress
    assert tick() is None and runs == []
    cache.delete(key)
    assert tick() == "ran" and runs == [1]
    assert cache.get(key) is None  # released after the run
    assert isinstance(tick, celery_mod.MCPanelTask)


# =====================================================================================
# 3. wait_operation survives transient agent errors
# =====================================================================================
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(client_mod, "monotonic", c.monotonic)
    monkeypatch.setattr(client_mod, "sleep", c.sleep)
    return c


def _polling_client(node, monkeypatch, responses):
    client = AgentClient(node)
    seq = list(responses)

    def operation(op_id):
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client, "operation", operation)
    return client


def test_wait_operation_rides_out_an_agent_restart(node, monkeypatch, clock):
    running = {"status": "running", "progress": 0.5}
    done = {"status": "succeeded", "progress": 1.0, "result": {"ok": True}}
    client = _polling_client(
        node,
        monkeypatch,
        [
            running,
            AgentUnavailable("connection refused"),
            AgentError("bad gateway", "agent_error", 502),
            AgentUnavailable("timed out"),
            running,  # the agent is back and still knows the operation
            done,
        ],
    )
    progress = []
    assert client.wait_operation("op1", on_progress=progress.append)["result"] == {"ok": True}
    assert [p["status"] for p in progress] == ["running", "running", "succeeded"]
    backoff = clock.sleeps[1:4]
    assert backoff == sorted(backoff) and backoff[-1] > backoff[0]  # exponential backoff


def test_wait_operation_gives_up_after_the_grace_period(node, monkeypatch, clock):
    client = _polling_client(node, monkeypatch, [AgentUnavailable("connection refused")])
    with pytest.raises(AgentUnavailable, match="Lost contact with the node agent"):
        client.wait_operation("op1", reconnect_grace=120)
    assert 120 <= clock.now - 1000.0 < 120 + client_mod.OPERATION_RETRY_MAX_DELAY + 1


def test_wait_operation_reports_a_restart_that_lost_the_operation(node, monkeypatch, clock):
    client = _polling_client(
        node, monkeypatch, [AgentUnavailable("connection reset"), AgentError("Operation not found", "agent_error", 404)]
    )
    with pytest.raises(AgentError) as exc:
        client.wait_operation("op1")
    assert exc.value.error_code == "operation_lost"
    assert "restarted while this operation was running" in str(exc.value.detail)


def test_wait_operation_does_not_retry_real_failures(node, monkeypatch, clock):
    client = _polling_client(node, monkeypatch, [AgentError("disk full", "internal_error", 500)])
    with pytest.raises(AgentError, match="disk full"):
        client.wait_operation("op1")
    failed = {"status": "failed", "error": "restic said no", "error_code": "restic_failed"}
    client = _polling_client(node, monkeypatch, [failed])
    with pytest.raises(AgentError) as exc:
        client.wait_operation("op1")
    assert exc.value.error_code == "restic_failed"
    assert clock.sleeps == []


# =====================================================================================
# 4./5. Migration: clean target folder, backups stay attributed to the node holding them
# =====================================================================================
class NodeFake(FakeAgent):
    """FakeAgent with the archive download / extract pair that migration uses."""

    def download(self, instance_id: Any, path: str, endpoint: str = "download"):  # type: ignore[override]
        if endpoint != "archive":
            return super().download(instance_id, path)
        root = self._root(str(instance_id))
        payload = {
            "files": {k: base64.b64encode(v).decode() for k, v in root["files"].items()},
            "dirs": sorted(root["dirs"]),
        }
        data = json.dumps(payload).encode()
        return iter([data]), len(data), None

    def extract(self, instance_id: Any, path: str, destination: str = "", **kw: Any) -> dict[str, Any]:
        iid = str(instance_id)
        payload = json.loads(self._read(iid, path))
        root = self._root(iid)
        for rel in payload["dirs"]:
            root["dirs"].add(rel)
        for rel, b64 in payload["files"].items():
            if rel in root["files"] and not kw.get("overwrite"):
                continue
            self.put_file(iid, rel, base64.b64decode(b64))
        if kw.get("delete_archive"):
            del root["files"][path]
        return self._op("import", iid, {"extracted": len(payload["files"])})


@pytest.fixture
def two_nodes(monkeypatch, node):
    other = Node.objects.create(name="node-b", agent_url="http://agent-b.invalid:8700", agent_token="t" * 30)
    agents = {node.pk: NodeFake(), other.pk: NodeFake()}

    def install(mapping):
        for name in (*FakeAgent.METHODS, "extract"):

            def method(client, *args, _name=name, **kwargs):
                fake = mapping[client.node.pk]
                fake.calls.append((_name, args, kwargs))
                return getattr(fake, _name)(*args, **kwargs)

            monkeypatch.setattr(AgentClient, name, method)

    install(agents)
    return node, other, agents, install


def _migrate(instance, target, capture, **params):
    return run("migrate", instance, capture, target_node=target.pk, **params)


def test_migrate_replaces_leftovers_in_the_target_folder(make_instance, two_nodes, django_capture_on_commit_callbacks):
    a, b, agents, _ = two_nodes
    inst = make_instance(name="Mover")
    agents[a.pk].put_file(inst.id, "server.properties", "motd=current\n")
    agents[a.pk].put_file(inst.id, "mods/new.jar", b"new")
    # an earlier failed/reverted migration left an old copy on B
    agents[b.pk].put_file(inst.id, "mods/old-removed.jar", b"stale")
    agents[b.pk].put_file(inst.id, "server.properties", "motd=stale\n")
    agents[b.pk].put_file(inst.id, "world/region/r.9.9.mca", b"stale-world")

    job = _migrate(inst, b, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert set(agents[b.pk].fs[str(inst.id)]["files"]) == {"server.properties", "mods/new.jar"}
    assert agents[b.pk].text(inst.id, "server.properties") == "motd=current\n"
    assert job.result["target_leftovers_removed"] == 3
    # the source folder was not touched (delete_source not requested) and has no probe left
    assert set(agents[a.pk].fs[str(inst.id)]["files"]) == {"server.properties", "mods/new.jar"}
    inst.refresh_from_db()
    assert inst.node_id == b.pk


def test_migrate_deletes_the_last_run_record_on_both_nodes(make_instance, two_nodes, django_capture_on_commit_callbacks):
    """The record an agent keeps of a stopped server's last run (console output included) does not
    move with the server: the source's is deleted, and so is one the target still holds from an
    earlier stay."""
    from apps.instances.spec import build_spec

    a, b, agents, _ = two_nodes
    inst = make_instance(name="Mover")
    for agent, line in ((agents[a.pk], "last run on A"), (agents[b.pk], "an earlier stay on B")):
        agent.put_file(inst.id, "server.properties", "motd=current\n")
        agent.apply(inst.id, build_spec(inst), start=True)
        agent.log_lines[str(inst.id)] = [line]
        agent.stop(inst.id)
        assert agent.retire(inst.id, settle_seconds=0)["recorded"] is True
    job = _migrate(inst, b, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert agents[a.pk].last_runs == {} and agents[b.pk].last_runs == {}
    assert agents[b.pk].logs(inst.id) == []  # the new container's own (empty) log, nothing in front of it
    assert agents[a.pk].status(inst.id) == {"exists": False, "operation": None}
    assert job.result["restarted"] is False


def test_migrate_between_older_agents_asks_for_no_record(make_instance, two_nodes, django_capture_on_commit_callbacks):
    a, b, agents, _ = two_nodes
    for agent in agents.values():
        agent.features = []  # agents from before the last-run record
    inst = make_instance(name="Mover")
    agents[a.pk].put_file(inst.id, "server.properties", "motd=current\n")
    job = _migrate(inst, b, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert not agents[a.pk].called("forget_last_run") and not agents[b.pk].called("forget_last_run")


def test_migrate_refuses_when_both_nodes_share_the_folder(make_instance, two_nodes, django_capture_on_commit_callbacks):
    a, b, agents, install = two_nodes
    shared = agents[a.pk]
    install({a.pk: shared, b.pk: shared})  # e.g. two node records for the same agent
    inst = make_instance(name="Shared")
    shared.put_file(inst.id, "world/level.dat", b"precious")

    job = _migrate(inst, b, django_capture_on_commit_callbacks, delete_source=True)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "same_storage")
    assert set(shared.fs[str(inst.id)]["files"]) == {"world/level.dat"}  # nothing deleted, no probe left
    assert not shared.called("delete_data")
    inst.refresh_from_db()
    assert inst.node_id == a.pk


def test_backups_stay_attributed_to_the_node_holding_them(make_instance, two_nodes, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    a, b, agents, _ = two_nodes
    inst = make_instance(name="Backed")
    agents[a.pk].put_file(inst.id, "world/level.dat", b"v1")
    old = Backup.objects.get(pk=run("backup", inst, capture, kind="manual").result["backup_id"])
    legacy = Backup.objects.get(pk=run("backup", inst, capture, kind="manual").result["backup_id"])
    Backup.objects.filter(pk=legacy.pk).update(meta={})  # a row from before node ids were recorded
    assert repository_node_id(old, inst) == a.pk

    assert _migrate(inst, b, capture).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    old.refresh_from_db()
    legacy.refresh_from_db()
    assert old.meta["node_id"] == legacy.meta["node_id"] == a.pk
    assert old.status == legacy.status == BackupStatus.SUCCEEDED
    assert local_backups(inst) == []

    # in-place restores of a backup in A's repository are refused with a clear message
    job = run("restore", inst, capture, backup_id=str(old.id), pre_restore_backup=True)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "backup_on_other_node")
    assert "node 'test-node'" in job.error and not agents[b.pk].called("restore_backup")
    job = run("restore_path", inst, capture, backup_id=str(old.id), path="world")
    assert job.error_code == "backup_on_other_node"

    # retention on B ignores A's backups (it could not prune them, and must not count them)
    RetentionPolicy.objects.update_or_create(instance=inst, defaults={"keep_last": 1, "keep_daily": 0, "keep_weekly": 0})
    new = run("backup", inst, capture, kind="manual")
    assert new.status == JobStatus.SUCCEEDED, new.error
    assert new.result["retention_removed"] == []
    assert not agents[b.pk].called("forget_backups")
    assert [x.pk for x in local_backups(inst)] == [Backup.objects.get(pk=new.result["backup_id"]).pk]

    # verification of B's repository does not mark A's backups as verified
    assert run("backup_verify", inst, capture).status == JobStatus.SUCCEEDED
    old.refresh_from_db()
    assert old.verified_at is None

    # deleting one of A's backups forgets it in A's repository
    assert run("backup_delete", inst, capture, backup_id=str(legacy.id)).status == JobStatus.SUCCEEDED
    assert [c[0][1] for c in agents[a.pk].called("forget_backups")] == [[legacy.snapshot_id]]


def test_restore_as_new_server_happens_on_the_node_holding_the_backup(
    make_instance, two_nodes, django_capture_on_commit_callbacks
):
    capture = django_capture_on_commit_callbacks
    a, b, agents, _ = two_nodes
    inst = make_instance(name="Origin")
    agents[a.pk].put_file(inst.id, "world/level.dat", b"from-a")
    backup_id = run("backup", inst, capture, kind="manual").result["backup_id"]
    assert _migrate(inst, b, capture).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    blocker = make_instance(name="Blocker", node=a)
    target = make_instance(name="Restored", node=b, game_port=blocker.game_port)  # as the view creates it

    job = run("restore_new", inst, capture, backup_id=backup_id, target_id=str(target.id))
    assert job.status == JobStatus.SUCCEEDED, job.error
    target.refresh_from_db()
    assert target.node_id == a.pk
    assert target.game_port != blocker.game_port  # re-allocated on A
    assert agents[a.pk].get_file(target.id, "world/level.dat") == b"from-a"
    assert str(target.id) in agents[a.pk].containers


def test_backup_views_use_the_node_holding_the_backup(
    make_instance, two_nodes, users, client_for, monkeypatch, django_capture_on_commit_callbacks
):
    capture = django_capture_on_commit_callbacks
    a, b, agents, _ = two_nodes
    inst = make_instance(name="Browsed")
    agents[a.pk].put_file(inst.id, "world/level.dat", b"from-a")
    backup_id = run("backup", inst, capture, kind="manual").result["backup_id"]
    assert _migrate(inst, b, capture).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    owner = client_for(users["owner"])
    seen: list[tuple[str, int]] = []

    def files(client, instance_id, snapshot_id, path=""):
        seen.append(("files", client.node.pk))
        return {"path": path, "entries": []}

    def download(client, instance_id, snapshot_id, path):
        seen.append(("download", client.node.pk))
        return iter([b"payload"]), None

    monkeypatch.setattr(AgentClient, "backup_files", files)
    monkeypatch.setattr(AgentClient, "backup_download", download)
    base = f"/api/instances/{inst.id}/backups/{backup_id}"

    # browsing and downloading read node A's repository, where the snapshot is
    assert owner.get(f"{base}/files?path=").status_code == 200
    dl = owner.get(f"{base}/download?path=world/level.dat")
    assert dl.status_code == 200 and b"".join(dl.streaming_content) == b"payload"
    assert seen == [("files", a.pk), ("download", a.pk)]

    rows = {r["id"]: r for r in owner.get(f"/api/instances/{inst.id}/backups").json()["backups"]}
    assert (rows[backup_id]["node_name"], rows[backup_id]["on_current_node"]) == (a.name, False)

    # in-place restores are refused right away, without queuing a job that would fail
    jobs = Job.objects.filter(instance=inst).count()
    for path, body in (("restore", {"confirm": True}), ("restore-path", {"path": "world", "confirm": True})):
        resp = owner.post(f"{base}/{path}", body, format="json")
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "backup_on_other_node", path
    assert Job.objects.filter(instance=inst).count() == jobs

    # restore as new: the port is checked on A and the new server is created there
    blocker = make_instance(name="Blocker", node=a)
    taken = owner.post(f"{base}/restore-as-new", {"name": "Copy", "game_port": blocker.game_port}, format="json")
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "port_conflict"
    with capture(execute=True):
        resp = owner.post(f"{base}/restore-as-new", {"name": "Copy"}, format="json")
    assert resp.status_code == 202, resp.content
    target = Instance.objects.get(pk=resp.json()["instance"]["id"])
    assert target.node_id == a.pk and target.game_port != blocker.game_port
    assert agents[a.pk].get_file(target.id, "world/level.dat") == b"from-a"
    assert Job.objects.get(pk=resp.json()["job"]["id"]).status == JobStatus.SUCCEEDED


def test_backups_on_a_deleted_node_cannot_be_read(make_instance, fake_agent, users, client_for):
    inst = make_instance(name="Orphaned")
    gone = Backup.objects.create(
        instance=inst,
        name="old",
        status=BackupStatus.SUCCEEDED,
        snapshot_id="e" * 64,
        meta={"node_id": 987654, "node_name": "retired"},
    )
    owner = client_for(users["owner"])
    base = f"/api/instances/{inst.id}/backups/{gone.id}"
    for resp in (owner.get(f"{base}/files?path="), owner.post(f"{base}/restore-as-new", {}, format="json")):
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "backup_node_missing"
    assert not Instance.objects.filter(name__contains="restored").exists()
    row = owner.get(f"/api/instances/{inst.id}/backups").json()["backups"][0]
    assert (row["on_current_node"], row["node_id"], row["node_name"]) == (False, None, "retired")


def test_delete_source_only_drops_backups_of_the_old_node(make_instance, two_nodes, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    a, b, agents, _ = two_nodes
    third = Node.objects.create(name="node-c", agent_url="http://agent-c.invalid:8700", agent_token="t" * 30)
    inst = make_instance(name="Hopper")
    agents[a.pk].put_file(inst.id, "world/level.dat", b"x")
    on_a = Backup.objects.get(pk=run("backup", inst, capture, kind="manual").result["backup_id"])
    on_c = Backup.objects.create(
        instance=inst, name="from C", status=BackupStatus.SUCCEEDED, snapshot_id="c" * 64, meta={"node_id": third.pk}
    )
    job = _migrate(inst, b, capture, delete_source=True)
    assert job.status == JobStatus.SUCCEEDED, job.error
    on_a.refresh_from_db()
    on_c.refresh_from_db()
    assert on_a.status == BackupStatus.DELETED
    assert on_c.status == BackupStatus.SUCCEEDED


def test_modpack_rollback_refuses_a_backup_on_another_node(
    inst, node, fake_agent, users, client_for, django_capture_on_commit_callbacks
):
    other = Node.objects.create(name="node-x", agent_url="http://agent-x.invalid:8700", agent_token="t" * 30)
    backup = Backup.objects.create(
        instance=inst,
        name="pre-update",
        kind="pre_update",
        status=BackupStatus.SUCCEEDED,
        snapshot_id="d" * 64,
        meta={"node_id": other.pk, "modpack_before": {"modpack_version_id": "1"}},
    )
    job = run("modpack_rollback", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id))
    assert (job.status, job.error_code) == (JobStatus.FAILED, "backup_on_other_node")
    assert not fake_agent.called("restore_backup")

    # the API refuses right away and does not offer it as a rollback point
    owner = client_for(users["owner"])
    jobs = Job.objects.filter(instance=inst).count()
    resp = owner.post(f"/api/instances/{inst.id}/modpack/rollback", {"backup_id": str(backup.id), "confirm": True}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "backup_on_other_node"
    assert Job.objects.filter(instance=inst).count() == jobs


# =====================================================================================
# 6. Scheduled snapshot retention is per schedule
# =====================================================================================
def test_snapshot_retention_only_prunes_the_schedules_own_snapshots(inst, fake_agent, django_capture_on_commit_callbacks):
    for label, kind in [
        ("my manual one", "manual"),
        ("before rollback", "pre_restore"),
        ("scheduled daily", "scheduled"),
        ("scheduled hourly", "scheduled"),
        ("scheduled daily", "scheduled"),
        ("scheduled hourly", "scheduled"),
        ("scheduled hourly", "scheduled"),
    ]:
        fake_agent.create_snapshot(inst.id, label, "none", kind)
    hourly_before = [s["name"] for s in fake_agent.snaps[str(inst.id)] if s["label"] == "scheduled hourly"]

    job = run("snapshot_create", inst, django_capture_on_commit_callbacks, label="scheduled hourly", kind="scheduled", keep=2)
    assert job.status == JobStatus.SUCCEEDED, job.error
    snaps = fake_agent.snaps[str(inst.id)]
    hourly = [s["name"] for s in snaps if s["label"] == "scheduled hourly"]
    assert hourly == [hourly_before[-1], job.result["name"]]  # the two newest of THIS schedule
    assert sorted(job.result["retention_removed"]) == sorted(hourly_before[:2])
    assert [s["label"] for s in snaps if s["label"] != "scheduled hourly"] == [
        "my manual one",
        "before rollback",
        "scheduled daily",
        "scheduled daily",
    ]


def test_snapshot_schedules_with_the_same_name_keep_their_own_snapshots(inst, fake_agent, django_capture_on_commit_callbacks):
    from apps.instances.models import Schedule
    from apps.instances.scheduling import execute

    def labels():
        return [s["label"] for s in fake_agent.snaps[str(inst.id)]]

    fake_agent.create_snapshot(inst.id, "scheduled hourly", "none", "scheduled")  # older version's label
    first = Schedule.objects.create(instance=inst, name="hourly", action="snapshot", cron="0 * * * *", payload={"keep": 1})
    second = Schedule.objects.create(instance=inst, name="hourly", action="snapshot", cron="30 * * * *", payload={"keep": 1})
    for sched in (first, second, first):
        with django_capture_on_commit_callbacks(execute=True):
            assert execute(sched).startswith("Queued job")
    # each schedule kept its newest snapshot; the ambiguous legacy one is left alone
    assert sorted(labels()) == sorted(["scheduled hourly", f"scheduled #{second.pk} hourly", f"scheduled #{first.pk} hourly"])

    # renaming keeps a schedule's older snapshots under its retention (matched by id)
    Schedule.objects.filter(pk=first.pk).update(name="renamed")
    first.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        execute(first)
    assert sorted(labels()) == sorted(["scheduled hourly", f"scheduled #{second.pk} hourly", f"scheduled #{first.pk} renamed"])


def test_snapshot_schedule_still_prunes_its_legacy_labelled_snapshots(inst, fake_agent, django_capture_on_commit_callbacks):
    from apps.instances.models import Schedule
    from apps.instances.scheduling import execute

    for _ in range(2):
        fake_agent.create_snapshot(inst.id, "scheduled daily", "none", "scheduled")
    fake_agent.create_snapshot(inst.id, "scheduled daily", "none", "manual")  # not a schedule's
    sched = Schedule.objects.create(instance=inst, name="daily", action="snapshot", cron="0 4 * * *", payload={"keep": 2})
    with django_capture_on_commit_callbacks(execute=True):
        execute(sched)
    snaps = fake_agent.snaps[str(inst.id)]
    assert [(s["label"], s["kind"]) for s in snaps] == [
        ("scheduled daily", "scheduled"),
        ("scheduled daily", "manual"),
        (f"scheduled #{sched.pk} daily", "scheduled"),
    ]


# =====================================================================================
# 7. Reconcile never writes stale values over columns jobs changed
# =====================================================================================
def test_reconcile_does_not_write_back_job_owned_columns(inst, fake_agent):
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    stale = Instance.objects.select_related("node").get(pk=inst.pk)  # loaded by a slow reconcile pass
    installed = timezone.now() - timedelta(days=3)
    # meanwhile a job recreated the container and recorded its new spec hash / install time
    Instance.objects.filter(pk=inst.pk).update(container_spec_hash="fresh-hash", installed_at=installed)
    apply_observation(stale, fake_agent.status(inst.id))
    inst.refresh_from_db()
    assert inst.container_spec_hash == "fresh-hash"
    assert inst.installed_at == installed
    assert inst.status == Status.RUNNING  # observed state is still written


def test_reconcile_fills_empty_job_columns_once(inst, fake_agent):
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert inst.installed_at is not None
    # the label is computed by the agent from its own form of the spec: adopting it made the
    # server read restart-required for ever (audit 2026-11-09, backend-lifecycle#8)
    assert inst.container_spec_hash == ""


def test_reconcile_skips_an_observation_made_before_archive_or_migration(inst, fake_agent, node):
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    stale = Instance.objects.select_related("node").get(pk=inst.pk)
    Instance.objects.filter(pk=inst.pk).update(archived_at=timezone.now(), status=Status.ARCHIVED, container_spec_hash="")
    assert apply_observation(stale, fake_agent.status(inst.id)) is False
    inst.refresh_from_db()
    assert (inst.status, inst.container_spec_hash) == (Status.ARCHIVED, "")

    other = Node.objects.create(name="node-m", agent_url="http://agent-m.invalid:8700", agent_token="t" * 30)
    Instance.objects.filter(pk=inst.pk).update(archived_at=None, status=Status.UPDATING, node=other)
    stale.archived_at = None
    assert apply_observation(stale, fake_agent.status(inst.id)) is False
    inst.refresh_from_db()
    assert inst.status == Status.UPDATING


# =====================================================================================
# 8./9. Crash-loop explanation is kept; Docker-restarted crashes notify (rate-limited)
# =====================================================================================
@pytest.fixture
def notifications(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "apps.notifications.service.notify", lambda event, title, message, **kw: sent.append((event, title, message)) or 1
    )
    return sent


def _crash_looping(fake_agent, inst):
    fake_agent.start_healthy = False
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    Instance.objects.filter(pk=inst.pk).update(desired_state="running", installed_at=timezone.now())


def test_crash_loop_auto_stop_explanation_survives_until_the_next_start(
    inst, fake_agent, notifications, django_capture_on_commit_callbacks
):
    _crash_looping(fake_agent, inst)
    fake_agent.set_container_state(inst.id, restart_count=1)
    reconcile_instance(inst.id)  # baseline of the not-ready streak
    fake_agent.set_container_state(inst.id, restart_count=7)
    reconcile_instance(inst.id)  # 6 crashes: auto-stop
    inst.refresh_from_db()
    assert inst.status == Status.FAILED and inst.status_detail.startswith(AUTO_STOP_PREFIX)
    assert inst.desired_state == "stopped" and fake_agent.containers[str(inst.id)]["running"] is False
    detail = inst.status_detail

    for _ in range(3):  # later reconcile passes see a plain stopped container
        reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert (inst.status, inst.status_detail) == (Status.FAILED, detail)

    fake_agent.start_healthy = True
    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    assert inst.status == Status.RUNNING and not inst.status_detail.startswith(AUTO_STOP_PREFIX)
    reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert inst.status == Status.RUNNING


def test_docker_restarted_crash_notifies_once(inst, fake_agent, notifications):
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    Instance.objects.filter(pk=inst.pk).update(desired_state="running", installed_at=timezone.now())
    reconcile_instance(inst.id)  # healthy, restart_count 0: baseline only
    assert [n for n in notifications if n[0] == "instance.failed"] == []

    # the server crashed and Docker's restart policy brought it back between two passes
    fake_agent.set_container_state(inst.id, restart_count=1, started_at=fake_agent._now())
    reconcile_instance(inst.id)
    failed = [n for n in notifications if n[0] == "instance.failed"]
    assert len(failed) == 1 and "restarted unexpectedly" in failed[0][1]

    fake_agent.set_container_state(inst.id, restart_count=3)  # flapping: no flood
    reconcile_instance(inst.id)
    assert len([n for n in notifications if n[0] == "instance.failed"]) == 1


def test_restart_count_changes_without_a_crash_do_not_notify(inst, fake_agent, notifications):
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    Instance.objects.filter(pk=inst.pk).update(desired_state="running", installed_at=timezone.now())
    reconcile_instance(inst.id)
    # recreated container (new id, count starts again) and a job-driven restart
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": [], "x": 1}, start=True)
    reconcile_instance(inst.id)
    job = Job.objects.create(kind="restart", title="restart", instance=inst, status=JobStatus.RUNNING)
    Instance.objects.filter(pk=inst.pk).update(active_job=job)
    fake_agent.set_container_state(inst.id, restart_count=1)
    reconcile_instance(inst.id)
    assert [n for n in notifications if n[0] == "instance.failed"] == []
