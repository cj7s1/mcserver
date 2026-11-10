"""instances.state.compute: every branch of the observed-status mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.instances import state
from apps.instances.models import Instance, Status

NOW = datetime(2026, 11, 3, 18, 0, 0, tzinfo=UTC)


def inst(**fields) -> Instance:
    values = {"name": "s", "game_port": 25565, "installed_at": NOW - timedelta(days=1), "applied_revision": 1}
    values.update(fields)
    return Instance(**values)


def container(**fields) -> dict:
    values = {
        "exists": True,
        "status": "running",
        "running": True,
        "restarting": False,
        "exit_code": 0,
        "oom_killed": False,
        "health": "healthy",
        "started_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    values.update(fields)
    return values


def compute(instance, c, job=None):
    return state.compute(instance, c, job, now=NOW)


def test_archived_wins_over_everything():
    status, detail = compute(inst(archived_at=NOW), container(), "stop")
    assert status == Status.ARCHIVED
    assert "Archived" in detail


@pytest.mark.parametrize(("job", "expected"), sorted(state.JOB_STATUS.items()))
def test_active_job_kind_maps_to_status(job, expected):
    assert compute(inst(), container(), job) == (expected, "")


def test_backup_job_on_stopped_server_reports_container_state():
    c = container(status="exited", running=False, exit_code=0)
    assert compute(inst(desired_state="stopped"), c, "backup") == (Status.STOPPED, "")


def test_backup_job_without_container_reports_container_state():
    assert compute(inst(installed_at=None, applied_revision=0), None, "backup")[0] == Status.CREATED


def test_job_kind_without_mapping_is_ignored():
    assert compute(inst(), container(), "start") == (Status.RUNNING, "")
    assert compute(inst(), container(), "content_install") == (Status.RUNNING, "")


def test_no_container_and_never_installed_is_created():
    assert compute(inst(installed_at=None, applied_revision=0), None) == (Status.CREATED, "Not installed yet")
    assert compute(inst(installed_at=None, applied_revision=0), {"exists": False})[0] == Status.CREATED


def test_no_container_after_install_is_stopped():
    status, detail = compute(inst(installed_at=NOW), {"exists": False})
    assert status == Status.STOPPED
    assert "No container" in detail


def test_no_container_and_never_installed_is_created_whatever_was_applied():
    """A server that was created without starting it (or cloned) gets a container that is removed
    again: it has an applied revision, but it was never installed."""
    assert compute(inst(installed_at=None, applied_revision=3), {"exists": False}) == (Status.CREATED, "Not installed yet")


def last_run(**fields) -> dict:
    values = {"status": "exited", "exit_code": 0, "oom_killed": False, "restart_count": 0, "started_at": "2026-11-03T10:00:00Z"}
    values.update(fields)
    return {"exists": False, "last_run": values}


@pytest.mark.parametrize("desired", ["stopped", "running"])
@pytest.mark.parametrize("exit_code", [0, 1, 137, None])
def test_retired_container_reads_as_a_stopped_server(desired, exit_code):
    """The record of the last run of a server without a container: that run is over, whatever its
    exit code was, also while a Start job is creating the next container (desired state running)."""
    assert compute(inst(desired_state=desired), last_run(exit_code=exit_code)) == (Status.STOPPED, "")


def test_retired_container_of_a_server_that_never_became_ready_is_stopped_not_created():
    # it ran (and failed) during its first start: "Not installed yet" would hide that
    assert compute(inst(installed_at=None), last_run(exit_code=1)) == (Status.STOPPED, "")


def test_retired_out_of_memory_kill_stays_visible():
    status, detail = compute(inst(desired_state="stopped"), last_run(exit_code=137, oom_killed=True))
    assert status == Status.FAILED and "out of memory" in detail


def test_job_status_and_archive_win_over_the_last_run():
    assert compute(inst(), last_run(), "restore") == (Status.RESTORING, "")
    assert compute(inst(archived_at=NOW), last_run(oom_killed=True))[0] == Status.ARCHIVED


@pytest.mark.parametrize("c", [container(restarting=True), container(status="restarting", running=False)])
def test_restarting_container_is_a_crash_loop(c):
    c["exit_code"] = 1
    status, detail = compute(inst(), c)
    assert status == Status.FAILED
    assert "Crash loop" in detail and "exit code 1" in detail


def test_running_and_healthy():
    assert compute(inst(), container()) == (Status.RUNNING, "")


def test_running_without_healthcheck_is_running_but_flagged():
    status, detail = compute(inst(), container(health=None))
    assert status == Status.RUNNING
    assert "readiness not verified" in detail


def test_running_not_ready_before_first_install_is_installing():
    status, detail = compute(inst(installed_at=None), container(health="unhealthy"))
    assert status == Status.INSTALLING
    assert "first start" in detail


def test_health_starting_is_starting():
    assert compute(inst(), container(health="starting"))[0] == Status.STARTING


def test_unhealthy_within_startup_grace_is_starting():
    started = (NOW - timedelta(minutes=2)).isoformat()
    assert compute(inst(), container(health="unhealthy", started_at=started))[0] == Status.STARTING


def test_unhealthy_after_grace_is_unhealthy():
    started = (NOW - state.STARTUP_GRACE - timedelta(seconds=1)).isoformat()
    status, detail = compute(inst(), container(health="unhealthy", started_at=started))
    assert status == Status.UNHEALTHY
    assert "not answering" in detail


def test_unhealthy_without_start_time_is_unhealthy():
    assert compute(inst(), container(health="unhealthy", started_at=None))[0] == Status.UNHEALTHY


def test_docker_nanosecond_timestamps_are_parsed():
    started = NOW - timedelta(minutes=1)
    docker_ts = started.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"
    assert state._parse(docker_ts) == started.replace(microsecond=123456)
    assert compute(inst(), container(health="unhealthy", started_at=docker_ts))[0] == Status.STARTING
    assert state._parse("garbage") is None
    assert state._parse(None) is None


def test_oom_killed_is_failed_with_advice():
    c = container(status="exited", running=False, exit_code=137, oom_killed=True)
    status, detail = compute(inst(desired_state="stopped"), c)
    assert status == Status.FAILED
    assert "out of memory" in detail


def test_unexpected_exit_while_desired_running_is_failed():
    c = container(status="exited", running=False, exit_code=1)
    status, detail = compute(inst(desired_state="running"), c)
    assert status == Status.FAILED
    assert "exit code 1" in detail


def test_clean_exit_while_desired_running_is_stopped_by_itself():
    c = container(status="exited", running=False, exit_code=0)
    assert compute(inst(desired_state="running"), c) == (Status.STOPPED, "Server stopped itself")


def test_created_container_never_started_is_created():
    c = container(status="created", running=False, exit_code=0, health=None)
    assert compute(inst(installed_at=None, desired_state="stopped"), c) == (
        Status.CREATED,
        "Installed container; not started yet",
    )


@pytest.mark.parametrize("docker_state", ["exited", "dead", "created"])
def test_stopped_states_when_desired_stopped(docker_state):
    c = container(status=docker_state, running=False, exit_code=143)
    assert compute(inst(desired_state="stopped"), c) == (Status.STOPPED, "")


def test_paused_container_is_stopped():
    c = container(status="paused", running=False)
    assert compute(inst(), c) == (Status.STOPPED, "Container paused")


def test_unknown_docker_state():
    status, detail = compute(inst(), container(status="removing", running=False))
    assert status == Status.UNKNOWN
    assert "removing" in detail
