"""The claim guard of Satisfactory (review 2026-11-09, item 1): no published server runs unclaimed.

* the publish guard needs the claim file ``saved/ServerSettings.7777.sav`` (regular, non-empty, no
  link) besides the token file, and fails closed for a running server whose claim cannot be
  verified (the review's probe: a stale token file let an unclaimed server through);
* every agent path that starts a container with published ports checks it (``/start``,
  ``/restart``, apply's start: ``docker_ops.start``);
* the check loop (``claimguard``) stops a running published server that turns out unclaimed and
  records why (``guard_stop`` in the status), leaves a claimed one running and never touches an
  unpublished one (the panel claims it like that).
"""

from __future__ import annotations

import os
import shutil
import time
from types import SimpleNamespace

import pytest

from mcagent import claimguard, docker_ops, drivers, main
from mcagent.drivers import http_api
from mcagent.drivers import satisfactory as sfd
from mcagent.errors import AgentError
from mcagent.profiles import satisfactory

from . import test_games_satisfactory as _sf
from .sf_fake import FakeSatisfactory
from .test_games_satisfactory import (
    IID,
    Container,
    claimed,
    driver_for,
    write_claim_file,
    write_token,
)

# the Satisfactory tests' fixtures
cert_dir = _sf.cert_dir
fake = _sf.fake
settings = _sf.settings


class Published(Container):
    """A Satisfactory container whose HostConfig publishes ports (or not)."""

    def __init__(self, port: int, *, running: bool = True, published: bool = True) -> None:
        super().__init__(port, running=running)
        self.published = published
        self.started_calls = 0

    @property
    def attrs(self) -> dict:
        attrs = super().attrs
        attrs["HostConfig"]["PortBindings"] = (
            {
                "7777/tcp": [{"HostIp": "", "HostPort": "29010"}],
                "7777/udp": [{"HostIp": "", "HostPort": "29010"}],
            }
            if self.published
            else {}
        )
        attrs["Created"] = "2026-11-10T19:59:00.1Z"
        if self.state != "running":
            attrs["State"]["FinishedAt"] = "2026-11-10T20:05:00.1Z"
        return attrs

    def start(self) -> None:
        self.started_calls += 1
        self.state = "running"


@pytest.fixture
def use_fake_driver(fake, monkeypatch):
    """Containers of the satisfactory profile get a driver that talks to the fake."""
    monkeypatch.setitem(drivers.FACTORIES, "satisfactory-api", lambda _o: driver_for(fake))
    monkeypatch.setattr(http_api, "_pins", {})


def claimed_folder(settings, fake, drv) -> None:
    """The panel claimed the server: the game wrote the claim file, the agent the claim marker, the
    panel the token file."""
    claimed(settings, fake, drv, Container(fake.port))


# --- the publish guard ------------------------------------------------------------------------------
def test_stale_token_file_does_not_let_an_unclaimed_server_through(settings, fake, cert_dir, monkeypatch):
    """The review's probe: claimed once (token file written), then the claim is lost."""
    monkeypatch.setattr(sfd, "CLAIM_WAIT", 1.5)
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    drv.publish_guard(settings, IID, None)  # claimed: fine
    # the claim is lost: ServerSettings.7777.sav removed (or a backup from before the claim restored)
    (settings.instances_dir / str(IID) / satisfactory.CLAIM_FILE).unlink()
    fresh = FakeSatisfactory(cert_dir)
    try:
        fresh_drv = driver_for(fresh)
        assert fresh_drv.is_unclaimed(settings, Container(fresh.port))
        for existing in (None, Container(fresh.port, running=False)):  # retired / stopped container
            with pytest.raises(AgentError) as exc:
                fresh_drv.publish_guard(settings, IID, existing)
            assert exc.value.code == "server_unclaimed" and exc.value.status == 409
            assert "ServerSettings.7777.sav" in str(exc.value)
        # a settings file is there again, but nobody verified it: refused before any probe
        write_claim_file(settings)
        with pytest.raises(AgentError) as exc:
            fresh_drv.publish_guard(settings, IID, None)
        assert exc.value.code == "server_unclaimed" and "claim-verified" in str(exc.value)
        # even a marker that matches the files: a running game that answers unclaimed is refused
        from mcagent import claimmarker

        fp = claimmarker.fingerprint(settings, IID, satisfactory.CLAIM_FILE)
        claimmarker.write(
            settings, IID, satisfactory.CLAIM_FILE, fp, since_generation=claimmarker.generation(IID)
        )
        with pytest.raises(AgentError) as exc:
            fresh_drv.publish_guard(settings, IID, Container(fresh.port))
        assert exc.value.code == "server_unclaimed"
        # running, API down (still loading): the claim cannot be verified, so no publish
        fresh.api_down = True
        started = time.monotonic()
        with pytest.raises(AgentError) as exc:
            fresh_drv.publish_guard(settings, IID, Container(fresh.port))
        assert exc.value.code == "claim_unverified" and exc.value.status == 409
        assert "try again" in str(exc.value).lower() and time.monotonic() - started < 6
    finally:
        fresh.close()


