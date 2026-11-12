"""The Palworld runtime profile: thijsvanloef/palworld-server-docker, an exact ``vX.Y.Z`` tag.

What the probe of 2026-11-10 observed (``v2.8.1``; design/probe-6games.md section 1):

* The image starts as root, changes ``steam`` to ``PUID``/``PGID``, ``chown -R`` /palworld and drops
  to it with ``setpriv``: it needs CHOWN DAC_OVERRIDE SETUID SETGID (FOWNER silences a harmless
  chmod error). The agent injects ``PUID``/``PGID`` = its runtime uid/gid.
* Everything lives under ``/palworld`` (no VOLUME): the 4.6 GB game (SteamCMD app 2394010, checked
  at every start when ``UPDATE_ON_BOOT`` is true; left out of backups) and ``Pal/Saved`` (config,
  ``SaveGames/0/<world guid>/``).
* ``PalWorldSettings.ini`` is generated from the environment at every start (envsubst, free text in
  double quotes: no ``"`` and no line breaks), so the panel's settings are variables with the
  patterns below; the image's other features are switched off (backups, auto update/reboot/pause,
  mods, Discord webhooks, player logging) and their variables refused.
* Ports: ``PORT`` (game, udp) and ``QUERY_PORT`` (udp; Palworld never answered A2S on it) are set
  1:1; the REST API (8212/tcp) and RCON (25575/tcp, switched off) are never published.
* Readiness: the log line ``Running Palworld dedicated server on :<port>``. Control: the
  ``palworld-api`` driver (REST, Basic ``admin:<ADMIN_PASSWORD>``). SIGTERM saves (exit 143).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from . import steam
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "palworld"
GAME_PORT = 8211
QUERY_PORT = 27015
REST_PORT = 8212
RCON_PORT = 25575
DEFAULT_IMAGES = ("thijsvanloef/palworld-server-docker", "docker.io/thijsvanloef/palworld-server-docker")
TAG_PATTERN = r"v\d{1,3}\.\d{1,3}\.\d{1,4}"
READY_PATTERN = r"Running Palworld dedicated server on :\d+"

RATE = r"\d{1,6}(?:\.\d{1,6})?"
INT = r"-?\d{1,9}"
# quoted free text in PalWorldSettings.ini: no double quote, no line break or other control character
FREE_TEXT = r'[^"\x00-\x1f\x7f]'

_RATES = (
    "DAYTIME_SPEEDRATE", "NIGHTTIME_SPEEDRATE", "EXP_RATE", "PAL_CAPTURE_RATE", "PAL_SPAWN_NUM_RATE",
    "PAL_DAMAGE_RATE_ATTACK", "PAL_DAMAGE_RATE_DEFENSE", "PLAYER_DAMAGE_RATE_ATTACK",
    "PLAYER_DAMAGE_RATE_DEFENSE", "PLAYER_STOMACH_DECREASE_RATE", "PLAYER_STAMINA_DECREASE_RATE",
    "PLAYER_AUTO_HP_REGEN_RATE", "PLAYER_AUTO_HP_REGEN_RATE_IN_SLEEP", "PAL_STOMACH_DECREASE_RATE",
    "PAL_STAMINA_DECREASE_RATE", "PAL_AUTO_HP_REGEN_RATE", "PAL_AUTO_HP_REGEN_RATE_IN_SLEEP",
    "BUILD_OBJECT_HP_RATE", "BUILD_OBJECT_DAMAGE_RATE", "BUILD_OBJECT_DETERIORATION_DAMAGE_RATE",
    "COLLECTION_DROP_RATE", "COLLECTION_OBJECT_HP_RATE", "COLLECTION_OBJECT_RESPAWN_SPEED_RATE",
    "ENEMY_DROP_ITEM_RATE", "DROP_ITEM_ALIVE_MAX_HOURS", "AUTO_RESET_GUILD_TIME_NO_ONLINE_PLAYERS",
    "PAL_EGG_DEFAULT_HATCHING_TIME", "WORK_SPEED_RATE", "AUTO_SAVE_SPAN", "ITEM_WEIGHT_RATE",
    "SERVER_REPLICATE_PAWN_CULL_DISTANCE", "EQUIPMENT_DURABILITY_DAMAGE_RATE",
    "ITEM_CONTAINER_FORCE_MARK_DIRTY_INTERVAL", "ITEM_CORRUPTION_MULTIPLIER",
    "PLAYER_DATA_PAL_STORAGE_UPDATE_CHECK_TICK_INTERVAL", "MONSTER_FARM_ACTION_SPEED_RATE",
    "AUTO_TRANSFER_MASTER_CHECK_INTERVAL_SECONDS", "BLOCK_RESPAWN_TIME", "RESPAWN_PENALTY_DURATION_THRESHOLD",
    "RESPAWN_PENALTY_TIME_SCALE", "VOICE_CHAT_MAX_VOLUME_DISTANCE", "VOICE_CHAT_ZERO_VOLUME_DISTANCE",
)  # fmt: skip
_INTS = (
    "DROP_ITEM_MAX_NUM", "DROP_ITEM_MAX_NUM_UNKO", "BASE_CAMP_MAX_NUM", "BASE_CAMP_WORKER_MAX_NUM",
    "GUILD_PLAYER_MAX_NUM", "BASE_CAMP_MAX_NUM_IN_GUILD", "COOP_PLAYER_MAX_NUM", "CHAT_POST_LIMIT_PER_MINUTE",
    "SUPPLY_DROP_SPAN", "MAX_BUILDING_LIMIT_NUM", "PHYSICS_ACTIVE_DROP_ITEM_MAX_NUM",
    "GUILD_REJOIN_COOLDOWN_MINUTES", "AUTO_TRANSFER_MASTER_THRESHOLD_DAYS", "MAX_GUILDS_PER_FRAME",
    "ADDITIONAL_DROP_ITEM_NUM_WHEN_PLAYER_KILLING_IN_PVP_MODE", "BUILDING_NAME_DISPLAY_CACHE_TTL_SECONDS",
)  # fmt: skip
_BOOLS = (
    "IS_RANDOMIZER_PAL_LEVEL_RANDOM", "ENABLE_PLAYER_TO_PLAYER_DAMAGE", "ENABLE_FRIENDLY_FIRE",
    "ENABLE_INVADER_ENEMY", "ACTIVE_UNKO", "ENABLE_AIM_ASSIST_PAD", "ENABLE_AIM_ASSIST_KEYBOARD",
    "AUTO_RESET_GUILD_NO_ONLINE_PLAYERS", "IS_MULTIPLAY", "IS_PVP", "HARDCORE", "CHARACTER_RECREATE_IN_HARDCORE",
    "PAL_LOST", "CAN_PICKUP_OTHER_GUILD_DEATH_PENALTY_DROP", "ENABLE_NON_LOGIN_PENALTY", "ENABLE_FAST_TRAVEL",
    "IS_START_LOCATION_SELECT_BY_MAP", "EXIST_PLAYER_AFTER_LOGOUT", "ENABLE_DEFENSE_OTHER_GUILD_PLAYER",
    "INVISIBLE_OTHER_GUILD_BASE_CAMP_AREA_FX", "BUILD_AREA_LIMIT", "SHOW_PLAYER_LIST", "USE_BACKUP_SAVE_DATA",
    "ENABLE_PREDATOR_BOSS_PAL", "ALLOW_GLOBAL_PALBOX_EXPORT", "ALLOW_GLOBAL_PALBOX_IMPORT", "ALLOW_CLIENT_MOD",
    "IS_SHOW_JOIN_LEFT_MESSAGE", "DISPLAY_PVP_ITEM_NUM_ON_WORLD_MAP_BASE_CAMP",
    "DISPLAY_PVP_ITEM_NUM_ON_WORLD_MAP_PLAYER", "ADDITIONAL_DROP_ITEM_WHEN_PLAYER_KILLING_IN_PVP_MODE_ENABLED",
    "ENABLE_VOICE_CHAT", "ALLOW_ENHANCE_STAT_HEALTH", "ALLOW_ENHANCE_STAT_ATTACK", "ALLOW_ENHANCE_STAT_STAMINA",
    "ALLOW_ENHANCE_STAT_WEIGHT", "ALLOW_ENHANCE_STAT_WORK_SPEED", "ENABLE_BUILDING_PLAYER_UID_DISPLAY",
    # start flags and the image's own switches the panel may set
    "COMMUNITY", "UPDATE_ON_BOOT",
)  # fmt: skip
_PLATFORM = r"Steam|Xbox|PS5|Mac"

# The variables the panel sets, with the values the agent accepts.
ENV_VALUES: dict[str, str] = {
    "TZ": steam.TZ,
    "PORT": steam.PORT,
    "QUERY_PORT": steam.PORT,
    "PUBLIC_PORT": steam.PORT,
    "PLAYERS": r"[1-9]|[12][0-9]|3[0-2]",
    "SERVER_NAME": FREE_TEXT + "{1,64}",
    "SERVER_DESCRIPTION": FREE_TEXT + "{0,256}",
    "SERVER_PASSWORD": r'[^"\s\x00-\x1f\x7f]{0,64}',
    "ADMIN_PASSWORD": r"[A-Za-z0-9_-]{12,64}",
    "RANDOMIZER_SEED": r"[A-Za-z0-9_-]{0,32}",
    "REGION": r"[A-Za-z0-9 _-]{0,32}",
    "DIFFICULTY": r"None|Casual|Normal|Hard",
    "RANDOMIZER_TYPE": r"None|Region|All",
    "DEATH_PENALTY": r"None|Item|ItemAndEquipment|All",
    "CROSSPLAY_PLATFORMS": rf"\((?:{_PLATFORM})(?:,(?:{_PLATFORM})){{0,3}}\)",
    "ADDITIONAL_DROP_ITEM_WHEN_PLAYER_KILLING_IN_PVP_MODE": r"[A-Za-z]{1,32}",
    **dict.fromkeys(_RATES, RATE),
    **dict.fromkeys(_INTS, INT),
    **dict.fromkeys(_BOOLS, steam.BOOL_ANY_CASE),
}
ALLOWED_ENV = frozenset(ENV_VALUES)
# what the agent fixes (the image's features the panel does its own way, or that download code)
FIXED_ENV = {
    "REST_API_ENABLED": "true",
    "REST_API_PORT": str(REST_PORT),
    "RCON_ENABLED": "false",
    "BACKUP_ENABLED": "false",
    "AUTO_UPDATE_ENABLED": "false",
    "AUTO_REBOOT_ENABLED": "false",
    "AUTO_PAUSE_ENABLED": "false",
    "MOD_ENABLED": "false",
    "ENABLE_PLAYER_LOGGING": "false",
    "DISABLE_GENERATE_SETTINGS": "false",
    "LOG_FORMAT_TYPE": "Text",  # the readiness line
}
RESERVED_ENV = frozenset({"PUID", "PGID", *FIXED_ENV})


def env_policy_message() -> str:
    return (
        "the image's other variables download mods or other branches, take Steam credentials, run "
        "backups, updates or restarts of their own, or send data to webhooks"
    )


env_policy = steam.allow_list("Palworld", ALLOWED_ENV, env_policy_message())


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {
        "PUID": str(settings.runtime_uid),
        "PGID": str(settings.runtime_gid),
        **FIXED_ENV,
        **steam.defaults(spec, {"PORT": str(GAME_PORT), "QUERY_PORT": str(QUERY_PORT)}),
        # the REST API's password (Basic admin:<password>): the panel's control secret unless the
        # panel sets an admin password of its own
        **steam.control_secret(spec, "ADMIN_PASSWORD"),
    }


PROFILE = RuntimeProfile(
    game=GAME,
    label="Palworld",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("drop_privileges_env", uid_env="PUID", gid_env="PGID"),
    cap_add=("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"),
    mounts=(Mount(".", "/palworld"),),
    injected_env=injected_env,
    reserved_env=RESERVED_ENV,
    env_policy=env_policy,
    container_ports=(),
    allow_extra_ports=False,
    never_publish=((REST_PORT, "tcp"), (RCON_PORT, "tcp")),
    health=Health("log", pattern=READY_PATTERN, start_period_s=900),
    stop_signal="SIGTERM",
    tty=False,
    stdin_open=False,
    hostname_prefix="palworld-",
    fetch_hosts=(),
    control="palworld-api",
    # its control port is a network listener (A-6): never on the host network or a LAN network, where
    # never_publish would not hold
    instance_network_only=True,
    control_options=MappingProxyType({"port": REST_PORT, "secret_env": "ADMIN_PASSWORD"}),
    default_backup_excludes=(
        "Pal/Binaries",
        "Pal/Content",
        "Pal/Plugins",
        "Engine",
        "steamapps",
        "linux64",
        ".steam",
        "*.so",
        "Manifest_*.txt",
        "PalServer.sh",
        "DefaultPalWorldSettings.ini",
        "backups",
        "Pal/Saved/Crashes",
    ),
    tag_pattern=TAG_PATTERN,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    clean_exit_codes=(0, 143),
    env_ports=MappingProxyType({"PORT": "udp", "QUERY_PORT": "udp"}),
    restart_policy="no",
    # a stop before the server was ever ready (still downloading the game): nothing to save, no waiting
    not_ready_stop_timeout=steam.NOT_READY_STOP_TIMEOUT,
    strict_env=True,
    redact_env=("ADMIN_PASSWORD", "SERVER_PASSWORD"),
)
