"""Fixture specs whose container arguments are pinned (tests/test_games_pins.py).

Multi-game phase 1 moves the Minecraft container code into a runtime profile and a control
driver; the container Docker gets for every one of these specs must stay byte for byte the same.
The expected values in ``pins/create_kwargs.json`` were captured from the code before the move.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from mcagent.config import Settings
from mcagent.schemas import InstanceSpec

IID = uuid.UUID("5e1f0c2a-7b3d-4e8f-9a01-23456789abcd")
PASSWORD = "pinned-rcon-password-0123456789"
STABLE = "itzg/minecraft-server:stable-java21"
PORT = {"container_port": 25565, "host_port": 25565}
JVM = "-XX:+UseG1GC -XX:MaxGCPauseMillis=200"


def _base(**over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "Survival",
        "image": STABLE,
        "memory_limit_mb": 2560,
        "rcon_password": PASSWORD,
        "stop_timeout": 60,
        "ports": [PORT],
        "env": {},
    }
    values.update(over)
    return values


def _env(server_type: str, **extra: str) -> dict[str, str]:
    """What the panel's spec.build_env sends for a server of this type (backend/apps/instances/spec.py)."""
    env = {
        "EULA": "TRUE",
        "TYPE": server_type,
        "MEMORY": "2048M",
        "STOP_DURATION": "40",
        "CREATE_CONSOLE_IN_PIPE": "true",
        "TZ": "Europe/Berlin",
    }
    env.update(extra)
    return env


# name -> (InstanceSpec arguments, settings variant)
CASES: dict[str, tuple[dict[str, Any], str]] = {
    "vanilla": (_base(env=_env("VANILLA", VERSION="1.21.10")), "plain"),
    "paper_extra_ports": (
        _base(
            name="Paper with Dynmap",
            env=_env("PAPER", VERSION="1.21.8", PAPER_BUILD="61"),
            ports=[
                PORT,
                {"container_port": 8123, "host_port": 25600, "protocol": "tcp"},
                {"container_port": 24454, "host_port": 25601, "protocol": "udp", "host_ip": "192.168.1.5"},
            ],
        ),
        "plain",
    ),
    "fabric": (
        _base(env=_env("FABRIC", VERSION="1.21.1", FABRIC_LOADER_VERSION="0.16.10"), memory_limit_mb=4096),
        "plain",
    ),
    "forge_old_java": (
        _base(
            image="itzg/minecraft-server:stable-java8",
            env=_env("FORGE", VERSION="1.12.2", FORGE_VERSION="14.23.5.2860"),
            stop_timeout=120,
        ),
        "plain",
    ),
    "modrinth_pack": (
        _base(
            name="Cobblemon",
            memory_limit_mb=8192,
            env=_env(
                "MODRINTH",
                MODRINTH_MODPACK="5FFgwNNP",
                MODRINTH_VERSION="kmmVnf3k",
                VERSION="1.21.1",
                MODRINTH_LOADER="fabric",
                MODRINTH_IGNORE_MISSING_FILES="mods/old.jar\nconfig/x.toml",
            ),
        ),
        "plain",
    ),
    "curseforge_pack": (
        _base(
            name="ATM10",
            memory_limit_mb=12288,
            env=_env(
                "AUTO_CURSEFORGE",
                CF_SLUG="all-the-mods-10",
                CF_FILE_ID="6487543",
                CF_API_KEY="$2a$10$not-a-real-key",
                CF_DOWNLOADS_REPO="/data/downloads",
                CF_IGNORE_MISSING_FILES="mods/removed.jar",
            ),
        ),
        "plain",
    ),
    "ftb_pack": (
        _base(env=_env("FTBA", FTB_MODPACK_ID="126", FTB_MODPACK_VERSION_ID="100155")),
        "plain",
    ),
    "imported": (
        _base(name="Imported world", env=_env("PAPER", VERSION="1.20.4"), restart_policy="on-failure"),
        "plain",
    ),
    "user_env_and_eula": (
        _base(
            env={**_env("PAPER", VERSION="1.21.10"), "EULA": "false", "MOTD": "Hello", "DIFFICULTY": "hard"}
        ),
        "plain",
    ),
    "jvm_flags": (
        _base(env=_env("PURPUR", VERSION="1.21.4", JVM_OPTS=JVM, USE_AIKAR_FLAGS="true")),
        "plain",
    ),
    "cpu_limit": (_base(env=_env("VANILLA", VERSION="LATEST"), cpu_limit=2.5), "plain"),
    "pinned_channel": (
        _base(image="itzg/minecraft-server:2025.10.1-java21", env=_env("VANILLA", VERSION="1.21.10")),
        "plain",
    ),
    "no_env_restart_no": (_base(env={}, ports=[], restart_policy="no", stop_timeout=5), "plain"),
    "custom_healthcheck_network": (
        _base(
            env=_env("VANILLA", VERSION="1.21.10"),
            healthcheck={"interval_s": 30, "timeout_s": 10, "start_period_s": 600, "retries": 3},
            network="mc_extra",
        ),
        "networks",
    ),
    "host_network": (_base(env=_env("VANILLA", VERSION="1.21.10"), network="host"), "host"),
    "proxy_and_certs": (_base(env={**_env("VANILLA", VERSION="1.21.10"), "HTTPS_PROXY": "user"}), "proxy"),
    "docker_io_long_stop": (
        _base(image="docker.io/itzg/minecraft-server:java25", env=_env("VANILLA"), stop_timeout=900),
        "plain",
    ),
}


def settings_for(variant: str, tmp_path: Path) -> Settings:
    common: dict[str, Any] = {
        "data_root": tmp_path / "data",
        "host_data_root": Path("/mnt/cache/appdata/mcpanel"),
        "token": "t",
        "runtime_uid": 99,
        "runtime_gid": 100,
    }
    if variant == "networks":
        common["allowed_networks"] = ["mc_extra"]
    elif variant == "host":
        common["allow_host_network"] = True
    elif variant == "proxy":
        common.update(
            runtime_extra_env={"HTTPS_PROXY": "http://proxy:3128", "NO_PROXY": "localhost"},
            runtime_ca_bundle=Path("/etc/ssl/ca.crt"),
            runtime_java_truststore=Path("/etc/ssl/cacerts"),
            shutdown_stop_timeout=25,
            pids_limit=4096,
            log_max_size="50m",
            log_max_files=3,
        )
    return Settings(**common)


def spec_for(name: str) -> InstanceSpec:
    return InstanceSpec(**CASES[name][0])
