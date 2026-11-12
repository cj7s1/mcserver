"""The Counter-Strike 2 runtime profile: joedwards32/cs2, an exact ``X.Y.Z`` tag.

What the probe of 2026-11-10 found (``5.0.1``/``latest``, inspection only; design/probe-6games.md 5):

* The image runs as ``1000:1000`` (USER) and ``/home/steam`` is 0700: no other uid works, and it
  needs no capability. The profile fixes that user (a literal owner): the agent gives the server
  folder 1000:1000 before every create and start and writes its files as 1000:1000.
* ``entry.sh`` runs SteamCMD (app 730, ~60 GB into the server folder, at every start) and the server
  through ``eval``, and edits ``server.cfg``/``gamemode_*.cfg`` with ``sed``: every variable it reads
  is shell code and part of a sed expression. So every value has a strict pattern (letters, digits
  and a few punctuation marks; never quotes, ``$``, backticks, ``;``, ``&``, ``|``, ``/``,
  backslashes or newlines), and the variables that download or add code (``CS2_CFG_URL``,
  ``CS2_ADDITIONAL_ARGS``, ``CS2_LOG_HTTP_URL``, ...) are fixed empty.
* ``pre.sh`` and ``post.sh`` of the server folder are *sourced* by ``entry.sh`` (copied from the
  image only when missing): the agent writes its own no-op hooks before every create and start, and
  the file API never writes them (``protected_paths``, with ``server.cfg``, which the image rewrites
  at every start with the RCON password in it).
* bash is PID 1 and ignores SIGTERM: the container runs with Docker's init (tini), and the stop first
  sends ``quit`` over RCON (no world to save).
* Ports: ``CS2_PORT`` (game, udp; RCON listens on the same number over tcp, which is never
  published) and ``TV_PORT`` (CSTV, udp), set 1:1. Health and status query: A2S on the game port.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import TYPE_CHECKING

from ..drivers.rcon import RconCommands
from . import steam
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from uuid import UUID

    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "cs2"
GAME_PORT = 27015
TV_PORT = 27020
OWNER = (1000, 1000)
MOUNT = "/home/steam/cs2-dedicated"
DEFAULT_IMAGES = ("joedwards32/cs2", "docker.io/joedwards32/cs2")
TAG_PATTERN = r"\d{1,3}\.\d{1,3}\.\d{1,3}"
HOOKS = ("pre.sh", "post.sh")
SERVER_CFG = "game/csgo/cfg/server.cfg"
# the agent's hooks (what the image ships, marked as the agent's): they run at every start
HOOK_SCRIPT = {
    "pre.sh": b'#!/bin/bash\n\n# PRE HOOK (written by MCPanel before every start; edits are replaced)\necho "pre-hook: noop"\n',
    "post.sh": b'#!/bin/bash\n\n# POST HOOK (written by MCPanel before every start; edits are replaced)\necho "post-hook: noop"\n',
}

_D01 = steam.ZERO_ONE
ENV_VALUES: dict[str, str] = {
    "TZ": steam.TZ,
    "CS2_SERVERNAME": r"[A-Za-z0-9 ._-]{1,64}",
    "CS2_PORT": steam.PORT,
    "TV_PORT": steam.PORT,
    "CS2_RCONPW": steam.ALNUM_SECRET,
    "CS2_PW": r"[A-Za-z0-9]{0,64}",
    "SRCDS_TOKEN": r"(?:[A-F0-9]{32})?",
    "CS2_LAN": _D01,
    "CS2_MAXPLAYERS": r"[1-9]|[1-5][0-9]|6[0-4]",
    "CS2_GAMETYPE": r"[0-9]",
    "CS2_GAMEMODE": r"[0-9]",
    "CS2_GAMEALIAS": r"[a-z_]{0,32}",
    "CS2_MAPGROUP": r"[a-z0-9_]{1,64}",
    "CS2_STARTMAP": r"[a-z0-9_]{1,64}",
    "CS2_HOST_WORKSHOP_COLLECTION": r"[0-9]{0,20}",
    "CS2_HOST_WORKSHOP_MAP": r"[0-9]{0,20}",
    "CS2_BOT_DIFFICULTY": r"[0-3]?",
    "CS2_BOT_QUOTA": r"[0-9]{0,2}",
    "CS2_BOT_QUOTA_MODE": r"(?:fill|competitive|normal)?",
    "CS2_CHEATS": _D01,
    "CS2_SERVER_HIBERNATE": _D01,
    "CS2_DISCONNECT_KILLS": _D01,
    "CS2_SERVER_DELTATICKS_ENFORCE": r"[0-9]",
    "CS2_LOG": r"on|off",
    "CS2_LOG_FILE": _D01,
    "CS2_LOG_ECHO": _D01,
    "CS2_LOG_MONEY": _D01,
    "CS2_LOG_DETAIL": r"[0-3]",
    "CS2_LOG_ITEMS": _D01,
    "TV_ENABLE": _D01,
    "TV_AUTORECORD": _D01,
    "TV_PW": r"[A-Za-z0-9]{0,64}",
    "TV_RELAY_PW": r"[A-Za-z0-9]{0,64}",
    "TV_MAXRATE": r"[0-9]{1,7}",
    "TV_DELAY": r"[0-9]{1,3}",
    "TV_RELAYVOICE": _D01,
    "STEAMAPPVALIDATE": _D01,
}
ALLOWED_ENV = frozenset(ENV_VALUES)
# fixed empty: they download a config pack, post logs to a URL, add free-form arguments, open a
# socat proxy or choose another app/branch/folder
FIXED_ENV = {
    "CS2_CFG_URL": "",
    "CS2_LOG_HTTP_URL": "",
    "CS2_ADDITIONAL_ARGS": "",
    "CS2_RCON_PORT": "",
    "CS2_IP": "0.0.0.0",  # noqa: S104 - inside the container
}
RESERVED_ENV = frozenset(
    {*FIXED_ENV, "DEBUG", "STEAMAPPID", "STEAMAPP", "STEAMAPPVERSION", "STEAMCMDDIR", "STEAMAPPDIR", "HOME"}
)

env_policy = steam.allow_list(
    "Counter-Strike 2",
    ALLOWED_ENV,
    "the image runs its start line through eval, so only the panel's settings with checked values are "
    "passed; the others download files, add arguments or choose another app or folder",
)


def _relay_password(spec: InstanceSpec) -> str:
    """A CSTV relay password derived from the control secret (the image's default is "changeme")."""
    return hashlib.sha256(f"mcpanel-cstv-relay:{spec.rcon_password}".encode()).hexdigest()[:24]


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {
        **FIXED_ENV,
        **steam.defaults(
            spec,
            {
                "CS2_PORT": str(GAME_PORT),
                "TV_PORT": str(TV_PORT),
                "TV_PW": "",
                "TV_RELAY_PW": _relay_password(spec),
            },
        ),
        # the agent's RCON password (the image's default is "changeme")
        **steam.control_secret(spec, "CS2_RCONPW"),
    }


def prepare_start(settings: Settings, instance_id: UUID) -> None:
    """Write the agent's own ``pre.sh``/``post.sh`` (entry.sh sources them at every start), replacing
    whatever is there (a symbolic link is replaced, never followed). The folder already belongs to
    1000:1000 (``docker_ops.prepare_start``)."""
    from ..docker_ops import instance_dir
    from ..safefs import SafeRoot

    root = SafeRoot(instance_dir(settings, instance_id), *OWNER)
    for name in HOOKS:
        root.write_bytes(name, HOOK_SCRIPT[name])


COMMANDS = RconCommands(quit_before_stop="quit")

PROFILE = RuntimeProfile(
    game=GAME,
    label="Counter-Strike 2",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("fixed", uid=OWNER[0], gid=OWNER[1]),
    cap_add=(),
    mounts=(Mount(".", MOUNT),),
    injected_env=injected_env,
    reserved_env=RESERVED_ENV,
    env_policy=env_policy,
    container_ports=(),
    allow_extra_ports=False,
    # RCON shares the game port's number over tcp: only the udp side is ever published
    never_publish=((GAME_PORT, "tcp"),),
    health=Health("a2s", port=GAME_PORT, port_env="CS2_PORT", start_period_s=3600),
    stop_signal="SIGTERM",
    tty=False,
    stdin_open=False,
    hostname_prefix="cs2-",
    fetch_hosts=(),
    control="rcon",
    # its control port is a network listener (A-6): never on the host network or a LAN network, where
    # never_publish would not hold
    instance_network_only=True,
    control_options=MappingProxyType(
        {
            "port": GAME_PORT,
            "port_env": "CS2_PORT",
            "secret_env": "CS2_RCONPW",
            "commands": COMMANDS,
            "a2s_port": GAME_PORT,
            "a2s_port_env": "CS2_PORT",
        }
    ),
    # everything but the configuration: the game (~60 GB) is downloaded again by SteamCMD
    default_backup_excludes=(
        "steamapps",
        "game/bin",
        "game/core",
        "game/csgo_core",
        "game/csgo_imported",
        "game/csgo_lv",
        "game/**/*.vpk",
        "game/csgo/maps",
        "game/csgo/logs",
    ),
    tag_pattern=TAG_PATTERN,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    install_retry_exits=(5, 8),
    clean_exit_codes=(0, 143),
    env_ports=MappingProxyType({"CS2_PORT": "udp", "TV_PORT": "udp"}),
    restart_policy="no",
    # a stop before the server was ever ready (still downloading the game): nothing to save, no waiting
    not_ready_stop_timeout=steam.NOT_READY_STOP_TIMEOUT,
    strict_env=True,
    prepare_start=prepare_start,
    init=True,
    redact_env=("CS2_RCONPW", "CS2_PW", "SRCDS_TOKEN", "TV_PW", "TV_RELAY_PW"),
    protected_paths=(*HOOKS, SERVER_CFG),
)
