"""The Satisfactory runtime profile: wolveix/satisfactory-server, an exact ``vX.Y.Z`` image tag.

What was observed on the real image (``v1.9.11``, 2026-11-10; the spike's section 5):

* The image starts as root (``init.sh``): it changes the ``steam`` user to ``PUID``/``PGID``,
  ``chown -R`` the server folder and drops to that user with ``gosu``. Without root it runs only as
  uid 1000 (the build-time uid), so on Unraid (99:100) it needs root mode with exactly
  CHOWN DAC_OVERRIDE SETUID SETGID (fewer fail at the log cleanup, the chown or the user switch).
  The agent injects ``PUID``/``PGID`` = its runtime uid/gid.
* Everything lives under ``/config`` (no VOLUME): ``gamefiles/`` (the 2.8 GB game, downloaded
  by SteamCMD and re-validated at every start; left out of backups), ``saved/`` (the game's
  SaveGames: ``ServerSettings.7777.sav`` with the claim and the API tokens, ``server/*.sav``),
  ``backups/`` (the image's copy of the saves at every start), ``logs/``.
* Ports: 7777/tcp (HTTPS API), 7777/udp (game + query) and the reliable-messaging port
  (``SERVERMESSAGINGPORT``, default 8888/tcp). The game port stays 7777 inside the container: the
  claim is stored per port (``ServerSettings.<port>.sav``), so another port is a new, unclaimed
  server. The messaging port is advertised to players as it is set, so the panel sets it to the
  host port and publishes it 1:1 (``env_ports``).
* Readiness: the UDP Lightweight Query on 7777/udp answers (also while a map loads, when the
  HTTPS API is briefly down).
* Control: the ``satisfactory-api`` driver (HTTPS API on the instance network, the API token in
  ``.mcpanel/api-token`` of the server folder). SIGINT/SIGTERM end the game without saving, so the
  driver saves and calls ``Shutdown``; ``SIGINT`` (the image's STOPSIGNAL) is only the fallback.
* SteamCMD fails now and then (exit 5 "Timed out", exit 8 "Missing configuration"): the panel
  retries the first start (``install_retry_exits``).
* Variables refused: the ones that download other code or skip the update (``STEAMAPPID``,
  ``STEAMBETA*``, ``SKIPUPDATE``), ``DEBUG`` (prints every variable and exits), ``VMOVERRIDE``,
  ``LOG``, ``MULTIHOME`` and the ports; the panel may set the player limit and the other launch
  settings with the values below.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from ..drivers.satisfactory import POLL_PACKET, REPLY_PATTERN
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "satisfactory"
GAME_PORT = 7777
DEFAULT_MESSAGING_PORT = 8888
TOKEN_FILE = ".mcpanel/api-token"  # noqa: S105 - a path, not a token
DEFAULT_IMAGES = ("wolveix/satisfactory-server", "docker.io/wolveix/satisfactory-server")
TAG_PATTERN = r"v\d{1,3}\.\d{1,3}\.\d{1,4}"

# The variables the panel sets, with the values the agent accepts.
ENV_VALUES = {
    "MAXPLAYERS": r"[1-9][0-9]{0,2}",
    "AUTOSAVENUM": r"[0-9]{1,3}",
    "MAXTICKRATE": r"[1-9][0-9]{0,2}",
    "TIMEOUT": r"[1-9][0-9]{0,3}",
    "SERVERSTREAMING": r"true|false",
    "DISABLESEASONALEVENTS": r"true|false",
    "SERVERMESSAGINGPORT": r"[1-9][0-9]{3,4}",
}
ALLOWED_ENV = frozenset({"TZ", *ENV_VALUES})
RESERVED_ENV = frozenset(
    {
        "PUID",
        "PGID",
        "SERVERGAMEPORT",
        "MULTIHOME",
        "STEAMAPPID",
        "STEAMBETA",
        "STEAMBETAID",
        "STEAMBETAKEY",
        "SKIPUPDATE",
        "DEBUG",
        "VMOVERRIDE",
        "LOG",
    }
)


def env_policy(key: str) -> str | None:
    """Only the time zone and the launch settings the panel sets (ENV_VALUES): an allow-list."""
    if key.upper() in ALLOWED_ENV:
        return None
    return (
        f"Environment variable {key} is not allowed for Satisfactory servers: the image's other variables "
        "download other branches, skip updates, print every variable or change the ports"
    )


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {
        "PUID": str(settings.runtime_uid),
        "PGID": str(settings.runtime_gid),
        # fixed: the claim is stored per game port
        "SERVERGAMEPORT": str(GAME_PORT),
    }


PROFILE = RuntimeProfile(
    game=GAME,
    label="Satisfactory",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("drop_privileges_env", uid_env="PUID", gid_env="PGID"),
    # init.sh as root: chown the server folder, usermod/groupmod, gosu to the runtime uid/gid
    cap_add=("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"),
    mounts=(Mount(".", "/config"),),
    injected_env=injected_env,
    reserved_env=RESERVED_ENV,
    env_policy=env_policy,
    container_ports=((GAME_PORT, "tcp"), (GAME_PORT, "udp")),
    allow_extra_ports=False,
    never_publish=(),
    health=Health(
        "udp-query", port=GAME_PORT, payload=POLL_PACKET, pattern=REPLY_PATTERN, start_period_s=600
    ),
    stop_signal="SIGINT",
    tty=False,
    stdin_open=False,
    hostname_prefix="satisfactory-",
    fetch_hosts=(),
    control="satisfactory-api",
    control_options=MappingProxyType({"port": GAME_PORT, "secret_file": TOKEN_FILE}),
    default_backup_excludes=("gamefiles",),
    tag_pattern=TAG_PATTERN,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    install_retry_exits=(5, 8),
    env_ports=MappingProxyType({"SERVERMESSAGINGPORT": "tcp"}),
)
