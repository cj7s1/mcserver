"""Fix round 2 of feature/more-games: the findings of the end-to-end run on real containers
(audit-2026-11-09/mg6-e2e/BUGS.md).

* BUG-1: a world reset or import that changes what the container is made from (Valheim's world name, Rust's
  seed) recreates the stopped container before it starts it again.
* BUG-4: Project Zomboid takes admin rights away with the branch's level (Build 41: none), and a player action
  whose console answer is the game's refusal fails instead of reading "done" (PZ, HKMP, CS2, Rust).
* BUG-5: the files that hold Project Zomboid's passwords (its logs record RCON logins, its ini, its own
  backup zips) never leave the panel unmasked: downloads are masked line by line, zips leave them out for
  everyone, the game's backups are never handed out and switched off; the editor masks the join password
  and keeps it on save.
* BUG-6: Stop ends a job that only waits for a first start that downloads the game.
* Rust: an empty description is sent as such (not the image's default text).
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from apps.instances.models import Instance
from apps.instances.spec import build_spec, rcon_password, restart_required
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient

from . import test_games_six as six
from .test_games_six import create, err, run, start


@pytest.fixture
def actions(fake_agent: Any, monkeypatch: pytest.MonkeyPatch) -> six.FakeActions:
    fake = six.FakeActions(fake_agent)
    monkeypatch.setattr(AgentClient, "game_action", lambda client, *a, **k: fake(client, *a, **k))
    return fake


# --- BUG-1: a new world that changes the container's configuration ------------------------------------------
@pytest.fixture
def containers_stay(settings: Any) -> None:
    """A stopped container stays until the reconciler retires it (seconds later on a real panel): the job's
    start comes first, as on the end-to-end run."""
    settings.MCPANEL_RETIRE_STOPPED_CONTAINERS = False


def test_reset_with_a_new_world_name_recreates_the_container(
    clients, fake_agent, django_capture_on_commit_callbacks, containers_stay
):
    vh = create(clients, django_capture_on_commit_callbacks, "valheim", options={"world_name": "Midgard"})
    fake_agent.put_file(vh.id, "config/worlds_local/Midgard/_main.1.fwl2", b"F")
    start(fake_agent, vh)
    assert fake_agent.containers[str(vh.id)]["spec"]["env"]["WORLD_NAME"] == "Midgard"
    job = run("saves_reset", vh, django_capture_on_commit_callbacks, backup=False, settings={"world_name": "Asgard"}, seed=None)
    assert job.status == JobStatus.SUCCEEDED, job.error
    container = fake_agent.containers[str(vh.id)]
    # the started container is made from the new configuration: the game creates (and loads) Asgard
    assert container["running"] and container["spec"]["env"]["WORLD_NAME"] == "Asgard"
    assert "its container is made again" in " ".join(job.log)
    vh.refresh_from_db()
    assert not restart_required(vh)  # (the panel's view matches the container: no "restart required")


def test_reset_with_a_new_seed_recreates_the_rust_container(
    clients, fake_agent, django_capture_on_commit_callbacks, containers_stay
):
    ru = create(clients, django_capture_on_commit_callbacks, "rust")
    fake_agent.put_file(ru.id, "server/rust/proceduralmap.3500.1.260.map", b"x")
    start(fake_agent, ru)
    job = run("saves_reset", ru, django_capture_on_commit_callbacks, backup=False, settings={"seed": 4242}, seed=None)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(ru.id)]["spec"]["env"]["RUST_SERVER_SEED"] == "4242"


def test_a_reset_that_changes_no_configuration_starts_the_container_as_it_is(
    clients, fake_agent, actions, django_capture_on_commit_callbacks, monkeypatch, containers_stay
):
    pw = create(clients, django_capture_on_commit_callbacks, "palworld")
    fake_agent.put_file(pw.id, "Pal/Saved/SaveGames/0/0123456789ABCDEF0123456789ABCDEF/Level.sav", b"L")
    start(fake_agent, pw)
    applied: list[str] = []
    original = fake_agent.apply
    monkeypatch.setattr(fake_agent, "apply", lambda iid, spec, **k: applied.append(str(iid)) or original(iid, spec, **k))
    job = run("saves_reset", pw, django_capture_on_commit_callbacks, backup=False, settings={}, seed=None)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert applied == [] and fake_agent.containers[str(pw.id)]["running"]
    assert "made again" not in " ".join(job.log)


# --- BUG-4: player actions read the game's answer ---------------------------------------------------------------
def test_zomboid_takes_admin_rights_away_with_the_branchs_level(clients, fake_agent, django_capture_on_commit_callbacks):
    owner = clients["owner"]
    fake_agent.command_handler = lambda iid, cmd: "User alice no longer has access level"
    for branch, level in (("legacy41", "none"), ("public", "user")):
        inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid", options={"branch": branch})
        start(fake_agent, inst)
        resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "deop", "name": "alice"}, format="json")
        assert resp.status_code == 200, resp.json()
        assert fake_agent.commands[-1] == (str(inst.id), f'setaccesslevel "alice" {level}')


@pytest.mark.parametrize(
    "game, action, name, reply",
    [
        ("project-zomboid", "deop", "alice", "Access Level '0' unknown, list of access level: player, admin, gm"),
        ("project-zomboid", "kick", "nobody", "User nobody doesn't exist."),
        ("project-zomboid", "pardon", "alice", 'Use this command to unban a user. Use: /unbanuser "username"'),
        ("hkmp", "kick", "Zote", "[INFO] kick Zote\n[INFO] Could not find player with name 'Zote'"),
        ("hkmp", "pardon", "KEY1", "[INFO] unban KEY1\n[INFO] Given argument is not a valid authentication key"),
        ("cs2", "kick", "Bob", "No user found with that name"),
    ],
)
def test_a_refusal_of_the_game_is_not_reported_as_done(
    game, action, name, reply, clients, fake_agent, django_capture_on_commit_callbacks
):
    inst = create(clients, django_capture_on_commit_callbacks, game)
    start(fake_agent, inst)
    fake_agent.command_handler = lambda iid, cmd: reply
    resp = clients["owner"].post(f"/api/instances/{inst.id}/players", {"action": action, "name": name}, format="json")
    assert resp.status_code == 409 and err(resp) == "game_refused", resp.json()
    assert reply.splitlines()[-1].split("'")[0][:20] in resp.json()["error"]["message"]


def test_names_and_echoes_are_not_taken_for_refusals(clients, fake_agent, django_capture_on_commit_callbacks):
    owner = clients["owner"]
    pz = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    start(fake_agent, pz)
    fake_agent.command_handler = lambda iid, cmd: "User unknown is now banned"
    resp = owner.post(f"/api/instances/{pz.id}/players", {"action": "ban", "name": "unknown"}, format="json")
    assert resp.status_code == 200, resp.json()
    hk = create(clients, django_capture_on_commit_callbacks, "hkmp")
    start(fake_agent, hk)
    fake_agent.command_handler = lambda iid, cmd: f"[INFO] {cmd}\n[INFO] Added player name 'not-a-valid' to pre-list"
    resp = owner.post(f"/api/instances/{hk.id}/players", {"action": "whitelist_add", "name": "not-a-valid"}, format="json")
    assert resp.status_code == 200, resp.json()


def test_rust_refusals_through_webrcon(clients, fake_agent, actions, django_capture_on_commit_callbacks, monkeypatch):
    ru = create(clients, django_capture_on_commit_callbacks, "rust")
    start(fake_agent, ru)
    monkeypatch.setattr(
        AgentClient,
        "game_action",
        lambda client, iid, action, params=None, timeout=60: {"ok": True, "output": "User 76561190000000009 isn't banned"},
    )
    resp = clients["owner"].post(
        f"/api/instances/{ru.id}/players", {"action": "pardon", "name": "76561190000000009"}, format="json"
    )
    assert resp.status_code == 409 and err(resp) == "game_refused"


# --- BUG-5: Project Zomboid's passwords in its files ------------------------------------------------------------
OLD_PASSWORD = "Rotated0ldPassw0rd"


@pytest.fixture
def pz_files(clients, fake_agent, django_capture_on_commit_callbacks) -> tuple[Instance, str]:
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid", options={"branch": "legacy41"})
    pw = rcon_password(inst)
    log = (
        f"LOG  : General , 1> RCON: ID=31 Type=3 Body='{pw}' Socket[addr=/172.21.0.2,port=38874,localport=27015]\n"
        f"LOG  : General , 2> RCON: ID=9 Type=3 Body='{OLD_PASSWORD}' Socket[addr=/172.21.0.2]\n"
        "LOG  : General , 3> *** SERVER STARTED ****\n"
    )
    fake_agent.put_file(inst.id, "data/server-console.txt", log)
    fake_agent.put_file(inst.id, "data/Logs/11-11-26_09-34-00_DebugLog-server.txt", log)
    fake_agent.put_file(inst.id, "data/Logs/logs_11-11/11-11-26_09-32-42_DebugLog-server.txt", log)
    fake_agent.put_file(inst.id, "data/backups/version/backup_1.zip", b"PK\x03\x04 (the ini inside)")
    fake_agent.put_file(inst.id, "data/Server/servertest.ini", f"PublicName=Knox\nRCONPassword={pw}\nPassword=joinme\n")
    fake_agent.put_file(inst.id, "data/Saves/Multiplayer/servertest/map_t.bin", b"MAP")
    return inst, pw


def _download(owner: Any, inst: Instance, path: str) -> Any:
    return owner.get(f"/api/instances/{inst.id}/files/download", {"path": path})


def _body(resp: Any) -> bytes:
    return b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content


def test_zomboid_logs_are_masked_in_every_download(clients, pz_files):
    inst, pw = pz_files
    owner = clients["owner"]
    for path in (
        "data/server-console.txt",
        "data/Logs/11-11-26_09-34-00_DebugLog-server.txt",
        "data/Logs/logs_11-11/11-11-26_09-32-42_DebugLog-server.txt",
    ):
        resp = _download(owner, inst, path)
        assert resp.status_code == 200, path
        body = _body(resp)
        # the current password and an older one (logs keep old lines): both hidden, the rest kept
        assert pw.encode() not in body and OLD_PASSWORD.encode() not in body, path
        assert body.count(b"Body='********'") == 2 and b"*** SERVER STARTED ****" in body, path
    ini = _body(_download(owner, inst, "data/Server/servertest.ini"))
    assert pw.encode() not in ini and b"joinme" not in ini and b"Password=********" in ini
    # the text editor hides the same (the join password too, which the settings form hides already)
    text = owner.get(f"/api/instances/{inst.id}/files/text", {"path": "data/Server/servertest.ini"}).json()["content"]
    assert "joinme" not in text and pw not in text and "Password=********" in text


def test_zomboid_backups_and_logs_never_go_into_a_zip(clients, fake_agent, pz_files):
    inst, _pw = pz_files
    owner = clients["owner"]  # (also for users who may configure the server: the RCON password is nobody's)
    resp = _download(owner, inst, "data/backups/version/backup_1.zip")
    assert resp.status_code == 403 and err(resp) == "managed_file"
    resp = owner.get(f"/api/instances/{inst.id}/files/archive", {"path": "data"})
    assert resp.status_code == 200, resp.content[:300]
    names = zipfile.ZipFile(io.BytesIO(_body(resp))).namelist()
    assert "data/Saves/Multiplayer/servertest/map_t.bin" in names
    assert not [n for n in names if "backups" in n or "Logs" in n or "server-console" in n or n.endswith(".ini")], names
    for path in ("data/backups", "data/backups/version", "data/Logs", "data/Logs/logs_11-11"):
        resp = owner.get(f"/api/instances/{inst.id}/files/archive", {"path": path})
        assert resp.status_code == 403 and err(resp) == "managed_file", path
    # a backup zip is not moved where it could be downloaded; it may be deleted
    resp = owner.post(
        f"/api/instances/{inst.id}/files/rename",
        {"source": "data/backups/version/backup_1.zip", "destination": "data/x.zip"},
        format="json",
    )
    assert resp.status_code == 403
    resp = owner.post(f"/api/instances/{inst.id}/files/delete", {"paths": ["data/backups/version/backup_1.zip"]}, format="json")
    assert resp.status_code in (200, 202), resp.json()
    assert fake_agent.get_file(inst.id, "data/backups/version/backup_1.zip") is None


def test_zomboid_editor_keeps_the_masked_passwords_on_save(clients, fake_agent, pz_files):
    inst, pw = pz_files
    owner = clients["owner"]
    text = owner.get(f"/api/instances/{inst.id}/files/text", {"path": "data/Server/servertest.ini"}).json()["content"]
    resp = owner.put(
        f"/api/instances/{inst.id}/files/text",
        {"path": "data/Server/servertest.ini", "content": text.replace("PublicName=Knox", "PublicName=Knox Two")},
        format="json",
    )
    assert resp.status_code == 200, resp.json()
    saved = fake_agent.text(inst.id, "data/Server/servertest.ini")
    assert "PublicName=Knox Two" in saved and "Password=joinme" in saved and f"RCONPassword={pw}" in saved
    # a password typed in the editor is written as typed
    resp = owner.put(
        f"/api/instances/{inst.id}/files/text",
        {"path": "data/Server/servertest.ini", "content": text.replace("Password=********\n", "Password=newone\n")},
        format="json",
    )
    assert resp.status_code == 200
    assert "\nPassword=newone\n" in fake_agent.text(inst.id, "data/Server/servertest.ini")


def test_zomboid_switches_its_own_backups_off(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    ini = fake_agent.text(inst.id, "data/Server/servertest.ini")
    for line in ("BackupsOnStart=false", "BackupsOnVersionChange=false", "BackupsPeriod=0", "UPnP=false"):
        assert line in ini.splitlines(), line
    # an ini the game rewrote with its defaults gets the panel's keys back before the next start
    fake_agent.put_file(inst.id, "data/Server/servertest.ini", "BackupsOnVersionChange=true\nBackupsPeriod=30\n")
    fake_agent.containers.pop(str(inst.id))  # stopped: the start makes its container (and writes the keys first)
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    ini = fake_agent.text(inst.id, "data/Server/servertest.ini").splitlines()
    assert "BackupsOnVersionChange=false" in ini and "BackupsPeriod=0" in ini
    resp = clients["owner"].patch(
        f"/api/instances/{inst.id}/config/server-ini", {"values": {"BackupsOnVersionChange": True}}, format="json"
    )
    assert resp.status_code == 400


# --- BUG-6: Stop while a job only waits for the first start ----------------------------------------------------
def _waiting_job(inst: Instance, waiting: bool, kind: str = "install") -> Job:
    job = Job.objects.create(
        kind=kind,
        title=f"Install {inst.name}",
        instance=inst,
        status=JobStatus.RUNNING,
        exclusive=True,
        result={"waiting_first_start": waiting},
    )
    Instance.objects.filter(pk=inst.pk).update(active_job=job, status="installing")
    inst.refresh_from_db()
    return job


def test_stop_ends_a_job_that_waits_for_the_first_start(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "cs2")
    start(fake_agent, inst)
    job = _waiting_job(inst, True)
    owner = clients["owner"]
    assert owner.get(f"/api/instances/{inst.id}").json()["active_job"]["stoppable"] is True
    with django_capture_on_commit_callbacks(execute=True):
        resp = owner.post(f"/api/instances/{inst.id}/power", {"action": "stop"}, format="json")
    assert resp.status_code == 202, resp.json()
    job.refresh_from_db()
    assert job.status == JobStatus.CANCELLED and job.error_code == "superseded" and "Stopped by owner" in job.error
    stop = Job.objects.get(pk=resp.json()["id"])
    assert stop.kind == "stop" and stop.status == JobStatus.SUCCEEDED, stop.error
    assert not fake_agent.containers[str(inst.id)]["running"]


def test_stop_still_waits_for_a_job_that_does_real_work(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "cs2")
    job = _waiting_job(inst, False, kind="backup")
    resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "stop"}, format="json")
    assert resp.status_code == 409 and err(resp) == "instance_busy"
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING
    assert "stoppable" not in clients["owner"].get(f"/api/instances/{inst.id}").json()["active_job"]
    # a start is never what ends the wait
    job.result = {"waiting_first_start": True}
    job.save(update_fields=["result"])
    resp = clients["owner"].post(f"/api/instances/{inst.id}/power", {"action": "restart"}, format="json")
    assert resp.status_code == 409 and err(resp) == "instance_busy"


def test_the_wait_marks_its_job_while_it_lasts(clients, fake_agent, django_capture_on_commit_callbacks):
    from apps.instances import game_handlers
    from apps.jobs.runner import JobContext

    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    job = Job.objects.create(kind="install", title="t", instance=inst, status=JobStatus.RUNNING)
    seen: list[Any] = []

    class Client:
        answers = iter([{"exists": True, "running": True, "health": "starting"}, {"exists": True, "running": True}])

        def status(self, _iid: Any) -> dict[str, Any]:
            seen.append(Job.objects.get(pk=job.pk).result.get("waiting_first_start"))
            return next(self.answers)

    ctx = JobContext(job)
    ctx.sleep_until = lambda _t: None  # type: ignore[method-assign]
    game_handlers.wait_first_start(inst, ctx, Client(), span=(0.9, 0.05))  # type: ignore[arg-type]
    assert seen == [True, True] and Job.objects.get(pk=job.pk).result["waiting_first_start"] is False


# --- Rust: the description -----------------------------------------------------------------------------------------
def test_rust_sends_an_empty_description(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    assert build_spec(inst)["env"]["RUST_SERVER_DESCRIPTION"] == ""
    resp = clients["owner"].patch(f"/api/instances/{inst.id}/config/server", {"values": {"description": "Weekly"}}, format="json")
    assert resp.status_code == 200
    inst.refresh_from_db()
    assert build_spec(inst)["env"]["RUST_SERVER_DESCRIPTION"] == "Weekly"
