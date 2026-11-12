"""Fix round 3 of feature/more-games: the re-test on real containers (audit-2026-11-09/mg6-e2e/BUGS.md BUG-9, BUG-10).

* BUG-9: a Stop that ends a job still waiting for the first start tells the agent the server never became ready
  (it then stops it at once instead of letting the image's download go on until the stop timeout kills it).
* BUG-10: backup and restore requests name the server's game, so a server without container or last-run record
  (created stopped, filled through an import) gets its game's backup excludes and restore carry-over.
"""

from __future__ import annotations

from apps.backups.models import Backup
from apps.instances.models import Instance
from apps.jobs.models import Job, JobStatus

from .test_games_six import create, run, start


def _waiting(inst: Instance) -> Job:
    job = Job.objects.create(
        kind="install",
        title=f"Install {inst.name}",
        instance=inst,
        status=JobStatus.RUNNING,
        exclusive=True,
        result={"waiting_first_start": True, "first_start": True},
    )
    Instance.objects.filter(pk=inst.pk).update(active_job=job, status="installing")
    inst.refresh_from_db()
    return job


# --- BUG-9 ----------------------------------------------------------------------------------------------------
def test_a_stop_that_ends_the_first_start_wait_says_the_server_was_not_ready(
    clients, fake_agent, django_capture_on_commit_callbacks
):
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    start(fake_agent, inst)
    _waiting(inst)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "stop"}, format="json")
    assert resp.status_code == 202, resp.json()
    stop = Job.objects.get(pk=resp.json()["id"])
    assert stop.status == JobStatus.SUCCEEDED and stop.params == {"not_ready": True}
    assert fake_agent.stop_requests[-1]["not_ready"] is True
    log = " ".join(stop.log)
    # what the agent did instead of a kill at the stop timeout: no "killed ... may not be saved" warning
    assert "had not become ready in this run" in log and "nothing to save" in log and "exit code 137" not in log


def test_a_usual_stop_does_not_say_so(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    start(fake_agent, inst)
    job = run("stop", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED and fake_agent.stop_requests[-1]["not_ready"] is False
    assert "had not become ready" not in " ".join(job.log)


def test_an_older_agent_gets_a_usual_stop_request(clients, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.features = [f for f in fake_agent.features if f != "stop_not_ready"]
    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    start(fake_agent, inst)
    _waiting(inst)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "stop"}, format="json")
    stop = Job.objects.get(pk=resp.json()["id"])
    # (the field is sent; an agent without the feature ignores it and stops as before)
    assert stop.status == JobStatus.SUCCEEDED and "had not become ready" not in " ".join(stop.log)


# --- BUG-10 ---------------------------------------------------------------------------------------------------
def test_backup_and_restore_name_the_game_of_a_server_that_never_ran(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "palworld")
    fake_agent.containers.pop(str(inst.id), None)  # created stopped: its never-started container is retired
    job = run("backup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.backup_games[-1] == {"game": "palworld"}
    backup = Backup.objects.filter(instance=inst).latest("created_at")
    job = run("restore", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.restore_games[-1] == {"game": "palworld"}


def test_minecraft_requests_stay_as_they_were(clients, fake_agent, make_instance, django_capture_on_commit_callbacks):
    inst = make_instance()
    fake_agent.put_file(inst.id, "server.properties", "motd=x\n")
    job = run("backup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.backup_games[-1] is None


def test_a_variant_goes_with_its_game(clients, fake_agent, django_capture_on_commit_callbacks):
    from apps.backups.handlers import agent_game

    inst = Instance(game="terraria", server_type="TSHOCK")
    hint = agent_game(inst)
    assert hint is not None and hint["game"] == "terraria" and "variant" in hint
