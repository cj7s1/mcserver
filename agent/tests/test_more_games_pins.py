"""Pinned container arguments of the six more games (Palworld, Valheim, Project Zomboid, HKMP, CS2,
Rust), key order included: a change to what these containers get must be deliberate. Regenerate
only on purpose: ``MCPANEL_CAPTURE_PINS=1 uv run pytest tests/test_more_games_pins.py``.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from mcagent import docker_ops, main
from mcagent.config import Settings
from mcagent.profiles import rust
from mcagent.schemas import InstanceSpec

PIN_FILE = Path(__file__).parent / "pins" / "more_games_create_kwargs.json"
IID = uuid.UUID("6a1d2c3b-4e5f-4a7b-8c9d-0e1f2a3b4c5d")
PASSWORD = "Zr8Kq2Lm5Nx7Pv3Tw9Yb4Hc6Jd1Fg0Se"
RUST_IMAGE = f"didstopia/rust-server@{rust.IMAGE_DIGESTS[0]}"


def _udp(port: int) -> dict[str, Any]:
    return {"container_port": port, "host_port": port, "protocol": "udp"}


def _spec(**values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Pinned",
        "memory_limit_mb": 8192,
        "rcon_password": PASSWORD,
        "stop_timeout": 120,
        "restart_policy": "no",
    }
    base.update(values)
    return base


# name -> (spec arguments, the image's declared VOLUMEs)
CASES: dict[str, tuple[dict[str, Any], list[str]]] = {
    "palworld": (
        _spec(
            game="palworld",
            image="thijsvanloef/palworld-server-docker:v2.8.1",
            memory_limit_mb=16384,
            env={
                "TZ": "Europe/Berlin",
                "PORT": "8215",
                "PUBLIC_PORT": "8215",
                "PLAYERS": "16",
                "SERVER_NAME": "Pal Island",
                "COMMUNITY": "false",
                "ADMIN_PASSWORD": "admin-Secret_123",
                "EXP_RATE": "1.5",
                "IS_PVP": "False",
            },
            ports=[_udp(8215)],
            stop_timeout=60,
        ),
        [],
    ),
    "palworld_defaults": (
        _spec(
            game="palworld", image="thijsvanloef/palworld-server-docker:v2.8.1", env={}, ports=[_udp(8211)]
        ),
        [],
    ),
    "valheim": (
        _spec(
            game="valheim",
            image="ghcr.io/community-valheim-tools/valheim-server:1.4.1",
            env={
                "TZ": "UTC",
                "SERVER_NAME": "Viking Hall",
                "SERVER_PORT": "2460",
                "WORLD_NAME": "Midgard",
                "SERVER_PASS": "skal-2026",
                "SERVER_PUBLIC": "false",
                "CROSSPLAY": "true",
                "SERVER_ARGS": "-saveinterval 600 -preset hard",
            },
            ports=[_udp(2460), _udp(2461), _udp(2462)],
        ),
        [],
    ),
    "project_zomboid": (
        _spec(
            game="project-zomboid",
            image="indifferentbroccoli/projectzomboid-server-docker:v1.2.0",
            env={
                "TZ": "UTC",
                "SERVER_NAME": "knox",
                "DEFAULT_PORT": "16300",
                "UDP_PORT": "16301",
                "MAX_PLAYERS": "12",
                "MEMORY_XMX_GB": "6",
                "ADMIN_PASSWORD": "zombie-Admin-1",
            },
            ports=[_udp(16300), _udp(16301)],
        ),
        [],
    ),
    "hkmp": (
        _spec(
            game="hkmp",
            image="maximalmax90/hkmpserver:v2.4.3-2",
            memory_limit_mb=512,
            env={"TZ": "UTC", "PORT": "2223", "FULL_SYNC": "true"},
            ports=[_udp(2223)],
            stop_timeout=40,
        ),
        ["/data"],
    ),
    "cs2": (
        _spec(
            game="cs2",
            image="joedwards32/cs2:5.0.1",
            memory_limit_mb=4096,
            env={
                "TZ": "UTC",
                "CS2_SERVERNAME": "Dust Club",
                "CS2_PORT": "27030",
                "TV_PORT": "27031",
                "TV_ENABLE": "1",
                "CS2_MAXPLAYERS": "12",
                "CS2_GAMEALIAS": "competitive",
                "CS2_STARTMAP": "de_dust2",
                "CS2_LAN": "0",
                "SRCDS_TOKEN": "0123456789ABCDEF0123456789ABCDEF",
            },
            ports=[_udp(27030), _udp(27031)],
            stop_timeout=30,
        ),
        [],
    ),
    "rust": (
        _spec(
            game="rust",
            image=RUST_IMAGE,
            memory_limit_mb=16384,
            env={
                "TZ": "UTC",
                "RUST_SERVER_NAME": "Wipe Day",
                "RUST_SERVER_IDENTITY": "main",
                "RUST_SERVER_PORT": "28100",
                "RUST_SERVER_QUERYPORT": "28101",
                "RUST_APP_PORT": "28102",
                "RUST_SERVER_WORLDSIZE": "3500",
                "RUST_SERVER_SEED": "12345",
                "RUST_SERVER_MAXPLAYERS": "50",
            },
            ports=[
                _udp(28100),
                _udp(28101),
                {"container_port": 28102, "host_port": 28102, "protocol": "tcp"},
            ],
            stop_timeout=180,
        ),
        [],
    ),
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        host_data_root=Path("/mnt/cache/appdata/mcpanel"),
        token="t",
        runtime_uid=99,
        runtime_gid=100,
    )


def _capture(tmp_path: Path) -> dict[str, Any]:
    settings = _settings(tmp_path)
    out: dict[str, Any] = {}
    for name, (values, volumes) in CASES.items():
        spec = InstanceSpec(**values)
        spec_hash = main._spec_hash(spec)
        kw = docker_ops.build_create_kwargs(settings, IID, spec, spec_hash, volumes)
        out[name] = {"spec_hash": spec_hash, "kwargs": json.loads(json.dumps(kw))}
    return out


@pytest.fixture
def pins(tmp_path, monkeypatch):
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    if os.environ.get("MCPANEL_CAPTURE_PINS") == "1":
        PIN_FILE.write_text(json.dumps(_capture(tmp_path), indent=1) + "\n")
    return json.loads(PIN_FILE.read_text())


def test_every_case_is_pinned(pins):
    assert set(pins) == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_create_kwargs_are_unchanged(name, pins, tmp_path):
    pinned = pins[name]
    values, volumes = CASES[name]
    spec = InstanceSpec(**values)
    assert main._spec_hash(spec) == pinned["spec_hash"]
    kw = docker_ops.build_create_kwargs(_settings(tmp_path), IID, spec, pinned["spec_hash"], volumes)
    got = json.loads(json.dumps(kw))
    assert got == pinned["kwargs"]
    assert list(got) == list(pinned["kwargs"])
    for key in ("environment", "labels", "volumes", "ports"):
        assert list(got[key]) == list(pinned["kwargs"][key]), key
