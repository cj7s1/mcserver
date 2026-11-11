"""Valheim (dedicated server on ghcr.io/community-valheim-tools/valheim-server).

What the panel knows about it (probe ``audit-2026-11-09/design/probe-6games.md`` section 2, observed on
``1.4.1`` and Valheim l-1.0.21):

* Image ``ghcr.io/community-valheim-tools/valheim-server:<pinned X.Y.Z>``; it downloads the game (SteamCMD
  app 896660, about 2 GB, kept twice) and checks it at every start: the server follows Steam's public
  branch. Root + ``PUID``/``PGID``; two folders (``config/`` = /config with the worlds and lists,
  ``opt/`` = /opt/valheim with the game).
* Ports: the game port on UDP (default 2456, ``SERVER_PORT``, published 1:1), the query port = game + 1
  and, with crossplay, game + 2 (both derived: the owner sets the game port only).
* No control at all: no console, no RCON, no save on request. SIGTERM saves the world (exit 0).
* Everything is passed on the command line from the environment: the server name, public listing,
  crossplay, the world name (chosen once) and the world preset/modifiers through ``SERVER_ARGS`` (only
  the whitelisted tokens the panel builds). The join password is required: at least 5 characters and it
  must not contain the server name (Iron Gate's rule; the probe saw the server start without it, the
  panel enforces it all the same).
* Lists: ``config/adminlist.txt``, ``bannedlist.txt`` and ``permittedlist.txt`` (one SteamID64 per line),
  edited by the panel; the image's ``*_IDS`` variables (which overwrite the files) are never set. No list
  of the players online (a private server answers no query).
* The world is the folder ``config/worlds_local/<world name>/`` (Valheim 1.0), older worlds the pair
  ``<world name>.db`` + ``.fwl``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError

from .base import CapabilityUnavailable, ConfigFile, GameOption, PortSpec, SecretSpec
from .foldersaves import FolderSaves
from .steamgame import SteamGameDefinition

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "valheim"
DEFAULT_REPOSITORY = "ghcr.io/community-valheim-tools/valheim-server"
IMAGE_TAG = "1.4.1"
GAME_PORT = 2456
PASSWORD = "server_password"
WORLDS_DIR = "config/worlds_local"
WORLD_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
STEAM_ID = r"^\d{17}$"
PASSWORD_PATTERN = r"^[A-Za-z0-9_.,:!?@#%+=-]{5,64}$"
# the server name the image passes on the command line (the agent's pattern)
NAME_PATTERN = r"^[A-Za-z0-9 _.,:!?'()#+@&-]{1,64}$"
NAME_HELP = "1 to 64 letters, digits, spaces and _ . , : ! ? ' ( ) # + @ & -"
LIST_FILES = {
    "whitelist": "config/permittedlist.txt",
    "ops": "config/adminlist.txt",
    "bans": "config/bannedlist.txt",
}
LIST_HEADERS = {
    "whitelist": "// List permitted players ID ONE per line",
    "ops": "// List admin players ID  ONE per line",
    "bans": "// List banned players ID  ONE per line",
}
PRESETS = ["", "normal", "casual", "easy", "hard", "hardcore", "immersive", "hammer"]
MODIFIERS = {
    "combat": ["", "veryeasy", "easy", "hard", "veryhard"],
    "deathpenalty": ["", "casual", "veryeasy", "easy", "hard", "hardcore"],
    "resources": ["", "muchless", "less", "more", "muchmore", "most"],
    "raids": ["", "none", "muchless", "less", "more", "muchmore"],
    "portals": ["", "casual", "hard", "veryhard"],
}
SETKEYS = {
    "nobuildcost": "No build cost",
    "playerevents": "Raids depend on each player",
    "passivemobs": "Passive enemies",
    "nomap": "No map",
}

SERVER_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "server_name",
        "type": "string",
        "label": "Server name",
        "pattern": NAME_PATTERN,
        "pattern_help": NAME_HELP,
        "default": "Valheim server",
        "initial": True,
    },
    {
        "key": "public",
        "type": "bool",
        "label": "List in the community server browser",
        "help": "Off: players join by address (Join IP). The query port answers only for listed servers.",
        "default": False,
        "initial": True,
    },
    {
        "key": "crossplay",
        "type": "bool",
        "label": "Crossplay (Xbox and Microsoft Store players)",
        "help": "Uses PlayFab's relay and the game port + 2 (published while this is on).",
        "default": False,
        "initial": True,
    },
    {
        "key": "save_interval",
        "type": "int",
        "label": "Save every (seconds)",
        "min": 60,
        "max": 7200,
        "default": 1800,
        "group": "Saving",
    },
    {
        "key": "backups",
        "type": "int",
        "label": "World backups the game keeps",
        "help": "Valheim's own copies next to the world (not the panel's backups).",
        "min": 0,
        "max": 20,
        "default": 4,
        "group": "Saving",
    },
    {
        "key": "preset",
        "type": "enum",
        "label": "World preset",
        "help": "Empty: Valheim's normal settings. Applies to the world the server runs.",
        "options": PRESETS,
        "default": "",
        "group": "World modifiers",
        "initial": True,
    },
    *(
        {
            "key": f"modifier_{name}",
            "type": "enum",
            "label": f"Modifier: {name}",
            "options": values,
            "default": "",
            "group": "World modifiers",
        }
        for name, values in MODIFIERS.items()
    ),
    *(
        {"key": f"key_{key}", "type": "bool", "label": label, "default": False, "group": "World modifiers"}
        for key, label in SETKEYS.items()
    ),
)
SERVER_DEFAULTS = {f["key"]: f["default"] for f in SERVER_FIELDS}

CONFIGS = (
    ConfigFile(
        id="server",
        path="(container settings)",
        codec="json",
        label="Server",
        help="Kept by the panel and passed to the server on its command line at its start (restart to apply).",
        fields=SERVER_FIELDS,
    ),
)

RESET_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "world_name",
        "type": "string",
        "label": "Name of the new world",
        "help": "Empty: the same name. Letters, digits, - and _ (the world folder's name).",
        "nullable": True,
        "pattern": WORLD_NAME_PATTERN,
        "pattern_help": "1 to 32 letters, digits, - and _",
    },
)


def valheim_name(text: str) -> str:
    """A default server name from the panel's name: the characters the image's pattern allows."""
    name = " ".join(re.sub(r"[^A-Za-z0-9 _.,:!?'()#+@&-]+", " ", text or "").split())[:64].strip()
    return name or "Valheim server"


def server_args(values: dict[str, Any]) -> str:
    """The ``SERVER_ARGS`` the panel builds: only whitelisted tokens, defaults left out."""
    args: list[str] = []
    if int(values["save_interval"]) != SERVER_DEFAULTS["save_interval"]:
        args.append(f"-saveinterval {int(values['save_interval'])}")
    if int(values["backups"]) != SERVER_DEFAULTS["backups"]:
        args.append(f"-backups {int(values['backups'])}")
    if values.get("preset") in PRESETS[1:]:
        args.append(f"-preset {values['preset']}")
    for name, options in MODIFIERS.items():
        value = values.get(f"modifier_{name}")
        if value in options[1:]:
            args.append(f"-modifier {name} {value}")
    for key in SETKEYS:
        if values.get(f"key_{key}"):
            args.append(f"-setkey {key}")
    return " ".join(args)


def parse_ids(text: str) -> list[str]:
    """The ids of a Valheim list file (comment lines start with //)."""
    out = []
    for line in (text or "").splitlines():
        value = line.strip()
        if value and not value.startswith("//") and value not in out:
            out.append(value)
    return out


class ValheimDefinition(SteamGameDefinition):
    id = GAME
    label = "Valheim"
    profile = GAME
    control = None
    repository = DEFAULT_REPOSITORY
    image_tag = IMAGE_TAG
    default_memory = 8192
    min_memory_mb = 4096
    default_stop_timeout = 120
    clean_exit_codes = frozenset({0})
    capability_set = frozenset({"players", "player_actions", "world_reset", "world_import_export", "config_files", "diagnostics"})
    player_name_pattern = STEAM_ID
    player_list_labels = (("whitelist", "Permitted (whitelist)"), ("ops", "Admins"), ("bans", "Banned"))
    virtual_configs = CONFIGS
    options = (
        GameOption(
            "world_name",
            "World name",
            "string",
            "Dedicated",
            "The world's folder name (config/worlds_local/<name>/): letters, digits, - and _. A world reset can "
            "start a world under a new name.",
            pinned=True,
            pattern=WORLD_NAME_PATTERN,
            pattern_help="1 to 32 letters, digits, - and _",
        ),
    )
    secret_specs = (
        SecretSpec(
            PASSWORD,
            "Join password",
            "Players need it to join. At least 5 characters, and it must not contain the server name.",
            required=True,
            pattern=PASSWORD_PATTERN,
            pattern_help="5 to 64 letters, digits and _ . , : ! ? @ # % + = -",
        ),
    )
    fixed_version = {
        "id": "public",
        "label": "Steam public branch",
        "help": "The image checks the game at every start, so the server runs the current public build (players' "
        "clients update themselves). There is no version to choose; 'update' means restart.",
        # "Update now" is a restart: the image downloads the newest build of the branch at every start
        "updates_on_restart": True,
    }
    notes = (
        "Dedicated server on the community-valheim-tools image. The first start downloads the game (about 4 GB on "
        "disk: the image keeps two copies) and takes about a minute; every start checks the game files.",
        "Needs at least 4 GB of memory (8 GB by default).",
        "Forward the game port and the next one (game + 1) on UDP; with crossplay also game + 2.",
        "The join password needs at least 5 characters and must not contain the server name.",
        "Valheim has no remote console: admins, bans and the permitted list are files the panel edits; there is "
        "no list of the players online.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this UDP port (Valheim's default is 2456). The query port is the next number.",
            one_to_one=True,
        ),
        PortSpec(
            "query",
            "Steam query",
            GAME_PORT + 1,
            "udp",
            GAME_PORT + 1,
            "Always the game port + 1 (UDP); the server browser and Join IP use it.",
            offset_of="game",
            offset=1,
            one_to_one=True,
        ),
        PortSpec(
            "crossplay",
            "Crossplay",
            GAME_PORT + 2,
            "udp",
            GAME_PORT + 2,
            "Always the game port + 2 (UDP); published only while crossplay is on.",
            offset_of="game",
            offset=2,
            one_to_one=True,
            when=("server", "crossplay", True),
        ),
    )

    def __init__(self) -> None:
        self.saves = FolderSaves(
            self,
            folder=WORLDS_DIR,
            markers=(r".+\.fwl2?",),
            marker_help="a zip of a world folder from worlds_local/ (with _main.<n>.fwl2 and .db2 files), or of an older "
            "world's <name>.fwl and <name>.db",
        )

    # -- settings and secrets ----------------------------------------------------------------------
    def initial_config(self, file_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return {"server_name": valheim_name(str(data.get("name") or ""))}

    @staticmethod
    def _name_in_password(name: str, password: str) -> bool:
        return bool(name) and name.strip().lower() in password.lower()

    def secret_error(
        self, key: str, value: str, *, instance: Instance | None = None, data: dict[str, Any] | None = None
    ) -> str | None:
        error = super().secret_error(key, value, instance=instance, data=data)
        if error or key != PASSWORD:
            return error
        if instance is not None:
            name = str(self.config_values(instance, "server")["server_name"])
        else:
            chosen = ((data or {}).get("settings") or {}).get("server") or {}
            name = str(chosen.get("server_name") or valheim_name(str((data or {}).get("name") or "")))
        if self._name_in_password(name, value):
            return "The join password must not contain the server name (Valheim refuses it)"
        return None

    def check_config(self, file_id: str, values: dict[str, Any], *, instance: Instance | None = None, data: Any = None) -> None:
        if file_id != "server" or instance is None:
            return
        password = instance.secret_values.get(PASSWORD) or ""
        if self._name_in_password(str(values.get("server_name") or ""), password):
            raise ApiError("The server name must not be part of the join password (Valheim refuses it)", code="invalid_setting")

    def world_name(self, instance: Instance) -> str:
        name = str((instance.game_options or {}).get("world_name") or "Dedicated")
        return name if re.fullmatch(WORLD_NAME_PATTERN, name) else "Dedicated"

    def game_env(self, instance: Instance) -> dict[str, str]:
        values = self.config_values(instance, "server")
        env = {
            "SERVER_NAME": str(values["server_name"]),
            "SERVER_PORT": str(self.host_port(instance, "game", GAME_PORT)),
            "WORLD_NAME": self.world_name(instance),
            "SERVER_PUBLIC": "true" if values["public"] else "false",
            "CROSSPLAY": "true" if values["crossplay"] else "false",
        }
        if password := instance.secret_values.get(PASSWORD):
            env["SERVER_PASS"] = password
        if args := server_args(values):
            env["SERVER_ARGS"] = args
        return env

    # -- players: the three list files ----------------------------------------------------------------
    def _client(self, instance: Instance) -> AgentClient:
        from apps.nodes.client import AgentClient

        return AgentClient(instance.node)

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        client = self._client(instance)
        running = bool(client.status(instance.id).get("running"))
        result: dict[str, Any] = {"running": running, "labels": dict(self.player_list_labels), "ip_bans": [], "known_players": []}
        for key, path in LIST_FILES.items():
            ids = parse_ids(_read_text(client, instance, path) or "")
            result[key] = [{"name": i, "reason": ""} if key == "bans" else {"name": i} for i in ids]
        result["no_online_list"] = True
        # the lists are files the panel writes: also while the server is stopped (read at its next start)
        result["changes_while_stopped"] = True
        result["id_hint"] = "Valheim's lists hold SteamID64s (17 digits, e.g. from steamid.io)."
        return result

    def player_action(self, instance: Instance, action: str, name: str, reason: str = "") -> dict[str, Any]:
        self.require(instance, "player_actions")
        key = {
            "whitelist_add": "whitelist",
            "whitelist_remove": "whitelist",
            "op": "ops",
            "deop": "ops",
            "ban": "bans",
            "pardon": "bans",
        }.get(action)
        if key is None:
            raise ApiError("Valheim has no console: players cannot be kicked from the panel", code="invalid_action")
        steam_id = self.validate_player_name(name)
        client = self._client(instance)
        path = LIST_FILES[key]
        text = _read_text(client, instance, path)
        ids = parse_ids(text or "")
        lines = (text or LIST_HEADERS[key]).splitlines()
        when = "Valheim reads its lists when it starts: the change applies at the latest at the next start"
        if action in ("whitelist_remove", "deop", "pardon"):
            if steam_id not in ids:
                raise ApiError(f"{steam_id} is not in {path}", code="not_listed", status_code=404)
            kept = [ln for ln in lines if ln.strip() != steam_id]
            client.write_text(instance.id, path, "\n".join(kept) + "\n")
            return {"via": "file", "output": f"Removed {steam_id} from {path}. {when}."}
        if steam_id in ids:
            return {"via": "file", "output": f"{steam_id} is already in {path}"}
        client.write_text(instance.id, path, "\n".join([*lines, steam_id]) + "\n")
        return {"via": "file", "output": f"Added {steam_id} to {path}. {when}."}

    def online_players(self, instance: Instance) -> dict[str, Any]:
        """Valheim answers no query on a private server and has no console: no list (409)."""
        raise CapabilityUnavailable(
            "Valheim has no way to list the players online (no console, and no query on a private server)",
            details={"game": self.id, "capability": "online_players"},
        )

    def consistency_note(self, instance: Instance) -> str:
        return ""  # (no save on request: consistent only while stopped)

    # -- the world -------------------------------------------------------------------------------
    def world_reset_fields(self) -> tuple[dict[str, Any], ...]:
        return RESET_FIELDS

    def world_label(self, instance: Instance) -> str:
        return self.world_name(instance)

    def world_paths(self, client: AgentClient, instance: Instance, purpose: str, params: dict[str, Any]) -> list[str]:
        name = self.world_name(instance)
        folder = f"{WORLDS_DIR}/{name}"
        if purpose == "export":
            return [folder]
        # (and the files of the older format, with the game's .old copies)
        return [folder, *(f"{WORLDS_DIR}/{name}{ext}" for ext in (".db", ".fwl", ".db.old", ".fwl.old"))]

    def world_target(self, client: AgentClient, instance: Instance, root_name: str, params: dict[str, Any]) -> str:
        return f"{WORLDS_DIR}/{self.world_name(instance)}"

    def place_world(self, client: AgentClient, instance: Instance, source: str, target: str) -> str | None:
        """A 1.0 world folder (``*.fwl2``) becomes the world folder; an older world (``<name>.fwl`` +
        ``.db``) goes into worlds_local/ and the world name follows it."""
        from .foldersaves import _entries

        files = [str(e.get("name")) for e in _entries(client, instance, source) if e.get("type") == "file"]
        if any(f.endswith(".fwl2") for f in files):
            client.rename(instance.id, source, target, timeout=600)
            return None
        stems = sorted({f[: -len(".fwl")] for f in files if f.endswith(".fwl")})
        if len(stems) != 1 or not re.fullmatch(WORLD_NAME_PATTERN, stems[0]):
            raise ApiError(
                "The zip must hold one world: <name>.fwl and <name>.db, with a name of letters, digits, - and _",
                code="invalid_world",
            )
        stem = stems[0]
        for f in files:
            if f.startswith(f"{stem}.") and f[len(stem) :] in (".db", ".fwl", ".db.old", ".fwl.old"):
                # (never over another world's files: the agent refuses an existing name, the job says so)
                client.rename(instance.id, f"{source}/{f}", f"{WORLDS_DIR}/{f}")
        self._set_world_name(instance, stem)
        return f"{WORLDS_DIR}/{stem}.fwl"

    def _set_world_name(self, instance: Instance, name: str) -> None:
        from apps.instances.models import Instance as InstanceModel
        from apps.instances.services import bump_revision

        instance.refresh_from_db(fields=["game_options"])
        options = dict(instance.game_options or {})
        if options.get("world_name") == name:
            return
        options["world_name"] = name
        instance.game_options = options
        InstanceModel.objects.filter(pk=instance.pk).update(game_options=options)
        bump_revision(instance)

    def prepare_world_reset(self, client: AgentClient, instance: Instance, values: dict[str, Any], seed: Any) -> str:
        name = str(values.get("world_name") or "").strip()
        if name:
            if not re.fullmatch(WORLD_NAME_PATTERN, name):
                raise ApiError("World names use 1 to 32 letters, digits, - and _", code="invalid_setting")
            self._set_world_name(instance, name)
        return ""

    def world_notes(self, instance: Instance) -> list[str]:
        return [
            "Valheim keeps its own world backups next to the world; they stay in place on a reset.",
            "Valheim cannot save on request: a download of a running server's world holds its last save.",
        ]


def _read_text(client: AgentClient, instance: Instance, path: str) -> str | None:
    try:
        return client.read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return None
        raise


__all__ = ["GAME", "ValheimDefinition", "parse_ids", "server_args"]
