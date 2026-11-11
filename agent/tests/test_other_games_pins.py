"""Pinned container arguments and ``/v1/info`` entries of the games that are not Minecraft.

Captured from the code before the six more games (Palworld, Valheim, Project Zomboid, HKMP, CS2,
Rust) were added: Factorio, Satisfactory and the three Terraria variants must keep getting exactly
the same container (key order included) and the same ``GET /v1/info`` entry. Regenerate only on
purpose: ``MCPANEL_CAPTURE_PINS=1 uv run pytest tests/test_other_games_pins.py``.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from mcagent import docker_ops, main, profiles
from mcagent.config import Settings
from mcagent.schemas import InstanceSpec

PIN_FILE = Path(__file__).parent / "pins" / "other_games_create_kwargs.json"
IID = uuid.UUID("0b8e3f6a-2c4d-4e5f-8a9b-0c1d2e3f4a5b")
PASSWORD = "Q3vN8kLm2PzX7rTy5WcB1dFg4HjK6sAe"


def _spec(**values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Pinned",
        "memory_limit_mb": 4096,
        "rcon_password": PASSWORD,
        "stop_timeout": 90,
        "restart_policy": "unless-stopped",
    }
    base.update(values)
    return base


# name -> (spec arguments, the image's declared VOLUMEs)
CASES: dict[str, tuple[dict[str, Any], list[str]]] = {
    "factorio": (
        _spec(
            game="factorio",
            image="factoriotools/factorio:2.1.24-rootless",
            env={"TZ": "Europe/Berlin", "DLC_SPACE_AGE": "true", "LOAD_LATEST_SAVE": "true"},
            ports=[{"container_port": 34197, "host_port": 34200, "protocol": "udp"}],
        ),
        ["/factorio"],
    ),
    "satisfactory": (
        _spec(
            game="satisfactory",
            image="wolveix/satisfactory-server:v1.9.11",
            memory_limit_mb=12288,
            env={"TZ": "UTC", "MAXPLAYERS": "4", "SERVERMESSAGINGPORT": "8890"},
            ports=[
                {"container_port": 7777, "host_port": 7790, "protocol": "tcp"},
                {"container_port": 7777, "host_port": 7790, "protocol": "udp"},
                {"container_port": 8890, "host_port": 8890, "protocol": "tcp"},
            ],
            restart_policy="no",
        ),
        [],
    ),
    "satisfactory_unpublished": (
        _spec(game="satisfactory", image="wolveix/satisfactory-server:v1.9.11", env={}, ports=[]),
        [],
    ),
    "terraria_vanilla": (
        _spec(
            game="terraria",
            variant="vanilla",
            image="passivelemon/terraria-docker:terraria-1.4.4.9",
            env={"TZ": "UTC"},
            ports=[{"container_port": 7777, "host_port": 7777, "protocol": "tcp"}],
        ),
        [],
    ),
    "terraria_tmodloader": (
        _spec(
            game="terraria",
            variant="tmodloader",
            image="passivelemon/terraria-docker:tmodloader-2025.8.3.1",
            env={"TZ": "UTC", "MODPACK": "MyPack"},
            ports=[{"container_port": 7777, "host_port": 7778, "protocol": "tcp"}],
        ),
        ["/opt/terraria/config"],
    ),
    "terraria_tshock": (
        _spec(
            game="terraria",
            variant="tshock",
            image="ryshe/terraria:tshock-1.4.4.9-5.2.4",
            env={"TZ": "UTC"},
            ports=[{"container_port": 7777, "host_port": 7779, "protocol": "tcp"}],
        ),
        ["/config", "/root/.local/share/Terraria/Worlds", "/tshock/ServerPlugins", "/tshock/logs"],
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
    out: dict[str, Any] = {"kwargs": {}, "info": {}}
    for name, (values, volumes) in CASES.items():
        spec = InstanceSpec(**values)
        spec_hash = main._spec_hash(spec)
        kw = docker_ops.build_create_kwargs(settings, IID, spec, spec_hash, volumes)
        out["kwargs"][name] = {"spec_hash": spec_hash, "kwargs": json.loads(json.dumps(kw))}
    games = profiles.info(settings)
    for game in ("factorio", "satisfactory", "terraria"):
        out["info"][game] = json.loads(json.dumps(games[game]))
    return out


@pytest.fixture
def pins(tmp_path, monkeypatch):
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    if os.environ.get("MCPANEL_CAPTURE_PINS") == "1":
        PIN_FILE.write_text(json.dumps(_capture(tmp_path), indent=1, sort_keys=False) + "\n")
    return json.loads(PIN_FILE.read_text())


def test_every_case_is_pinned(pins):
    assert set(pins["kwargs"]) == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_create_kwargs_are_unchanged(name, pins, tmp_path):
    pinned = pins["kwargs"][name]
    values, volumes = CASES[name]
    spec = InstanceSpec(**values)
    assert main._spec_hash(spec) == pinned["spec_hash"]
    kw = docker_ops.build_create_kwargs(_settings(tmp_path), IID, spec, pinned["spec_hash"], volumes)
    got = json.loads(json.dumps(kw))
    assert got == pinned["kwargs"]
    assert list(got) == list(pinned["kwargs"])
    for key in ("environment", "labels", "volumes", "ports"):
        assert list(got[key]) == list(pinned["kwargs"][key]), key


@pytest.mark.parametrize("game", ["factorio", "satisfactory", "terraria"])
def test_info_entries_are_unchanged(game, pins, tmp_path):
    assert json.loads(json.dumps(profiles.info(_settings(tmp_path))[game])) == pins["info"][game]