def test_a_running_server_whose_api_comes_up_while_the_guard_waits(settings, fake, monkeypatch):
    import threading

    drv, c = driver_for(fake), Container(fake.port)
    claimed_folder(settings, fake, drv)
    fake.api_down = True
    threading.Timer(1.0, lambda: setattr(fake, "api_down", False)).start()
    drv.publish_guard(settings, IID, c)  # claimed once the API answers


def test_unexpected_api_answer_fails_closed(settings, fake, monkeypatch):
    drv, c = driver_for(fake), Container(fake.port)
    claimed_folder(settings, fake, drv)
    fake.fail["PasswordlessLogin"] = (200, {"errorCode": "something_new", "errorMessage": "?"})
    with pytest.raises(AgentError) as exc:
        drv.publish_guard(settings, IID, c)
    assert exc.value.code == "claim_unverified"


@pytest.mark.parametrize("kind", ["missing", "empty", "symlink", "symlinked_saved", "folder"])
def test_claim_file_must_be_a_regular_non_empty_file(settings, fake, tmp_path, kind):
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    folder = settings.instances_dir / str(IID)
    claim = folder / satisfactory.CLAIM_FILE
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ServerSettings.7777.sav").write_bytes(b"real settings")
    if kind == "missing":
        claim.unlink()
    elif kind == "empty":
        claim.write_bytes(b"")
    elif kind == "symlink":
        claim.unlink()
        claim.symlink_to(elsewhere / "ServerSettings.7777.sav")
    elif kind == "symlinked_saved":
        shutil.rmtree(folder / "saved")
        (folder / "saved").symlink_to(elsewhere)
    else:
        claim.unlink()
        claim.mkdir()
    assert drv.claim_file_present(settings, IID) is False
    with pytest.raises(AgentError) as exc:  # a stopped container: the files decide
        drv.publish_guard(settings, IID, Container(fake.port, running=False))
    assert exc.value.code == "server_unclaimed"


def test_the_claim_file_is_derived_from_the_game_port():
    assert satisfactory.CLAIM_FILE == f"saved/ServerSettings.{satisfactory.GAME_PORT}.sav"
    assert drivers.for_profile(satisfactory.PROFILE).claim_file == satisfactory.CLAIM_FILE
    assert sfd.SatisfactoryDriver(port=7779).claim_file == "saved/ServerSettings.7779.sav"
    with pytest.raises(ValueError):
        sfd.SatisfactoryDriver(port=7777, claim_file="../ServerSettings.7777.sav")


def test_the_token_file_is_still_required(settings, fake):
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    token = (settings.instances_dir / str(IID) / satisfactory.TOKEN_FILE).read_text()
    (settings.instances_dir / str(IID) / satisfactory.TOKEN_FILE).unlink()
    with pytest.raises(AgentError) as exc:
        drv.publish_guard(settings, IID, None)
    assert exc.value.code == "server_unclaimed"
    write_token(settings, token.strip())
    drv.publish_guard(settings, IID, None)


# --- /start, /restart --------------------------------------------------------------------------------
@pytest.fixture
def one_container(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: box.get("c"))
    monkeypatch.setattr(docker_ops, "reattach_recreated_networks", lambda _c, *_a: [])
    return box


def test_start_of_a_published_unclaimed_server_is_refused(
    api, auth, settings, fake, use_fake_driver, one_container
):
    one_container["c"] = c = Published(fake.port, running=False)
    write_token(settings, "bm9wZQ.00")  # a stale token file: not enough
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "server_unclaimed"
    assert c.started_calls == 0 and c.state == "exited"
    # the settings file alone proves nothing (the game writes it before anybody claimed it)
    write_claim_file(settings)
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "server_unclaimed" and c.started_calls == 0
    # the agent's verification recorded in the claim marker does
    from mcagent import claimmarker

    fp = claimmarker.fingerprint(settings, IID, satisfactory.CLAIM_FILE)
    assert claimmarker.write(
        settings, IID, satisfactory.CLAIM_FILE, fp, since_generation=claimmarker.generation(IID)
    )
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert r.status_code == 200 and c.started_calls == 1


def test_start_of_an_unpublished_server_is_not_guarded(
    api, auth, settings, fake, use_fake_driver, one_container
):
    # the claim step needs the server running unclaimed, without ports
    one_container["c"] = c = Published(fake.port, running=False, published=False)
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert r.status_code == 200 and c.started_calls == 1


