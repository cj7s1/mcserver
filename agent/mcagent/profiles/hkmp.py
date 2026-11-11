"""The Hollow Knight Multiplayer (HKMP) runtime profile: maximalmax90/hkmpserver, ``vX.Y.Z[-N]``.

What the probe of 2026-11-10 observed (``v2.4.3-2``; design/probe-6games.md section 3):

* The entrypoint (root) creates a user for ``PUID``/``PGID`` when missing, ``chown -R`` /data when
  its owner differs, installs ``HKMPServer.exe`` from the image into /data at every start and runs it
  (mono) in a ``screen`` session as that user: CHOWN DAC_OVERRIDE FOWNER SETUID SETGID. Nothing is
  downloaded at start (the image tag is the HKMP version).
* /data (the image's VOLUME) is the server folder: the server's JSON files (``serversettings.json``,
  ``whitelist.json``, ``banned.json``, ``authorized.json``) and ``logs/server.log``; no world.
* Console: the image's helper ``hkmp <command>`` types the line with ``screen -X stuff``, which
  reads ``^X`` and backslash escapes (``announce x^Mlist`` ran ``list`` too), and prints what the
  server appended to its log: the console driver's ``exec`` transport with the ``screen`` guard and
  that output as the reply. Lines that are the helper's own subcommands are refused (``check`` asks
  GitHub, ``attach``/``console`` need a terminal).
* Health: the image's HEALTHCHECK (``hkmp healthcheck``; it does not write to the server log).
* Ports: ``PORT`` (udp), set 1:1. SIGTERM -> console ``exit`` within ``STOP_TIMEOUT`` (30 s), exit 0.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from . import steam
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "hkmp"
GAME_PORT = 2222
STOP_TIMEOUT = 30
DEFAULT_IMAGES = ("maximalmax90/hkmpserver", "docker.io/maximalmax90/hkmpserver")
TAG_PATTERN = r"v\d{1,3}\.\d{1,3}\.\d{1,3}(?:-\d{1,3})?"
# the helper's own subcommands: they do not reach the server console
HELPER_COMMANDS = (
    "console", "tui", "attach", "status", "check", "logs", "healthcheck", "help", "-h", "--help",
    "version", "--version",
)  # fmt: skip

ENV_VALUES: dict[str, str] = {
    "TZ": steam.TZ,
    "PORT": steam.PORT,
    "FULL_SYNC": steam.BOOL,
}
ALLOWED_ENV = frozenset(ENV_VALUES)
RESERVED_ENV = frozenset({"PUID", "PGID", "STOP_TIMEOUT", "HKMP_HOME", "HKMP_DATA", "SCREENDIR", "HOME"})

env_policy = steam.allow_list(
    "HKMP", ALLOWED_ENV, "the image's other variables move its folders or the console's socket"
)


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {
        "PUID": str(settings.runtime_uid),
        "PGID": str(settings.runtime_gid),
        "STOP_TIMEOUT": str(STOP_TIMEOUT),
        **steam.defaults(spec, {"PORT": str(GAME_PORT)}),
    }


PROFILE = RuntimeProfile(
    game=GAME,
    label="Hollow Knight (HKMP)",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("drop_privileges_env", uid_env="PUID", gid_env="PGID"),
    cap_add=("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"),
    mounts=(Mount(".", "/data"),),
    injected_env=injected_env,
    reserved_env=RESERVED_ENV,
    env_policy=env_policy,
    container_ports=(),
    allow_extra_ports=False,
    never_publish=(),
    health=Health("image"),
    stop_signal="SIGTERM",
    tty=False,
    stdin_open=False,
    hostname_prefix="hkmp-",
    fetch_hosts=(),
    control="console",
    control_options=MappingProxyType(
        {
            "transport": "exec",
            "exec_argv": ("hkmp", "{line}"),
            "exec_user_env": ("PUID", "PGID"),
            "guard": "screen",
            "exec_reply": "stdout",
            "refused_lines": HELPER_COMMANDS,
            "max_line_len": 256,
            "save_command": None,
            "broadcast": "announce {text}",
        }
    ),
    default_backup_excludes=("HKMPServer.exe", "HKMPServer.pdb", "logs"),
    tag_pattern=TAG_PATTERN,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    env_ports=MappingProxyType({"PORT": "udp"}),
    restart_policy="no",
    strict_env=True,
)
