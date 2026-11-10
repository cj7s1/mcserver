"""What a job says about the stop it asked for, what a delete left behind, and which container
format an apply really produced (review 2026-11-10)."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.instances import handlers
from apps.instances import spec as spec_module
from apps.instances.models import Instance
from apps.instances.spec import build_spec, current_spec_hash, restart_required, spec_hash
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus

pytestmark = pytest.mark.django_db

KILLED = "the server did not stop in time and was killed (exit code 137)"
UNCLEAN = "the server did not shut down cleanly (exit code {})"
FLUSHED = "The world was saved (save-all flush) before the server was stopped"
RECREATING = "Recreating the container to apply changed settings or a newer MCPanel version (world data is kept)"


@pytest.fixture
def running(make_instance, fake_agent):
    inst = make_instance(name="Survival", desired_state="running")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(
        container_spec_hash=current_spec_hash(inst), installed_at=timezone.now(), status="running"
    )
    inst.refresh_from_db()
    return inst


def run(kind, inst, capture, **params) -> Job:
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED, job.error
    return job


def text(job) -> str:
    return "\n".join(job.log)


# --- the exit code of the stopped run -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, None), (137, KILLED), (255, UNCLEAN.format(255)), (143, UNCLEAN.format(143)), (1, UNCLEAN.format(1))],
)
def test_stop_job_warns_about_every_unclean_exit(exit_code, expected, running, fake_agent, django_capture_on_commit_callbacks):
    """The server runner gives up before Docker's kill and the container then exits 255, not 137."""
    fake_agent.stop_exit_code = exit_code
    log = text(run("stop", running, django_capture_on_commit_callbacks))
    assert FLUSHED in log
    if expected is None:
        assert "warning" not in log
    else:
        assert f"warning: {expected}" in log


def test_restart_job_reports_the_run_it_stopped_not_the_new_one(running, fake_agent, django_capture_on_commit_callbacks):
    """After a restart the status in the agent's answer belongs to the new run (exit code 0)."""
    fake_agent.stop_exit_code = 137
    job = run("restart", running, django_capture_on_commit_callbacks)
    assert fake_agent.called("restart") and not fake_agent.called("apply")
    assert fake_agent.containers[str(running.id)]["exit_code"] == 0  # the new run
    assert f"warning: {KILLED}" in text(job)


def test_restart_job_that_recreates_the_container_reports_the_stop_too(running, fake_agent, django_capture_on_commit_callbacks):
    Instance.objects.filter(pk=running.pk).update(memory_mb=running.memory_mb + 512)  # restart required
    fake_agent.stop_exit_code = 255
    job = run("restart", running, django_capture_on_commit_callbacks)
    assert fake_agent.called("apply")
    assert f"warning: {UNCLEAN.format(255)}" in text(job)
    assert RECREATING in text(job)


def test_restart_log_line_is_also_true_after_an_upgrade(running, fake_agent, django_capture_on_commit_callbacks):
    """After an upgrade every server reads restart-required although none of its settings changed
    (its container was made by the previous version): the log must not say "Configuration changed"."""
    legacy = spec_hash(build_spec(running), 1)  # what the previous release stored
    Instance.objects.filter(pk=running.pk).update(container_spec_hash=legacy)
    running.refresh_from_db()
    assert restart_required(running) is True
    job = run("restart", running, django_capture_on_commit_callbacks)
    log = text(job)
    assert RECREATING in log and "Configuration changed" not in log
    running.refresh_from_db()
    assert restart_required(running) is False


