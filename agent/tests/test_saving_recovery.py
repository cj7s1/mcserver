"""World saving that a backup or snapshot switched off is switched on again even when the agent
process ends inside the pause (audit agent-docker#3): a marker under the agent's state folder,
save-on from the shutdown hook, at start-up and periodically for marked running servers."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from mcagent import docker_ops, main, rcon

from . import test_security_audit
from .test_security_audit import _FakeRestic, _wait_op

running_server = test_security_audit.running_server  # the fixture


def _marker(settings, iid):
    return settings.state_dir / "saving-paused" / str(iid)


def test_the_pause_is_recorded_on_disk_while_it_lasts(api, auth, agent_settings, running_server, monkeypatch):
    iid, sent, _fail = running_server
    seen: list[bool] = []

    class Restic(_FakeRestic):
        def backup(self, source, tags, excludes, progress=None):  # type: ignore[no-untyped-def]
            seen.append(_marker(agent_settings, iid).exists())
            return super().backup(source, tags, excludes, progress)

    monkeypatch.setattr(main, "Restic", Restic)
    r = api.post(f"/v1/instances/{iid}/backups", json={"backup_id": "b1"}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    assert seen == [True] and sent == ["save-off", "save-all flush", "save-on"]
    assert not _marker(agent_settings, iid).exists()


def test_a_failed_save_on_keeps_the_marker(api, auth, agent_settings, running_server, monkeypatch):
    iid, _sent, fail = running_server
    monkeypatch.setattr(main, "Restic", _FakeRestic)
    fail["save-on"] = rcon.RconError("Connection refused")
    r = api.post(f"/v1/instances/{iid}/backups", json={"backup_id": "b2"}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert any("FAILED to re-enable" in w or "save-on" in w for w in op["result"]["warnings"]), op
    assert _marker(agent_settings, iid).exists()  # the periodic recovery keeps trying
    del fail["save-on"]
    assert main._recover_saving(agent_settings) == 0
    assert not _marker(agent_settings, iid).exists()


def _leave_marker(settings, iid) -> None:
    path = _marker(settings, iid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1700000000\n")


def test_recovery_sends_save_on_to_a_running_server_an_earlier_agent_left_paused(
    agent_settings, running_server
):
    iid, sent, _fail = running_server
    _leave_marker(agent_settings, iid)
    (_marker(agent_settings, iid).parent / ".tmp-junk").write_text("x")  # not a server id: ignored
    assert main._recover_saving(agent_settings) == 0
    assert sent == ["save-on"] and not _marker(agent_settings, iid).exists()


def test_recovery_of_a_server_that_is_not_running_only_drops_the_marker(
    agent_settings, running_server, monkeypatch
):
    iid, sent, _fail = running_server
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"exists": False})
    _leave_marker(agent_settings, iid)
    assert main._recover_saving(agent_settings) == 0
    assert sent == [] and not _marker(agent_settings, iid).exists()


def test_recovery_keeps_trying_while_the_server_cannot_be_reached(agent_settings, running_server):
    iid, sent, fail = running_server
    fail["save-on"] = rcon.RconError("Connection refused")
    _leave_marker(agent_settings, iid)
    assert main._recover_saving(agent_settings) == 1
    assert sent == ["save-on"] and _marker(agent_settings, iid).exists()


def test_recovery_leaves_a_pause_of_this_process_alone(agent_settings, running_server):
    iid, sent, _fail = running_server
    _leave_marker(agent_settings, iid)
    main._paused_here.add(iid)
    try:
        assert main._recover_saving(agent_settings) == 0
    finally:
        main._paused_here.discard(iid)
    assert sent == [] and _marker(agent_settings, iid).exists()


@pytest.fixture
def lifecycle(agent_settings, monkeypatch):
    """Run the real start-up and shutdown hooks with Docker unreachable (save-on goes to the fake RCON)."""

    def no_docker(*_a, **_k):
        raise ConnectionError("Docker is not reachable")

    monkeypatch.setattr(main, "get_settings", lambda: agent_settings)
    for name in ("_blocked_networks", "_remembered_networks", "_attached_networks"):
        monkeypatch.setattr(main, name, getattr(main, name))  # put back after the test
    monkeypatch.setattr(docker_ops, "ensure_network", no_docker)
    monkeypatch.setattr(docker_ops, "client", no_docker)
    yield
    main._background_stop.set()


def test_agent_start_up_re_enables_saving(agent_settings, running_server, lifecycle):
    iid, sent, _fail = running_server
    _leave_marker(agent_settings, iid)
    asyncio.run(main._startup())
    deadline = time.monotonic() + 10
    while _marker(agent_settings, iid).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sent == ["save-on"] and not _marker(agent_settings, iid).exists()


def test_agent_shutdown_re_enables_saving_of_its_own_pauses(agent_settings, running_server, lifecycle):
    iid, sent, _fail = running_server
    other = uuid.uuid4()
    _leave_marker(agent_settings, iid)
    _leave_marker(agent_settings, other)
    main._paused_here.add(iid)  # a backup of this process is between save-off and save-on
    try:
        asyncio.run(main._shutdown())
    finally:
        main._paused_here.discard(iid)
    assert sent == ["save-on", "save-on"]
    assert not _marker(agent_settings, iid).exists() and not _marker(agent_settings, other).exists()