def test_restart_is_refused_before_the_stop(
    api, auth, settings, fake, use_fake_driver, one_container, monkeypatch
):
    one_container["c"] = c = Published(fake.port)
    stops: list = []
    monkeypatch.setattr(docker_ops, "stop", lambda *a, **k: stops.append(a) or docker_ops.StopResult())
    r = api.post(f"/v1/instances/{IID}/restart", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "server_unclaimed"
    assert stops == [] and c.state == "running" and c.started_calls == 0


def test_restart_start_is_guarded_too(settings, fake, use_fake_driver, one_container, monkeypatch):
    """docker_ops.start is the one start of the agent (apply, /start, /restart all use it)."""
    one_container["c"] = c = Published(fake.port, running=False)
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    with pytest.raises(AgentError) as exc:
        docker_ops.start(IID)
    assert exc.value.code == "server_unclaimed" and c.started_calls == 0


def test_start_after_restoring_a_backup_from_before_the_claim(
    api, auth, settings, fake, use_fake_driver, one_container
):
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    one_container["c"] = c = Published(fake.port, running=False)
    folder = settings.instances_dir / str(IID)
    # the restore puts the backup's folder in place: it has the token file the panel wrote (before
    # every apply) but not the claim, which the game wrote later
    restored = settings.staging_dir / "restored"
    (restored / ".mcpanel").mkdir(parents=True)
    shutil.copy(folder / satisfactory.TOKEN_FILE, restored / satisfactory.TOKEN_FILE)
    (restored / "saved" / "server").mkdir(parents=True)
    shutil.rmtree(folder)
    os.rename(restored, folder)
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "server_unclaimed"
    assert c.started_calls == 0


def test_minecraft_start_is_unchanged(settings, monkeypatch):
    calls: list = []
    c = SimpleNamespace(
        id="mc",
        name="mc",
        labels={docker_ops.LABEL_MANAGED: "true", docker_ops.LABEL_INSTANCE: str(IID)},
        attrs={"HostConfig": {"PortBindings": {"25565/tcp": [{"HostPort": "25565"}]}}},
        start=lambda: calls.append("start"),
        reload=lambda: None,
        status="exited",
    )
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: c)
    monkeypatch.setattr(docker_ops, "reattach_recreated_networks", lambda _c, *_a: [])
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    docker_ops.start(IID)
    assert calls == ["start"]


# --- the check loop ----------------------------------------------------------------------------------
class _Docker:
    def __init__(self, containers):
        self.listed = containers
        self.containers = SimpleNamespace(list=self._list)

    def _list(self, **kwargs):
        assert kwargs["filters"] == {"label": "io.mcpanel.managed=true", "status": "running"}
        return [c for c in self.listed if c.state == "running"]


def test_loop_stops_an_unclaimed_published_server_and_records_why(
    api, auth, settings, fake, use_fake_driver, one_container, monkeypatch
):
    one_container["c"] = c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    guard = claimguard.ClaimGuard(settings)
    guard.check_all()
    assert c.state == "exited" and c.log == [("docker stop", claimguard.STOP_TIMEOUT)]
    assert "Shutdown" not in fake.functions() and "SaveGame" not in fake.functions()  # no save
    body = api.get(f"/v1/instances/{IID}/status", headers=auth).json()
    assert body["guard_stop"]["code"] == "server_unclaimed"
    assert "not claimed" in body["guard_stop"]["message"] and body["guard_stop"]["at"].endswith("Z")
    # also without a container (retired): the last run's reason
    one_container["c"] = None
    body = api.get(f"/v1/instances/{IID}/status", headers=auth).json()
    assert body["exists"] is False and body["guard_stop"]["code"] == "server_unclaimed"
    # the list too (a container-less server under guard_stops)
    monkeypatch.setattr(docker_ops, "list_managed", lambda: [])
    listed = api.get("/v1/instances", headers=auth).json()
    assert listed["guard_stops"][str(IID)]["code"] == "server_unclaimed"
    # the record lives in the agent's state folder, not in the server folder
    assert claimguard.path(settings, IID).parent.parent == settings.state_dir
    assert oct(claimguard.path(settings, IID).stat().st_mode & 0o777) == "0o600"


def test_loop_leaves_a_claimed_server_running_and_rechecks_rarely(
    settings, fake, use_fake_driver, monkeypatch
):
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    guard = claimguard.ClaimGuard(settings)
    fake.requests.clear()
    guard.check_all()
    assert c.state == "running" and fake.functions() == ["HealthCheck", "PasswordlessLogin"]
    guard.check_all()  # verified: not asked again for minutes
    assert len(fake.requests) == 2
    monkeypatch.setattr(claimguard, "RECHECK_SECONDS", 0.0)
    guard.check_all()
    assert len(fake.requests) == 4 and c.state == "running"


