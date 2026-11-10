"""Pinned Minecraft outputs for the multi-game refactor (phase 1: no behaviour change).

Captured from the code before the Minecraft container and control code moved into a runtime
profile (``mcagent/profiles``) and a control driver (``mcagent/drivers``); they must keep passing
unchanged afterwards:

* ``build_create_kwargs`` for representative specs (``games_pin_cases.py``), key order included,
  and the agent's spec-hash label;
* the exact warnings and RCON traffic of a graceful stop;
* the saving pause around backups and snapshots (commands, timeouts, progress messages, warnings);
* ``GET /v1/info`` (every key that existed before).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcagent import docker_ops, main, rcon
from mcagent.docker_ops import AgentError

from .games_pin_cases import CASES, IID, settings_for, spec_for
from .test_graceful_stop import FLUSH, PASSWORD, server, settings  # noqa: F401 - fixtures
from .test_graceful_stop import IID as STOP_IID

PINS = json.loads((Path(__file__).parent / "pins" / "create_kwargs.json").read_text())


# --- container creation -----------------------------------------------------------------------------
def test_every_case_is_pinned():
    assert set(PINS) == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_create_kwargs_are_unchanged(name, tmp_path):
    pinned = PINS[name]
    spec = spec_for(name)
    assert main._spec_hash(spec) == pinned["spec_hash"]
    kw = docker_ops.build_create_kwargs(
        settings_for(CASES[name][1], tmp_path), IID, spec, pinned["spec_hash"]
    )
    got = json.loads(json.dumps(kw))
    assert got == pinned["kwargs"]
    # the order Docker receives (environment list, labels, mounts) is part of the pin too
    assert list(got) == list(pinned["kwargs"])
    for key in ("environment", "labels", "volumes", "ports"):
        assert list(got[key]) == list(pinned["kwargs"][key]), key


def test_create_kwargs_have_no_game_label_for_minecraft(tmp_path):
    for name in CASES:
        labels = docker_ops.build_create_kwargs(
            settings_for(CASES[name][1], tmp_path), IID, spec_for(name), "h"
        )["labels"]
        assert not any(k.startswith(("io.mcpanel.game", "io.mcpanel.variant")) for k in labels)


def test_the_validation_pass_of_apply_uses_the_same_arguments(tmp_path):
    """apply_spec calls build_create_kwargs once with the hash "validate" before it queues."""
    plain = settings_for("plain", tmp_path)
    kw = docker_ops.build_create_kwargs(plain, IID, spec_for("vanilla"), "validate")
    assert kw["labels"]["io.mcpanel.spec-hash"] == "validate"


# --- graceful stop ------------------------------------------------------------------------------------
def test_stop_pin_rcon_up(server, settings):  # noqa: F811
    server.rcon.up.set()
    server.container.stop_may_return.set()
    result = docker_ops.stop(STOP_IID, 90, settings)
    assert server.log == [("rcon", FLUSH), ("docker stop", 90)]
    assert [(c.command, c.timeout, c.connect_timeout) for c in server.rcon.calls] == [(FLUSH, 45.0, 3.0)]
    assert result.as_dict() == {"flushed": True, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}


def test_stop_pin_rcon_not_up_yet(server, settings):  # noqa: F811
    server.rcon.open_after_refusals_during_stop = 2
    iid = STOP_IID
    result = docker_ops.stop(iid, 90, settings)
    assert server.log == [
        ("rcon refused", FLUSH),
        ("docker stop", 90),
        ("rcon refused", "stop"),
        ("rcon refused", "stop"),
        ("rcon", "stop"),
    ]
    assert [(c.command, c.timeout, c.connect_timeout) for c in server.rcon.calls][-1] == ("stop", 5.0, 3.0)
    assert result.as_dict() == {
        "flushed": False,
        "stop_sent_via_rcon": True,
        "warnings": [
            "The world was not flushed before the stop: RCON is not available (the server is probably "
            "still starting): Cannot connect to RCON at 172.30.0.5:25575: [Errno 111] Connect call failed"
        ],
        "exit_code": 0,
    }


def test_stop_pin_flush_fails(server, settings):  # noqa: F811
    server.rcon.up.set()
    server.rcon.fail[FLUSH] = rcon.RconError("Timed out waiting for RCON response")
    server.container.stop_may_return.set()
    iid = STOP_IID
    result = docker_ops.stop(iid, 30, settings)
    assert server.log == [("rcon", FLUSH), ("docker stop", 30)]
    assert result.as_dict() == {
        "flushed": False,
        "stop_sent_via_rcon": False,
        "warnings": [
            "save-all flush before the stop failed, the world may not be saved: Timed out waiting for RCON response"
        ],
        "exit_code": 0,
    }


def test_stop_pin_no_password(server, settings):  # noqa: F811
    server.container.env = ["EULA=TRUE"]
    server.container.stop_may_return.set()
    iid = STOP_IID
    result = docker_ops.stop(iid, 60, settings)
    assert server.log == [("docker stop", 60)]
    assert result.warnings == [
        "The world was not flushed before the stop: Container has no RCON password configured"
    ]


def test_stop_pin_published_rcon_mode(server, settings):  # noqa: F811
    settings.rcon_mode = "published"
    server.container.ports = {
        "25565/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25570"}],
        "25575/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25580"}],
    }
    seen: list[tuple] = []
    real = server.rcon

    def published(host, port, password, command, *, timeout, connect_timeout):
        seen.append((host, port, password, command))
        return real("172.30.0.5", 25575, password, command, timeout=timeout, connect_timeout=connect_timeout)

    docker_ops_run = docker_ops.run_command_sync
    docker_ops.run_command_sync = published
    try:
        real.up.set()
        server.container.stop_may_return.set()
        iid = STOP_IID
        docker_ops.stop(iid, 60, settings)
    finally:
        docker_ops.run_command_sync = docker_ops_run
    assert seen == [("127.0.0.1", 25580, PASSWORD, FLUSH)]


def test_rcon_endpoint_and_game_endpoint_pins(server, settings):  # noqa: F811
    iid = STOP_IID
    assert docker_ops.rcon_endpoint(settings, iid) == ("172.30.0.5", 25575, PASSWORD)
    assert docker_ops.game_endpoint(settings, iid) == ("172.30.0.5", 25565)
    assert (docker_ops.GAME_PORT, docker_ops.RCON_PORT) == (25565, 25575)
    settings.rcon_mode = "published"
    assert docker_ops.game_endpoint(settings, iid) == ("127.0.0.1", 25570)
    with pytest.raises(AgentError) as exc:
        docker_ops.rcon_endpoint(settings, iid)
    assert (exc.value.code, str(exc.value)) == ("rcon_unavailable", "RCON port is not published")


# --- the saving pause around backups and snapshots ----------------------------------------------------
class _Op:
    def __init__(self) -> None:
        self.updates: list[tuple[float | None, str | None]] = []

    def update(self, progress=None, message=None):  # type: ignore[no-untyped-def]
        self.updates.append((progress, message))


@pytest.fixture
def paused(agent_settings, monkeypatch):
    sent: list[tuple[str, float]] = []
    fail: dict[str, BaseException] = {}

    async def fake_run_command(host, port, password, command, timeout=30.0):  # type: ignore[no-untyped-def]
        assert (host, port, password) == ("127.0.0.1", 25575, "pw")
        sent.append((command, timeout))
        if command in fail:
            raise fail[command]
        return ""

    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": True})
    monkeypatch.setattr(docker_ops, "rcon_endpoint", lambda _s, _iid: ("127.0.0.1", 25575, "pw"))
    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main, "SAVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(main, "SAVE_ON_RETRY_SECONDS", 0)
    return SimpleNamespace(settings=agent_settings, sent=sent, fail=fail)


def test_saving_pause_pin(paused):
    op = _Op()
    with main._world_saving_paused(paused.settings, IID, "auto", op, "backup", 0.02) as state:
        assert (paused.settings.state_dir / "saving-paused" / str(IID)).exists()
        assert paused.sent == [("save-off", 30), ("save-all flush", 300)]
    assert paused.sent == [("save-off", 30), ("save-all flush", 300), ("save-on", 30)]
    assert op.updates == [
        (0.02, "Pausing world saving (save-off)"),
        (0.04, "Flushing world to disk (save-all flush)"),
    ]
    assert (state.running, state.paused, state.consistent, state.warnings) == (True, True, True, [])
    assert not (paused.settings.state_dir / "saving-paused" / str(IID)).exists()


def test_saving_pause_pin_flush_fails(paused):
    paused.fail["save-all flush"] = rcon.RconError("Timed out waiting for RCON response")
    with main._world_saving_paused(paused.settings, IID, "auto", _Op(), "snapshot", 0.05) as state:
        pass
    assert [c for c, _t in paused.sent] == ["save-off", "save-all flush", "save-on"]
    assert state.consistent is False
    assert state.warnings == [
        "Could not pause world saving (Timed out waiting for RCON response); snapshot may be inconsistent"
    ]


def test_saving_pause_pin_require(paused):
    paused.fail["save-off"] = rcon.RconError("nope")
    with pytest.raises(AgentError) as exc:
        with main._world_saving_paused(paused.settings, IID, "require", _Op(), "backup", 0.02):
            raise AssertionError("not reached")
    assert (exc.value.code, exc.value.status, str(exc.value)) == (
        "consistency_failed",
        409,
        "Could not coordinate world saving: nope",
    )
    assert [c for c, _t in paused.sent] == ["save-off", "save-on"]


def test_saving_pause_pin_save_on_fails(paused):
    paused.fail["save-on"] = rcon.RconError("Connection refused")
    with main._world_saving_paused(paused.settings, IID, "auto", _Op(), "backup", 0.02) as state:
        pass
    assert [c for c, _t in paused.sent] == ["save-off", "save-all flush"] + [
        "save-on"
    ] * main.SAVE_ON_ATTEMPTS
    assert state.warnings == ["FAILED to re-enable world saving (save-on): Connection refused"]
    assert (paused.settings.state_dir / "saving-paused" / str(IID)).exists()
    del paused.fail["save-on"]
    assert main._recover_saving(paused.settings) == 0
    assert paused.sent[-1] == ("save-on", 30.0)


@pytest.mark.parametrize("consistency", ["auto", "require", "none"])
def test_saving_pause_pin_not_running_or_none(paused, monkeypatch, consistency):
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"exists": False})
    with main._world_saving_paused(paused.settings, IID, consistency, _Op(), "backup", 0.02) as state:
        pass
    assert paused.sent == [] and state.consistent is True and state.warnings == []


def test_saving_pause_pin_consistency_none_on_a_running_server(paused):
    with main._world_saving_paused(paused.settings, IID, "none", _Op(), "backup", 0.02) as state:
        pass
    assert paused.sent == [] and state.consistent is False


def test_shutdown_save_on_pin(paused):
    marker = paused.settings.state_dir / "saving-paused" / str(IID)
    marker.parent.mkdir(parents=True)
    marker.write_text("1\n")
    main._resume_all_saving(paused.settings, 3.0)
    assert paused.sent == [("save-on", 3.0)] and not marker.exists()


# --- GET /v1/info -------------------------------------------------------------------------------------
def test_info_pin(api, auth, agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {"server_version": "29.0.0"})
    monkeypatch.setattr(main.storage, "capabilities", lambda _settings: {"backend": "plain"})
    monkeypatch.setattr(main, "restic_available", lambda _settings: {"version": "0.18.1"})
    monkeypatch.setattr(main, "_tls_fingerprint", lambda _settings: None)
    body = api.get("/v1/info", headers=auth).json()
    pinned = {
        "version": main.__version__,
        "container_format": 2,
        "docker": {"server_version": "29.0.0"},
        "restic": {"version": "0.18.1"},
        "storage": {"backend": "plain"},
        "tls": {"mode": "off", "fingerprint": None},
        "policy": {
            "allowed_images": ["itzg/minecraft-server", "docker.io/itzg/minecraft-server"],
            "networks": ["mcpanel_instances"],
            "port_min": 1024,
            "port_max": 65535,
            "max_memory_mb": 65536,
            "runtime_uid": agent_settings.runtime_uid,
            "runtime_gid": agent_settings.runtime_gid,
        },
    }
    assert {k: body[k] for k in pinned} == pinned
    # the features of before keep their order at the front
    assert body["features"][:4] == ["retire", "pull_newer", "ping", "clone_level_name"]
