"""Pins of the games before the six new ones (Minecraft's catalog entry, Factorio, Terraria with its three
variants, Satisfactory): their catalog entries and container specs stay exactly what they were when
Palworld, Valheim, Project Zomboid, HKMP, Counter-Strike 2 and Rust were added (contract
games-6-contract.md: no behaviour change for the existing games).

``tests/other_games_pins.json`` holds the expected values. ``MCPANEL_WRITE_PINS=1`` rewrites it from the
current code (only when a change of one of these games is intended).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from apps import games
from apps.instances.spec import build_spec

PINS = Path(__file__).with_name("other_games_pins.json")
EXISTING = ("minecraft", "factorio", "terraria", "satisfactory")


def _servers(make_instance: Any) -> dict[str, Any]:
    common = {"jvm_flags_preset": "none", "rcon_password": "p" * 32, "host_ip": "0.0.0.0"}
    out = {
        "factorio": make_instance(
            name="Factory",
            game="factorio",
            server_type="",
            minecraft_version="2.1.23",
            image="factoriotools/factorio:2.1.23-rootless",
            memory_mb=2048,
            stop_timeout=120,
            game_port=34197,
            ports=[{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34197}],
            game_options={"space_age": True, "save_name": "world"},
            **common,
        ),
        "satisfactory-closed": make_instance(
            name="Ficsit",
            game="satisfactory",
            server_type="",
            minecraft_version="stable",
            image="wolveix/satisfactory-server:v1.9.11",
            memory_mb=12288,
            stop_timeout=120,
            restart_policy="no",
            game_port=7777,
            ports=[
                {"name": "game", "container_port": 7777, "protocol": "udp", "host_port": 7777},
                {"name": "api", "container_port": 7777, "protocol": "tcp", "host_port": 7777},
                {"name": "messaging", "container_port": 8888, "protocol": "tcp", "host_port": 8888},
            ],
            game_options={"access": "open", "launch": {"max_players": 6}},
            **common,
        ),
    }
    out["satisfactory-open"] = make_instance(
        name="Ficsit open",
        game="satisfactory",
        server_type="",
        minecraft_version="stable",
        image="wolveix/satisfactory-server:v1.9.11",
        memory_mb=12288,
        stop_timeout=120,
        game_port=7787,
        ports=[
            {"name": "game", "container_port": 7777, "protocol": "udp", "host_port": 7787},
            {"name": "api", "container_port": 7777, "protocol": "tcp", "host_port": 7787},
            {"name": "messaging", "container_port": 8888, "protocol": "tcp", "host_port": 8898},
        ],
        game_options={"access": "password"},
        setup_state={"ports_open": True},
        **common,
    )
    versions = {"vanilla": "1.4.5.7", "tmodloader": "2026.09.3.1", "tshock": "1.4.5.8-6.2.1"}
    for i, (variant, version) in enumerate(versions.items()):
        out[f"terraria-{variant}"] = make_instance(
            name=f"Terra {variant}",
            game="terraria",
            server_type=variant,
            minecraft_version=version,
            image="x",
            memory_mb=1536,
            stop_timeout=60,
            game_port=7777 + i,
            ports=[{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7777 + i}],
            game_options={"world_name": "world", "owner_account": "owner", "modpack": "mcpanel"},
            **common,
        )
    return out


def _current(make_instance: Any) -> dict[str, Any]:
    catalog = {d["id"]: d for d in games.catalog() if d["id"] in EXISTING}
    specs = {key: build_spec(inst) for key, inst in _servers(make_instance).items()}
    return json.loads(json.dumps({"catalog": catalog, "specs": specs}, sort_keys=True))


@pytest.fixture
def pinned_tz(settings: Any) -> None:
    settings.TIME_ZONE = "Europe/Berlin"


def test_existing_games_are_pinned(make_instance, pinned_tz):
    current = _current(make_instance)
    if os.environ.get("MCPANEL_WRITE_PINS") == "1":
        PINS.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    expected = json.loads(PINS.read_text())
    assert sorted(current["catalog"]) == sorted(EXISTING)
    for game in EXISTING:
        assert current["catalog"][game] == expected["catalog"][game], game
    assert current["specs"] == expected["specs"]
