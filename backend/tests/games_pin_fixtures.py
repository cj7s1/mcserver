"""Fixture servers whose container spec, spec hash and API representation are pinned by
tests/test_games_phase1_pins.py (multi-game phase 1: Minecraft must not change at all).

Each entry is (Instance field values, system settings, Django settings) for one server. The
values are literal on purpose: the expected outputs were recorded from the code before the game
definition layer existed, and must stay byte-identical after it.
"""

from __future__ import annotations

from typing import Any

RCON = "pinned-rcon-password-0123456789abcdef"

BASE: dict[str, Any] = {
    "name": "Pinned",
    "game_port": 25600,
    "server_type": "VANILLA",
    "minecraft_version": "1.20.4",
    "java_version": 17,
    "image": "itzg/minecraft-server:stable-java17",
    "memory_mb": 2048,
    "jvm_flags_preset": "none",
    "stop_timeout": 90,
    "rcon_password": RCON,
}

# name -> (instance fields over BASE, SystemSetting values, Django settings)
FIXTURES: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {
    "vanilla": ({}, {}, {}),
    "paper": (
        {
            "server_type": "PAPER",
            "minecraft_version": "1.21.1",
            "loader_version": "130",
            "java_version": 21,
            "jvm_flags_preset": "aikar",
        },
        {},
        {},
    ),
    "fabric": (
        {
            "server_type": "FABRIC",
            "minecraft_version": "1.21.1",
            "loader_version": "0.16.14",
            "java_version": 21,
            "jvm_flags_preset": "meowice",
            "memory_mb": 4096,
        },
        {},
        {},
    ),
    "forge": (
        {
            "server_type": "FORGE",
            "minecraft_version": "1.20.1",
            "loader_version": "47.3.0",
            "memory_mb": 6144,
            "stop_timeout": 120,
        },
        {},
        {},
    ),
    "modrinth_pack": (
        {
            "server_type": "MODRINTH",
            "source": "modrinth",
            "minecraft_version": "1.20.1",
            "modpack_provider": "modrinth",
            "modpack_project_id": "1KVo5zza",
            "modpack_slug": "fabulously-optimized",
            "modpack_name": "Fabulously Optimized",
            "modpack_version_id": "abcd1234",
            "modpack_version_name": "5.0.0",
            "modpack_loader": "fabric",
            "loader_version": "0.16.14",
            "memory_mb": 6144,
            "modpack_meta": {"removed_pack_files": {"release": "abcd1234", "paths": ["mods/old.jar", "mods/client-only.jar"]}},
        },
        {},
        {},
    ),
    "curseforge_pack": (
        {
            "server_type": "AUTO_CURSEFORGE",
            "source": "curseforge",
            "minecraft_version": "1.21.1",
            "modpack_provider": "curseforge",
            "modpack_project_id": "925200",
            "modpack_slug": "all-the-mods-10",
            "modpack_name": "All the Mods 10",
            "modpack_version_id": "6543210",
            "modpack_loader": "neoforge",
            "java_version": 21,
            "memory_mb": 8192,
            "env": {"CF_EXCLUDE_MODS": "foo,bar", "CF_IGNORE_MISSING_FILES": "mods/user.jar"},
            "modpack_meta": {"removed_pack_files": {"release": "6543210", "paths": ["mods/pack.jar"]}},
        },
        {},
        {"CURSEFORGE_API_KEY": "pinned-cf-key"},
    ),
    "ftb_pack": (
        {
            "server_type": "FTBA",
            "source": "ftb",
            "minecraft_version": "1.20.1",
            "modpack_provider": "ftb",
            "modpack_project_id": "119",
            "modpack_version_id": "100051",
            "modpack_loader": "forge",
        },
        {},
        {},
    ),
    "imported": (
        {"source": "imported", "server_type": "CUSTOM", "minecraft_version": "LATEST", "java_version": 21, "loader_version": ""},
        {},
        {},
    ),
    "extra_ports": (
        {
            "host_ip": "192.168.1.10",
            "extra_ports": [
                {"container_port": 24454, "host_port": 24454, "protocol": "udp", "host_ip": "", "label": "voice"},
                {"container_port": 8100, "host_port": 8100, "protocol": "tcp", "host_ip": "10.0.0.5", "label": "map"},
            ],
        },
        {},
        {},
    ),
    "user_env": (
        {"env": {"MOTD": "Hello", "DIFFICULTY": "hard", "TZ": "Europe/Berlin", "RCON_PASSWORD": "dropped", "type": "PAPER"}},
        {"timezone": "America/New_York"},
        {},
    ),
    "jvm_flags": (
        {"jvm_flags_preset": "aikar", "jvm_args": "  -XX:+UseG1GC \n  -Dfoo=bar ", "memory_overhead_mb": 1000},
        {},
        {},
    ),
    "cpu_limit": ({"cpu_limit": 2.5, "network": "mcpanel_extra", "restart_policy": "on-failure"}, {}, {}),
    "pinned_channel": (
        {"java_version": 25, "minecraft_version": "1.21.10"},
        {"runtime_image_channel": "pinned:2026.10.1", "runtime_image_repository": "docker.io/itzg/minecraft-server"},
        {},
    ),
    "latest_channel": ({"java_version": 8, "minecraft_version": "1.12.2"}, {"runtime_image_channel": "latest"}, {}),
}
