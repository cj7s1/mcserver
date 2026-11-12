"""The Valheim runtime profile: ghcr.io/community-valheim-tools/valheim-server, an exact ``X.Y.Z`` tag.

What the probe of 2026-11-10 observed (``1.4.1``, Valheim l-1.0.21; design/probe-6games.md section 2):

* The image starts as root (``bootstrap``): ``groupmod``/passwd rewrite for ``PUID``/``PGID``,
  ``chown -R`` of /config and /opt/valheim, then tini + supervisord, which runs the updater and the
  server as that user. supervisord must signal its programs: CHOWN DAC_OVERRIDE SETUID SETGID KILL.
  ``PUID``/``PGID`` default to 0 (the game as root), so the agent always injects its runtime ids.
* Two mounts: ``config/`` -> /config (worlds in ``worlds_local/<WORLD_NAME>/``, the admin/banned/
  permitted lists, the image's backups when enabled) and ``opt/`` -> /opt/valheim (the 2 GB game,
  twice: SteamCMD's download and the runtime copy; left out of backups).
* No console, no RCON, no API (control ``none``). SIGTERM saves the world (exit 0, ~9 s).
* Ports: ``SERVER_PORT`` (game, udp) and the two after it (query = +1, crossplay = +2), set 1:1.
  The status/supervisor HTTP servers (80, 9001) stay off and are never published.
* Every ``*_HOOK`` variable is shell code the image runs (``PRE_SUPERVISOR_HOOK`` as root), the mod
  loaders download code, ``SERVER_ARGS`` is appended unquoted: only the panel's variables below are
  accepted, ``SERVER_ARGS`` only as a list of known game options.
* Readiness: ``Game server connected`` (the image's own "listening" line comes before the world).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from . import steam
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "valheim"
GAME_PORT = 2456
DEFAULT_IMAGES = ("ghcr.io/community-valheim-tools/valheim-server",)
TAG_PATTERN = r"\d{1,3}\.\d{1,3}\.\d{1,3}"
READY_PATTERN = r"Game server connected"

# SERVER_ARGS: only these game options (one or more, separated by single spaces)
_ARG = (
    r"-(?:saveinterval|backups|backupshort|backuplong) [0-9]{1,6}"
    r"|-preset [A-Za-z]{1,16}"
    r"|-modifier [A-Za-z]{1,16} [A-Za-z]{1,16}"
    r"|-setkey [A-Za-z0-9_]{1,32}"
)
SERVER_ARGS_PATTERN = rf"(?:(?:{_ARG})(?: (?:{_ARG})){{0,15}})?"

ENV_VALUES: dict[str, str] = {
    "TZ": steam.TZ,
    "SERVER_NAME": r"[A-Za-z0-9 _.,:!?'()#+@&-]{1,64}",
    "SERVER_PORT": steam.PORT,
    "WORLD_NAME": r"[A-Za-z0-9_-]{1,32}",
    "SERVER_PASS": r"[A-Za-z0-9_.,:!?@#%+=-]{5,64}",
    "SERVER_PUBLIC": steam.BOOL,
    "CROSSPLAY": steam.BOOL,
    "STEAMCMD_ARGS": r"(?:validate)?",
    "SERVER_ARGS": SERVER_ARGS_PATTERN,
}
ALLOWED_ENV = frozenset(ENV_VALUES)
FIXED_ENV = {
    "BACKUPS": "false",
    "UPDATE_CRON": "",
    "RESTART_CRON": "",
    "UPDATE_IF_IDLE": "false",
    "STATUS_HTTP": "false",
    "SUPERVISOR_HTTP": "false",
}
RESERVED_ENV = frozenset({"PUID", "PGID", *FIXED_ENV})

env_policy = steam.allow_list(
    "Valheim",
    ALLOWED_ENV,
    "the image's other variables run shell hooks, download mods, rewrite the admin lists at every "
    "start or open HTTP servers",
)


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {
        "PUID": str(settings.runtime_uid),
        "PGID": str(settings.runtime_gid),
        **FIXED_ENV,
        **steam.defaults(spec, {"SERVER_PORT": str(GAME_PORT)}),
    }


# the digest the tag pointed to when the probe ran (2026-11-11, audit-2026-11-09/design/probe-6games.md);
# a re-pushed tag is refused (A-8)
TAG_DIGESTS = MappingProxyType(
    {"1.4.1": ("sha256:f34d67ba8023e343b10ffda0144759a8c8d986f276510460315a01051bed8a6d",)}
)


PROFILE = RuntimeProfile(
    game=GAME,
    label="Valheim",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("drop_privileges_env", uid_env="PUID", gid_env="PGID"),
    cap_add=("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"),
    mounts=(Mount("config", "/config"), Mount("opt", "/opt/valheim")),
    injected_env=injected_env,
    reserved_env=RESERVED_ENV,
    env_policy=env_policy,
    container_ports=(),
    allow_extra_ports=False,
    never_publish=((80, "tcp"), (9001, "tcp")),
    health=Health("log", pattern=READY_PATTERN, start_period_s=900),
    stop_signal="SIGTERM",
    tty=False,
    stdin_open=False,
    hostname_prefix="valheim-",
    fetch_hosts=(),
    control="none",
    default_backup_excludes=("opt", "config/backups"),
    tag_pattern=TAG_PATTERN,
    tag_digests=TAG_DIGESTS,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    env_ports=MappingProxyType({"SERVER_PORT": "udp"}),
    env_port_offsets=MappingProxyType({"SERVER_PORT": ((1, "udp"), (2, "udp"))}),
    restart_policy="no",
    # a stop before the server was ever ready (still downloading the game): nothing to save, no waiting
    not_ready_stop_timeout=steam.NOT_READY_STOP_TIMEOUT,
    strict_env=True,
    redact_env=("SERVER_PASS",),
    # without it the image starts the world with its default join password "secret" (I-3)
    required_env=("SERVER_PASS",),
)
