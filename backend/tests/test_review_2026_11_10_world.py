# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""World reset (review of 2026-11-10, findings 14 and 15): the job remembers that it stopped the
server, and a worker stop ends it as interrupted, not as "marked as failed"."""

from __future__ import annotations

from typing import Any

import pytest

from apps.jobs import shutdown
from apps.jobs.models import Job, JobStatus

from .test_audit_job_endings import no_stop_flag  # noqa: F401 (fixture)
from .test_world_reset_api import last_job, log_of, reset, server, start_container  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db


def test_a_world_reset_remembers_that_it_stopped_the_server(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    seen: list[Any] = []
    fake_agent.hooks["stop"] = lambda *a, **k: seen.append(Job.objects.get(kind="world_reset").result)
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks, stop_if_running=True, pre_restore_backup=False)
    assert resp.status_code == 202, resp.content
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert seen == [{"stopped_server": True}]


def test_a_worker_stop_during_a_world_reset_is_an_interruption(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.hooks["stop"] = lambda *a, **k: shutdown.request_stop()
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks, stop_if_running=True, pre_restore_backup=True)
    assert resp.status_code == 202, resp.content
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "interrupted")
    text = log_of(job)
    assert "The panel was being stopped, so this job ended here. The server it had stopped was left stopped." in text
    assert "marked as failed" not in text
    server.refresh_from_db()
    assert server.desired_state == "stopped"  # settle_interrupted: it had stopped the server
    assert not fake_agent.called("world_reset") and not fake_agent.called("create_backup")
