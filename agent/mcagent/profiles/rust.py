"""The Rust runtime profile: didstopia/rust-server, pinned by digest (the image has only ``latest``).

What the probe of 2026-11-10 found (inspection of the image's scripts; design/probe-6games.md 6):

* ``/entrypoint.sh`` (root) changes the ``docker`` user to ``PUID``/``PGID``, ``chown -R`` the game
  folders and ``gosu``s to it: CHOWN DAC_OVERRIDE SETUID SETGID (``--user`` fails).
* /steamcmd/rust is the server folder: the game (~10 GB, SteamCMD app 258550 validated at every
  start; left out of backups) and ``server/<identity>/`` (map, saves, player databases, cfg).
* ``start.sh`` builds the start line with ``$RUST_STARTUP_COMMAND`` unquoted (the RCON password,
  port and ``+rcon.web`` are word-split there) and the other settings as quoted arguments: the
  password is letters and digits only, the startup arguments are fixed, Oxide/uMod, the update
  scheduler (it restarts the server by itself), the heartbeat and passwordless sudo are off.
* Control: WebRCON (``+rcon.web 1``, 28016/tcp, never published): the ``webrcon`` driver. The
  image's SIGTERM trap sends ``quit`` over WebRCON (which saves); without WebRCON the server would be
  killed unsaved, so ``RUST_RCON_WEB`` is fixed to 1. The nginx WebRCON page (8080) is never published.
* Ports: ``RUST_SERVER_PORT`` (game, udp), ``RUST_SERVER_QUERYPORT`` (Steam query, udp) and
  ``RUST_APP_PORT`` (Rust+, tcp), set 1:1. Health and status query: A2S on the query port.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from . import steam
from .base import Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

GAME = "rust"
GAME_PORT = 28015
QUERY_PORT = 28017
RCON_PORT = 28016
APP_PORT = 28082
WEB_PORT = 8080
DEFAULT_IMAGES = ("didstopia/rust-server", "docker.io/didstopia/rust-server")
# the panel's pinned runtime image (2026-08-20 build); a new one comes with a panel and agent release
IMAGE_DIGESTS = ("sha256:c60a3ab044eb69019616951087834468aed478a11b449391376b23db05ddc38a",)
NO_TAG = r"(?!)"  # digests only: the image's tags move
STARTUP_ARGUMENTS = "-batchmode -load -nographics +server.secure 1"

# text Rust gets as one argument: no quotes, no control characters, not starting like an option
_ARG_TEXT = r'[^"\x00-\x1f\x7f+-][^"\x00-\x1f\x7f]'
_URL = r"(?:https://[A-Za-z0-9._~:/?#%=&+-]{1,200})?"
ENV_VALUES: dict[str, str] = {
    "TZ": steam.TZ,
    "RUST_SERVER_NAME": _ARG_TEXT + "{0,63}",
    "RUST_SERVER_DESCRIPTION": rf"(?:{_ARG_TEXT}{{0,511}})?",
    "RUST_SERVER_IDENTITY": r"[a-z0-9_-]{1,32}",
    "RUST_SERVER_PORT": steam.PORT,
    "RUST_SERVER_QUERYPORT": steam.PORT,
    "RUST_APP_PORT": rf"-1|{steam.PORT}",
    "RUST_RCON_PASSWORD": steam.ALNUM_SECRET,
    "RUST_SERVER_SEED": r"[0-9]{1,9}|[12][0-9]{9}",
    "RUST_SERVER_WORLDSIZE": r"[1-5][0-9]{3}|6000",
    "RUST_SERVER_MAXPLAYERS": r"[1-9][0-9]{0,2}",
    "RUST_SERVER_SAVE_INTERVAL": r"[1-9][0-9]{1,4}",
    "RUST_START_MODE": r"0|2",
    "RUST_SERVER_URL": _URL,
    "RUST_SERVER_BANNER_URL": _URL,
}
ALLOWED_ENV = frozenset(ENV_VALUES)
FIXED_ENV = {
    "RUST_SERVER_STARTUP_ARGUMENTS": STARTUP_ARGUMENTS,
    "RUST_RCON_WEB": "1",
    "RUST_RCON_PORT": str(RCON_PORT),
    "RUST_RCON_SECURE_WEBSOCKET": "0",
    "RUST_UPDATE_CHECKING": "0",
    "RUST_HEARTBEAT": "0",
    "RUST_OXIDE_ENABLED": "0",
    "RUST_OXIDE_UPDATE_ON_BOOT": "0",
    "RUST_SERVER_LEVELURL": "",
    "ENABLE_PASSWORDLESS_SUDO": "false",
}
RESERVED_ENV = frozenset(
    {"PUID", "PGID", "CHOWN_DIRS", "RUST_BRANCH", "RUST_UPDATE_BRANCH", "RUST_RCON_HOSTNAME", *FIXED_ENV}
)

env_policy = steam.allow_list(
    "Rust",
    ALLOWED_ENV,
    "the image's other variables add free-form start arguments, download Oxide or custom maps, "
    "restart the server by themselves or give its user sudo",
)


def injected_env(profile: RuntimeProfile, settings: Settings, spec: InstanceSpec) -> dict[str, str]:
    return {
        "PUID": str(settings.runtime_uid),
        "PGID": str(settings.runtime_gid),
        **FIXED_ENV,
        **steam.defaults(
            spec,
            {
                "RUST_SERVER_PORT": str(GAME_PORT),
                "RUST_SERVER_QUERYPORT": str(QUERY_PORT),
                "RUST_APP_PORT": str(APP_PORT),
                # the image's default links to its Docker Hub page in the server browser
                "RUST_SERVER_URL": "",
            },
        ),
        # WebRCON's password (the URL path of its WebSocket): the control secret unless the panel sets one
        **steam.control_secret(spec, "RUST_RCON_PASSWORD"),
    }


PROFILE = RuntimeProfile(
    game=GAME,
    label="Rust",
    variants=(),
    images=DEFAULT_IMAGES,
    user=UserModel("drop_privileges_env", uid_env="PUID", gid_env="PGID"),
    cap_add=("CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"),
    mounts=(Mount(".", "/steamcmd/rust"),),
    injected_env=injected_env,
    reserved_env=RESERVED_ENV,
    env_policy=env_policy,
    container_ports=(),
    allow_extra_ports=False,
    never_publish=((RCON_PORT, "tcp"), (WEB_PORT, "tcp")),
    health=Health("a2s", port=QUERY_PORT, port_env="RUST_SERVER_QUERYPORT", start_period_s=3600),
    stop_signal="SIGTERM",
    tty=False,
    stdin_open=False,
    hostname_prefix="rust-",
    fetch_hosts=(),
    control="webrcon",
    control_options=MappingProxyType(
        {
            "port": RCON_PORT,
            "secret_env": "RUST_RCON_PASSWORD",
            "a2s_port": QUERY_PORT,
            "a2s_port_env": "RUST_SERVER_QUERYPORT",
        }
    ),
    # only the server's own data (server/<identity>/) matters; the rest is the game
    default_backup_excludes=(
        "Bundles",
        "RustDedicated_Data",
        "RustDedicated",
        "steamapps",
        "linux64",
        "EasyAntiCheat",
        "*.so",
        "logs",
    ),
    tag_pattern=NO_TAG,
    env_values=MappingProxyType(ENV_VALUES),
    mount_image_volumes=True,
    install_retry_exits=(1,),
    env_ports=MappingProxyType(
        {"RUST_SERVER_PORT": "udp", "RUST_SERVER_QUERYPORT": "udp", "RUST_APP_PORT": "tcp"}
    ),
    restart_policy="no",
    # a stop before the server was ever ready (still downloading the game): nothing to save, no waiting
    not_ready_stop_timeout=steam.NOT_READY_STOP_TIMEOUT,
    strict_env=True,
    image_digests=IMAGE_DIGESTS,
    redact_env=("RUST_RCON_PASSWORD",),
)
