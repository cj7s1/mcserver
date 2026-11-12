"""Fixes of the security and bug review of 2026-11-11, slice E (audit-2026-11-09/security-review/e-bugs/FINDINGS.md).

* E-1 (panel side, interface I-1): a Stop says ``not_ready`` only when the job it ended waited for the server's
  first start and the panel has not seen the run ready; a Start of a container that already runs opens no
  start watch. Adopted from ``e-bugs/poc/backend/test_e_start_on_running_then_stop.py``.
* E-8: a Start that continues the first download of a server that was never ready is watched like a first start.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.instances import game_handlers, services
from apps.instances.models import Instance
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus

from .test_games_six import create, run, start


def _waiting_job(inst: Instance, *, first: bool) -> Job:
    job = Job.objects.create(
        kind="install" if first else "start",
        title="wait",
        instance=inst,
        status=JobStatus.RUNNING,
        exclusive=True,
        result={game_handlers.WAITING_FIRST_START: True, game_handlers.FIRST_START: first},
    )
    Instance.objects.filter(pk=inst.pk).update(active_job=job, status="starting")
    inst.refresh_from_db()
    return job


def _stop(clients: Any, inst: Instance, capture: Any) -> Job:
    with capture(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "stop"}, format="json")
    assert resp.status_code == 202, resp.json()
    return Job.objects.get(pk=resp.json()["id"])


# --- E-1 ------------------------------------------------------------------------------------------------------
def test_e1_a_start_of_a_running_server_opens_no_start_watch_and_the_stop_saves(
    clients, fake_agent, django_capture_on_commit_callbacks, monkeypatch
):
    """The PoC's chain: a Project Zomboid server up for ten days reads unhealthy (the agent's restart, E-2); a
    Start on it no longer waits as if it had just been started, and the Stop after it is the ordinary one."""
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    start(fake_agent, inst)
    Instance.objects.filter(pk=inst.pk).update(installed_at="2026-11-01T00:00:00Z", ready_at="2026-11-01T00:05:00Z")
    fake_agent.age_containers(inst.id, seconds=10 * 86400)
    fake_agent.set_container_state(inst.id, health="unhealthy")
    Instance.objects.filter(pk=inst.pk).update(status="unhealthy", health="unhealthy")
    waits: list[float] = []
    monkeypatch.setattr(runner.JobContext, "sleep_until", lambda self, deadline: waits.append(deadline))
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "start"}, format="json")
    assert resp.status_code == 202, resp.json()
    start_job = Job.objects.get(pk=resp.json()["id"])
    assert start_job.status == JobStatus.SUCCEEDED, start_job.error
    assert waits == [] and not (start_job.result or {}).get(game_handlers.WAITING_FIRST_START)
    inst.refresh_from_db()
    stop = _stop(clients, inst, django_capture_on_commit_callbacks)
    assert stop.status == JobStatus.SUCCEEDED and not (stop.params or {}).get("not_ready")
    assert fake_agent.stop_requests[-1]["not_ready"] is False


def test_e1_a_stop_during_the_start_watch_of_a_server_that_was_ready_before_saves(
    clients, users, fake_agent, django_capture_on_commit_callbacks, monkeypatch
):
    """A stopped, installed server is started; while the Start job watches it (the image checks the game files),
    Stop ends that job as before, but the stop is the ordinary one: the server has a world to save."""
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    Instance.objects.filter(pk=inst.pk).update(installed_at=timezone.now() - timedelta(days=3))
    fake_agent.start_healthy = False
    pressed: list[Any] = []

    def sleep_until(self: Any, _deadline: float) -> None:
        if not pressed:
            assert self.job.result.get(game_handlers.WAITING_FIRST_START) is True
            assert self.job.result.get(game_handlers.FIRST_START) is False
            pressed.append(services.power(Instance.objects.get(pk=inst.pk), "stop", users["owner"]))
        self.check_cancelled()

    monkeypatch.setattr(runner.JobContext, "sleep_until", sleep_until)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "start"}, format="json")
    assert resp.status_code == 202, resp.json()
    start_job = Job.objects.get(pk=resp.json()["id"])
    stop = Job.objects.get(pk=pressed[0].pk)
    assert start_job.status == JobStatus.CANCELLED and start_job.error_code == "superseded"
    assert stop.status == JobStatus.SUCCEEDED and not (stop.params or {}).get("not_ready")
    assert fake_agent.stop_requests[-1]["not_ready"] is False


def test_e1_a_first_start_wait_of_a_run_the_panel_saw_ready_is_an_ordinary_stop(
    clients, fake_agent, django_capture_on_commit_callbacks
):
    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    start(fake_agent, inst)
    started = timezone.now() - timedelta(hours=6)
    Instance.objects.filter(pk=inst.pk).update(started_at=started, ready_at=started + timedelta(minutes=20))
    _waiting_job(inst, first=True)
    stop = _stop(clients, inst, django_capture_on_commit_callbacks)
    assert stop.status == JobStatus.SUCCEEDED and not (stop.params or {}).get("not_ready")
    assert fake_agent.stop_requests[-1]["not_ready"] is False


def test_e1_a_first_start_wait_of_a_run_never_seen_ready_still_stops_at_once(
    clients, fake_agent, django_capture_on_commit_callbacks
):
    """BUG-9 stays fixed: ``ready_at`` empty, or left from an earlier run (older than this run's start)."""
    inst = create(clients, django_capture_on_commit_callbacks, "cs2")
    start(fake_agent, inst)
    started = timezone.now() - timedelta(minutes=5)
    Instance.objects.filter(pk=inst.pk).update(started_at=started, ready_at=started - timedelta(days=1))
    fake_agent.set_container_state(inst.id, started_at=started.isoformat().replace("+00:00", "Z"))
    _waiting_job(inst, first=True)
    stop = _stop(clients, inst, django_capture_on_commit_callbacks)
    assert stop.status == JobStatus.SUCCEEDED and stop.params == {"not_ready": True}
    assert fake_agent.stop_requests[-1]["not_ready"] is True


def test_e1_the_stop_job_checks_the_containers_own_start_again(clients, fake_agent, django_capture_on_commit_callbacks):
    """A queued not-ready stop whose run the reconciler has seen ready meanwhile is sent as an ordinary stop."""
    inst = create(clients, django_capture_on_commit_callbacks, "palworld")
    start(fake_agent, inst)
    container_start = fake_agent.containers[str(inst.id)]["started_at"]
    Instance.objects.filter(pk=inst.pk).update(started_at=None, ready_at=timezone.now() + timedelta(seconds=1))
    job = run("stop", inst, django_capture_on_commit_callbacks, not_ready=True)
    assert container_start and job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.stop_requests[-1]["not_ready"] is False


def test_e1_seen_ready_in_run() -> None:
    now = timezone.now()
    inst = Instance(ready_at=None, started_at=now)
    assert not services.seen_ready_in_run(inst)
    inst.ready_at = now - timedelta(seconds=1)
    assert not services.seen_ready_in_run(inst)  # left from an earlier run
    inst.ready_at = now + timedelta(seconds=1)
    assert services.seen_ready_in_run(inst)
    # the container's own start wins over the recorded one
    assert not services.seen_ready_in_run(inst, (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"))
    assert services.seen_ready_in_run(inst, now - timedelta(minutes=1))


# --- E-8 ------------------------------------------------------------------------------------------------------
def test_e8_a_start_that_continues_a_first_download_is_watched_like_a_first_start(
    clients, fake_agent, django_capture_on_commit_callbacks, monkeypatch
):
    """Created without a start (never ready): the Start job waits up to the first-start timeout (not 10 minutes),
    retries SteamCMD's failures, and a Stop meanwhile may stop it at once (nothing to save)."""
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    assert inst.installed_at is None
    fake_agent.start_healthy = False
    monkeypatch.setattr(game_handlers, "START_WATCH_TIMEOUT", 0)  # a Start of an installed server would give up at once
    seen: list[dict[str, Any]] = []

    def sleep_until(self: Any, _deadline: float) -> None:
        seen.append(dict(self.job.result or {}))
        if len(seen) == 1:  # SteamCMD fails once (a retryable exit) ...
            fake_agent.set_container_state(inst.id, running=False, status="exited", exit_code=1, started_at="t-1")
        elif len(seen) == 2:
            assert fake_agent.containers[str(inst.id)]["running"]  # ... and was started again
            fake_agent.set_container_state(inst.id, health="healthy")

    monkeypatch.setattr(runner.JobContext, "sleep_until", sleep_until)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "start"}, format="json")
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert seen and all(r.get(game_handlers.FIRST_START) is True for r in seen)
    log = " ".join(job.log)
    assert "starting it again" in log and "The server is ready" in log and "not ready within" not in log


def test_e8_a_start_of_an_installed_server_keeps_the_short_watch(
    clients, fake_agent, django_capture_on_commit_callbacks, monkeypatch
):
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    Instance.objects.filter(pk=inst.pk).update(installed_at=timezone.now() - timedelta(days=1))
    fake_agent.start_healthy = False
    monkeypatch.setattr(game_handlers, "START_WATCH_TIMEOUT", 0)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "start"}, format="json")
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED and "not ready within 0 minutes" in " ".join(job.log)


