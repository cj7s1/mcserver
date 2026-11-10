"""Schedules (audit 2026-11-09): busy servers put a run off instead of dropping it, backups before
snapshots before power actions, impossible cron dates, one bad schedule cannot stop the others,
no late power actions after downtime, missed backups catch up once the server settled, daylight
saving time, and Celery beat in UTC."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.backups.models import BackupKind
from apps.instances import scheduling
from apps.instances.models import Instance, Schedule
from apps.instances.scheduling import next_run, run_due_schedules, validate_cron
from apps.instances.spec import build_spec, current_spec_hash
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus

pytestmark = pytest.mark.django_db
LA = ZoneInfo("America/Los_Angeles")
BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture
def server(make_instance, fake_agent):
    inst = make_instance(name="Survival", desired_state="running")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.put_file(inst.id, "server.properties", "motd=x\n")
    Instance.objects.filter(pk=inst.pk).update(
        container_spec_hash=current_spec_hash(inst),
        installed_at=timezone.now(),
        status="running",
        container_state="running",
        ready_at=timezone.now() - timedelta(hours=1),
    )
    inst.refresh_from_db()
    return inst


@pytest.fixture(autouse=True)
def panel_up_long(monkeypatch):
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(days=1))


def due(inst, name, action, *, minutes_ago: float = 0.2, cron="0 4 * * *", **fields) -> Schedule:
    return Schedule.objects.create(
        instance=inst,
        name=name,
        action=action,
        cron=cron,
        next_run_at=timezone.now() - timedelta(minutes=minutes_ago),
        **fields,
    )


def jobs(inst, kind):
    return list(Job.objects.filter(instance=inst, kind=kind))


# --- backend-jobs#1 / backend-backups#3: two schedules in one tick ----------------------------------------
def test_backup_and_snapshot_due_together_both_run(server, fake_agent, monkeypatch, django_capture_on_commit_callbacks):
    """'Hourly snapshot' sorts before 'Nightly backup' by name; before the fix the backup was skipped every night."""
    snap = due(server, "Hourly snapshot", "snapshot", cron="0 * * * *")
    backup = due(server, "Nightly backup", "backup")
    # the first tick: the backup goes first (ordering by action), the snapshot finds the server busy
    with monkeypatch.context() as m:
        m.setattr(runner, "dispatch", lambda job_id: True)  # the backup job stays queued and holds the lock
        with django_capture_on_commit_callbacks(execute=True):
            run_due_schedules(timezone.now())
    backup.refresh_from_db()
    snap.refresh_from_db()
    assert backup.last_result.startswith("Queued job")
    assert len(jobs(server, "backup")) == 1
    assert snap.last_result.startswith("Waiting: Another operation is in progress: Scheduled backup: Nightly backup")
    assert snap.deferred_at is not None and snap.next_run_at <= timezone.now()  # still due
    # the backup finishes; the next tick runs the snapshot
    with django_capture_on_commit_callbacks(execute=True):
        runner.run_job(str(jobs(server, "backup")[0].id))
    with django_capture_on_commit_callbacks(execute=True):
        run_due_schedules(timezone.now())
    snap.refresh_from_db()
    assert snap.last_result.startswith("Queued job") and snap.deferred_at is None
    assert snap.next_run_at > timezone.now()
    assert [j.status for j in jobs(server, "snapshot_create")] == [JobStatus.SUCCEEDED]


def test_a_run_put_off_for_an_hour_is_given_up_and_notified(server, fake_agent, monkeypatch):
    sent = []
    monkeypatch.setattr("apps.notifications.service.notify", lambda event, title, message, **kw: sent.append((event, title)))
    blocker = runner.enqueue("backup", title="Manual backup", instance=server)
    sched = due(server, "Nightly backup", "backup")
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("Waiting:")
    Schedule.objects.filter(pk=sched.pk).update(deferred_at=timezone.now() - timedelta(minutes=61))
    assert run_due_schedules(timezone.now()) == 1
    sched.refresh_from_db()
    assert sched.last_result.startswith("Failed: not run for 60 minutes")
    assert sched.deferred_at is None and sched.next_run_at > timezone.now()
    assert ("backup.failed", f"Schedule 'Nightly backup' did not run on {server.name}") in sent
    assert any(event == "job.failed" for event, _ in sent)
    assert Job.objects.filter(kind="backup").count() == 1 and blocker.status == JobStatus.QUEUED


def test_a_command_runs_while_a_backup_makes_the_status_backing_up(server, fake_agent):
    Instance.objects.filter(pk=server.pk).update(status="backing_up")
    sched = due(server, "Say", "command", cron="* * * * *", payload={"command": "say hi"})
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("OK"), sched.last_result
    assert (str(server.id), "say hi") in fake_agent.commands


# --- backend-jobs#2: impossible dates and isolation --------------------------------------------------------
@pytest.mark.parametrize("expr", ["0 0 31 2 *", "0 4 30 2 *", "0 4 31 4 *"])
def test_cron_without_a_next_date_is_refused(expr, server, clients):
    with pytest.raises(ValueError, match="never matches a date"):
        validate_cron(expr)
    resp = clients["owner"].post(
        f"/api/instances/{server.id}/schedules", {"name": "x", "action": "backup", "cron": expr}, format="json"
    )
    assert resp.status_code == 400, resp.content


def test_one_broken_schedule_does_not_stop_the_others(server, make_instance, fake_agent):
    # a row saved by an older release that accepted the expression
    broken = Schedule.objects.create(instance=server, name="AAA broken", action="backup", cron="0 0 31 2 *")
    other = make_instance(name="Zulu")
    fake_agent.apply(other.id, build_spec(other), start=True)
    Instance.objects.filter(pk=other.pk).update(container_state="running", status="running")
    later = due(other, "Say", "command", cron="* * * * *", payload={"command": "say hi"})
    run_due_schedules(timezone.now())
    later.refresh_from_db()
    broken.refresh_from_db()
    assert later.last_result.startswith("OK")
    assert broken.enabled is False and broken.last_result.startswith("Paused: the cron expression")


def test_an_unexpected_error_is_recorded_and_the_loop_goes_on(server, make_instance, fake_agent, monkeypatch):
    first = due(server, "A", "command", cron="* * * * *", payload={"command": "say a"})
    second = due(server, "B", "command", cron="* * * * *", payload={"command": "say b"})
    real = scheduling.execute

    def boom(sched):
        if sched.name == "A":
            raise RuntimeError("x" * 1000)
        return real(sched)

    monkeypatch.setattr(scheduling, "execute", boom)
    assert run_due_schedules(timezone.now()) == 2
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.last_result.startswith("Error: xxx") and len(first.last_result) <= 300
    assert first.next_run_at > timezone.now()
    assert second.last_result.startswith("OK")


# --- backend-jobs#4 / scenario-boot-shutdown#5: after downtime ---------------------------------------------
@pytest.mark.parametrize("action", ["restart", "stop", "start", "command"])
def test_a_power_action_missed_during_downtime_is_not_run_late(action, server, fake_agent):
    sched = due(server, "Daily", action, minutes_ago=5 * 60, payload={"command": "say hi"}, only_when_running=False)
    assert run_due_schedules(timezone.now()) == 1
    sched.refresh_from_db()
    assert sched.last_result.startswith("Missed: due at")
    assert sched.next_run_at > timezone.now()
    assert not Job.objects.filter(instance=server).exists()
    assert not fake_agent.commands


def test_a_power_action_a_few_minutes_late_still_runs(server, fake_agent):
    sched = due(server, "Say", "command", minutes_ago=3, cron="* * * * *", payload={"command": "say hi"})
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("OK")


def test_a_missed_backup_waits_for_the_server_to_settle_then_runs_once(
    server, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=1))
    Instance.objects.filter(pk=server.pk).update(status="starting", ready_at=None)
    sched = due(server, "Nightly backup", "backup", minutes_ago=6 * 60)
    assert run_due_schedules(timezone.now()) == 0
    sched.refresh_from_db()
    assert sched.last_result.startswith("Waiting: catching up a run that was missed")
    assert not jobs(server, "backup")
    # ready for 3 minutes now: it runs once, and the next run is the next regular time
    Instance.objects.filter(pk=server.pk).update(status="running", ready_at=timezone.now() - timedelta(minutes=3))
    with django_capture_on_commit_callbacks(execute=True):
        assert run_due_schedules(timezone.now()) == 1
    sched.refresh_from_db()
    assert sched.last_result.startswith("Queued job") and sched.next_run_at > timezone.now()
    assert [j.status for j in jobs(server, "backup")] == [JobStatus.SUCCEEDED]
    with django_capture_on_commit_callbacks(execute=True):
        assert run_due_schedules(timezone.now()) == 0
    assert len(jobs(server, "backup")) == 1


def test_a_missed_backup_of_a_stopped_server_runs_once_the_panel_was_up_five_minutes(server, fake_agent, monkeypatch):
    Instance.objects.filter(pk=server.pk).update(status="stopped", container_state="exited", ready_at=None)
    sched = due(server, "Nightly backup", "backup", minutes_ago=6 * 60, only_when_running=False)
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=2))
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("Waiting:")
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=6))
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("Queued job")
    assert Job.objects.get(instance=server, kind="backup").params["kind"] == BackupKind.SCHEDULED


# --- backend-jobs#5: daylight saving time -----------------------------------------------------------------
def test_a_time_in_the_repeated_hour_runs_once():
    first = next_run("30 1 * * *", datetime(2027, 11, 7, 7, 0, tzinfo=UTC), LA)  # 00:00 PDT
    assert first == datetime(2027, 11, 7, 1, 30, tzinfo=LA) and first.utcoffset() == timedelta(hours=-7)
    again = next_run("30 1 * * *", first + timedelta(seconds=5), LA)
    assert again.astimezone(LA).date().isoformat() == "2027-11-08"  # not 01:30 PST the same night
    # also when the tick comes during the second 01:xx hour
    assert next_run("30 1 * * *", datetime(2027, 11, 7, 9, 20, tzinfo=UTC), LA).astimezone(LA).day == 8


def test_a_time_in_the_skipped_hour_runs_when_it_ends():
    when = next_run("30 2 * * *", datetime(2027, 3, 14, 8, 0, tzinfo=UTC), LA)  # 00:00 PST
    assert when.astimezone(LA).replace(tzinfo=None) == datetime(2027, 3, 14, 3, 0)
    assert next_run("30 2 * * *", when, LA).astimezone(LA).replace(tzinfo=None) == datetime(2027, 3, 15, 2, 30)


def test_schedules_follow_the_servers_time_zone_setting():
    from apps.core.models import SystemSetting

    SystemSetting.set("timezone", "America/Los_Angeles")
    when = next_run("0 4 * * *", datetime(2027, 6, 1, 0, 0, tzinfo=UTC))
    assert when == datetime(2027, 6, 1, 4, 0, tzinfo=LA)


def test_beat_runs_in_utc_whatever_the_panel_time_zone():
    """With CELERY_TIMEZONE = TIME_ZONE (America/Los_Angeles) beat sent nothing for the repeated hour."""
    env = {**os.environ, "TZ": "America/Los_Angeles", "MCPANEL_TESTING": "1", "DJANGO_SETTINGS_MODULE": "mcpanel.settings"}
    out = subprocess.run(
        [sys.executable, "-c", "from django.conf import settings; print(settings.TIME_ZONE, settings.CELERY_TIMEZONE)"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.split() == ["America/Los_Angeles", "UTC"]


def test_an_interval_tick_crossing_the_fall_back_hour_is_on_time():
    from celery.schedules import schedule

    from mcpanel.celery import app

    last = datetime(2027, 11, 7, 8, 59, 55, tzinfo=UTC)  # 01:59:55 PDT
    now = datetime(2027, 11, 7, 9, 0, 6, tzinfo=UTC)  # 01:00:06 PST
    every = schedule(timedelta(seconds=10), app=app, nowfun=lambda: now)
    assert app.conf.timezone == "UTC"
    assert every.is_due(last).is_due is True
