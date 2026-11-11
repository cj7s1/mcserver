"""The Terraria runtime profiles: vanilla and tModLoader (PassiveLemon's image), TShock (ryshe's).

Game ``terraria``, one profile per variant (``vanilla``, ``tmodloader``, ``tshock``). What was
observed on the real images (spike of 2026-11-10 and the probes for this profile):

Vanilla and tModLoader, ``passivelemon/terraria-docker:terraria-<version>`` /
``:tmodloader-<version>`` (exact tags only):

* The entrypoint starts as root, creates the user ``terraria`` from ``PUID``/``PGID``, chowns
  ``/opt/terraria`` (the server folder included) and drops to it: it needs CHOWN, DAC_OVERRIDE,
  FOWNER, SETUID and SETGID (``--user`` fails). ``groupadd``/``addgroup`` fails when ``PGID``
  already exists in the image (gid 100, ``users``, in both: Unraid's 99:100), so a runtime gid the
  image already has is replaced by the runtime uid as the group (:func:`terraria_ids`).
* The server folder is ``/opt/terraria/config``. ``SERVERCONFIG=1`` makes the server read the
  ``serverconfig.txt`` the panel writes there (world, autocreate, port, ...); the image's other
  variables only fill that file and are refused (they are written into it unescaped).
* The server runs in a tmux pane; commands go in only through the image's ``inject`` script
  (``docker exec ... inject "<line>"``, the console driver's ``exec`` transport, run as the
  server's own uid:gid). Lines tmux would read as keys are refused (console_io's ``tmux`` guard).
* Ready once ``Server started`` is printed. SIGTERM saves (after a fixed 5 s warning) and exits
  143, which is this image's clean stop. ``save`` ends with ``Backing up world file`` (vanilla);
  tModLoader's save prints nothing at its end, so the driver waits for the ``.twld`` file.
* tModLoader needs ``MODPACK`` and ``ModPacks/<pack>/Mods/enabled.json`` (else it exits 0) and
  downloads the .NET runtime from Microsoft at every container creation.

TShock, ``ryshe/terraria:tshock-<terraria>-<tshock>`` (exact tags only):

* Run without the image's bootstrap script (which needs root and ignores stop signals): the
  entrypoint is ``/tshock/TShock.Server`` itself, as the runtime uid:gid, with no capabilities,
  working folder ``/tmp`` and ``HOME``/``DOTNET_BUNDLE_EXTRACT_BASE_DIR`` there; the server
  folder is ``/config`` (TShock's config, database and the world), logs go to ``/config/logs``,
  the TShock plugin is loaded from the image's ``/plugins`` (``-additionalplugins``), and the
  image's four VOLUMEs other than ``/config`` are tmpfs. World, autocreate size and seed come
  from the ``serverconfig.txt`` the panel writes (``-config``).
* Commands through TShock's REST API (port 7878, never published; the driver reads the panel's
  token from ``config.json``), the console through stdin as the fallback (``stdin_open``).
* SIGINT saves and exits 0: the stop signal. The one-time ``/setup <code>`` is hidden in every
  console text the agent hands out.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "terraria"
GAME_PORT = 7777
REST_PORT = 7878
PASSIVELEMON_IMAGES = ("passivelemon/terraria-docker", "docker.io/passivelemon/terraria-docker")
TSHOCK_IMAGES = ("ryshe/terraria", "docker.io/ryshe/terraria")
VANILLA_TAG = r"terraria-\d{1,3}\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?"
TMODLOADER_TAG = r"tmodloader-\d{4}\.\d{1,2}\.\d{1,3}\.\d{1,3}"
TSHOCK_TAG = r"tshock-\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}-\d{1,3}\.\d{1,3}\.\d{1,3}"
READY_PATTERN = r"Server started"
SAVE_DONE_LOG = r"Backing up world file"
PASSIVELEMON_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")
PASSIVELEMON_DATA = "/opt/terraria/config"
TSHOCK_DATA = "/config"
MODPACK_PATTERN = r"[A-Za-z0-9_-]{1,64}"

# Groups both PassiveLemon images already have (Debian and Alpine /etc/group): the entrypoint's
# groupadd/addgroup fails with one of them as PGID.
IMAGE_GIDS = frozenset(
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27}
    | {28, 29, 30, 33, 34, 35, 37, 38, 39, 40, 42, 43, 44, 45, 46, 50, 60, 82, 100, 123, 300, 406, 999}
    | {65533, 65534}
)
FALLBACK_GID = 4999

# The image's own variables (written unescaped into serverconfig.txt, or selecting other files):
# never from a spec. The agent injects PUID, PGID and SERVERCONFIG.
PASSIVELEMON_RESERVED = frozenset(
    {
        "PUID", "PGID", "SERVERCONFIG", "AUTOCREATE", "BANLIST", "DIFFICULTY", "LANGUAGE", "MAXPLAYERS",
        "MOTD", "NPCSTREAM", "PASSWORD", "PORT", "PRIORITY", "SECURE", "SEED", "UPNP", "WORLDNAME", "VERSION",
    }
)  # fmt: skip
TSHOCK_RESERVED = frozenset(
    {"HOME", "DOTNET_BUNDLE_EXTRACT_BASE_DIR", "WORLD_FILENAME", "CONFIGPATH", "LOGPATH"}
)


def terraria_ids(settings: Settings) -> tuple[int, int]:
    """(PUID, PGID) for PassiveLemon's entrypoint: the runtime uid and gid, except that a gid the
    image already has (100 on Unraid) becomes the uid (99), or FALLBACK_GID when that exists too."""
    uid, gid = int(settings.runtime_uid), int(settings.runtime_gid)
    if gid in IMAGE_GIDS:
        gid = uid if uid not in IMAGE_GIDS else FALLBACK_GID
    return uid, gid


def _passivelemon_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    uid, gid = terraria_ids(settings)
    return {"PUID": str(uid), "PGID": str(gid), "SERVERCONFIG": "1"}


def _tshock_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {"HOME": "/tmp", "DOTNET_BUNDLE_EXTRACT_BASE_DIR": "/tmp"}


def _allow_list(label: str, allowed: frozenset[str]):
    def policy(key: str) -> str | None:
        if key.upper() in allowed:
            return None
        return (
            f"Environment variable {key} is not allowed for {label} servers: the image's other variables "
            "are written into its config files or change how the server starts"
        )

    return policy


def _passivelemon_profile(
    variant: str, label: str, tag: str, *, env_values: dict[str, str], save: dict
) -> RuntimeProfile:
    return RuntimeProfile(
        game=GAME,
        label=label,
        variants=(variant,),
        images=PASSIVELEMON_IMAGES,
        user=UserModel("drop_privileges_env", uid_env="PUID", gid_env="PGID"),
        cap_add=PASSIVELEMON_CAPS,
        mounts=(Mount(".", PASSIVELEMON_DATA),),
        injected_env=_passivelemon_env,
        reserved_env=PASSIVELEMON_RESERVED - frozenset(env_values),
        env_policy=_allow_list(label, frozenset({"TZ", *env_values})),
        container_ports=((GAME_PORT, "tcp"),),
        allow_extra_ports=False,
        never_publish=(),
        health=Health("log", pattern=READY_PATTERN, start_period_s=900),
        stop_signal="SIGTERM",
        tty=False,
        stdin_open=False,
        hostname_prefix="terraria-",
        fetch_hosts=("dot.net", "builds.dotnet.microsoft.com") if variant == "tmodloader" else (),
        control="console",
        control_options=MappingProxyType(
            {
                "transport": "exec",
                "exec_argv": ("inject", "{line}"),
                "exec_user_env": ("PUID", "PGID"),
                "guard": "tmux",
                "max_line_len": 256,
                "broadcast": "say {text}",
                **save,
            }
        ),
        tag_pattern=tag,
        env_values=MappingProxyType(env_values),
        mount_image_volumes=True,
        clean_exit_codes=(0, 143),
    )


VANILLA = _passivelemon_profile(
    "vanilla",
    "Terraria",
    VANILLA_TAG,
    env_values={},
    save={"save_command": "save", "save_done_log": SAVE_DONE_LOG, "save_timeout": 120.0},
)
TMODLOADER = _passivelemon_profile(
    "tmodloader",
    "tModLoader",
    TMODLOADER_TAG,
    env_values={"MODPACK": MODPACK_PATTERN},
    save={"save_command": "save", "save_done_file": "Worlds/*.twld", "save_timeout": 120.0},
)

TSHOCK_COMMAND = (
    "-configpath", TSHOCK_DATA,
    "-logpath", f"{TSHOCK_DATA}/logs",
    "-additionalplugins", "/plugins",
    "-config", f"{TSHOCK_DATA}/serverconfig.txt",
)  # fmt: skip

TSHOCK = RuntimeProfile(
    game=GAME,
    label="TShock",
    variants=("tshock",),
    images=TSHOCK_IMAGES,
    user=UserModel("fixed"),
    cap_add=(),
    mounts=(Mount(".", TSHOCK_DATA),),
    injected_env=_tshock_env,
    reserved_env=TSHOCK_RESERVED,
    env_policy=_allow_list("TShock", frozenset({"TZ"})),
    container_ports=((GAME_PORT, "tcp"),),
    allow_extra_ports=False,
    never_publish=((REST_PORT, None),),
    health=Health("log", pattern=READY_PATTERN, start_period_s=900),
    stop_signal="SIGINT",
    tty=False,
    stdin_open=True,
    hostname_prefix="tshock-",
    fetch_hosts=(),
    control="tshock-rest",
    control_options=MappingProxyType(
        {"port": REST_PORT, "config_file": "config.json", "username": "mcpanel"}
    ),
    # TShock's own rolling world backups and its logs: the panel's backups have the world already
    default_backup_excludes=("logs", "backups"),
    tag_pattern=TSHOCK_TAG,
    mount_image_volumes=True,
    volume_tmpfs=("/root/.local/share/Terraria/Worlds", "/tshock/ServerPlugins", "/tshock/logs"),
    redact_secret_in_logs=True,
    log_redact_patterns=(r"(/setup )\d+",),
    entrypoint=("/tshock/TShock.Server",),
    command=TSHOCK_COMMAND,
    working_dir="/tmp",
)

PROFILES = (VANILLA, TMODLOADER, TSHOCK)
