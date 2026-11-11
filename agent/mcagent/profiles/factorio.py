"""The Factorio runtime profile: factoriotools/factorio, the ``-rootless`` image of an exact version.

What was observed on the real image (2026-11-10, ``2.1.23-rootless`` / ``2.1.24-rootless``):

* The rootless image runs as any uid with ``--user`` and no capabilities at all (``/opt/factorio``
  is mode 0777), so the agent sets the runtime uid:gid and adds no capability. The regular tag
  starts as root and needs CHOWN DAC_OVERRIDE SETUID SETGID: it is not allowed (tag pattern).
* Everything lives under ``/factorio`` (the image's only VOLUME): config/, saves/, mods/, ...
* The game port is 34197/udp; RCON listens on 27015/tcp and is never published. The RCON
  password is read from ``config/rconpw`` (the panel writes it from the server's control secret;
  the image generates one only when the file is missing) and passed on the server's command line.
* The entrypoint runs with ``set -x``: it prints that command line, password included, into the
  container log, so the agent redacts it from every console text (``redact_secret_in_logs``).
* Ready once ``Starting RCON interface at`` is printed (about a second after the map loaded). An
  RCON health check would print ``New RCON connection from ...`` every interval, so readiness is
  the agent-side log probe.
* ``/server-save`` answers at once ("Saving the map"); the save is done when ``Saving finished``
  is printed. SIGTERM saves and exits 0; ``/quit`` does the same from inside the game (which the
  panel turns into its own Stop, or Docker's restart policy would start the server again).
* ``DLC_SPACE_AGE``, ``GENERATE_NEW_SAVE``, ``LOAD_LATEST_SAVE`` and ``SAVE_NAME`` select the save
  and the Space Age mods; the panel sets them, with values this profile checks. Every other image
  variable is refused: some download code (``UPDATE_MODS_ON_START`` with ``USERNAME``/``TOKEN``),
  others point the server at other paths (``CONFIG``, ``SAVES``, ``*_FILE``, ...).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from ..drivers.rcon import RconCommands, RconStep
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "factorio"
GAME_PORT = 34197
RCON_PORT = 27015
SECRET_FILE = "config/rconpw"  # noqa: S105 - a path, not a password
DEFAULT_IMAGES = ("factoriotools/factorio", "docker.io/factoriotools/factorio")
TAG_PATTERN = r"\d{1,4}\.\d{1,4}\.\d{1,5}-rootless"
READY_PATTERN = r"Starting RCON interface at"
SAVE_DONE_PATTERN = r"Saving finished"

# The variables the panel sets, with the values the agent accepts.
ENV_VALUES = {
    "DLC_SPACE_AGE": r"true|false",
    "GENERATE_NEW_SAVE": r"true|false",
    "LOAD_LATEST_SAVE": r"true|false",
    "SAVE_NAME": r"[A-Za-z0-9_-]{1,64}",  # a bare name: an absolute path would be used as it is
}
ALLOWED_ENV = frozenset({"TZ", *ENV_VALUES})


def env_policy(key: str) -> str | None:
    """Only the time zone and the variables the panel sets (ENV_VALUES): an allow-list."""
    if key.upper() in ALLOWED_ENV:
        return None
    return (
        f"Environment variable {key} is not allowed for Factorio servers: the image's other variables "
        "download mods, take credentials or move the server's files"
    )


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {"PORT": str(GAME_PORT), "RCON_PORT": str(RCON_PORT)}


COMMANDS = RconCommands(
    save_before_stop="/server-save",
    stop=None,  # SIGTERM saves and exits 0
    save=(RconStep("/server-save", 120, "Saving the map (/server-save)"),),
    save_done_log=SAVE_DONE_PATTERN,
)

PROFILE = RuntimeProfile(
    game=GAME,
    label="Factorio",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("fixed"),
    cap_add=(),
    mounts=(Mount(".", "/factorio"),),
    injected_env=injected_env,
    reserved_env=frozenset({"PORT", "RCON_PORT", "BIND", "PUID", "PGID"}),
    env_policy=env_policy,
    container_ports=((GAME_PORT, "udp"),),
    allow_extra_ports=False,
    never_publish=((RCON_PORT, None),),
    health=Health("log", pattern=READY_PATTERN, start_period_s=600),
    stop_signal="SIGTERM",
    tty=False,
    stdin_open=False,
    hostname_prefix="factorio-",
    fetch_hosts=(),
    control="rcon",
    control_options=MappingProxyType({"port": RCON_PORT, "secret_file": SECRET_FILE, "commands": COMMANDS}),
    default_backup_excludes=("temp",),
    tag_pattern=TAG_PATTERN,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    redact_secret_in_logs=True,
    log_redact_patterns=(r"(--rcon-password[ =]+)\S+",),
)
