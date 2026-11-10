"""The Minecraft runtime profile: itzg/minecraft-server exactly as the agent has always run it.

Everything here reproduces what ``docker_ops.build_create_kwargs`` did before multi-game
support (tests/test_games_pins.py pins it): the itzg environment, ``/data``, the six
capabilities, ``mc-health``, SIGTERM, the ``mc-`` hostname, stdin open and no TTY.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..drivers.minecraft import GAME_PORT, RCON_PORT, SECRET_ENV
from ..schemas import RESERVED_ENV
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "minecraft"
CONTROL = "minecraft-rcon"
DEFAULT_IMAGES = ("itzg/minecraft-server", "docker.io/itzg/minecraft-server")

# --- the environment policy, re-validated on the agent ------------------------------------------------
# The panel refuses these from users (backend/apps/instances/spec.py, env_override_error): they
# install unverified code, run commands, grant operator rights, print the RCON password into the
# log, weaken built-in protection or override what the panel manages. The panel never sends them,
# except the ones it sets itself (PANEL_SET_ENV); so the agent refuses the same families, and a
# compromised panel cannot use them either. Keep in step with the panel's lists.
BLOCKED_EXACT = frozenset(
    {
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
)
BLOCKED_PREFIXES = (
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
BLOCKED_SUFFIXES = (
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
# Pack tuning users may set: it only selects among files of the pinned, already verified pack.
ALLOWED_PACK_TUNING = frozenset(
    {
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
)
# Variables of the refused families that the panel's spec.build_env sets itself, from dedicated
# settings (JVM arguments, the pinned pack, the CurseForge API key and download folder).
PANEL_SET_ENV = frozenset(
    {
        "JVM_OPTS",
        "CF_SLUG",
        "CF_FILE_ID",
        "CF_API_KEY",
        "CF_DOWNLOADS_REPO",
        "FTB_MODPACK_ID",
        "FTB_MODPACK_VERSION_ID",
    }
)


def env_policy(key: str) -> str | None:
    """Why a spec may not set ``key`` for an itzg container; None when it may."""
    name = key.upper()
    if name in PANEL_SET_ENV or name in ALLOWED_PACK_TUNING:
        return None
    if name in BLOCKED_EXACT or name.startswith(BLOCKED_PREFIXES) or name.endswith(BLOCKED_SUFFIXES):
        return (
            f"Environment variable {key} is not allowed: it would install unverified code, run "
            "commands, grant player rights or reveal secrets"
        )
    return None


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    """itzg's variables, in the order the agent has always set them."""
    return {
        "EULA": spec.env.get("EULA", "TRUE"),
        str(profile.user.uid_env): str(settings.runtime_uid),
        str(profile.user.gid_env): str(settings.runtime_gid),
        "ENABLE_RCON": "true",
        SECRET_ENV: spec.rcon_password,
        "RCON_PORT": str(RCON_PORT),
        "SERVER_PORT": str(GAME_PORT),
    }


PROFILE = RuntimeProfile(
    game=GAME,
    label="Minecraft",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("drop_privileges_env", uid_env="UID", gid_env="GID"),
    # The itzg entrypoint starts as root to fix ownership, then drops to UID/GID.
    cap_add=("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"),
    mounts=(Mount(".", "/data"),),
    injected_env=injected_env,
    reserved_env=frozenset(RESERVED_ENV),
    env_policy=env_policy,
    container_ports=((GAME_PORT, "tcp"),),
    # Dynmap, BlueMap, voice chat and the like listen on ports of their own.
    allow_extra_ports=True,
    never_publish=((RCON_PORT, None),),
    health=Health("cmd", argv=("mc-health",)),
    # mc-server-runner turns SIGTERM into `stop`.
    stop_signal="SIGTERM",
    tty=False,
    # Kept for itzg's console pipe; nothing writes to the container's stdin.
    stdin_open=True,
    hostname_prefix="mc-",
    # = fetch.DEFAULT_ALLOWED_HOSTS (tests/test_games.py keeps them equal)
    fetch_hosts=(
        "cdn.modrinth.com",
        "cdn-raw.modrinth.com",
        "edge.forgecdn.net",
        "mediafilez.forgecdn.net",
        "media.forgecdn.net",
        "hangarcdn.papermc.io",
    ),
    control=CONTROL,
    java_truststore=True,
)
