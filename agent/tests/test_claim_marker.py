"""The claim marker and the start wrapper (review of 2026-11-11, items 1 and 2).

The real-container gate started a published Satisfactory container whose claim was lost by hand
(``docker start``): the game answered an InitialAdmin login on its published port for 1.6 s before the
check loop stopped it. The settings file is written before anybody claims the server, so it proves
nothing. Now:

* the agent writes ``.mcpanel/claim-verified`` (size, mtime_ns, SHA-256 of the settings file) only
  after it verified the claim through the API (InitialAdmin refused, admin login works) on an
  unpublished container, or right after the panel's own settings change on a published container
  whose marker matched before;
* the container's entrypoint is a start wrapper that exits 78 unless the marker matches (published
  containers only), before the game runs;
* every file operation under ``saved/``, restores, clones and the panel's ``mark_unclaimed`` remove
  the marker; the check loop removes it when the file changes outside the panel's operations, stops a
  fresh published run without it at once (Docker's start event), and records the wrapper's refusal;
* SaveGame is not called while no game is loaded (it never answers then: the gate's item 2).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from mcagent import claimguard, claimmarker, docker_ops, main
from mcagent.errors import AgentError
from mcagent.profiles import satisfactory

from . import test_claim_guard as _cg
from . import test_games_satisfactory as _sf
from .test_claim_guard import Published, _Docker, claimed_folder
from .test_games_satisfactory import ADMIN, IID, Container, claimed, driver_for, write_claim_file

cert_dir = _sf.cert_dir
use_fake_driver = _cg.use_fake_driver
one_container = _cg.one_container
fake = _sf.fake
settings = _sf.settings

CLAIM = satisfactory.CLAIM_FILE


def folder(settings) -> Path:
    return settings.instances_dir / str(IID)


def mark_verified(settings) -> None:
    """What the agent records after it verified the claim."""
    fp = claimmarker.fingerprint(settings, IID, CLAIM)
    assert claimmarker.write(settings, IID, CLAIM, fp, since_generation=claimmarker.generation(IID))


# --- the start wrapper (run for real with /bin/sh) -----------------------------------------------------------
def run_wrapper(settings, published: str | None) -> subprocess.CompletedProcess:
    script = claimmarker.wrapper_script(
        mount=str(folder(settings)), claim_file=CLAIM, original=("/bin/echo", "GAME-STARTED")
    )
    path = folder(settings) / "wrapper-under-test.sh"
    path.write_bytes(script)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if published is not None:
        env[claimmarker.PUBLISHED_ENV] = published
    return subprocess.run(["/bin/sh", str(path)], env=env, capture_output=True, text=True, timeout=30)


def test_wrapper_refuses_a_published_start_without_a_verified_claim(settings):
    write_claim_file(settings, b"settings the game wrote before anybody claimed it")
    for published in ("1", None):  # a container without the variable counts as published (fail closed)
        r = run_wrapper(settings, published)
        assert r.returncode == claimmarker.REFUSED_EXIT == 78
        assert "GAME-STARTED" not in r.stdout and "no claim marker" in r.stdout
    # verified: the image's entrypoint runs
    mark_verified(settings)
    r = run_wrapper(settings, "1")
    assert r.returncode == 0 and "GAME-STARTED" in r.stdout
    # the file changed since (same size, other bytes; or a new mtime only): refused again
    claim = folder(settings) / CLAIM
    data = claim.read_bytes()
    claim.write_bytes(data[:-1] + b"X")
    assert run_wrapper(settings, "1").returncode == 78
    claim.write_bytes(data)
    st = claim.stat()
    os.utime(claim, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
    r = run_wrapper(settings, "1")
    assert r.returncode == 78 and "changed since the claim was verified" in r.stdout
    claim.unlink()
    assert run_wrapper(settings, "1").returncode == 78


def test_wrapper_lets_an_unpublished_container_start(settings):
    """The panel claims the server on a container without ports (nothing to verify yet)."""
    r = run_wrapper(settings, "0")
    assert r.returncode == 0 and "GAME-STARTED" in r.stdout


def test_profile_runs_the_wrapper_and_execs_the_images_entrypoint(settings):
    assert satisfactory.PROFILE.entrypoint == ("/bin/sh", "/config/.mcpanel/start-guard.sh")
    assert b"exec /init.sh" in satisfactory.START_WRAPPER and b"/config/saved/ServerSettings.7777.sav" in (
        satisfactory.START_WRAPPER
    )
    assert satisfactory.PROFILE.restart_policy == "no"
    assert claimmarker.PUBLISHED_ENV in satisfactory.RESERVED_ENV  # a spec cannot switch the check off
    satisfactory.prepare_start(settings, IID)
    wrapper = folder(settings) / claimmarker.WRAPPER
    assert (
        wrapper.read_bytes() == satisfactory.START_WRAPPER and oct(wrapper.stat().st_mode & 0o777) == "0o444"
    )


def test_every_agent_start_writes_the_wrapper(settings, fake, use_fake_driver, one_container, monkeypatch):
    one_container["c"] = c = Published(fake.port, running=False, published=False)
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    (folder(settings) / ".mcpanel").mkdir()
    (folder(settings) / claimmarker.WRAPPER).write_text("exec /init.sh  # changed inside the container\n")
    docker_ops.start(IID, settings)
    assert c.started_calls == 1
    assert (folder(settings) / claimmarker.WRAPPER).read_bytes() == satisfactory.START_WRAPPER


# --- writing the marker ----------------------------------------------------------------------------------
def test_claim_on_an_unpublished_container_writes_a_matching_marker(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    recorded = claimmarker.read(settings, IID)
    assert recorded is not None and recorded["claim_file"] == CLAIM
    assert claimmarker.matches(settings, IID, CLAIM)
    # verified: InitialAdmin refused, then the admin login with the panel's password
    names = [f for f, _d, _l in fake.requests]
    assert names[-2:] == ["PasswordlessLogin", "PasswordLogin"]
    assert oct((folder(settings) / claimmarker.MARKER).stat().st_mode & 0o777) == "0o444"


def test_claim_on_a_published_container_writes_no_marker(settings, fake):
    drv, c = driver_for(fake), Published(fake.port)
    fake.settings_file = folder(settings) / CLAIM
    result = drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)
    assert result["claim_verified"] is False and claimmarker.read(settings, IID) is None


def test_claim_rerun_removes_the_marker_first(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    fake.fail["PasswordLogin"] = (200, {"errorCode": "wrong_password", "errorMessage": "no"})
    with pytest.raises(AgentError) as exc:
        drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)
    assert exc.value.code == "claimed_elsewhere" and claimmarker.read(settings, IID) is None


def test_settings_changes_of_the_panel_keep_the_server_startable(settings, fake):
    """Join password, new game and a load of another session make the game rewrite its settings file;
    the agent verifies the claim again and records the new file."""
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    for action, params in (
        ("client_password", {"password": "join-me"}),
        ("new_game", {"session_name": "First"}),
        ("new_game", {"session_name": "Second"}),
        ("load_game", {"save_name": "First_autosave_0"}),
    ):
        before = fake.settings_writes
        result = drv.game_action(settings, c, action, {**params, "admin_password": ADMIN}, 30)
        assert fake.settings_writes == before + 1  # the fake game wrote the file
        assert result["claim_verified"] is True and claimmarker.matches(settings, IID, CLAIM), action
    # the same on a published container whose marker matched before the change
    pub = Published(fake.port)
    result = drv.game_action(
        settings, pub, "client_password", {"password": "other", "admin_password": ADMIN}, 30
    )
    assert result["claim_verified"] is True and claimmarker.matches(settings, IID, CLAIM)
    # without the admin password nothing is verified: the stale marker goes
    result = drv.game_action(settings, c, "client_password", {"password": "third"}, 30)
    assert result["claim_verified"] is False and claimmarker.read(settings, IID) is None


def test_a_published_change_without_a_valid_marker_is_not_trusted(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    write_claim_file(settings, b"changed by hand")  # the marker no longer matches
    pub = Published(fake.port)
    result = drv.game_action(settings, pub, "client_password", {"password": "x", "admin_password": ADMIN}, 30)
    assert result["claim_verified"] is False and claimmarker.read(settings, IID) is None


def test_a_change_under_saved_while_the_claim_is_verified_wins(settings, fake, monkeypatch):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    real = drv.verify_claim

    def verify_and_race(*a, **k):
        real(*a, **k)
        claimmarker.files_changed(settings, IID, ["saved/server/x.sav"], "an upload")  # meanwhile

    monkeypatch.setattr(drv, "verify_claim", verify_and_race)
    result = drv.game_action(
        settings, c, "client_password", {"password": "join", "admin_password": ADMIN}, 30
    )
    assert result["claim_verified"] is False and claimmarker.read(settings, IID) is None


def test_verify_claim_action_only_on_an_unpublished_container(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    claimmarker.remove(settings, IID, "test")
    with pytest.raises(AgentError) as exc:
        drv.game_action(settings, Published(fake.port), "verify_claim", {"admin_password": ADMIN}, 30)
    assert exc.value.code == "claim_unverified" and claimmarker.read(settings, IID) is None
    with pytest.raises(AgentError) as exc:
        drv.game_action(settings, c, "verify_claim", {"admin_password": "wrong-password"}, 30)
    assert exc.value.code == "claimed_elsewhere" and claimmarker.read(settings, IID) is None
    assert drv.game_action(settings, c, "verify_claim", {"admin_password": ADMIN}, 30) == {
        "claim_verified": True
    }
    assert claimmarker.matches(settings, IID, CLAIM)
    # an unclaimed game (a lost settings file) never gets a marker
    fake.claimed = False
    claimmarker.remove(settings, IID, "test")
    with pytest.raises(AgentError) as exc:
        drv.game_action(settings, c, "verify_claim", {"admin_password": ADMIN}, 30)
    assert exc.value.code == "server_unclaimed" and claimmarker.read(settings, IID) is None


def test_the_publish_guard_needs_the_marker_not_just_the_settings_file(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    drv.publish_guard(settings, IID, None)
    claimmarker.remove(settings, IID, "test")
    with pytest.raises(AgentError) as exc:
        drv.publish_guard(settings, IID, None)
    assert exc.value.code == "server_unclaimed" and "claim-verified" in str(exc.value)


# --- removing the marker -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("put", "/files/text", {"json": {"path": "saved/ServerSettings.7777.sav", "content": "x"}}),
        ("put", "/files/upload?path=saved/server/a.sav&overwrite=true", {"content": b"SAVE"}),
        ("post", "/files/mkdir", {"json": {"path": "saved/new"}}),
        ("post", "/files/rename", {"json": {"source": "saved/server", "destination": "saved/old"}}),
        ("post", "/files/delete", {"json": {"paths": ["saved/ServerSettings.7777.sav"]}}),
    ],
)
def test_file_operations_under_saved_remove_the_marker(api, auth, settings, method, path, kwargs):
    write_claim_file(settings)
    (folder(settings) / "saved" / "server").mkdir(exist_ok=True)
    mark_verified(settings)
    r = getattr(api, method)(f"/v1/instances/{IID}{path}", headers=auth, **kwargs)
    assert r.status_code in (200, 201), r.text
    assert claimmarker.read(settings, IID) is None


def test_other_file_operations_keep_the_marker(api, auth, settings):
    write_claim_file(settings)
    mark_verified(settings)
    # the panel writes the API token before every apply
    r = api.put(
        f"/v1/instances/{IID}/files/text", headers=auth, json={"path": ".mcpanel/api-token", "content": "t"}
    )
    assert r.status_code == 200
    r = api.put(f"/v1/instances/{IID}/files/text", headers=auth, json={"path": "notes.txt", "content": "x"})
    assert r.status_code == 200 and claimmarker.matches(settings, IID, CLAIM)


@pytest.mark.parametrize("target", [claimmarker.MARKER, claimmarker.WRAPPER, "./.mcpanel//claim-verified"])
def test_the_marker_and_the_wrapper_cannot_be_written_through_the_file_api(api, auth, settings, target):
    write_claim_file(settings)
    r = api.put(f"/v1/instances/{IID}/files/text", headers=auth, json={"path": target, "content": "size=1\n"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "managed_file"
    r = api.put(f"/v1/instances/{IID}/files/upload?path={target}&overwrite=true", headers=auth, content=b"x")
    assert r.status_code == 403
    (folder(settings) / "forged").mkdir()
    (folder(settings) / "forged" / "claim-verified").write_text("forged\n")
    r = api.post(
        f"/v1/instances/{IID}/files/rename",
        headers=auth,
        json={"source": "forged", "destination": ".mcpanel"},
    )
    assert r.status_code == 403
    assert claimmarker.read(settings, IID) is None


def test_the_panel_can_ask_for_the_marker_to_go(api, auth, settings):
    write_claim_file(settings)
    mark_verified(settings)
    r = api.delete(f"/v1/instances/{IID}/claim-marker", headers=auth)
    assert r.status_code == 200 and r.json() == {"removed": True}
    assert api.delete(f"/v1/instances/{IID}/claim-marker", headers=auth).json() == {"removed": False}
    assert "claim_marker" in main.FEATURES


def test_a_restore_never_brings_a_marker_back(api, auth, settings, monkeypatch):
    """A backup holds .mcpanel/claim-verified too: a restored marker is never trusted (it would match the
    restored settings file of whatever state the backup had)."""
    from mcagent.backups import Restic

    write_claim_file(settings)
    mark_verified(settings)
    marker_bytes = (folder(settings) / claimmarker.MARKER).read_bytes()
    claim_bytes = (folder(settings) / CLAIM).read_bytes()

    def fake_restore(self, snapshot_id, target, progress=None, **kwargs):
        # what restic puts back: the backup's files, its claim marker included
        (target / "saved").mkdir(parents=True)
        (target / CLAIM).write_bytes(claim_bytes)
        (target / ".mcpanel").mkdir()
        (target / claimmarker.MARKER).write_bytes(marker_bytes)
        return {"message_type": "summary"}

    monkeypatch.setattr(Restic, "restore", fake_restore)
    monkeypatch.setattr("mcagent.backups.shutil.which", lambda _b: "/bin/true")  # no restic binary needed
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": False})
    r = api.post(f"/v1/instances/{IID}/backups/restore", json={"snapshot_id": "abcdef12"}, headers=auth)
    assert r.status_code == 200, r.text
    op = r.json()["operation"]
    for _ in range(100):
        op = api.get(f"/v1/operations/{op['id']}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert op["status"] == "succeeded", op
    assert (folder(settings) / CLAIM).exists() and claimmarker.read(settings, IID) is None


# --- the check loop ----------------------------------------------------------------------------------------
def _fresh(c) -> None:
    c.started = time.strftime("%Y-%m-%dT%H:%M:%S.000000001Z", time.gmtime())


def test_a_fresh_published_run_without_a_marker_is_stopped_at_once(
    settings, fake, use_fake_driver, monkeypatch
):
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)  # claimed, the API would even say so
    claimmarker.remove(settings, IID, "the settings file was removed and put back by hand")
    c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    guard = claimguard.ClaimGuard(settings)
    fake.requests.clear()
    guard.check(c, fresh=True)  # Docker's start event
    assert c.state == "exited" and fake.requests == []  # before any API call
    record = claimguard.load(settings, IID)
    assert record["code"] == "server_unclaimed" and "claim marker" in record["message"]


def test_a_young_run_seen_by_the_poll_is_checked_too(settings, fake, use_fake_driver, monkeypatch):
    c = Published(fake.port)
    _fresh(c)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    claimguard.ClaimGuard(settings).check_all()
    assert c.state == "exited"


def test_an_older_run_with_a_changed_settings_file_keeps_running(
    settings, fake, use_fake_driver, monkeypatch
):
    """An admin in the game changed a setting: the marker goes (the next start is verified), the
    claimed server keeps running."""
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    c = Published(fake.port)  # started long ago
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    guard = claimguard.ClaimGuard(settings)
    guard.check_all()
    assert c.state == "running" and claimmarker.matches(settings, IID, satisfactory.CLAIM_FILE)
    claimmarker._changing.clear()  # the claim's grace period is over
    fake.client_password = "set in the game"
    fake.touch_settings()
    guard.check_all()
    assert c.state == "running" and claimmarker.read(settings, IID) is None


def test_a_change_inside_an_operation_of_the_panel_is_not_from_outside(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    with claimmarker.panel_change(IID):
        fake.touch_settings()
        assert claimmarker.check_outside_change(settings, IID, CLAIM) is False
    assert claimmarker.check_outside_change(settings, IID, CLAIM) is False  # grace period
    assert claimmarker.read(settings, IID) is not None


def test_the_wrappers_refusal_is_recorded_for_the_panel(settings, fake, use_fake_driver, monkeypatch):
    c = Published(fake.port, running=False)
    c.exit_code = 78

    class _Containers:
        @staticmethod
        def get(_cid):
            return c

    monkeypatch.setattr(docker_ops, "client", lambda: type("D", (), {"containers": _Containers})())
    guard = claimguard.ClaimGuard(settings)
    claimguard.handle_event(
        guard, {"Type": "container", "Action": "die", "id": c.id, "Actor": {"Attributes": {"exitCode": "78"}}}
    )
    record = claimguard.load(settings, IID)
    assert record["code"] == "server_unclaimed" and "start guard refused" in record["message"]
    claimguard.clear(settings, IID)
    claimguard.handle_event(
        guard, {"Type": "container", "Action": "die", "id": c.id, "Actor": {"Attributes": {"exitCode": "0"}}}
    )
    assert claimguard.load(settings, IID) is None


def test_start_events_are_checked_at_once(settings, monkeypatch):
    seen: list = []
    guard = claimguard.ClaimGuard(settings)
    monkeypatch.setattr(guard, "on_start", lambda cid: seen.append(("start", cid)))
    monkeypatch.setattr(guard, "on_die", lambda cid, code: seen.append(("die", cid, code)))
    stop = __import__("threading").Event()
    windows: list = []

    class _Events:
        def events(self, **kwargs):
            windows.append(kwargs)
            assert kwargs["filters"]["event"] == ["start", "die"] and kwargs["decode"] is True
            if len(windows) == 1:
                yield {"Type": "container", "Action": "start", "id": "abc"}
                yield {
                    "Type": "container",
                    "Action": "die",
                    "id": "abc",
                    "Actor": {"Attributes": {"exitCode": "78"}},
                }
            else:
                stop.set()
                return

    monkeypatch.setattr(docker_ops, "client", lambda: _Events())
    claimguard.watch_events(settings, stop, guard, window=1)
    assert seen == [("start", "abc"), ("die", "abc", 78)]
    assert windows[1]["since"] == windows[0]["until"]  # no gap between two windows


# --- item 2: no SaveGame while no game is loaded --------------------------------------------------------
def test_no_save_while_no_game_is_loaded(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)  # claimed, no game (the real server then names SERVER-SESSION...)
    fake.requests.clear()
    assert drv.save_now(settings, c) is None
    assert drv.save(lambda cmd, t: drv.save_now(settings, c), lambda *_a: None) == "nothing_loaded"
    assert "SaveGame" not in fake.functions()
    sessions = drv.game_action(settings, c, "sessions", {}, 30)
    assert sessions["current"] is None and sessions["game_loaded"] is False
    drv.game_action(settings, c, "new_game", {"session_name": "Now", "admin_password": ADMIN}, 30)
    assert drv.save_now(settings, c) == "Now_mcpanel" and "SaveGame" in fake.functions()
    assert drv.game_action(settings, c, "sessions", {}, 30)["current"] == "Now"


@pytest.mark.parametrize(
    ("running", "outcome", "how"),
    [(False, None, "stopped"), (True, "nothing_loaded", "no_game"), (True, None, "saved")],
)
def test_backup_says_how_it_was_made_consistent(settings, monkeypatch, running, outcome, how):
    """item 5: the panel's job log said "saved through its API" for a stopped server and after a failed
    or skipped save; the agent now reports what happened (``saving`` of the backup's result)."""
    from types import SimpleNamespace

    from mcagent import drivers

    class Driver(drivers.ControlDriver):
        can_save = True

        def save(self, send, report, wait_log=None):
            return outcome

    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": running})
    monkeypatch.setattr(drivers, "for_status", lambda _s: Driver())
    monkeypatch.setattr(main, "SAVE_SETTLE_SECONDS", 0)
    op = SimpleNamespace(update=lambda *a, **k: None)
    with main._world_saving_paused(settings, IID, "auto", op, "the backup", 0.0) as state:
        pass
    assert state.how == how and state.consistent
