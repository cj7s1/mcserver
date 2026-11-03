"""Translate an Instance into the agent's container spec (itzg/minecraft-server env)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.conf import settings

from apps.core.models import SystemSetting

from .models import Instance, ServerType

# Environment variables the panel manages; advanced users cannot override them.
MANAGED_ENV = {
    "EULA",
    "TYPE",
    "VERSION",
    "MEMORY",
    "INIT_MEMORY",
    "MAX_MEMORY",
    "JVM_OPTS",
    "USE_AIKAR_FLAGS",
    "USE_MEOWICE_FLAGS",
    "FABRIC_LOADER_VERSION",
    "QUILT_LOADER_VERSION",
    "FORGE_VERSION",
    "NEOFORGE_VERSION",
    "PAPER_BUILD",
    "PURPUR_BUILD",
    "FOLIABUILD",
    "MODRINTH_MODPACK",
    "MODRINTH_VERSION",
    "MODRINTH_PROJECT",
    "MODRINTH_VERSION_ID",
    "CF_API_KEY",
    "CF_SLUG",
    "CF_FILE_ID",
    "CF_PAGE_URL",
    "FTB_MODPACK_ID",
    "FTB_MODPACK_VERSION_ID",
    "STOP_DURATION",
    "MODPACK_PLATFORM",
    "MOD_PLATFORM",
    "UID",
    "GID",
    "SERVER_PORT",
    "RCON_PORT",
    "ENABLE_RCON",
    "RCON_PASSWORD",
    "RCON_PASSWORD_FILE",
    "SKIP_SUDO",
    "LOAD_ENV_FROM_FILE",
    "LOAD_ENV_FROM_ARCHIVE",
    "LOAD_ENV_FROM_GENERIC_PACK",
}

LOADER_ENV = {
    ServerType.FABRIC: "FABRIC_LOADER_VERSION",
    ServerType.QUILT: "QUILT_LOADER_VERSION",
    ServerType.FORGE: "FORGE_VERSION",
    ServerType.NEOFORGE: "NEOFORGE_VERSION",
    ServerType.PAPER: "PAPER_BUILD",
    ServerType.PURPUR: "PURPUR_BUILD",
    ServerType.FOLIA: "FOLIABUILD",
}


def image_for(java_version: int) -> str:
    repo = SystemSetting.get("runtime_image_repository") or "itzg/minecraft-server"
    channel = str(SystemSetting.get("runtime_image_channel") or "stable")
    if channel == "latest":
        tag = f"java{java_version}"
    elif channel.startswith("pinned:"):
        tag = f"{channel.split(':', 1)[1]}-java{java_version}"
    else:
        tag = f"stable-java{java_version}"
    return f"{repo}:{tag}"


def build_env(instance: Instance) -> dict[str, str]:
    env: dict[str, str] = {}
    # advanced overrides first so managed keys always win
    for key, value in (instance.env or {}).items():
        if key.upper() not in MANAGED_ENV:
            env[str(key)] = str(value)

    st = instance.server_type
    env["EULA"] = "TRUE"
    env["TYPE"] = st
    env["MEMORY"] = f"{instance.memory_mb}M"
    env["STOP_DURATION"] = str(max(10, instance.stop_timeout - 20))
    tz = SystemSetting.get("timezone") or settings.TIME_ZONE
    if tz and "TZ" not in env:
        env["TZ"] = tz
    if instance.jvm_args.strip():
        env["JVM_OPTS"] = " ".join(instance.jvm_args.split())
    if instance.jvm_flags_preset == "aikar":
        env["USE_AIKAR_FLAGS"] = "true"
    elif instance.jvm_flags_preset == "meowice" and instance.java_version >= 17:
        env["USE_MEOWICE_FLAGS"] = "true"

    if st == ServerType.MODRINTH:
        env["MODRINTH_MODPACK"] = instance.modpack_project_id or instance.modpack_slug
        if instance.modpack_version_id:
            env["MODRINTH_VERSION"] = instance.modpack_version_id
        if instance.minecraft_version and instance.minecraft_version.upper() != "LATEST":
            env["VERSION"] = instance.minecraft_version
        if instance.modpack_loader in ("fabric", "forge", "quilt", "neoforge"):
            env["MODRINTH_LOADER"] = instance.modpack_loader
    elif st == ServerType.AUTO_CURSEFORGE:
        if instance.modpack_slug:
            env["CF_SLUG"] = instance.modpack_slug
        if instance.modpack_version_id:
            env["CF_FILE_ID"] = instance.modpack_version_id
        key = SystemSetting.get_secret("curseforge_api_key") or settings.CURSEFORGE_API_KEY or ""
        if key:
            env["CF_API_KEY"] = key
    elif st == ServerType.FTBA:
        env["FTB_MODPACK_ID"] = instance.modpack_project_id
        if instance.modpack_version_id:
            env["FTB_MODPACK_VERSION_ID"] = instance.modpack_version_id
    else:
        env["VERSION"] = instance.minecraft_version or "LATEST"
        loader_key = LOADER_ENV.get(ServerType(st)) if st in ServerType.values else None
        if loader_key and instance.loader_version:
            env[loader_key] = instance.loader_version
    return env


def build_spec(instance: Instance) -> dict[str, Any]:
    ports = [{"container_port": 25565, "host_port": instance.game_port, "protocol": "tcp", "host_ip": instance.host_ip}]
    for extra in instance.extra_ports or []:
        ports.append(
            {
                "container_port": int(extra["container_port"]),
                "host_port": int(extra["host_port"]),
                "protocol": extra.get("protocol", "tcp"),
                "host_ip": extra.get("host_ip") or instance.host_ip,
            }
        )
    spec: dict[str, Any] = {
        "name": instance.name,
        "image": instance.image,
        "env": build_env(instance),
        "memory_limit_mb": instance.container_memory_mb,
        "ports": ports,
        "restart_policy": instance.restart_policy,
        "stop_timeout": instance.stop_timeout,
        "rcon_password": instance.rcon_password,
    }
    if instance.cpu_limit:
        spec["cpu_limit"] = instance.cpu_limit
    if instance.network:
        spec["network"] = instance.network
    return spec


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def current_spec_hash(instance: Instance) -> str:
    return spec_hash(build_spec(instance))


def restart_required(instance: Instance) -> bool:
    """True when the running container was created from an older configuration."""
    if not instance.container_spec_hash:
        return False
    return instance.container_spec_hash != current_spec_hash(instance)