# --- E-4 (I-3) ------------------------------------------------------------------------------------------------
# adopted from e-bugs/poc/backend/test_e_clone_drops_secrets.py
def _clone(clients: Any, capture: Any, src: Instance, name: str) -> Instance:
    with capture(execute=True):
        resp = clients["owner"].post(f"/api/instances/{src.id}/clone", {"name": name}, format="json")
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["job"]["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    return Instance.objects.get(pk=resp.json()["instance"]["id"])


def test_e4_a_cloned_valheim_server_keeps_the_join_password(clients, fake_agent, django_capture_on_commit_callbacks):
    from apps.instances.spec import build_spec

    src = create(clients, django_capture_on_commit_callbacks, "valheim", secrets={"server_password": "sesame-42"})
    clone = _clone(clients, django_capture_on_commit_callbacks, src, "Copy of the world")
    assert clone.secret_values == {"server_password": "sesame-42"}
    assert build_spec(clone)["env"]["SERVER_PASS"] == "sesame-42"


def test_e4_a_valheim_server_restored_as_new_keeps_the_join_password(clients, fake_agent, django_capture_on_commit_callbacks):
    from apps.backups.models import Backup
    from apps.instances.spec import build_spec

    src = create(clients, django_capture_on_commit_callbacks, "valheim", secrets={"server_password": "sesame-42"})
    assert run("backup", src, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    backup = Backup.objects.get(instance=src)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{src.id}/backups/{backup.id}/restore-as-new", {"name": "Restored"}, format="json"
        )
    assert resp.status_code == 202, resp.json()
    new = Instance.objects.get(pk=resp.json()["instance"]["id"])
    assert Job.objects.get(pk=resp.json()["job"]["id"]).status == JobStatus.SUCCEEDED
    assert build_spec(new)["env"].get("SERVER_PASS") == "sesame-42"


def test_e4_a_cloned_palworld_server_keeps_the_join_password(clients, fake_agent, django_capture_on_commit_callbacks):
    from apps.instances.spec import build_spec

    src = create(clients, django_capture_on_commit_callbacks, "palworld", secrets={"server_password": "join-me"})
    clone = _clone(clients, django_capture_on_commit_callbacks, src, "Pal copy")
    assert build_spec(clone)["env"].get("SERVER_PASSWORD") == "join-me"
    # the generated admin password goes with it (the copied world's admins log in with it)
    assert clone.secret_values.get("admin_password") == src.secret_values.get("admin_password")


def test_e4_a_cs2_copy_keeps_the_passwords_but_not_the_servers_own_token(clients, fake_agent, django_capture_on_commit_callbacks):
    token = "A" * 32
    src = create(clients, django_capture_on_commit_callbacks, "cs2", secrets={"server_password": "joinme1", "gslt": token})
    assert src.secret_values["gslt"] == token
    clone = _clone(clients, django_capture_on_commit_callbacks, src, "CS copy")
    assert clone.secret_values.get("server_password") == "joinme1" and "gslt" not in clone.secret_values


def test_e4_a_valheim_server_without_its_join_password_is_not_started(clients, fake_agent, django_capture_on_commit_callbacks):
    """A copy made before this fix: the start is refused (409 secret_required with the key), at the request and
    in a job (a schedule, the resume after a host restart)."""
    src = create(clients, django_capture_on_commit_callbacks, "valheim")
    Instance.objects.filter(pk=src.pk).update(secrets="")
    src.refresh_from_db()
    for action in ("start", "restart"):
        resp = clients["owner"].post(f"/api/instances/{src.id}/power", {"action": action}, format="json")
        assert resp.status_code == 409, resp.json()
        error = resp.json()["error"]
        assert error["code"] == "secret_required" and error["details"] == {"key": "server_password", "label": "Join password"}
    job = run("start", src, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED and job.error_code == "secret_required"
    assert str(src.id) not in fake_agent.containers or not fake_agent.containers[str(src.id)]["running"]
    # set it: the start goes through
    src.set_secret_values({"server_password": "sesame-42"})
    src.save(update_fields=["secrets"])
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{src.id}/power", {"action": "start"}, format="json")
    assert resp.status_code == 202 and Job.objects.get(pk=resp.json()["id"]).status == JobStatus.SUCCEEDED
