# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Schedules (review of 2026-11-10, findings 3 and 4): an edit ends the wait of an earlier due time,
and a run that was put off never runs late: not after its wait ran out, and a power action not
after the panel was down while it waited."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.instances import scheduling
from apps.instances.models import Schedule
from apps.instances.scheduling import run_due_schedules
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus

from .test_audit_schedules import due, panel_up_long, server  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


@pytest.fixture
def notified(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("apps.notifications.service.notify", lambda event, title, message, **kw: sent.append((event, title)))
    return sent


def put_off(server, name: str, action: str, **fields) -> tuple[Schedule, Job]:
    blocker = runner.enqueue("backup", title="Manual backup", instance=server)
    sched = due(server, name, action, **fields)
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.deferred_at is not None and sched.last_result.startswith("Waiting:")
    return sched, blocker


# --- finding 3: an edit resets the wait too ------------------------------------------------------------
def test_editing_a_waiting_schedule_ends_its_wait(server, fake_agent, clients, notified):
    sched, _blocker = put_off(server, "Nightly backup", "backup")
    Schedule.objects.filter(pk=sched.pk).update(deferred_at=timezone.now() - timedelta(minutes=50))
    r = clients["owner"].patch(f"/api/instances/{server.id}/schedules/{sched.id}", {"cron": "30 5 * * *"}, format="json")
    assert r.status_code == 200, r.content
    sched.refresh_from_db()
    assert sched.deferred_at is None and sched.next_run_at is None
    # its new time comes while the server is still busy: a new wait of its own, not the end of the old one
    Schedule.objects.filter(pk=sched.pk).update(next_run_at=timezone.now() - timedelta(seconds=5))
    run_due_schedules(timezone.now() + timedelta(minutes=15))
    sched.refresh_from_db()
    assert sched.last_result.startswith("Waiting:"), sched.last_result
    assert notified == []


def test_switching_a_waiting_schedule_off_and_on_does_not_keep_the_old_wait(server, fake_agent, clients):
    sched, blocker = put_off(server, "Nightly stop", "stop", only_when_running=False)
    for enabled in (False, True):
        r = clients["owner"].patch(f"/api/instances/{server.id}/schedules/{sched.id}", {"enabled": enabled}, format="json")
        assert r.status_code == 200, r.content
    sched.refresh_from_db()
    assert sched.deferred_at is None
    # the next due time is five hours in the past (the panel was down): the misfire rule applies again
    Job.objects.filter(pk=blocker.pk).update(status=JobStatus.SUCCEEDED)
    Schedule.objects.filter(pk=sched.pk).update(next_run_at=timezone.now() - timedelta(hours=5))
    run_due_schedules(timezone.now())
    sched.refresh_from_db()
    assert sched.last_result.startswith("Missed: due at"), sched.last_result
    assert not Job.objects.filter(instance=server, kind="stop").exists()


# --- finding 4: a run that was put off never runs late ---------------------------------------------------
def test_a_power_action_whose_wait_ran_out_during_downtime_is_given_up(server, fake_agent, notified):
    sched, blocker = put_off(server, "Nightly restart", "restart")
    # the panel was down for six hours; the backup that held the server was failed meanwhile
    Job.objects.filter(pk=blocker.pk).update(status=JobStatus.FAILED)
    six_hours_ago = timezone.now() - timedelta(hours=6)
    Schedule.objects.filter(pk=sched.pk).update(deferred_at=six_hours_ago, next_run_at=six_hours_ago)
    assert run_due_schedules(timezone.now()) == 1
    sched.refresh_from_db()
    assert sched.last_result.startswith("Failed: not run for 60 minutes"), sched.last_result
    assert "Another operation is in progress" in sched.last_result  # the reason it waited for
    assert sched.deferred_at is None and sched.next_run_at > timezone.now()
    assert not Job.objects.filter(instance=server, kind="restart").exists()
    assert ("job.failed", f"Schedule 'Nightly restart' did not run on {server.name}") in notified


def test_a_backup_whose_wait_ran_out_is_given_up_before_it_runs(server, fake_agent, notified):
    sched, blocker = put_off(server, "Nightly backup", "backup")
    Job.objects.filter(pk=blocker.pk).update(status=JobStatus.SUCCEEDED)
    Instance = server.__class__
    Instance.objects.filter(pk=server.pk).update(active_job=None)
    Schedule.objects.filter(pk=sched.pk).update(deferred_at=timezone.now() - timedelta(minutes=60))
    assert run_due_schedules(timezone.now()) == 1
    sched.refresh_from_db()
    assert sched.last_result.startswith("Failed: not run for 60 minutes"), sched.last_result
    assert Job.objects.filter(instance=server, kind="backup").count() == 1  # only the blocker
    assert ("backup.failed", f"Schedule 'Nightly backup' did not run on {server.name}") in notified


def test_a_waiting_power_action_is_missed_when_the_panel_restarted_meanwhile(server, fake_agent, monkeypatch):
    sched, blocker = put_off(server, "Nightly stop", "stop", only_when_running=False)
    Job.objects.filter(pk=blocker.pk).update(status=JobStatus.FAILED)
    Schedule.objects.filter(pk=sched.pk).update(
        deferred_at=timezone.now() - timedelta(minutes=30), next_run_at=timezone.now() - timedelta(minutes=30)
    )
    # the panel came back two minutes ago: it was down for most of the 30 minutes the stop waited
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=2))
    assert run_due_schedules(timezone.now()) == 1
    sched.refresh_from_db()
    assert sched.last_result.startswith("Missed: due at"), sched.last_result
    assert sched.deferred_at is None and sched.next_run_at > timezone.now()
    assert not Job.objects.filter(instance=server, kind="stop").exists()


def test_a_waiting_power_action_still_runs_within_its_wait_while_the_panel_ran(server, fake_agent):
    sched, blocker = put_off(server, "Nightly restart", "restart")
    Job.objects.filter(pk=blocker.pk).update(status=JobStatus.FAILED)
    Schedule.objects.filter(pk=sched.pk).update(
        deferred_at=timezone.now() - timedelta(minutes=30), next_run_at=timezone.now() - timedelta(minutes=30)
    )
    run_due_schedules(timezone.now())  # the panel has been up for a day (panel_up_long)
    sched.refresh_from_db()
    assert sched.last_result.startswith("Queued job"), sched.last_result
    assert Job.objects.filter(instance=server, kind="restart").exists()