def test_restart_of_a_server_without_a_container_says_so(running, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.remove_container(running.id)
    job = run("restart", running, django_capture_on_commit_callbacks)
    assert "The server has no container; creating it" in text(job) and RECREATING not in text(job)
    assert fake_agent.containers[str(running.id)]["running"] is True


def test_recreate_job_reports_what_the_stop_did(running, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.stop_exit_code = 137
    fake_agent.stop_info = {"flushed": False, "warnings": ["The world was not flushed before the stop: RCON is not available"]}
    job = run("recreate", running, django_capture_on_commit_callbacks)
    assert job.result == {"restarted": True}
    log = text(job)
    assert "warning: The world was not flushed before the stop: RCON is not available" in log
    assert f"warning: {KILLED}" in log and FLUSHED not in log


def test_stop_of_a_server_that_was_not_running_warns_about_nothing(running, fake_agent, django_capture_on_commit_callbacks):
    """It crashed earlier (exit code 1): that is not the result of this stop."""
    fake_agent._set_exited(fake_agent.containers[str(running.id)], 1)
    log = text(run("stop", running, django_capture_on_commit_callbacks))
    assert fake_agent.called("stop")
    assert "warning" not in log and FLUSHED not in log


def test_agent_without_stop_details_logs_nothing(running, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.reports_stop = False  # an agent from before the "stop" object
    fake_agent.stop_exit_code = 137
    log = text(run("stop", running, django_capture_on_commit_callbacks))
    assert "warning" not in log and FLUSHED not in log


# --- deleting a server: what stayed on disk ---------------------------------------------------------
def test_delete_job_result_names_what_stayed_on_disk(running, fake_agent, django_capture_on_commit_callbacks):
    snapshot = f"/mnt/cache/mcpanel/snapshots/{running.id}/20261101T000000Z"
    fake_agent.delete_data_warnings = [f"Could not delete snapshot {snapshot}: Operation not permitted."]
    fake_agent.delete_data_leftovers = [snapshot]
    job = run("delete", running, django_capture_on_commit_callbacks)
    assert not Instance.objects.filter(pk=running.pk).exists()  # the server is gone all the same
    assert job.result == {
        "deleted": str(running.id),
        "name": "Survival",
        "warnings": [f"Could not delete snapshot {snapshot}: Operation not permitted."],
        "leftovers": [snapshot],
    }
    log = text(job)
    assert f"Warning: Could not delete snapshot {snapshot}" in log
    assert f"Still on the disk of node 'test-node': {snapshot}" in log


def test_delete_job_result_without_leftovers(running, fake_agent, django_capture_on_commit_callbacks):
    job = run("delete", running, django_capture_on_commit_callbacks)
    assert job.result == {"deleted": str(running.id), "name": "Survival", "warnings": [], "leftovers": []}
    assert "Still on the disk" not in text(job)


# --- the container format the agent really applied ---------------------------------------------------
def test_spec_hash_per_container_format():
    spec = {"image": "itzg/minecraft-server:java21", "stop_timeout": 90}
    assert spec_hash(spec) == spec_hash(spec, spec_module.CONTAINER_FORMAT)
    assert spec_hash(spec, 1) != spec_hash(spec, 2)
    # format 1 is what releases before the number stored: the hash of the bare spec
    import hashlib
    import json

    assert spec_hash(spec, 1) == hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"container_format": 2}, 2),
        ({"container_format": 1}, 1),
        ({}, 1),  # an agent from before the number
        (None, 1),
        ({"container_format": "2"}, 1),
        ({"container_format": True}, 1),
        ({"container_format": 0}, 1),
        ({"container_format": 99}, spec_module.CONTAINER_FORMAT),  # an agent newer than the panel
    ],
)
def test_applied_container_format(result, expected):
    assert spec_module.applied_container_format(result) == expected


def test_container_made_by_an_up_to_date_agent_does_not_read_restart_required(
    running, fake_agent, django_capture_on_commit_callbacks
):
    Instance.objects.filter(pk=running.pk).update(container_spec_hash="stale")
    job = run("restart", running, django_capture_on_commit_callbacks)
    running.refresh_from_db()
    assert running.container_spec_hash == current_spec_hash(running) and restart_required(running) is False
    assert "older than the panel" not in text(job)


@pytest.mark.parametrize("reported", [None, 1])
def test_container_made_by_an_older_agent_keeps_reading_restart_required(
    reported, running, fake_agent, django_capture_on_commit_callbacks
):
    """Mixed versions: the agent created the container without Docker's stop timeout. The panel
    must not record it as a container of its own format, or it would never be recreated."""
    fake_agent.container_format = reported
    Instance.objects.filter(pk=running.pk).update(container_spec_hash="stale")
    job = run("restart", running, django_capture_on_commit_callbacks)
    running.refresh_from_db()
    assert running.container_spec_hash == spec_hash(build_spec(running), 1)
    assert restart_required(running) is True
    assert "warning: the agent of node 'test-node' is older than the panel" in text(job)
    # the next restart recreates it again, and once the agent is up to date the flag clears
    applied = len(fake_agent.called("apply"))
    run("restart", running, django_capture_on_commit_callbacks)
    assert len(fake_agent.called("apply")) == applied + 1
    fake_agent.container_format = spec_module.CONTAINER_FORMAT
    run("restart", running, django_capture_on_commit_callbacks)
    running.refresh_from_db()
    assert len(fake_agent.called("apply")) == applied + 2 and restart_required(running) is False


def test_an_agent_newer_than_the_panel_does_not_cause_a_restart_loop(running, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.container_format = spec_module.CONTAINER_FORMAT + 1
    Instance.objects.filter(pk=running.pk).update(container_spec_hash="stale")
    run("restart", running, django_capture_on_commit_callbacks)
    running.refresh_from_db()
    assert restart_required(running) is False


def test_log_stop_ignores_answers_it_does_not_understand():
    lines: list[str] = []

    class Ctx:
        def log(self, line: str) -> None:
            lines.append(line)

    for answer in (None, True, {}, {"stop": None}, {"stop": "yes"}, {"exit_code": 137}, {"stop": {}, "exit_code": 137}):
        handlers._log_stop(Ctx(), answer)  # type: ignore[arg-type]
    assert lines == []
