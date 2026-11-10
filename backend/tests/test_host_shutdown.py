"""Stopping servers before the host stops Docker (manage.py host_shutdown) and starting them again afterwards."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.instances import scheduling, services, state, tasks
from apps.instances.management.commands import host_shutdown
from apps.instances.models import Instance, Status
from apps.instances.spec import build_spec, current_spec_hash
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentError, AgentUnavailable
from apps.nodes.models import Node

pytestmark = pytest.mark.django_db

STOP_AUDIT = "instance.stopped_for_host_shutdown"
RESUME_AUDIT = "instance.resumed_after_boot"


@pytest.fixture
def server(make_instance, fake_agent):
    """Create a server with a container: ``server("A", running=True)``."""

    def _make(name: str, *, running: bool, **fields):
        inst = make_instance(name=name, desired_state="running" if running else "stopped", **fields)
        fake_agent.apply(inst.id, build_spec(inst), start=running)
        Instance.objects.filter(pk=inst.pk).update(container_spec_hash=current_spec_hash(inst), installed_at=timezone.now())
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
    """The hook then stops the containers with docker stop; the marks bring them back after boot."""
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
    assert f"FAILED    {busy.name}: Instance is busy with a 'backup' operation" in out
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


def test_a_mark_dated_in_the_future_does_not_block_forever(server, fake_agent, monkeypatch, django_capture_on_commit_callbacks):
    inst = server("Alpha", running=False)
    Instance.objects.filter(pk=inst.pk).update(resume_after_boot=True, resume_flagged_at=timezone.now() + timedelta(days=2))
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(days=30))
    with django_capture_on_commit_callbacks(execute=True):
        assert scheduling.resume_flagged_servers()["started"] == str(inst.id)
    assert is_running(fake_agent, inst)


def test_runtime_started_at_is_a_past_moment():
    started = scheduling.runtime_started_at()
    assert started.tzinfo is not None
    assert started <= timezone.now()


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


def test_nothing_happens_while_the_agent_is_unreachable(server, fake_agent, after_boot, django_capture_on_commit_callbacks):
    a, b = server("Alpha", running=False), server("Bravo", running=False)
    flag(a)
    flag(b)
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
    server, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    busy, free = server("Alpha", running=False), server("Bravo", running=False)
    flag(busy)
    flag(free)
    job = Job.objects.create(kind="backup", title="Backup", instance=busy, status=JobStatus.RUNNING, heartbeat_at=timezone.now())
    Instance.objects.filter(pk=busy.pk).update(active_job=job)
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers()
    assert result["started"] == str(free.id) and result["waiting"] == 1
    assert flags(busy) == (True, True) and not is_running(fake_agent, busy)
    assert flags(free) == (False, False) and is_running(fake_agent, free)
    # once the job is over it gets its turn
    job.status = JobStatus.FAILED
    job.save(update_fields=["status"])
    with django_capture_on_commit_callbacks(execute=True):
        assert scheduling.resume_flagged_servers()["started"] == str(busy.id)
    assert is_running(fake_agent, busy)


def test_resume_recreates_a_container_from_before_this_release(
    server, fake_agent, after_boot, django_capture_on_commit_callbacks
):
    inst = server("Alpha", running=False)
    legacy = hashlib.sha256(json.dumps(build_spec(inst), sort_keys=True).encode()).hexdigest()[:16]
    Instance.objects.filter(pk=inst.pk).update(container_spec_hash=legacy)
    flag(inst)
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
    # without the mark the usual texts apply
    inst.resume_after_boot = False
    assert state.compute(inst, container, None)[0] == Status.FAILED
