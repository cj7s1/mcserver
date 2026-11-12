"""A-5 and A-6 of the security review (2026-11-11): what the agent lets a spec expose, per game, and which
routes answer without a token. Adopted from the reviewers' PoC (audit-2026-11-09/security-review/a-exposure/
poc/test_exposure.py), turned around to assert the fixed behaviour. (A-4: tests/test_source_guard.py.)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcagent import docker_ops, main, profiles
from mcagent.config import Settings
from mcagent.errors import AgentError
from mcagent.schemas import InstanceSpec

from .test_more_games_pins import CASES as MORE
from .test_other_games_pins import CASES as OTHER

IID = uuid.UUID("5ec0a000-0000-4000-8000-00000000000a")
MINECRAFT = {
    "name": "mc",
    "image": "itzg/minecraft-server:stable-java21",
    "env": {"EULA": "TRUE", "TYPE": "VANILLA"},
    "memory_limit_mb": 2048,
    "rcon_password": "Q3vN8kLm2PzX7rTy5WcB1dFg4HjK6sAe",
    "ports": [{"container_port": 25565, "host_port": 25565}],
}
ALL: dict[str, tuple[dict[str, Any], list[str]]] = {"minecraft": (MINECRAFT, []), **OTHER, **MORE}
ALL.pop("satisfactory_unpublished")
ALL.pop("palworld_defaults")
# games whose control has no network listener: they keep the agent's network policy
NO_CONTROL_LISTENER = {"terraria_vanilla", "terraria_tmodloader", "valheim", "hkmp"}
# every control port of every game: (case, container port, protocol, what it is)
CONTROL = [
    ("minecraft", 25575, "tcp", "RCON"),
    ("factorio", 27015, "tcp", "RCON"),
    ("terraria_tshock", 7878, "tcp", "TShock REST"),
    ("palworld", 8212, "tcp", "REST API"),
    ("palworld", 25575, "tcp", "RCON (switched off)"),
    ("project_zomboid", 27015, "tcp", "RCON"),
    ("cs2", 27030, "tcp", "RCON (the game port's number over TCP)"),
    ("rust", 28016, "tcp", "WebRCON"),
    ("rust", 8080, "tcp", "WebRCON page (nginx)"),
    ("valheim", 80, "tcp", "status HTTP (off)"),
    ("valheim", 9001, "tcp", "supervisor HTTP (off)"),
]


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch):
    import os

    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)


def _settings(tmp_path: Path, **over: Any) -> Settings:
    return Settings(data_root=tmp_path / "data", token="t", runtime_uid=99, runtime_gid=100, **over)


def _build(tmp_path: Path, case: str, settings_over: dict[str, Any] | None = None, **spec_over: Any) -> dict:
    values, volumes = ALL[case]
    spec = InstanceSpec(**{**values, **spec_over})
    return docker_ops.build_create_kwargs(
        _settings(tmp_path, **(settings_over or {})), IID, spec, "h", volumes
    )


def _refused(
    tmp_path: Path, case: str, settings_over: dict[str, Any] | None = None, **spec_over: Any
) -> str | None:
    try:
        _build(tmp_path, case, settings_over, **spec_over)
    except AgentError as exc:
        return exc.code
    return None


@pytest.mark.parametrize(("case", "port", "proto", "what"), CONTROL)
def test_control_port_is_never_published(tmp_path, case, port, proto, what):
    ports = [*ALL[case][0].get("ports", []), {"container_port": port, "host_port": 40000, "protocol": proto}]
    assert _refused(tmp_path, case, ports=ports) == "port_not_allowed", what


@pytest.mark.parametrize("case", sorted(ALL))
def test_host_and_lan_networks_only_for_games_without_a_control_listener(tmp_path, case):
    """A-6: on the host network or a macvlan/ipvlan network nothing is "published", so never_publish does not
    hold there. Every game but Minecraft whose control port is a network listener stays on the instance
    network; the others (and Minecraft, unchanged) keep the agent's policy."""
    default = _refused(tmp_path, case, network="host")
    host = _refused(tmp_path, case, {"allow_host_network": True}, network="host")
    lan = _refused(tmp_path, case, {"allowed_networks": ["br0"]}, network="br0")
    if case == "minecraft" or case in NO_CONTROL_LISTENER:
        assert default == "network_not_allowed"  # the agent's default policy has only the instance network
        assert host is None and lan is None
    else:
        assert default == host == lan == "network_not_supported", case
    _build(tmp_path, case)  # the default network is always fine


def test_the_host_rule_holds_for_any_later_profile(tmp_path, monkeypatch):
    """Not only the flag: a profile with a network control that forgot instance_network_only is still
    refused the host network."""
    profile = profiles.get("factorio")
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    profiles.register(
        type(profile)(
            **{
                **{f: getattr(profile, f) for f in profile.__dataclass_fields__},
                "instance_network_only": False,
            }
        )
    )
    assert (
        _refused(tmp_path, "factorio", {"allow_host_network": True}, network="host")
        == "network_not_supported"
    )
    assert _refused(tmp_path, "factorio", {"allowed_networks": ["br0"]}, network="br0") is None


def test_which_profiles_are_instance_network_only():
    table = {
        profile.game
        + ("/" + "+".join(profile.variants) if profile.variants else ""): profile.instance_network_only
        for profile in profiles.all_profiles()
    }
    assert {key for key, only in table.items() if only} == {
        "factorio",
        "satisfactory",
        "terraria/tshock",
        "palworld",
        "project-zomboid",
        "cs2",
        "rust",
    }


def test_profiles_never_add_network_or_admin_caps():
    dangerous = {
        "NET_ADMIN",
        "NET_RAW",
        "SYS_ADMIN",
        "SYS_PTRACE",
        "SYS_MODULE",
        "MKNOD",
        "DAC_READ_SEARCH",
        "SYS_CHROOT",
    }
    for profile in profiles.all_profiles():
        assert not (set(profile.cap_add) & dangerous), profile.game


# --- A-5: no route answers without a token but health and the log stream (which checks it itself) -------
def test_no_schema_or_docs_route():
    unprotected = sorted(
        r.path for r in main.app.routes if not getattr(r, "dependencies", None) and r.path.startswith("/")
    )
    assert unprotected == ["/v1/health", "/v1/instances/{instance_id}/logs/stream"], unprotected
    client = TestClient(main.app)
    for path in ("/v1/openapi.json", "/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404, path
