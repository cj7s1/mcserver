"""How jobs end (audit 2026-11-09): finish() only ends an active job, an abandoned or cancelled job
stops waiting for the agent within seconds, a stopping worker ends its waits at once, and a job
that was cut off after it stopped a server says so and records the server as stopped."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.instances.models import Instance
from apps.instances.spec import build_spec, current_spec_hash
from apps.jobs import runner, shutdown
from apps.jobs import tasks as job_tasks
from apps.jobs.models import Job, JobStatus
from apps.nodes import client as client_module

pytestmark = pytest.mark.django_db


class Clock:
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
    c = Clock()
    for module in (client_module, runner):
        monkeypatch.setattr(module, "monotonic", c.monotonic)
        monkeypatch.setattr(module, "sleep", c.sleep)
    return c


@pytest.fixture(autouse=True)
def no_stop_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPANEL_WORKER_FLAG_DIR", str(tmp_path))
    monkeypatch.delenv("MCPANEL_SUPERVISOR_STATUS", raising=False)
    shutdown.clear()
    yield
    shutdown.clear()


@pytest.fixture
def server(make_instance, fake_agent):
    inst = make_instance(name="Survival", desired_state="running")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.put_file(inst.id, "server.properties", "motd=x\n")
    Instance.objects.filter(pk=inst.pk).update(
        container_spec_hash=current_spec_hash(inst), installed_at=timezone.now(), status="running", container_state="running"
    )
    inst.refresh_from_db()
    return inst


def keep_running(fake_agent, kind: str) -> None:
    """Agent operations of ``kind`` stay "running" (the panel keeps polling them)."""
    real = fake_agent._op

    def op(k, iid, result=None, *, error=None):
        made = real(k, iid, result, error=error)
        if k == kind and not error:
            fake_agent.operations[made["id"]].update(status="running", finished_at=None)
            made.update(status="running")
        return made

    fake_agent._op = op


# --- backend-jobs#6 / backend-backups#8: finish() and abandoned jobs -------------------------------------
def test_a_handler_that_returns_after_its_job_was_abandoned_does_not_turn_it_succeeded(server, monkeypatch):
    job = runner.enqueue("backup", title="Backup", instance=server)

    def handler(job_, ctx):
        Job.objects.filter(pk=job_.pk).update(status=JobStatus.FAILED, error_code="abandoned")  # "Mark as failed"
        return {"done": True}

    monkeypatch.setitem(runner.HANDLERS, "backup", handler)
    runner.run_job(str(job.id))
    job.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert job.result.get("done") is None


def test_finish_does_not_overwrite_an_ended_job(server):
    job = runner.enqueue("backup", title="Backup", instance=server)
    stale = Job.objects.get(pk=job.pk)
    assert runner.finish(job, JobStatus.SUCCEEDED) is True
    assert runner.finish(stale, JobStatus.FAILED, error="Marked as failed by x", error_code="abandoned") is False
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED and job.error == ""


def test_abandoning_a_job_that_just_finished_is_refused(server, clients, monkeypatch):
    job = runner.enqueue("backup", title="Backup", instance=server)
    real = runner.finish

    def finished_meanwhile(j, *a, **kw):
        Job.objects.filter(pk=j.pk).update(status=JobStatus.SUCCEEDED)
        return real(j, *a, **kw)

    monkeypatch.setattr(runner, "finish", finished_meanwhile)
    resp = clients["owner"].post(f"/api/jobs/{job.id}/abandon")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_active"
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED


def test_an_abandoned_restore_stops_waiting_and_never_replaces_the_files(
    server, fake_agent, clock, django_capture_on_commit_callbacks
):
    fake_agent.put_file(server.id, "world/level.dat", b"old")
    with django_capture_on_commit_callbacks(execute=True):
        backup_job = runner.enqueue("backup", title="Backup", instance=server)
    from apps.backups.models import Backup

    backup = Backup.objects.get(job=backup_job)
    fake_agent.put_file(server.id, "world/level.dat", b"new")
    keep_running(fake_agent, "backup")  # the safety backup takes long
    polls = {"n": 0}

    def abandon_on_third_poll(op_id):
        polls["n"] += 1
        if polls["n"] == 3:
            Job.objects.filter(kind="restore").update(status=JobStatus.FAILED, error_code="abandoned")

    fake_agent.hooks["operation"] = abandon_on_third_poll
    with django_capture_on_commit_callbacks(execute=True):
        job = runner.enqueue("restore", title="Restore", instance=server, params={"backup_id": str(backup.id)})
    job.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert not fake_agent.called("restore_backup")
    assert fake_agent.get_file(server.id, "world/level.dat") == b"new"
    assert clock.now - 1000.0 < 10  # seconds, not the 6 h timeout of the wait


# --- scenario-boot-shutdown#6: a stopping worker -------------------------------------------------------------
def test_a_stopping_worker_ends_the_wait_and_fails_the_job_as_interrupted(
    server, fake_agent, clock, django_capture_on_commit_callbacks
):
    keep_running(fake_agent, "backup")
    fake_agent.hooks["operation"] = lambda op_id: shutdown.request_stop()
    with django_capture_on_commit_callbacks(execute=True):
        job = runner.enqueue("backup", title="Backup", instance=server)
    job.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    assert "being stopped" in job.error
    assert clock.now - 1000.0 < 5
    server.refresh_from_db()
    assert server.active_job_id is None


def test_a_countdown_ends_at_once_when_the_worker_stops(server, fake_agent, clock, django_capture_on_commit_callbacks):
    fake_agent.command_handler = lambda iid, cmd: "There are 2 of a max of 20 players online: a, b" if cmd == "list" else None
    fake_agent.hooks["command"] = lambda *a, **k: shutdown.request_stop() if a[1].startswith("tellraw") else None
    from apps.instances import services

    with django_capture_on_commit_callbacks(execute=True):
        job = services.enqueue_power(server, "restart", warn_minutes=15)
    job.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    assert clock.now - 1000.0 < 5  # not the 15 minutes of the countdown
    assert not fake_agent.called("restart")


def test_the_flag_of_the_parent_process_counts_only_while_the_supervisor_stops(tmp_path, monkeypatch):
    import json
    import os

    status = tmp_path / "supervisor.json"
    status.write_text(json.dumps({"phase": "running"}))
    monkeypatch.setenv("MCPANEL_SUPERVISOR_STATUS", str(status))
    (tmp_path / f"{shutdown.FLAG_PREFIX}{os.getppid()}").write_text("")
    # a SIGTERM to restart the worker after Redis came back: the job goes on
    assert shutdown.stop_requested() is False
    status.write_text(json.dumps({"phase": "stopping"}))
    assert shutdown.stop_requested() is True
    shutdown.clear()
    (tmp_path / f"{shutdown.FLAG_PREFIX}{os.getppid()}").unlink()
    assert shutdown.stop_requested() is False


def test_a_new_worker_process_clears_an_old_flag_of_its_pid(tmp_path):
    import os

    flag = tmp_path / f"{shutdown.FLAG_PREFIX}{os.getpid()}"
    shutdown.request_stop()
    assert flag.exists()
    shutdown.clear()
    assert not flag.exists() and shutdown.stop_requested() is False


# --- backend-jobs#3 / scenario-boot-shutdown#7: a job cut off after it stopped the server -----------------
def test_the_watchdog_says_that_an_interrupted_job_left_the_server_stopped(server, fake_agent):
    job = runner.enqueue("restore", title="Restore", instance=server, params={"backup_id": "x"})
    fake_agent.stop(server.id)
    Instance.objects.filter(pk=server.pk).update(container_state="exited", status="stopped")
    Job.objects.filter(pk=job.pk).update(
        status=JobStatus.RUNNING,
        started_at=timezone.now() - timedelta(hours=1),
        heartbeat_at=timezone.now() - timedelta(hours=1),
        result={"stopped_server": True},
    )
    job_tasks.watchdog()
    job.refresh_from_db()
    server.refresh_from_db()
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    assert "the server was left stopped" in job.error
    assert server.desired_state == "stopped"


def test_ensure_stopped_remembers_it_in_the_job(server, fake_agent, monkeypatch):
    from apps.instances.handlers import ensure_stopped

    job = runner.enqueue("restore", title="Restore", instance=server, params={})
    Job.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING)
    ctx = runner.JobContext(Job.objects.get(pk=job.pk))
    assert ensure_stopped(server, ctx) is True
    assert Job.objects.get(pk=job.pk).result == {"stopped_server": True}