def test_loop_never_touches_an_unpublished_server(settings, fake, use_fake_driver, monkeypatch):
    c = Published(fake.port, published=False)  # the panel's claim step runs it like this
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    claimguard.ClaimGuard(settings).check_all()
    assert c.state == "running" and fake.requests == []
    assert claimguard.load(settings, IID) is None


def test_loop_waits_for_the_api_then_stops(settings, fake, use_fake_driver, monkeypatch):
    c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    guard = claimguard.ClaimGuard(settings)
    fake.api_down = True  # loading: nobody can claim it either
    guard.check_all()
    assert c.state == "running" and claimguard.load(settings, IID) is None
    fake.api_down = False
    guard.check_all()
    assert c.state == "exited" and claimguard.load(settings, IID)["code"] == "server_unclaimed"


def test_loop_fails_closed_on_a_changed_certificate(settings, fake, use_fake_driver, monkeypatch):
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    http_api._pins[(c.id, c.started)] = "0" * 64  # another peer than earlier in this run
    claimguard.ClaimGuard(settings).check_all()
    assert c.state == "exited"
    record = claimguard.load(settings, IID)
    assert record["code"] == "claim_unverified" and "certificate" in record["message"]


def test_loop_fails_closed_on_an_unknown_error_code(settings, fake, use_fake_driver, monkeypatch):
    c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    fake.fail["PasswordlessLogin"] = (200, {"errorCode": "internal", "errorMessage": "boom"})
    claimguard.ClaimGuard(settings).check_all()
    assert c.state == "exited" and claimguard.load(settings, IID)["code"] == "claim_unverified"


def test_loop_fails_closed_on_a_reply_the_parser_did_not_expect(settings, fake, use_fake_driver, monkeypatch):
    """B-3: an exception that is not an AgentError (deeply nested JSON raises RecursionError) stops the
    published run like claim_unverified, instead of being logged while the run goes on."""
    c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))

    def unexpected(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array\nforged")

    monkeypatch.setattr(sfd.SatisfactoryDriver, "claim_state", unexpected)
    claimguard.ClaimGuard(settings).check_all()
    assert c.state == "exited"
    record = claimguard.load(settings, IID)
    assert record["code"] == "claim_unverified" and "\n" not in record["message"]


def test_a_later_claimed_run_clears_the_record(
    api, auth, settings, fake, use_fake_driver, one_container, monkeypatch
):
    c = Published(fake.port)
    one_container["c"] = c
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    guard = claimguard.ClaimGuard(settings)
    guard.check_all()
    assert claimguard.load(settings, IID) is not None
    # claimed meanwhile (the panel's setup), started again: a new run
    drv = driver_for(fake)
    claimed_folder(settings, fake, drv)
    c.state, c.started = "running", "2026-11-10T21:00:00.5Z"
    # until the new run is verified the record (about the previous run) is not reported for it
    assert "guard_stop" not in api.get(f"/v1/instances/{IID}/status", headers=auth).json()
    guard.check_all()
    assert c.state == "running" and claimguard.load(settings, IID) is None


def test_loop_takes_the_lock_when_free_and_does_not_wait_for_an_operation(
    settings, fake, use_fake_driver, monkeypatch
):
    import threading

    c = Published(fake.port)
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker([c]))
    monkeypatch.setattr(claimguard, "LOCK_WAIT", 0.2)
    held, release = threading.Event(), threading.Event()

    def operation():
        with docker_ops.instance_lock(IID):
            held.set()
            release.wait(10)

    t = threading.Thread(target=operation, daemon=True)
    t.start()
    held.wait(5)
    try:
        started = time.monotonic()
        claimguard.ClaimGuard(settings).check_all()
        assert c.state == "exited" and time.monotonic() - started < 5
    finally:
        release.set()
        t.join(5)


def test_loop_survives_errors_and_stops_when_asked(settings, monkeypatch):
    import threading

    calls: list = []

    class Broken:
        @property
        def containers(self):
            calls.append(1)
            raise RuntimeError("docker down")

    monkeypatch.setattr(docker_ops, "client", lambda: Broken())
    stop = threading.Event()
    t = threading.Thread(target=claimguard.run, args=(settings, stop, 0.01), daemon=True)
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(2)
    assert not t.is_alive() and len(calls) >= 2


def test_apply_clears_the_record(settings):
    claimguard.record(settings, IID, container_id="x", started_at="y", code="server_unclaimed", message="m")
    assert claimguard.load(settings, IID) is not None
    assert claimguard.clear(settings, IID) and claimguard.load(settings, IID) is None
    import inspect

    assert "claimguard.clear(settings, instance_id)" in inspect.getsource(main.apply_spec)


def test_features_advertise_the_claim_guard():
    assert "claim_guard" in main.FEATURES


def test_startup_starts_the_loop():
    import inspect

    assert '_spawn("claim-guard", claimguard.run, settings, _background_stop)' in inspect.getsource(
        main._startup
    )
