"""In-game warnings before restarts/stops (apps/instances/power_warnings.py) with the fake agent.

Long waits run on a fake clock (``runner.monotonic``/``runner.sleep``), so a 5-minute
countdown finishes instantly while the handler still sees the time pass.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.instances import power_warnings as pw
from apps.instances.models import Instance, Schedule
from apps.instances.scheduling import run_due_schedules
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus

pytestmark = pytest.mark.django_db


# --- fixtures --------------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.t = 10_000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(runner, "monotonic", c.monotonic)
    monkeypatch.setattr(runner, "sleep", c.sleep)
    return c


@pytest.fixture
def owner(client_for, users):
    return client_for(users["owner"])


@pytest.fixture
def server(make_instance, fake_agent):
    """A running server (container up, status running) with two players online."""
    inst = make_instance(name="Warned")
    fake_agent.prepare(inst.id, {"server.properties": "motd=x\n"})
    fake_agent.apply(inst.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running")
    inst.refresh_from_db()
    return inst


class Players:
    """command_handler for the fake agent: answers `list` and records tellraw/say timing."""

    def __init__(self, fake_agent, instance, clock, online: int = 2) -> None:
        self.fake_agent = fake_agent
        self.instance = instance
        self.clock = clock
        self.online = online
        self.events: list[tuple[float, str, str | None]] = []  # (time, command, active_job_id)
        self.on_message = None  # optional callable(command)
        fake_agent.command_handler = self

    def set_online(self, n: int) -> None:
        self.online = n
        cache.delete(f"players:online:{self.instance.pk}")  # online_players caches briefly

    def __call__(self, _iid: str, command: str) -> str | None:
        if command == "list":
            names = ", ".join(f"P{i}" for i in range(self.online))
            return f"There are {self.online} of a max of 20 players online: {names}"
        lock = Instance.objects.filter(pk=self.instance.pk).values_list("active_job_id", flat=True).first()
        self.events.append((self.clock.t, command, str(lock) if lock else None))
        if self.on_message:
            self.on_message(command)
        return ""

    def messages(self) -> list[str]:
        out = []
        for _t, cmd, _lock in self.events:
            assert cmd.startswith("tellraw @a "), cmd
            component = json.loads(cmd[len("tellraw @a ") :])
            out.append("".join(part["text"] for part in component["extra"]))
        return out


def _power(client, capture, inst, **body):
    with capture(execute=True):
        return client.post(f"/api/instances/{inst.id}/power", body, format="json")


# --- pure helpers -------------------------------------------------------------------------------
def test_marks_and_texts():
    assert pw.warning_marks(0) == []
    assert pw.warning_marks(1) == [60, 10]
    assert pw.warning_marks(5) == [300, 60, 10]
    assert pw.warning_marks(15) == [900, 600, 300, 60, 10]
    assert pw.describe_remaining(60) == "1 minute"
    assert pw.describe_remaining(600) == "10 minutes"
    assert pw.describe_remaining(10) == "10 seconds"
    assert pw.warning_text("restart", 300) == "The server is restarting in 5 minutes."
    assert pw.warning_text("stop", 10) == "The server is shutting down in 10 seconds."


@pytest.mark.parametrize("value,expected", [(None, 0), ("", 0), (0, 0), (5, 5), ("10", 10), (15, 15)])
def test_validate_warn_minutes_ok(value, expected):
    assert pw.validate_warn_minutes(value) == expected


@pytest.mark.parametrize("value", [7, -1, 60, "abc", True, 5.5, [5], {"m": 5}])
def test_validate_warn_minutes_rejects(value):
    with pytest.raises(ValueError):
        pw.validate_warn_minutes(value)


@pytest.mark.parametrize("bad", ["two\nlines", "tab\there", "§cred", "x" * 101, 42, "\x00nul"])
def test_validate_message_rejects(bad):
    with pytest.raises(ValueError):
        pw.validate_message(bad)


def test_tellraw_keeps_operator_text_inside_a_json_string():
    hostile = 'Bye"},{"text":"x","clickEvent":{"action":"run_command","value":"/op Evil"}} @a \\'
    cmd = pw.tellraw_command("The server is restarting in 1 minute.", hostile)
    assert cmd.startswith("tellraw @a {") and "\n" not in cmd
    component = json.loads(cmd[len("tellraw @a ") :])
    assert set(component) == {"text", "extra"}
    assert all(set(part) == {"text", "color"} for part in component["extra"])
    assert component["extra"][-1]["text"] == f" {hostile}"
    # stored params are normalised again at use time (formatting codes and control chars dropped)
    assert pw.clean_message("§4Red\nline\x07 ok") == "Redline ok"


# --- the countdown --------------------------------------------------------------------------------
def test_restart_with_warning_warns_then_restarts_without_holding_the_lock(
    server, owner, fake_agent, clock, django_capture_on_commit_callbacks
):
    players = Players(fake_agent, server, clock)
    r = _power(owner, django_capture_on_commit_callbacks, server, action="restart", warn_minutes=5, warn_message="Daily")
    assert r.status_code == 202, r.json()
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.params == {"warn_minutes": 5, "warn_message": "Daily"}
    assert "5 min warning" in job.title
    assert players.messages() == [
        "[Server] The server is restarting in 5 minutes. Daily",
        "[Server] The server is restarting in 1 minute. Daily",
        "[Server] The server is restarting in 10 seconds. Daily",
    ]
    times = [t for t, _c, _l in players.events]
    assert times[1] - times[0] == pytest.approx(240) and times[2] - times[1] == pytest.approx(50)
    locks = [lock for _t, _c, lock in players.events]
    assert locks[0] is None and locks[1] is None  # other operations may run during the warnings
    assert locks[2] == str(job.id)  # the last seconds and the action hold the lock
    assert clock.slept == pytest.approx(300)
    assert len(fake_agent.called("restart")) == 1
    server.refresh_from_db()
    assert server.active_job_id is None
    ev = AuditEvent.objects.get(action="instance.restart")
    assert ev.details["warn_minutes"] == 5


def test_stop_with_warning(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    players = Players(fake_agent, server, clock)
    r = _power(owner, django_capture_on_commit_callbacks, server, action="stop", warn_minutes=1)
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert players.messages()[0] == "[Server] The server is shutting down in 1 minute."
    assert not fake_agent.containers[str(server.id)]["running"]


def test_no_players_means_no_warning(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    players = Players(fake_agent, server, clock, online=0)
    r = _power(owner, django_capture_on_commit_callbacks, server, action="restart", warn_minutes=10)
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED
    assert players.events == [] and clock.slept == 0
    assert any("No players online" in line for line in job.log)
    assert len(fake_agent.called("restart")) == 1


def test_unreachable_console_skips_warning(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    from apps.nodes.client import AgentError

    def broken(_iid, command):
        raise AgentError("RCON refused", "rcon_error", 502)

    fake_agent.command_handler = broken
    r = _power(owner, django_capture_on_commit_callbacks, server, action="restart", warn_minutes=5)
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED and clock.slept == 0
    assert any("RCON" in line for line in job.log)


def test_stopped_server_gets_a_plain_job(make_instance, fake_agent, owner, clock, django_capture_on_commit_callbacks):
    inst = make_instance(name="Off")
    fake_agent.prepare(inst.id)
    r = _power(owner, django_capture_on_commit_callbacks, inst, action="restart", warn_minutes=5)
    job = Job.objects.get(pk=r.json()["id"])
    assert job.exclusive and job.params == {} and "warning" not in job.title
    assert clock.slept == 0


def test_everyone_leaving_ends_the_countdown_early(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    players = Players(fake_agent, server, clock)
    players.on_message = lambda cmd: players.set_online(0)
    r = _power(owner, django_capture_on_commit_callbacks, server, action="restart", warn_minutes=10)
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED
    assert len(players.events) == 1  # only the 10-minute warning
    assert clock.slept == pytest.approx(300)  # woke at T-5 min, nobody left
    assert any("All players left" in line for line in job.log)
    assert len(fake_agent.called("restart")) == 1


def test_server_stopping_during_countdown_skips_the_restart(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    players = Players(fake_agent, server, clock)
    players.on_message = lambda cmd: fake_agent.stop(server.id)
    r = _power(owner, django_capture_on_commit_callbacks, server, action="restart", warn_minutes=5)
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED
    assert "restart was skipped" in job.result["skipped"]
    assert fake_agent.called("restart") == []
    server.refresh_from_db()
    assert server.active_job_id is None


def test_cancel_during_countdown_tells_players(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    players = Players(fake_agent, server, clock)
    state = {}

    def cancel_once(cmd):
        if not state:
            job = Job.objects.get(instance=server, kind="restart")
            info = pw.countdown_info(Instance.objects.get(pk=server.pk))
            state["info"] = info
            pw.cancel_countdown(job.id, message="Cancelled by owner")

    players.on_message = cancel_once
    r = _power(owner, django_capture_on_commit_callbacks, server, action="restart", warn_minutes=5)
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.CANCELLED
    assert state["info"]["phase"] == "warning" and state["info"]["action"] == "restart"
    assert state["info"]["action_at"]
    assert players.messages() == [
        "[Server] The server is restarting in 5 minutes.",
        "[Server] The server restart was cancelled.",
    ]
    assert clock.slept <= pw.FINAL_WARNING  # noticed within one poll interval
    assert fake_agent.called("restart") == []
    server.refresh_from_db()
    assert server.active_job_id is None


def test_abandoned_countdown_stops_and_keeps_failed_status(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    players = Players(fake_agent, server, clock)

    def abandon_once(cmd):
        job = Job.objects.get(instance=server, kind="stop")
        if job.status == JobStatus.RUNNING:
            runner.finish(job, JobStatus.FAILED, error="Marked as failed by owner", error_code="abandoned")

    players.on_message = abandon_once
    r = _power(owner, django_capture_on_commit_callbacks, server, action="stop", warn_minutes=1)
    job = Job.objects.get(pk=r.json()["id"])
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")  # not overwritten
    assert players.messages()[-1] == "[Server] The server shutdown was cancelled."
    assert fake_agent.containers[str(server.id)]["running"]


def test_final_phase_waits_for_the_lock_then_gives_up(server, owner, fake_agent, clock, django_capture_on_commit_callbacks):
    players = Players(fake_agent, server, clock)

    def start_backup(cmd):
        if "1 minute" in cmd:  # another exclusive operation starts during the warnings
            other = Job.objects.create(kind="backup", title="Nightly backup", instance=server, status=JobStatus.RUNNING)
            Instance.objects.filter(pk=server.pk).update(active_job=other)

    players.on_message = start_backup
    r = _power(owner, django_capture_on_commit_callbacks, server, action="restart", warn_minutes=1)
    job = Job.objects.get(pk=r.json()["id"])
    assert (job.status, job.error_code) == (JobStatus.FAILED, "instance_busy")
    assert "Nightly backup" in job.error
    assert clock.slept == pytest.approx(50 + pw.LOCK_WAIT_SECONDS)
    assert any("Waiting for 'Nightly backup'" in line for line in job.log)
    assert players.messages()[-1] == "[Server] The server restart was cancelled."
    assert fake_agent.called("restart") == []


def test_claim_lock_waits_for_other_job_to_finish(server, clock, monkeypatch):
    other = Job.objects.create(kind="backup", title="Backup", instance=server, status=JobStatus.RUNNING)
    Instance.objects.filter(pk=server.pk).update(active_job=other)
    job = Job.objects.create(kind="restart", title="r", instance=server, status=JobStatus.RUNNING, exclusive=False)

    def finishing_sleep(seconds):
        clock.sleep(seconds)
        if clock.slept >= 30:
            Job.objects.filter(pk=other.pk).update(status=JobStatus.SUCCEEDED)

    monkeypatch.setattr(runner, "sleep", finishing_sleep)
    runner.claim_lock(job, runner.JobContext(job), wait=600)
    server.refresh_from_db()
    job.refresh_from_db()
    assert server.active_job_id == job.id and job.exclusive
    assert 30 <= clock.slept < 40


# --- API: cancel, supersede, dedupe, validation, serializer ------------------------------------------
def _queue_countdown(server, minutes=5):
    """A countdown job as the API would queue it, without running the worker."""
    from apps.instances.services import enqueue_power

    return enqueue_power(server, "restart", warn_minutes=minutes, warn_message="")


def test_countdown_is_listed_and_cancellable(server, clients, fake_agent):
    job = _queue_countdown(server)
    assert job.exclusive is False
    server.refresh_from_db()
    assert server.active_job_id is None
    detail = clients["owner"].get(f"/api/instances/{server.id}").json()
    assert detail["power_countdown"]["job_id"] == str(job.id)
    assert detail["power_countdown"]["phase"] == "queued" and detail["power_countdown"]["warn_minutes"] == 5
    listed = next(i for i in clients["owner"].get("/api/instances/").json() if i["id"] == str(server.id))
    assert listed["power_countdown"]["job_id"] == str(job.id)

    assert clients["viewer"].post(f"/api/instances/{server.id}/power/cancel").status_code == 403
    r = clients["owner"].post(f"/api/instances/{server.id}/power/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert AuditEvent.objects.filter(action="instance.power_cancelled").exists()
    assert clients["owner"].get(f"/api/instances/{server.id}").json()["power_countdown"] is None
    r = clients["owner"].post(f"/api/instances/{server.id}/power/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_countdown"
    runner.run_job(str(job.id))  # a late worker pickup does nothing
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED


def test_acting_countdown_cannot_be_cancelled(server):
    job = _queue_countdown(server)
    Job.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING, result={"countdown": {"phase": "acting"}})
    with pytest.raises(Exception) as exc:
        pw.cancel_countdown(job.id, message="x")
    assert exc.value.error_code == "not_cancellable"


def test_second_countdown_is_refused_and_immediate_action_supersedes(server, clients, django_capture_on_commit_callbacks):
    pending = _queue_countdown(server)
    r = clients["owner"].post(f"/api/instances/{server.id}/power", {"action": "stop", "warn_minutes": 1}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "countdown_pending"
    r = clients["owner"].post(f"/api/instances/{server.id}/power", {"action": "restart"}, format="json")
    assert r.status_code == 202
    pending.refresh_from_db()
    assert pending.status == JobStatus.CANCELLED
    assert pending.result["countdown"]["notify_players"] is False  # the server restarts anyway
    assert "Superseded" in pending.message


def test_countdown_refused_while_another_operation_runs(server, clients):
    other = Job.objects.create(kind="backup", title="Backup", instance=server, status=JobStatus.RUNNING)
    Instance.objects.filter(pk=server.pk).update(active_job=other)
    r = clients["owner"].post(f"/api/instances/{server.id}/power", {"action": "restart", "warn_minutes": 5}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "instance_busy"


@pytest.mark.parametrize(
    "body,field",
    [
        ({"action": "restart", "warn_minutes": 7}, "warn_minutes"),
        ({"action": "start", "warn_minutes": 5}, "warn_minutes"),
        ({"action": "kill", "warn_minutes": 1}, "warn_minutes"),
        ({"action": "restart", "warn_minutes": 5, "warn_message": "a\nb"}, "warn_message"),
        ({"action": "restart", "warn_minutes": 5, "warn_message": "§kobfuscated"}, "warn_message"),
        ({"action": "restart", "warn_minutes": 5, "warn_message": "x" * 101}, "warn_message"),
    ],
)
def test_power_validation(server, clients, body, field):
    r = clients["owner"].post(f"/api/instances/{server.id}/power", body, format="json")
    assert r.status_code == 400, r.json()
    assert field in json.dumps(r.json())


# --- schedules --------------------------------------------------------------------------------------
def test_schedule_payload_is_validated_and_normalised(server, clients):
    url = f"/api/instances/{server.id}/schedules"
    base = {"name": "Nightly", "action": "restart", "cron": "0 4 * * *"}
    r = clients["owner"].post(url, {**base, "payload": {"warn_minutes": 5, "warn_message": " Daily restart "}}, format="json")
    assert r.status_code == 201, r.json()
    assert r.json()["payload"] == {"warn_minutes": 5, "warn_message": "Daily restart"}
    r = clients["owner"].post(url, {**base, "payload": {}}, format="json")
    assert r.json()["payload"] == {"warn_minutes": 0}
    for bad in ({"warn_minutes": 3}, {"warn_minutes": 5, "warn_message": "line\nbreak"}):
        r = clients["owner"].post(url, {**base, "payload": bad}, format="json")
        assert r.status_code == 400, bad


def test_scheduled_restart_queues_a_countdown(server, fake_agent):
    sched = Schedule.objects.create(
        instance=server,
        name="Nightly",
        action="restart",
        cron="0 4 * * *",
        payload={"warn_minutes": 10, "warn_message": "Nightly restart"},
        next_run_at=timezone.now() - timedelta(minutes=1),
    )
    assert run_due_schedules(timezone.now()) == 1
    sched.refresh_from_db()
    assert "players warned 10 min before" in sched.last_result
    job = Job.objects.get(instance=server, kind="restart")
    assert job.exclusive is False and job.params == {"warn_minutes": 10, "warn_message": "Nightly restart"}
    assert job.title == "Scheduled restart: Nightly (10 min warning)"
    # the next run while this one is still counting down is skipped, not doubled
    sched.next_run_at = timezone.now() - timedelta(minutes=1)
    sched.save(update_fields=["next_run_at"])
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("Skipped: A countdown is already running")
