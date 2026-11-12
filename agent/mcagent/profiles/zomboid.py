"""The Project Zomboid runtime profile: indifferentbroccoli/projectzomboid-server-docker, ``vX.Y.Z``.

What the probe of 2026-11-10 observed (``v1.2.0``, game 42.22.2; design/probe-6games.md section 4):

* ``init.sh`` requires ``PUID``/``PGID``, changes ``steam`` to them, ``chown -R`` both folders and
  runs the game as the user it was started as: as root there is no privilege drop. So the container
  runs as the image's ``steam`` user, a literal ``1000:1000`` without capabilities, like CS2 (A-3 of
  the review of 2026-11-11; the probe and an agent-level run showed the server, update-on-start, RCON
  and the save working so; ``groupmod`` and one ``chown`` of init.sh fail harmlessly). The agent gives
  the server folder 1000:1000 before every create and start (``literal_owner``). As uid 1000 the
  image's start.sh cannot write ``/home/steam/server/rcon.yml``, which its SIGTERM trap (save +
  quit) needs: the agent writes that file and mounts it read-only (``agent_files``).
* Two mounts: ``game/`` -> /project-zomboid (7 GB, DepotDownloader validates it at every start with
  ``UPDATE_ON_START``; left out of backups) and ``data/`` -> /project-zomboid-config (``Server/<name>.ini``,
  ``Saves/Multiplayer/<name>/``, the SQLite user database, the game's own backups and logs).
* ``start.sh`` writes ``DefaultPort UDPPort RCONPort RCONPassword MaxPlayers`` into the ini with
  ``sed`` (``|`` as the delimiter) and the RCON password into a YAML file, and always passes
  ``-adminpassword <password>``, which the launcher prints into the log: both passwords are hidden in
  every log text the agent hands out (``redact_env``).
* Ports: ``DEFAULT_PORT`` (game + Steam query, udp) and ``UDP_PORT`` (udp), set 1:1; RCON 27015/tcp
  is never published.
* Control: Source RCON (``players``, ``servermsg``, ``save`` -> "World saved", the log's ``Saving
  finish``); A2S answers on the game port. SIGTERM saves (the image sends ``save`` + ``quit``), exit 143.
* Readiness: ``*** SERVER STARTED ****``.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from ..drivers.rcon import RconCommands, RconStep
from . import steam
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "project-zomboid"
OWNER = (1000, 1000)  # the image's steam user: the game runs as it, without capabilities (A-3)
GAME_PORT = 16261
UDP_PORT = 16262
RCON_PORT = 27015
DEFAULT_IMAGES = (
    "indifferentbroccoli/projectzomboid-server-docker",
    "docker.io/indifferentbroccoli/projectzomboid-server-docker",
)
TAG_PATTERN = r"v\d{1,3}\.\d{1,3}\.\d{1,3}"
READY_PATTERN = r"\*\*\* SERVER STARTED \*\*\*\*"
# the game's own line ("LOG  : General      f:0 st:395,038,282> Saving finish"): the message right after
# the channel's prefix, never text of a chat line (C-3)
SAVE_DONE_PATTERN = r"^LOG\s*:\s*General\b[^>]*>(?:\s*[\d,]+>)?\s*Saving finish\.?\s*$"

ENV_VALUES: dict[str, str] = {
    "TZ": steam.TZ,
    "SERVER_NAME": r"[A-Za-z0-9_-]{1,32}",  # names every file of the server
    "DEFAULT_PORT": steam.PORT,
    "UDP_PORT": steam.PORT,
    "ADMIN_PASSWORD": r"[A-Za-z0-9_.,:!?@#%+=-]{8,64}",
    "MAX_PLAYERS": r"[1-9]|[1-9][0-9]|100",
    "MEMORY_XMX_GB": r"[1-9][0-9]{0,2}",
    "SERVER_BRANCH": r"(?:legacy41|42\.[0-9]{1,3})?",
    "UPDATE_ON_START": steam.BOOL,
    # injected only (RCON_PASSWORD is reserved for the agent by the spec schema): the pattern
    # applies to the control secret it is made of
    "RCON_PASSWORD": steam.ALNUM_SECRET,
}
ALLOWED_ENV = frozenset(k for k in ENV_VALUES if k != "RCON_PASSWORD")
FIXED_ENV = {
    "ADMIN_USERNAME": "admin",
    "RCON_PORT": str(RCON_PORT),
    "CONFIG_DIR": "/project-zomboid-config",
    "STEAM_VAC": "true",
    "USE_STEAM": "true",
    "VM_ARGS": "",
    "MEMORY_XMS_GB": "",
    "FIX_MOD_CASE": "false",
}
RESERVED_ENV = frozenset({"PUID", "PGID", "HOME", "RCON_PASSWORD", *FIXED_ENV})

env_policy = steam.allow_list(
    "Project Zomboid",
    ALLOWED_ENV,
    "the image's other variables add JVM options, change its folders or the Steam mode",
)


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    gb = steam.memory_gb(spec)
    xmx = spec.env.get("MEMORY_XMX_GB")
    if xmx is not None and xmx.isdigit() and int(xmx) >= gb:
        raise steam.refuse(
            f"MEMORY_XMX_GB={xmx} leaves the Java runtime no room in a container of {spec.memory_limit_mb} MB "
            f"(at most {max(1, gb - 1)})"
        )
    return {
        # init.sh requires them; the container's user is the literal owner, whatever the runtime ids
        "PUID": str(OWNER[0]),
        "PGID": str(OWNER[1]),
        **FIXED_ENV,
        # the RCON password is always the panel's control secret (the agent's RCON driver uses it)
        "RCON_PASSWORD": spec.rcon_password,
        **steam.defaults(
            spec,
            {
                "DEFAULT_PORT": str(GAME_PORT),
                "UDP_PORT": str(UDP_PORT),
                "MEMORY_XMX_GB": str(max(2, gb - 2) if gb > 2 else 1),
            },
        ),
        # the image's default admin password is "admin": the control secret unless the panel sets one
        **steam.control_secret(spec, "ADMIN_PASSWORD"),
    }


RCON_YML = "/home/steam/server/rcon.yml"


def agent_files(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    """The image's rcon.yml (what its start.sh writes): its SIGTERM trap saves and quits through it,
    and as uid 1000 start.sh cannot write it into the image's root-owned /home/steam/server (A-3: a
    stop from Docker itself, such as a host shutdown, would otherwise not save)."""
    return {
        RCON_YML: f'default:\n  address: "127.0.0.1:{RCON_PORT}"\n  password: "{spec.rcon_password}"\n',
    }


COMMANDS = RconCommands(
    save_before_stop="save",
    stop="quit",  # delivered while docker stop waits, if RCON was not up yet (the image's trap needs it)
    save=(RconStep("save", 120, "Saving the world (save)"),),
    save_done_log=SAVE_DONE_PATTERN,
)

# the digest the tag pointed to when the probe ran (2026-11-11, audit-2026-11-09/design/probe-6games.md);
# a re-pushed tag is refused (A-8)
TAG_DIGESTS = MappingProxyType(
    {"v1.2.0": ("sha256:0bfd0599c0cf3edf66ba40937ce3ae2a7c5de92073dc99d1c7e9a01fa581414e",)}
)


PROFILE = RuntimeProfile(
    game=GAME,
    label="Project Zomboid",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("fixed", uid=OWNER[0], gid=OWNER[1]),
    cap_add=(),
    agent_files=agent_files,
    mounts=(Mount("game", "/project-zomboid"), Mount("data", "/project-zomboid-config")),
    injected_env=injected_env,
    reserved_env=RESERVED_ENV,
    env_policy=env_policy,
    container_ports=(),
    allow_extra_ports=False,
    never_publish=((RCON_PORT, "tcp"),),
    health=Health("log", pattern=READY_PATTERN, start_period_s=1200),
    stop_signal="SIGTERM",
    tty=False,
    stdin_open=False,
    hostname_prefix="zomboid-",
    fetch_hosts=(),
    control="rcon",
    # its control port is a network listener (A-6): never on the host network or a LAN network, where
    # never_publish would not hold
    instance_network_only=True,
    control_options=MappingProxyType(
        {
            "port": RCON_PORT,
            "secret_env": "RCON_PASSWORD",
            "commands": COMMANDS,
            "a2s_port": GAME_PORT,
            "a2s_port_env": "DEFAULT_PORT",
        }
    ),
    default_backup_excludes=(
        "game",
        "data/backups",
        "data/Logs",
        "data/server-console.txt",
        "data/Recording",
    ),
    tag_pattern=TAG_PATTERN,
    tag_digests=TAG_DIGESTS,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    install_retry_exits=(1,),
    clean_exit_codes=(0, 143),
    env_ports=MappingProxyType({"DEFAULT_PORT": "udp", "UDP_PORT": "udp"}),
    restart_policy="no",
    # a stop before the server was ever ready (still downloading the game): nothing to save, no waiting
    not_ready_stop_timeout=steam.NOT_READY_STOP_TIMEOUT,
    strict_env=True,
    redact_env=("ADMIN_PASSWORD", "RCON_PASSWORD"),
)
