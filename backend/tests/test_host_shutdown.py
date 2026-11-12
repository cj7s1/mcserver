"""Stopping servers before the host stops Docker (manage.py host_shutdown) and starting them again afterwards."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.backups.models import Backup, BackupStatus
from apps.instances import reconcile, scheduling, services, state, tasks
from apps.instances.management.commands import host_shutdown
from apps.instances.models import Instance, Status
from apps.instances.spec import build_spec, current_spec_hash
from apps.jobs import runner
from apps.jobs import tasks as job_tasks
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentError, AgentUnavailable
from apps.nodes.models import Node

pytestmark = pytest.mark.django_db

STOP_AUDIT = "instance.stopped_for_host_shutdown"
RESUME_AUDIT = "instance.resumed_after_boot"
NOT_RESUMED_AUDIT = "instance.not_resumed_after_boot"


@pytest.fixture
def server(make_instance, fake_agent):
    """Create a server with a container: ``server("A", running=True)``.

    ``running=False`` is a server the user stopped (``desired_state`` "stopped"); a server that
    the hook stopped is ``server("A", running=False, desired_state="running")`` plus ``flag``.
    """

    def _make(name: str, *, running: bool, **fields):
        fields.setdefault("desired_state", "running" if running else "stopped")
        inst = make_instance(name=name, **fields)
        fake_agent.apply(inst.id, build_spec(inst), start=running)
        Instance.objects.filter(pk=inst.pk).update(container_spec_hash=current_spec_hash(inst), installed_at=timezone.now())
        inst.refresh_from_db()
        return inst

    return _make


@pytest.fixture
def marked(server, fake_agent):
    """A server the hook stopped before Docker went down: meant to run, container exited, marked."""

    def _make(name: str, *, minutes_ago: float = 5, **fields):
        inst = server(name, running=True, **fields)
        fake_agent.stop(inst.id)  # the hook's stop: the container has exited with code 0
        flag(inst, minutes_ago=minutes_ago)
        inst.refresh_from_db()
        return inst

    return _make


def shutdown(**options) -> str:
    out = io.StringIO()
    call_command("host_shutdown", stdout=out, stderr=out, **options)
    return out.getvalue()


def is_running(fake_agent, inst) -> bool:
    return bool(fake_agent.containers[str(inst.id)]["running"])


def flag(inst, *, minutes_ago: float = 5) -> None:
    Instance.objects.filter(pk=inst.pk).update(
        resume_after_boot=True, resume_flagged_at=timezone.now() - timedelta(minutes=minutes_ago)
    )


def flags(inst) -> tuple[bool, bool]:
    inst.refresh_from_db()
    return inst.resume_after_boot, inst.resume_flagged_at is not None


@pytest.fixture
def after_boot(monkeypatch):
    """The panel process started after the servers were marked (Docker went down and came back)."""
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=1))


def run_job(kind, inst, capture):
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst)
    job.refresh_from_db()
    return job


# =====================================================================================
# manage.py host_shutdown
# =====================================================================================
def test_running_servers_are_marked_and_stopped_in_parallel(server, make_instance, fake_agent):
    a, b, c = (server(name, running=True) for name in ("Alpha", "Bravo", "Charlie"))
    stopped = server("Delta", running=False)
    never_started = make_instance(name="Echo")  # no container at all
    # every stop request waits until all three are in flight: one after the other would time out
    together = threading.Barrier(3, timeout=5)
    fake_agent.hooks["stop"] = lambda *_a, **_k: together.wait()

    out = shutdown()

    for inst in (a, b, c):
        assert flags(inst) == (True, True)
        assert inst.desired_state == "running"  # they are meant to run: only the mark says why they are down
        assert not is_running(fake_agent, inst)
        assert f"stopped   {inst.name} in" in out
    for inst in (stopped, never_started):
        assert flags(inst) == (False, False)
        assert inst.name not in out
    assert stopped.desired_state == "stopped"
    assert sorted(str(args[0]) for args, _kw in fake_agent.called("stop")) == sorted(str(i.id) for i in (a, b, c))
    assert all(kw == {"timeout": 45} for _args, kw in fake_agent.called("stop"))  # min(server's 90 s, --timeout)
    assert "3 stopped, 0 failed, 0 without an answer, 2 not running (left alone)" in out
    assert "3 marked to be started again after the next boot" in out

    events = list(AuditEvent.objects.filter(action=STOP_AUDIT))
    assert sorted(e.instance_id for e in events) == sorted(i.id for i in (a, b, c))
    for event in events:
        assert event.actor is None and event.actor_username == ""  # the system
        assert event.success is True
        assert event.details["result"] == "stopped" and event.details["timeout"] == 45
    # no job, so no Celery worker, no Redis and no free worker slot is needed
    assert Job.objects.count() == 0


def test_nothing_running_is_not_an_error(server, fake_agent):
    idle = server("Idle", running=False)
    out = shutdown()
    assert "no running servers" in out
    assert flags(idle) == (False, False)
    assert not fake_agent.called("stop") and not AuditEvent.objects.filter(action=STOP_AUDIT).exists()


def test_the_stop_timeout_is_the_smaller_of_the_servers_and_the_commands(server, fake_agent):
    quick = server("Quick", running=True, stop_timeout=30)
    slow = server("Slow", running=True, stop_timeout=300)
    shutdown(timeout=45)
    asked = {str(args[0]): kw["timeout"] for args, kw in fake_agent.called("stop")}
    assert asked == {str(quick.id): 30, str(slow.id): 45}


def test_unreachable_agent_fails_without_marking_anything(server, fake_agent):
    inst = server("Alpha", running=True)
    fake_agent.failures["list_containers"] = AgentUnavailable("Cannot reach the node agent")
    with pytest.raises(CommandError, match="could not be reached"):
        shutdown()
    assert flags(inst) == (False, False)
    assert is_running(fake_agent, inst)
    assert not AuditEvent.objects.filter(action=STOP_AUDIT).exists()


def test_agent_that_stops_answering_fails_but_keeps_the_marks(server, fake_agent):
    """The hook then leaves the servers to Docker's own stop. Docker starts them again at boot; a
    server Docker does not start (restart policy "no") is started by the panel, because of the mark."""
    a, b = server("Alpha", running=True), server("Bravo", running=True)
    fake_agent.failures["stop"] = AgentUnavailable("Cannot reach the node agent")
    with pytest.raises(CommandError, match="stopped answering"):
        shutdown()
    assert flags(a) == (True, True) and flags(b) == (True, True)
    events = AuditEvent.objects.filter(action=STOP_AUDIT)
    assert events.count() == 2
    assert all(e.success is False and e.details["result"] == "failed" for e in events)


def test_one_refused_stop_does_not_fail_the_command(server, fake_agent):
    ok, busy = server("Alpha", running=True), server("Bravo", running=True)

    def refuse(instance_id, **_kw):
        if str(instance_id) == str(busy.id):
            raise AgentError("Instance is busy with a 'backup' operation", "instance_busy", 409)

    fake_agent.hooks["stop"] = refuse
    out = shutdown()  # exit status 0: the agent was reached
    assert f"stopped   {ok.name}" in out
    assert f"FAILED    {busy.name}: The server is busy with another operation on its node (backup)" in out
    assert "1 stopped, 1 failed" in out
    assert not is_running(fake_agent, ok) and is_running(fake_agent, busy)
    # both were running, so both come back after the next boot
    assert flags(ok) == (True, True) and flags(busy) == (True, True)
    failed = AuditEvent.objects.get(action=STOP_AUDIT, instance=busy)
    assert failed.success is False and "busy" in failed.details["error"]


def test_a_server_held_by_a_job_is_stopped_all_the_same(server, fake_agent):
    inst = server("Alpha", running=True)
    job = Job.objects.create(kind="backup", title="Backup", instance=inst, status=JobStatus.RUNNING, heartbeat_at=timezone.now())
    Instance.objects.filter(pk=inst.pk).update(active_job=job)
    out = shutdown()
    assert f"stopped   {inst.name}" in out
    assert not is_running(fake_agent, inst)
    assert flags(inst) == (True, True)
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING and inst.active_job_id == job.id  # the job and its lock are left alone
    assert Job.objects.count() == 1


def test_the_command_returns_at_its_time_limit(server, fake_agent, monkeypatch):
    fine, hanging = server("Alpha", running=True), server("Bravo", running=True)
    monkeypatch.setattr(host_shutdown, "MARGIN_SECONDS", 0.3)
    release = threading.Event()

    def hang(instance_id, **_kw):
        if str(instance_id) == str(hanging.id):
            release.wait(20)

    fake_agent.hooks["stop"] = hang
    began = time.monotonic()
    try:
        out = shutdown(timeout=1)
        took = time.monotonic() - began
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name.startswith("host-shutdown-"):
                thread.join(5)
    assert took < 4  # --timeout 1 plus the margin, not the 20 s the agent needs
    assert f"stopped   {fine.name}" in out
    assert f"NO ANSWER {hanging.name}" in out
    assert "1 stopped, 0 failed, 1 without an answer" in out
    assert flags(fine) == (True, True) and flags(hanging) == (True, True)
    late = AuditEvent.objects.get(action=STOP_AUDIT, instance=hanging)
    assert late.success is False and late.details["result"] == "no_answer"


def test_container_that_still_runs_after_the_stop_is_reported(server, fake_agent, monkeypatch):
    inst = server("Alpha", running=True)
    from apps.nodes.client import AgentClient

    monkeypatch.setattr(AgentClient, "stop", lambda _client, _iid, timeout=None: {"exists": True, "running": True})
    out = shutdown()
    assert f"FAILED    {inst.name}: the agent answered" in out
    assert AuditEvent.objects.get(action=STOP_AUDIT).success is False
    assert flags(inst) == (True, True)


def test_unclean_exit_code_is_shown(server, fake_agent):
    inst = server("Alpha", running=True)
    original = fake_agent._set_exited
    fake_agent._set_exited = lambda c, _code: original(c, 137)  # killed when the time ran out
    out = shutdown()
    assert f"stopped   {inst.name} in" in out and "exit code 137: not a clean stop" in out
    assert AuditEvent.objects.get(action=STOP_AUDIT).details["exit_code"] == 137


def test_archived_servers_and_other_nodes_are_left_alone(server, make_instance, fake_agent):
    mine = server("Mine", running=True)
    archived = server("Archived", running=True)  # a leftover container of an archived server
    Instance.objects.filter(pk=archived.pk).update(archived_at=timezone.now(), status=Status.ARCHIVED)
    other_node = Node.objects.create(name="node-b", agent_url="http://agent-b.invalid:8700", agent_token="t" * 30)
    elsewhere = make_instance(name="Elsewhere", node=other_node, desired_state="running")
    fake_agent.apply(elsewhere.id, build_spec(elsewhere), start=True)

    out = shutdown()  # the default node
    assert [str(args[0]) for args, _kw in fake_agent.called("stop")] == [str(mine.id)]
    assert flags(mine) == (True, True)
    assert flags(archived) == (False, False) and flags(elsewhere) == (False, False)
    assert "without a server in the panel were left to Docker" in out  # the fake agent lists every container

    out = shutdown(node="node-b")
    assert f"stopped   {elsewhere.name}" in out and flags(elsewhere) == (True, True)
    with pytest.raises(CommandError, match="no node named 'nope'"):
        shutdown(node="nope")


@pytest.mark.parametrize("bad", [0, -5, 901])
def test_timeout_must_be_sane(bad, server):
    server("Alpha", running=True)
    with pytest.raises(CommandError, match="between 1 and 900"):
        shutdown(timeout=bad)


# =====================================================================================
# Resume after the next boot (scheduler tick)
# =====================================================================================
def test_one_server_is_started_per_tick(server, fake_agent, after_boot, django_capture_on_commit_callbacks):
    a, b, c = (server(name, running=True) for name in ("Alpha", "Bravo", "Charlie"))
    shutdown()
    # the mark was written before "Docker went down": date it accordingly
    for inst in (a, b, c):
        flag(inst)
    expected = [a, b, c]  # by name
    for n, inst in enumerate(expected, start=1):
        with django_capture_on_commit_callbacks(execute=True):
            tasks.scheduler_tick()
        assert is_running(fake_agent, inst)
        assert flags(inst) == (False, False)
        assert [is_running(fake_agent, i) for i in expected] == [True] * n + [False] * (3 - n)
        assert [flags(i)[0] for i in expected] == [False] * n + [True] * (3 - n)
        assert Job.objects.filter(kind="start").count() == n
    with django_capture_on_commit_callbacks(execute=True):
        tasks.scheduler_tick()
    assert Job.objects.filter(kind="start").count() == 3  # nothing left to do

    jobs = list(Job.objects.filter(kind="start"))
    assert all(j.status == JobStatus.SUCCEEDED and j.user is None for j in jobs)
    assert all(j.title == "Start server (after the host restart)" for j in jobs)
    events = list(AuditEvent.objects.filter(action=RESUME_AUDIT))
    assert sorted(e.instance_id for e in events) == sorted(i.id for i in expected)
    assert all(e.actor is None and e.success and e.details["job"] and e.details["stopped_at"] for e in events)


def test_nothing_is_started_while_the_host_is_still_shutting_down(server, fake_agent, django_capture_on_commit_callbacks):
    """Between the hook and the end of Docker the panel still runs: a start now would be killed seconds later."""
    stopped, still_stopping = server("Alpha", running=True), server("Bravo", running=True)
    fake_agent.hooks["stop"] = lambda iid, **_kw: None
    shutdown()
    fake_agent.set_container_state(still_stopping.id, running=True, status="running")  # its stop is not through yet
    assert not is_running(fake_agent, stopped) and is_running(fake_agent, still_stopping)
    # runtime_started_at() is real here: this test process started long before the marks were written
    assert scheduling.runtime_started_at() < Instance.objects.get(pk=stopped.pk).resume_flagged_at
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers()
    assert result == {"started": None, "cleared": [], "waiting": 2}
    assert flags(stopped) == (True, True) and flags(still_stopping) == (True, True)  # not started, not forgotten
    assert not Job.objects.exists() and not fake_agent.called("start")


def test_a_mark_dated_in_the_future_does_not_block_forever(marked, fake_agent, monkeypatch, django_capture_on_commit_callbacks):
    inst = marked("Alpha")
    Instance.objects.filter(pk=inst.pk).update(resume_after_boot=True, resume_flagged_at=timezone.now() + timedelta(days=2))
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(days=30))
    with django_capture_on_commit_callbacks(execute=True):
        assert scheduling.resume_flagged_servers()["started"] == str(inst.id)
    assert is_running(fake_agent, inst)


def test_runtime_started_at_is_a_past_moment():
    started = scheduling.runtime_started_at()
    assert started.tzinfo is not None
    assert started <= timezone.now()


def test_the_panel_start_time_is_the_start_time_of_pid_1(monkeypatch):
    """The start of the container, not of the process that asks: a worker that the supervisor
    started again a minute ago must not make a mark written two minutes ago look like one from
    before a boot. The name of PID 1 may hold blanks and brackets (proc(5): field 2)."""
    real_open = open
    opened: list[str] = []
    files = {
        "/proc/1/stat": "1 (tini (x) y) S 0 1 1 0 -1 4194560 100 0 0 0 5 6 0 0 20 0 1 0 123456 1000000 80 18446744073709551615 1 1\n",
        "/proc/stat": "cpu  1 2 3 4\ncpu0 1 2 3 4\nbtime 1700000000\nprocesses 99\n",
    }

    def fake_open(path, *args, **kwargs):
        if str(path) in files:
            opened.append(str(path))
            return io.StringIO(files[str(path)])
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(scheduling.os, "sysconf", lambda name: {"SC_CLK_TCK": 100}[name])
    started = scheduling.runtime_started_at()
    # field 22 of /proc/1/stat (123456 clock ticks after the host booted) on top of the boot time
    assert started == datetime.fromtimestamp(1700000000 + 1234.56, tz=UTC)
    assert opened == ["/proc/1/stat", "/proc/stat"]
    # without a usable /proc: the load time of the module, never an error
    files["/proc/1/stat"] = "garbage"
    assert scheduling.runtime_started_at() == scheduling._LOADED_AT


def test_mark_is_dropped_when_the_server_runs_already(server, fake_agent, after_boot, django_capture_on_commit_callbacks):
    inst = server("Alpha", running=True)  # Docker's restart policy or the user was faster
    flag(inst)
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers()
    assert result == {"started": None, "cleared": [str(inst.id)], "waiting": 0}
    assert flags(inst) == (False, False)
    assert not Job.objects.exists() and not AuditEvent.objects.filter(action=RESUME_AUDIT).exists()


def test_archived_server_is_not_started(server, fake_agent, after_boot, django_capture_on_commit_callbacks):
    inst = server("Alpha", running=False)
    flag(inst)
    Instance.objects.filter(pk=inst.pk).update(archived_at=timezone.now(), status=Status.ARCHIVED)
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers()
    assert result["started"] is None and result["cleared"] == [str(inst.id)]
    assert flags(inst) == (False, False)
    assert not Job.objects.exists() and not is_running(fake_agent, inst)
    assert not fake_agent.called("list_containers")  # no agent needed to drop the mark


def test_nothing_happens_while_the_agent_is_unreachable(marked, fake_agent, after_boot, django_capture_on_commit_callbacks):
    a, b = marked("Alpha"), marked("Bravo")
    fake_agent.failures["list_containers"] = AgentUnavailable("Cannot reach the node agent")
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers()
    assert result == {"started": None, "cleared": [], "waiting": 2}
    assert flags(a) == (True, True) and flags(b) == (True, True)
    assert not Job.objects.exists()
    assert len(fake_agent.called("list_containers")) == 1  # asked once per node, not once per server
    # the agent is back at the next tick
    del fake_agent.failures["list_containers"]
    with django_capture_on_commit_callbacks(execute=True):
        assert scheduling.resume_flagged_servers()["started"] == str(a.id)


def test_a_server_held_by_a_job_waits_and_the_next_one_is_started(
    marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    busy, free = marked("Alpha"), marked("Bravo")
    job = Job.objects.create(kind="backup", title="Backup", instance=busy, status=JobStatus.RUNNING, heartbeat_at=timezone.now())
    Instance.objects.filter(pk=busy.pk).update(active_job=job)
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers()
    assert result["started"] == str(free.id) and result["waiting"] == 1
    assert flags(busy) == (True, True) and not is_running(fake_agent, busy)
    assert flags(free) == (False, False) and is_running(fake_agent, free)
    # Once the job is over it gets its turn. The backup failed, but a backup only reads the
    # server's files: its failure is no reason to keep the server down.
    runner.finish(job, JobStatus.FAILED, error="restic: repository is locked", error_code="backup_failed")
    with django_capture_on_commit_callbacks(execute=True):
        assert scheduling.resume_flagged_servers()["started"] == str(busy.id)
    assert is_running(fake_agent, busy)
    assert not AuditEvent.objects.filter(action=NOT_RESUMED_AUDIT).exists()


def test_resume_recreates_a_container_from_before_this_release(
    marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    inst = marked("Alpha")
    legacy = hashlib.sha256(json.dumps(build_spec(inst), sort_keys=True).encode()).hexdigest()[:16]
    Instance.objects.filter(pk=inst.pk).update(container_spec_hash=legacy)
    old_container = fake_agent.containers[str(inst.id)]["id"]
    applied_before = len(fake_agent.called("apply"))
    with django_capture_on_commit_callbacks(execute=True):
        scheduling.resume_flagged_servers()
    assert len(fake_agent.called("apply")) == applied_before + 1  # recreated, not just started
    assert fake_agent.containers[str(inst.id)]["id"] != old_container and is_running(fake_agent, inst)
    inst.refresh_from_db()
    assert inst.container_spec_hash == current_spec_hash(inst)


def test_a_failing_resume_step_does_not_stop_the_schedules(monkeypatch):
    def boom(_now):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(scheduling, "resume_flagged_servers", boom)
    assert tasks.scheduler_tick() == 0  # run_due_schedules still ran


# --- explicit actions settle the mark ----------------------------------------------------------------
@pytest.mark.parametrize("kind", ["stop", "start", "restart", "archive"])
def test_explicit_job_clears_the_mark(kind, server, fake_agent, django_capture_on_commit_callbacks):
    inst = server("Alpha", running=False)
    flag(inst)
    assert run_job(kind, inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    assert flags(inst) == (False, False)


def test_stop_from_the_api_clears_the_mark_and_the_server_stays_down(
    server, fake_agent, clients, after_boot, django_capture_on_commit_callbacks
):
    inst = server("Alpha", running=False)
    flag(inst)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "stop"}, format="json")
    assert resp.status_code == 202
    assert flags(inst) == (False, False)
    with django_capture_on_commit_callbacks(execute=True):
        assert scheduling.resume_flagged_servers() == {"started": None, "cleared": [], "waiting": 0}
    assert not is_running(fake_agent, inst) and not Job.objects.filter(kind="start").exists()


def test_kill_clears_the_mark(server, fake_agent, users):
    inst = server("Alpha", running=False)
    flag(inst)
    services.power(inst, "kill", users["owner"])
    assert flags(inst) == (False, False)
    assert inst.desired_state == "stopped"


def test_delete_clears_the_mark_even_when_it_fails(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server("Alpha", running=False)
    flag(inst)
    fake_agent.failed_operations["delete_data"] = ("Permission denied", "internal_error")
    assert run_job("delete", inst, django_capture_on_commit_callbacks).status == JobStatus.FAILED
    assert flags(inst) == (False, False)  # the user wanted it gone: it is not started again behind their back


# --- what the server page shows meanwhile ---------------------------------------------------------------
def test_status_says_why_the_server_is_down(server, fake_agent):
    inst = server("Alpha", running=True)
    shutdown()
    inst.refresh_from_db()
    container = fake_agent.status(inst.id)
    assert state.compute(inst, container, None) == (
        Status.STOPPED,
        "Stopped because the host was shutting down; it is started again automatically",
    )
    container["exit_code"] = 137
    status, detail = state.compute(inst, container, None)
    assert status == Status.STOPPED and "exit code 137: not a clean stop" in detail
    # a server that is no longer meant to run is not promised a start (the resume step drops such a mark)
    inst.desired_state = "stopped"
    assert state.compute(inst, container, None) == (Status.STOPPED, "")
    inst.desired_state = "running"
    # without the mark the usual texts apply
    inst.resume_after_boot = False
    assert state.compute(inst, container, None)[0] == Status.FAILED


def test_host_shutdown_prints_only_its_own_lines(server, fake_agent, caplog):
    """The hook copies every line of the command's output into the host's syslog: the JSON log
    line of each audit entry does not belong there. The entries themselves are still written."""
    a, b = server("Alpha", running=True), server("Bravo", running=True)
    before = logging.root.manager.disable
    with caplog.at_level(logging.INFO):
        out = shutdown()
        assert [r for r in caplog.records if r.levelno < logging.ERROR] == []
        assert AuditEvent.objects.filter(action=STOP_AUDIT).count() == 2
        # afterwards (and after a failure) logging is as it was
        assert logging.root.manager.disable == before
        with pytest.raises(CommandError):
            shutdown(node="nope")
        assert logging.root.manager.disable == before
        logging.getLogger("mcpanel.audit").info("audit something else")
        assert [r.getMessage() for r in caplog.records] == ["audit something else"]
    lines = out.splitlines()
    assert len(lines) == 3  # one per server and the summary
    assert lines[0].startswith(f"stopped   {a.name} in") and lines[1].startswith(f"stopped   {b.name} in")
    assert lines[2].startswith("host_shutdown: node ")


# =====================================================================================
# Marks that must not start a server (review 2026-11-10)
# =====================================================================================
def resume(capture) -> dict:
    with capture(execute=True):
        return scheduling.resume_flagged_servers()


def not_resumed(inst) -> AuditEvent:
    return AuditEvent.objects.get(action=NOT_RESUMED_AUDIT, instance=inst)


def test_a_server_that_is_no_longer_meant_to_run_is_not_started(
    marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    """Something decided after the mark that the server stays down, and left the mark: no start."""
    inst = marked("Alpha")
    Instance.objects.filter(pk=inst.pk).update(desired_state="stopped")
    result = resume(django_capture_on_commit_callbacks)
    assert result == {"started": None, "cleared": [str(inst.id)], "waiting": 0}
    assert flags(inst) == (False, False) and not is_running(fake_agent, inst)
    assert not Job.objects.exists() and not fake_agent.called("list_containers")  # decided without the agent
    event = not_resumed(inst)
    assert event.actor is None and event.details["reason"] == "not_meant_to_run" and event.details["stopped_at"]
    # and it stays that way
    assert resume(django_capture_on_commit_callbacks) == {"started": None, "cleared": [], "waiting": 0}


def test_crash_loop_stop_settles_the_mark(marked, fake_agent, after_boot, django_capture_on_commit_callbacks):
    """Docker restarted the server after boot and it crash-looped: the panel stopped it for good."""
    inst = marked("Alpha")
    fake_agent.set_container_state(inst.id, running=True, status="running", health="starting")
    reconcile._stop_crash_loop(inst, 5)
    assert flags(inst) == (False, False)
    assert inst.desired_state == "stopped" and not is_running(fake_agent, inst)
    assert resume(django_capture_on_commit_callbacks) == {"started": None, "cleared": [], "waiting": 0}
    assert not Job.objects.filter(kind="start").exists() and not is_running(fake_agent, inst)


def test_a_crash_loop_stop_that_did_not_work_keeps_the_mark(marked, fake_agent):
    inst = marked("Alpha")
    fake_agent.failures["stop"] = AgentUnavailable("Cannot reach the node agent")
    reconcile._stop_crash_loop(inst, 5)
    assert flags(inst) == (True, True) and inst.desired_state == "running"  # nothing was decided


def test_failed_restore_is_not_followed_by_an_automatic_start(marked, fake_agent, after_boot, django_capture_on_commit_callbacks):
    """The restore failed half-way: its files may be incomplete, and the server was left stopped."""
    inst = marked("Alpha")
    backup = Backup.objects.create(instance=inst, name="b", kind="manual", status=BackupStatus.SUCCEEDED, snapshot_id="abc123")
    fake_agent.failed_operations["restore"] = ("restic: unexpected EOF", "restic_failed")
    with django_capture_on_commit_callbacks(execute=True):
        job = runner.enqueue(
            "restore", title="Restore", instance=inst, params={"backup_id": str(backup.id), "pre_restore_backup": False}
        )
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.error_code == "restic_failed"
    assert flags(inst) == (True, True) and inst.desired_state == "running"  # the job itself decided nothing
    result = resume(django_capture_on_commit_callbacks)
    assert result == {"started": None, "cleared": [str(inst.id)], "waiting": 0}
    assert flags(inst) == (False, False) and not is_running(fake_agent, inst)
    assert not Job.objects.filter(kind="start").exists()
    details = not_resumed(inst).details
    assert (details["reason"], details["job"], details["kind"], details["error_code"]) == (
        "job_failed",
        str(job.id),
        "restore",
        "restic_failed",
    )
    # the status no longer promises an automatic start
    inst.refresh_from_db()
    assert "started again automatically" not in state.compute(inst, fake_agent.status(inst.id), None)[1]


def finished_job(inst, kind, status, *, minutes_ago: float, exclusive: bool = True, error_code: str = "") -> Job:
    job = Job.objects.create(kind=kind, title=kind, instance=inst, status=status, exclusive=exclusive, error_code=error_code)
    Job.objects.filter(pk=job.pk).update(finished_at=timezone.now() - timedelta(minutes=minutes_ago))
    return job


@pytest.mark.parametrize(
    ("kind", "status", "error_code"),
    [
        ("restore", JobStatus.FAILED, "restic_failed"),
        ("world_reset", JobStatus.FAILED, "abandoned"),  # marked as failed by a user
        ("software_update", JobStatus.FAILED, "interrupted"),  # the worker died half-way
        ("modpack_update", JobStatus.CANCELLED, ""),
        ("some_future_kind", JobStatus.FAILED, "internal_error"),  # unknown kinds count: the safe side
    ],
)
def test_a_job_that_did_not_succeed_after_the_mark_keeps_the_server_down(
    kind, status, error_code, marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    inst = marked("Alpha", minutes_ago=5)
    job = finished_job(inst, kind, status, minutes_ago=2, error_code=error_code)
    result = resume(django_capture_on_commit_callbacks)
    assert result["started"] is None and result["cleared"] == [str(inst.id)]
    assert flags(inst) == (False, False) and not is_running(fake_agent, inst)
    assert not Job.objects.filter(kind="start").exists()
    details = not_resumed(inst).details
    assert (details["reason"], details["job"], details["status"]) == ("job_failed", str(job.id), status)


@pytest.mark.parametrize(
    ("kind", "status", "minutes_ago", "exclusive"),
    [
        ("restore", JobStatus.SUCCEEDED, 2, True),  # it worked: the server is fine
        ("restore", JobStatus.FAILED, 9, True),  # before the mark: the server ran again after it
        ("backup", JobStatus.FAILED, 2, True),  # only reads the server's files
        ("snapshot_create", JobStatus.FAILED, 2, True),
        ("restart", JobStatus.FAILED, 2, True),  # interrupted by the shutdown itself: it was meant to run
        ("recreate", JobStatus.FAILED, 2, True),  # only the container; the start makes it again
        ("stop", JobStatus.CANCELLED, 2, False),  # a countdown that was cancelled never took the server
    ],
)
def test_other_jobs_do_not_keep_a_marked_server_down(
    kind, status, minutes_ago, exclusive, marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    inst = marked("Alpha", minutes_ago=5)
    finished_job(inst, kind, status, minutes_ago=minutes_ago, exclusive=exclusive)
    assert resume(django_capture_on_commit_callbacks)["started"] == str(inst.id)
    assert is_running(fake_agent, inst) and not AuditEvent.objects.filter(action=NOT_RESUMED_AUDIT).exists()


@pytest.mark.parametrize("kind", ["stop", "restore", "world_reset", "software_update"])
def test_a_job_that_was_never_picked_up_does_not_keep_a_marked_server_down(
    kind, marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    """The host was down for hours. A job that was still queued when it went down is failed by
    the watchdog as never started: it did nothing to the server, which was running and is
    started again like every other marked server."""
    inst = marked("Alpha", minutes_ago=200)
    job = Job.objects.create(kind=kind, title=kind, instance=inst, exclusive=True)  # QUEUED; its message is gone
    Job.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=3))
    Instance.objects.filter(pk=inst.pk).update(active_job=job)
    assert job_tasks.watchdog() == 1
    job.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "not_started") and job.finished_at > inst.resume_flagged_at
    assert resume(django_capture_on_commit_callbacks)["started"] == str(inst.id)
    assert is_running(fake_agent, inst) and not AuditEvent.objects.filter(action=NOT_RESUMED_AUDIT).exists()


def test_a_job_no_worker_knew_does_not_keep_a_marked_server_down(
    marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    inst = marked("Alpha", minutes_ago=5)
    finished_job(inst, "restore", JobStatus.FAILED, minutes_ago=2, error_code="unknown_job_kind")
    assert resume(django_capture_on_commit_callbacks)["started"] == str(inst.id)


def test_a_failed_job_on_one_server_does_not_touch_the_next(marked, fake_agent, after_boot, django_capture_on_commit_callbacks):
    broken, fine = marked("Alpha"), marked("Bravo")
    finished_job(broken, "restore", JobStatus.FAILED, minutes_ago=1)
    result = resume(django_capture_on_commit_callbacks)
    assert result == {"started": str(fine.id), "cleared": [str(broken.id)], "waiting": 0}
    assert is_running(fake_agent, fine) and not is_running(fake_agent, broken)


def test_no_start_when_the_mark_was_settled_between_the_read_and_the_start(
    marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    """A kill (or a stop) lands while the resume step is asking the agent: its decision wins."""
    inst = marked("Alpha")

    def kill_meanwhile() -> None:
        Instance.objects.filter(pk=inst.pk).update(desired_state="stopped", resume_after_boot=False, resume_flagged_at=None)

    fake_agent.hooks["list_containers"] = kill_meanwhile
    result = resume(django_capture_on_commit_callbacks)
    assert result == {"started": None, "cleared": [], "waiting": 0}
    assert not Job.objects.exists() and not fake_agent.called("start") and not is_running(fake_agent, inst)
    assert not AuditEvent.objects.filter(action=RESUME_AUDIT).exists()
    assert flags(inst) == (False, False) and inst.desired_state == "stopped"


# --- a mark that waits although Docker was never stopped ---------------------------------------------
def test_a_mark_that_waits_in_the_same_panel_runtime_is_given_up_after_ten_minutes(
    marked, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    """host_shutdown was run by hand and Docker was not stopped: the panel that would resume the
    servers is this one, which never does. The status must not promise a start for ever."""
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(days=3))
    recent, old = marked("Alpha", minutes_ago=9), marked("Bravo", minutes_ago=11)
    assert "started again automatically" in state.compute(old, fake_agent.status(old.id), None)[1]
    result = resume(django_capture_on_commit_callbacks)
    assert result == {"started": None, "cleared": [str(old.id)], "waiting": 1}
    assert flags(recent) == (True, True)  # a real shutdown may still be under way
    assert flags(old) == (False, False) and old.desired_state == "running"
    assert not Job.objects.exists() and not is_running(fake_agent, old)
    assert not fake_agent.called("list_containers")
    details = not_resumed(old).details
    assert details["reason"] == "host_not_restarted" and details["waited_seconds"] >= 660
    # the usual text for a server that is down although it is meant to run
    assert state.compute(old, fake_agent.status(old.id), None) == (Status.STOPPED, "Server stopped itself")
    assert scheduling.RESUME_STALE_AFTER == timedelta(minutes=10)


def test_a_server_that_was_not_started_again_says_that_it_was_stopped_at_host_shutdown(
    server, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    """Not "Server stopped itself": the shutdown step stopped it, and the mark was given up."""
    inst = server("Alpha", running=True)
    shutdown()  # stops it, marks it and writes the audit entry
    reconcile.reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert inst.status_detail == "Stopped because the host was shutting down; it is started again automatically"
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(days=3))
    flag(inst, minutes_ago=11)  # Docker was never stopped: the mark is given up
    assert resume(django_capture_on_commit_callbacks)["cleared"] == [str(inst.id)]
    reconcile.reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert (inst.status, inst.status_detail) == (Status.STOPPED, state.NOT_RESUMED_DETAIL)
    assert "host shutdown" in inst.status_detail and "not started again automatically" in inst.status_detail

    # started by hand, and later it stops by itself (an in-game /stop): that is what it says then
    assert run_job("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    fake_agent.set_container_state(
        inst.id, status="exited", running=False, exit_code=0, started_at=fake_agent._now(), finished_at=fake_agent._now()
    )
    reconcile.reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert (inst.status, inst.status_detail) == (Status.STOPPED, state.STOPPED_ITSELF_DETAIL)


def test_an_old_mark_is_still_resumed_by_the_panel_that_starts_after_it(
    marked, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    """The host was off for days: that is no stale mark, the panel started after it was written."""
    inst = marked("Alpha", minutes_ago=3 * 24 * 60)
    assert resume(django_capture_on_commit_callbacks)["started"] == str(inst.id)
    assert is_running(fake_agent, inst)


# --- what host_shutdown records about servers the panel thought were stopped ------------------------
def test_a_server_that_ran_without_the_panel_noting_it_comes_back(
    server, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    """Started by an update with "start afterwards" or outside the panel: desired_state said "stopped"."""
    inst = server("Alpha", running=True, desired_state="stopped")
    shutdown()
    assert flags(inst) == (True, True) and inst.desired_state == "running"
    flag(inst)
    assert resume(django_capture_on_commit_callbacks)["started"] == str(inst.id)
    assert is_running(fake_agent, inst)


def test_a_stop_that_was_still_in_progress_is_not_undone_after_boot(
    server, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    """The user pressed Stop and shut the host down while the server was still stopping."""
    inst = server("Alpha", running=True, desired_state="stopped")  # the stop job has recorded its decision
    job = Job.objects.create(
        kind="stop", title="Stop server", instance=inst, status=JobStatus.RUNNING, heartbeat_at=timezone.now()
    )
    Instance.objects.filter(pk=inst.pk).update(active_job=job)
    shutdown()
    assert flags(inst) == (True, True) and inst.desired_state == "stopped"
    flag(inst)
    runner.finish(job, JobStatus.FAILED, error=runner.INTERRUPTED_ERROR, error_code="interrupted")  # the watchdog after boot
    result = resume(django_capture_on_commit_callbacks)
    assert result == {"started": None, "cleared": [str(inst.id)], "waiting": 0}
    assert not is_running(fake_agent, inst) and not Job.objects.filter(kind="start").exists()
    assert not_resumed(inst).details["reason"] == "not_meant_to_run"
