"""Translate an Instance into the agent's container spec (itzg/minecraft-server env)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from django.conf import settings

from apps.core.crypto import is_unreadable
from apps.core.models import SystemSetting
from apps.core.utils import random_secret

from .models import Instance, ServerType

log = logging.getLogger(__name__)

# Environment variables the panel manages itself (dedicated settings).
_PANEL_ENV = {
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
    "MODRINTH_LOADER",
    "CF_API_KEY",
    "CF_API_KEY_FILE",
    "CF_SLUG",
    "CF_FILE_ID",
    "CF_PAGE_URL",
    "FTB_MODPACK_ID",
    "FTB_MODPACK_VERSION_ID",
    "STOP_DURATION",
    "CREATE_CONSOLE_IN_PIPE",
    "CONSOLE_IN_NAMED_PIPE",
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
    "CF_DOWNLOADS_REPO",
    "LOAD_ENV_FROM_FILE",
    "LOAD_ENV_FROM_ARCHIVE",
    "LOAD_ENV_FROM_GENERIC_PACK",
    "SKIP_SERVER_PROPERTIES",
    "OVERRIDE_SERVER_PROPERTIES",
}

# Extra variables (instance.env, permission instance.configure) may tune the runtime, but must
# not do what other permissions guard, nor anything nobody can review (docs/SECURITY.md,
# "Advanced environment variables"). Refused families, by exact name, prefix or suffix:
#  * install code or content from sources the content manager does not hash-verify
#    (instance.content): MODS/PLUGINS lists, SPIGET_RESOURCES, MODRINTH_PROJECTS,
#    CURSEFORGE_FILES, MODPACK zips, GENERIC_PACK(S), PACKWIZ_URL, DATAPACKS, WORLD, extra and
#    default config downloads, custom server jars and installers, every *_URL/*_REPO source;
#  * run commands or replace what is executed (instance.command): RCON_CMDS_*, the stop
#    command, pre-start scripts, SSH/WebSocket remote consoles, unauthenticated remote JMX
#    (ENABLE_JMX), the server runner, Java agents and JVM option variables (the JVM is
#    configured through the JVM settings), and variables the shell, the dynamic loader or the
#    JVM read on their own (PS4, BASH_ENV, LD_*, JAVA_TOOL_OPTIONS, ...);
#  * weaken built-in protection: SKIP_LOG4J_PATCHER (Log4Shell on old versions);
#  * print secrets into the console log that console viewers can read (DEBUG traces the
#    startup scripts with `set -x`, which prints the RCON password; DUMP_SERVER_PROPERTIES);
#  * grant operator status or edit the whitelist (instance.players): OPS*, WHITELIST lists;
#  * override panel-managed behaviour: RCON_*, the server.properties source, CF_*/FTB_* except
#    the pack tuning options below, LOAD_ENV_FROM_*.
_BLOCKED_EXACT = {
    "MODS",
    "PLUGINS",
    "MODPACK",
    "CURSEFORGE_FILES",
    "DATAPACKS",
    "WORLD",
    "APPLY_EXTRA_FILES",
    "DOWNLOAD_EXTRA_CONFIGS",
    "SERVER",
    "SERVER_RUNNER",
    "PRE_START_SCRIPT",
    "STOP_SERVER_DELAY_COMMAND",
    "ENABLE_SSH",
    "ENABLE_WEBSOCKET",
    "ENABLE_JMX",
    "DUMP_SERVER_PROPERTIES",
    "SKIP_LOG4J_PATCHER",
    "SCRIPTS",
    "USE_MODPACK_START_SCRIPT",
    "CLEANROOM_FROM_FILE",
    "OPS",
    "OPS_FILE",
    "EXISTING_OPS_FILE",
    "OVERRIDE_OPS",
    "APPEND_OPS",
    "WHITELIST",
    "WHITELIST_FILE",
    "EXISTING_WHITELIST_FILE",
    "OVERRIDE_WHITELIST",
    "APPEND_WHITELIST",
    "WHITELIST_PROP",
    "MODRINTH_PROJECTS",
    "VERSION_FROM_MODRINTH_PROJECTS",
    "PATH",
    "HOME",
    "ENV",
    "BASH_ENV",
    "SHELLOPTS",
    "BASHOPTS",
    "PS4",
    "PROMPT_COMMAND",
    "IFS",
    "GLOBIGNORE",
    "_JAVA_OPTIONS",
}
_BLOCKED_PREFIXES = (
    "MODS_",
    "PLUGINS_",
    "SPIGET_",
    "CURSEFORGE_",
    "GENERIC_PACK",
    "PACKWIZ_",
    "DATAPACKS_",
    "VANILLATWEAKS_",
    "WORLD_",
    "CUSTOM_",
    "COPY_",
    "RCON_",
    "SSH_",
    "WEBSOCKET_",
    "MC_SERVER_RUNNER",
    "JMX_",
    "DEBUG",
    "JVM_",
    "JAVA_",
    "JDK_",
    "LD_",
    "BASH_FUNC_",
    "LOAD_ENV_FROM_",
    "SERVER_PROPERTIES",
    "MANAGEMENT_SERVER_",
    "DOWNLOAD_",
    "CF_",
    "FTB_",
)
_BLOCKED_SUFFIXES = (
    "_URL",
    "_URLS",
    "_REPO",
    "_INSTALLER",
    "_CUSTOM_JAR",
    "_LAUNCHER",
    "_COMMAND",
    "_COMMANDS",
    "_SCRIPT",
)
# Pack tuning that only selects among files of the pinned, already verified pack.
_ALLOWED_PACK_TUNING = {
    "FTB_FORCE_REINSTALL",
    "CF_EXCLUDE_MODS",
    "CF_FORCE_INCLUDE_MODS",
    "CF_EXCLUDE_ALL_MODS",
    "CF_EXCLUDE_INCLUDE_FILE",
    "CF_FORCE_SYNCHRONIZE",
    "CF_OVERRIDES_EXCLUSIONS",
    "CF_OVERRIDES_SKIP_EXISTING",
    "CF_IGNORE_MISSING_FILES",
    "CF_SET_LEVEL_FROM",
    "CF_PARALLEL_DOWNLOADS",
    "CF_FORCE_REINSTALL_MODLOADER",
    "CF_API_CACHE_DEFAULT_TTL",
    "CF_USE_HTTP2",
    "CF_MOD_LOADER_VERSION",
}


def env_override_error(key: str) -> str | None:
    """Why an extra (instance.env) variable is refused, or None if it may be set."""
    name = str(key).upper()
    if name in _PANEL_ENV:
        return f"{key} is managed by MCPanel; use the dedicated setting instead"
    if name in _ALLOWED_PACK_TUNING:
        return None
    if name in _BLOCKED_EXACT or name.startswith(_BLOCKED_PREFIXES) or name.endswith(_BLOCKED_SUFFIXES):
        return (
            f"{key} is not allowed: it would install unverified code, run commands or grant player "
            "rights (use the mods/plugins manager, the console or the players page instead)"
        )
    return None


class _ReservedEnv(frozenset):
    """Panel-managed keys (iterating yields only those) whose ``in`` test also covers the
    refused families above, so every ``key in MANAGED_ENV`` check enforces the full policy."""

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and env_override_error(key) is not None


# Variables advanced users cannot set (see env_override_error); kept under this name because
# the serializer and build_env test membership with it.
MANAGED_ENV: frozenset[str] = _ReservedEnv(_PANEL_ENV)

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
    # Console input pipe. On SIGTERM mc-server-runner sends "stop" through rcon-cli and, when
    # RCON is not listening yet (a stop while the server is still starting), writes it to the
    # server console. Without the pipe that console is the container's read-only stdin: the
    # write fails ("bad file descriptor"), the server finishes starting and is killed after
    # STOP_DURATION. With it the runner keeps the server's stdin, the "stop" waits there and
    # the server shuts down cleanly once it reads its console. The pipe stays at the image's
    # default path inside the container (CONSOLE_IN_NAMED_PIPE is reserved too). Commands from
    # the panel keep going through RCON; nothing uses the container's stdin.
    env["CREATE_CONSOLE_IN_PIPE"] = "true"
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
        # Manually downloaded files (author-restricted mods) are uploaded to /data/downloads.
        env["CF_DOWNLOADS_REPO"] = "/data/downloads"
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


def rcon_password(instance: Instance) -> str:
    """The instance's RCON password; a new one if the stored one cannot be decrypted.

    After MCPANEL_SECRET_KEY changed without MCPANEL_SECRET_KEY_FALLBACKS the stored password
    is unreadable (apps/core/crypto.py). It is only ever used for the container the panel
    creates next (the agent reads the running container's password from its config), so a
    fresh one is saved: the current container keeps working and the next recreation, which the
    changed spec hash asks for, applies the new password.
    """
    current = instance.rcon_password
    if current and not is_unreadable(current):
        return current
    new = random_secret(32)
    instance.rcon_password = new
    if instance.pk and not instance._state.adding:
        Instance.objects.filter(pk=instance.pk).update(rcon_password=new)
        log.warning("Generated a new RCON password for server %s (the stored one was unreadable)", instance.pk)
    return new


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
        "rcon_password": rcon_password(instance),
    }
    if instance.cpu_limit:
        spec["cpu_limit"] = instance.cpu_limit
    if instance.network:
        spec["network"] = instance.network
    return spec


# How the agent turns a spec into a container, for changes that the spec itself does not show.
# The number is part of the hash "restart required" compares, so raising it makes every
# container created by an older release read as restart-required: it is recreated at its next
# start or restart (world data is never touched). It is mixed in here, on the panel side, and
# is not part of the spec sent to the agent (the agent's spec schema has no such field).
#   1  releases before the shutdown-safety fix (hash of the bare spec)
#   2  the agent gives Docker a stop timeout (Config.StopTimeout) when it creates the container
# The agent keeps the same list (agent/mcagent/docker_ops.py, CONTAINER_FORMAT) and reports the
# number it applied with every container it creates. The stored hash is made with that number
# (apply_container), not with the panel's: a container created by an agent that is older than
# the panel keeps reading restart-required until an up-to-date agent has recreated it.
CONTAINER_FORMAT = 2


def spec_hash(spec: dict[str, Any], container_format: int | None = None) -> str:
    """Hash of a spec as a container of ``container_format`` holds it (default: this release's)."""
    applied = CONTAINER_FORMAT if container_format is None else container_format
    # format 1 is the hash of the bare spec: what releases before the number stored
    hashed = spec if applied <= 1 else {**spec, "container_format": applied}
    return hashlib.sha256(json.dumps(hashed, sort_keys=True).encode()).hexdigest()[:16]


def applied_container_format(apply_result: dict[str, Any] | None) -> int:
    """The container format the agent used for the container it just created (result of an apply).

    Agents from before the number existed report none: format 1. An agent that is newer than
    the panel may report a higher number than the panel knows; that counts as the panel's own,
    or the server would read restart-required for ever.
    """
    reported = (apply_result or {}).get("container_format")
    if isinstance(reported, bool) or not isinstance(reported, int) or reported < 1:
        return 1
    return min(reported, CONTAINER_FORMAT)


def current_spec_hash(instance: Instance) -> str:
    return spec_hash(build_spec(instance))


def spec_outdated(instance: Instance) -> bool:
    """True when the container this server last got was created from another configuration (or
    container format) than the current one. Says nothing about whether that container still
    exists: the jobs ask the agent for that."""
    if not instance.container_spec_hash:
        return False
    return instance.container_spec_hash != current_spec_hash(instance)


def restart_required(instance: Instance) -> bool:
    """What the panel shows as "restart required": the server HAS a container, and it was created
    from an older configuration.

    A server without a container has nothing to restart: a stopped server's container is removed
    (reconcile.maybe_retire), and its next start creates one from the current settings anyway.
    Without this, every stopped server would read restart-required after an upgrade that raises
    CONTAINER_FORMAT, and after every change of a setting. Whether there is a container is what
    the reconciler last saw (``container_state``, empty without one).
    """
    return bool(instance.container_state) and spec_outdated(instance)
