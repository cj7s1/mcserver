"""Rust (dedicated server on didstopia/rust-server, pinned by digest).

What the panel knows about it (probe ``audit-2026-11-09/design/probe-6games.md`` section 6; the image was
inspected, the game was not downloaded on the probe's desktop):

* Image ``didstopia/rust-server@sha256:<pinned digest>``: the image has no usable version tags (only
  ``latest``), so the panel release pins a digest the agent's profile allows. It runs SteamCMD (app 258550)
  at every start: the server follows Steam's public branch (about 10 GB). Root + ``PUID``/``PGID``.
* Ports: the game on UDP (default 28015, ``RUST_SERVER_PORT``), the Steam query port on UDP (default 28017,
  ``RUST_SERVER_QUERYPORT``) and, with Rust+ on, the companion app port on TCP (default 28082,
  ``RUST_APP_PORT``), all published 1:1. WebRCON (28016/tcp) and the image's web UI (8080/tcp) stay
  internal.
* Control: the ``webrcon`` driver (WebSocket, password = the panel's control secret, which the image
  gets as ``RUST_RCON_PASSWORD``). Players with ``playerlist``; kick, ban/unban, owners (``ownerid`` /
  ``removeowner`` + ``server.writecfg``); the status (players online) through A2S on the query port.
* Settings: a virtual config (name, description, players, world size, seed, save interval, Rust+)
  passed as variables. The seed is fixed at creation (random unless chosen) so the map stays the same.
* The world is the identity folder ``server/<identity>/``: the map ``*.map``, the saves ``*.sav*`` and
  the player databases. A reset (a "wipe") moves the map, the saves and the world's databases aside
  (blueprints only when asked) and starts with a new seed; an import replaces the whole identity
  folder; a download is a zip of it. Owners and bans are in ``cfg/users.cfg`` and ``cfg/bans.cfg``.
* Stop: ``server.save`` through WebRCON, then SIGTERM (the image's trap sends ``quit``, which saves).
"""

from __future__ import annotations

import re
import secrets as _secrets
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError

from .base import ConfigFile, GameOption, PortSpec
from .foldersaves import FolderSaves, _entries
from .steamgame import SteamGameDefinition

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "rust"
DEFAULT_REPOSITORY = "didstopia/rust-server"
IMAGE_DIGEST = "sha256:c60a3ab044eb69019616951087834468aed478a11b449391376b23db05ddc38a"
GAME_PORT = 28015
QUERY_PORT = 28017
APP_PORT = 28082
RCON_PORT = 28016
WEB_PORT = 8080
IDENTITY_PATTERN = r"^[a-z0-9_-]{1,32}$"
STEAM_ID = r"^7656\d{13}$"
# texts on the start line: no quotes, backslashes, ; or control characters, not starting with + or -
TEXT_PATTERN = r'^(?![+-])[^"\\;\x00-\x1f\x7f]{1,64}$'
DESCRIPTION_PATTERN = r'^(?:(?![+-])[^"\\;\x00-\x1f\x7f]{1,512})?$'
MAX_SEED = 2147483647
_CFG_LINE = re.compile(r'^\s*(ownerid|moderatorid|banid)\s+(\d{17})\s*(?:"([^"]*)")?\s*(?:"([^"]*)")?')

SERVER_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "server_name",
        "type": "string",
        "label": "Server name",
        "pattern": TEXT_PATTERN,
        "pattern_help": "1 to 64 characters without quotes, backslashes or ;, not starting with + or -",
        "default": "Rust server",
        "initial": True,
    },
    {
        "key": "description",
        "type": "string",
        "label": "Description",
        "pattern": DESCRIPTION_PATTERN,
        "pattern_help": "at most 512 characters without quotes, backslashes or ;, not starting with + or -",
        "default": "",
    },
    {"key": "max_players", "type": "int", "label": "Max players", "min": 1, "max": 500, "default": 50, "initial": True},
    {
        "key": "world_size",
        "type": "int",
        "label": "Map size",
        "help": "1000 to 6000 (3500 is Rust's default). A new size makes a new map: use a world reset.",
        "min": 1000,
        "max": 6000,
        "default": 3500,
        "initial": True,
        "group": "Map",
    },
    {
        "key": "seed",
        "type": "int",
        "label": "Map seed",
        "help": "Chosen at random when the server is created; a world reset can pick a new one.",
        "min": 0,
        "max": MAX_SEED,
        "default": None,
        "nullable": True,
        "initial": True,
        "group": "Map",
    },
    {
        "key": "save_interval",
        "type": "int",
        "label": "Save every (seconds)",
        "min": 60,
        "max": 3600,
        "default": 600,
        "group": "Saving",
    },
    {
        "key": "rust_plus",
        "type": "bool",
        "label": "Rust+ companion app",
        "help": "Publishes the app port (TCP) while on.",
        "default": False,
        "group": "Network",
    },
)

CONFIGS = (
    ConfigFile(
        id="server",
        path="(container settings)",
        codec="json",
        label="Server",
        help="Kept by the panel and passed to the server at its start (restart to apply).",
        fields=SERVER_FIELDS,
    ),
)
RESET_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "seed",
        "type": "int",
        "label": "Seed of the new map",
        "help": "Empty: a new random map.",
        "min": 0,
        "max": MAX_SEED,
        "nullable": True,
    },
    {
        "key": "wipe_blueprints",
        "type": "bool",
        "label": "Also wipe blueprints",
        "help": "Moves the players' learned blueprints (player.blueprints.*.db) aside as well.",
        "default": False,
    },
)
# the files of a map wipe (Facepunch's wiki and community practice; blueprints only when asked)
WIPE_FILES = re.compile(r".+\.map$|.+\.sav(\.\d+)?$|sv\.files\.\d+\.db$|player\.(states|deaths)\.\d+\.db$")
BLUEPRINTS = re.compile(r"player\.blueprints\.\d+\.db$")


def parse_cfg(text: str) -> dict[str, list[dict[str, str]]]:
    """``cfg/users.cfg`` / ``cfg/bans.cfg`` lines: ``ownerid|moderatorid|banid <SteamID64> "name" "reason"``."""
    out: dict[str, list[dict[str, str]]] = {"ownerid": [], "moderatorid": [], "banid": []}
    for line in (text or "").splitlines():
        if m := _CFG_LINE.match(line):
            kind, steam_id, name, reason = m.groups()
            if not any(e["id"] == steam_id for e in out[kind]):
                out[kind].append({"id": steam_id, "name": (name or "")[:64], "reason": (reason or "")[:200]})
    return out


def random_seed() -> int:
    return _secrets.randbelow(MAX_SEED) + 1


class RustDefinition(SteamGameDefinition):
    id = GAME
    label = "Rust"
    profile = GAME
    control = "webrcon"
    repository = DEFAULT_REPOSITORY
    image_digest = IMAGE_DIGEST
    default_memory = 16384
    min_memory_mb = 10240
    default_stop_timeout = 180
    clean_exit_codes = frozenset({0})
    retryable_install_exits = frozenset({1})  # the image exits 1 when SteamCMD fails
    first_start_timeout = 2 * 3600  # the first start downloads about 10 GB and generates the map
    capability_set = frozenset(
        {
            "console_commands",
            "broadcast",
            "players",
            "player_actions",
            "query",
            "consistent_backups",
            "world_reset",
            "world_import_export",
            "config_files",
            "diagnostics",
        }
    )
    stop_words = frozenset({"quit", "global.quit", "restart", "global.restart"})
    # Rust's console refusals of kick/unban/removeowner ("Player not found", "User ... isn't banned")
    refusal_pattern = (
        r"(?i)\bnot found\b|couldn'?t find|isn'?t (?:banned|an owner)|is not (?:banned|an owner)|unknown command|invalid"
    )
    refuse_stop_commands = True
    command_hint = "Rust console commands, e.g. status, say Hello, server.save, playerlist."
    flush_command = "server.save"
    # a SteamID64, or the name of a player who is online (resolved to the id)
    player_name_pattern = r'^[^"\\;\x00-\x1f\x7f]{1,64}$'
    player_list_labels = (("ops", "Owners (admins)"), ("bans", "Bans"))
    virtual_configs = CONFIGS
    options = (
        GameOption(
            "identity",
            "Server identity",
            "string",
            "rust",
            "The folder of the server's map and saves (server/<identity>/): lower-case letters, digits, - and _. Chosen once.",
            pinned=True,
            pattern=IDENTITY_PATTERN,
            pattern_help="1 to 32 lower-case letters, digits, - and _",
        ),
    )
    fixed_version = {
        "id": "public",
        "label": "Steam public branch",
        "help": "The image runs SteamCMD at every start, so the server always runs the current build (players' clients "
        "update themselves). There is no version to choose; 'update' means restart.",
        # "Update now" is a restart: the image downloads the newest build of the branch at every start
        "updates_on_restart": True,
    }
    notes = (
        "Dedicated server on the didstopia/rust-server image (pinned by digest). The first start downloads the game "
        "(about 10 GB on disk) and generates the map, which takes several minutes; every start checks for an update.",
        "Needs at least 10 GB of memory (16 GB by default; big maps need more).",
        "Forward the game port and the query port on UDP, and the Rust+ app port on TCP while Rust+ is on. WebRCON "
        "stays inside the panel's network.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this UDP port (Rust's default is 28015).",
            one_to_one=True,
            avoid=frozenset({RCON_PORT}),
        ),
        PortSpec(
            "query",
            "Steam query",
            QUERY_PORT,
            "udp",
            QUERY_PORT,
            "The server browser asks this UDP port (default 28017); it must differ from the game port.",
            one_to_one=True,
            avoid=frozenset({RCON_PORT}),
        ),
        PortSpec(
            "app",
            "Rust+ app",
            APP_PORT,
            "tcp",
            APP_PORT,
            "The Rust+ companion app connects here (TCP); published only while Rust+ is on.",
            one_to_one=True,
            when=("server", "rust_plus", True),
            avoid=frozenset({RCON_PORT, WEB_PORT}),
        ),
    )

    def __init__(self) -> None:
        self.saves = FolderSaves(
            self,
            folder="server",
            markers=(r".+\.map", r".+\.sav(\.\d+)?"),
            marker_help="a zip of a server identity folder (server/<identity>/) with the map (.map) and saves (.sav)",
        )

    # -- settings --------------------------------------------------------------------------------
    def identity(self, instance: Instance) -> str:
        name = str((instance.game_options or {}).get("identity") or "rust")
        return name if re.fullmatch(IDENTITY_PATTERN, name) else "rust"

    def initial_config(self, file_id: str, data: dict[str, Any]) -> dict[str, Any]:
        name = re.sub(r'["\\;\x00-\x1f\x7f]+', " ", str(data.get("name") or "")).strip().lstrip("+-").strip()[:64]
        return {"server_name": name} if name else {}

    def create_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        fields = super().create_fields(data)
        server = fields["game_options"]["server"]
        if server.get("seed") is None:
            server["seed"] = random_seed()  # fixed from now on: the map stays the same at every start
        return fields

    def write_virtual_config(
        self, client: AgentClient, instance: Instance, cfg: ConfigFile, values: dict[str, Any]
    ) -> dict[str, Any]:
        if cfg.file_id == "server" and "seed" in values and values["seed"] is None:
            values = {**values, "seed": random_seed()}  # (cleared: a new random map at the next start)
        return super().write_virtual_config(client, instance, cfg, values)

    def game_env(self, instance: Instance) -> dict[str, str]:
        values = self.config_values(instance, "server")
        env = {
            "RUST_SERVER_NAME": str(values["server_name"]),
            "RUST_SERVER_IDENTITY": self.identity(instance),
            "RUST_SERVER_PORT": str(self.host_port(instance, "game", GAME_PORT)),
            "RUST_SERVER_QUERYPORT": str(self.host_port(instance, "query", QUERY_PORT)),
            "RUST_SERVER_MAXPLAYERS": str(values["max_players"]),
            "RUST_SERVER_WORLDSIZE": str(values["world_size"]),
            "RUST_SERVER_SAVE_INTERVAL": str(values["save_interval"]),
            "RUST_APP_PORT": str(self.host_port(instance, "app", APP_PORT)) if values["rust_plus"] else "-1",
        }
        if values.get("seed") is not None:
            env["RUST_SERVER_SEED"] = str(values["seed"])
        # always set: empty leaves the argument out (Rust's own empty description) instead of the image's default
        # "This is a Rust server running inside a Docker container!"
        env["RUST_SERVER_DESCRIPTION"] = str(values.get("description") or "")
        return env

    # -- console and players -------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        return text.strip().lstrip("/").strip()

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        line = self.chat_text(text, message)
        if line:
            client.game_action(instance.id, "say", {"message": line}, timeout=15)

    def _players(self, client: AgentClient, instance: Instance) -> list[dict[str, str]]:
        answer = client.game_action(instance.id, "players", {}, timeout=20)
        out = []
        for entry in answer.get("players") or []:
            if not isinstance(entry, dict):
                continue
            steam_id = str(entry.get("SteamID") or entry.get("steamid") or "")
            name = str(entry.get("DisplayName") or entry.get("name") or "").strip()[:64]
            if steam_id or name:
                out.append({"name": name or steam_id, "id": steam_id})
        return out

    def online_players(self, instance: Instance, *, max_age: int = 30) -> dict[str, Any]:
        """WebRCON's ``playerlist`` (names and SteamIDs; the metrics count through A2S instead)."""
        from django.core.cache import cache

        from apps.nodes.client import AgentClient

        key = f"players:online:{instance.pk}"
        if max_age and (hit := cache.get(key)) is not None:
            return hit
        players = self._players(AgentClient(instance.node), instance)
        result = {
            "online": len(players),
            "max": self.config_values(instance, "server")["max_players"],
            "players": [p["name"] for p in players],
            "entries": players,
        }
        cache.set(key, result, max_age or 1)
        return result

    def _cfg(self, client: AgentClient, instance: Instance, name: str) -> dict[str, list[dict[str, str]]]:
        return parse_cfg(_read_text(client, instance, f"server/{self.identity(instance)}/cfg/{name}") or "")

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        from apps.nodes.client import AgentClient

        client = AgentClient(instance.node)
        running = bool(client.status(instance.id).get("running"))
        users = self._cfg(client, instance, "users.cfg")
        bans = self._cfg(client, instance, "bans.cfg")
        result: dict[str, Any] = {
            "running": running,
            "labels": dict(self.player_list_labels),
            "whitelist": [],
            "ops": [{"name": e["id"], "display_name": e["name"]} for e in users["ownerid"]],
            "bans": [{"name": e["id"], "display_name": e["name"], "reason": e["reason"]} for e in bans["banid"]],
            "ip_bans": [],
            "known_players": [],
            "id_hint": "Owners and bans use SteamID64s (17 digits); an online player's name works too.",
            # owners and bans change through WebRCON: only while the server runs
            "changes_while_stopped": False,
        }
        if running:
            try:
                result["online"] = self.online_players(instance, max_age=0)
            except ApiError as exc:
                result["online"] = {"error": str(exc.detail)}
        return result

    def player_action(self, instance: Instance, action: str, name: str, reason: str = "") -> dict[str, Any]:
        from django.core.cache import cache

        from apps.nodes.client import AgentClient

        self.require(instance, "player_actions")
        actions = {"kick": "kick", "ban": "ban", "pardon": "unban", "op": "owner_add", "deop": "owner_remove"}
        if action not in actions:
            raise ApiError("Rust servers have no whitelist (owners, bans and kicks only)", code="invalid_action")
        name = self.validate_player_name(name)
        client = AgentClient(instance.node)
        if not client.status(instance.id).get("running"):
            raise ApiError(
                "The server is not running: Rust changes owners and bans through WebRCON", code="not_running", status_code=409
            )
        steam_id, display = name, ""
        if not re.fullmatch(STEAM_ID, name):
            match = next((p for p in self._players(client, instance) if p["name"].lower() == name.lower() and p["id"]), None)
            if match is None:
                raise ApiError("Use the player's SteamID64, or the name of a player who is online", code="invalid_player_name")
            steam_id, display = match["id"], match["name"]
        params: dict[str, Any] = {"steamid": steam_id}
        text = self.chat_text(reason, limit=120)
        if action in ("kick", "ban", "op"):
            params["reason"] = text
        if action in ("ban", "op"):
            params["name"] = self.chat_text(display, limit=64)
        answer = client.game_action(instance.id, actions[action], params, timeout=30)
        cache.delete(f"players:online:{instance.pk}")
        output = self.checked_reply(answer.get("output"), said=(steam_id, display, name, params.get("reason") or ""))
        return {"via": "webrcon", "output": output}

    def masked_text(self, instance: Instance, path: str, content: str) -> str:
        """The WebRCON password (the control secret) is on Rust's command line; whatever of it reaches a file
        (the image's log copies in logs/) is hidden."""
        from apps.instances.spec import rcon_password

        value = rcon_password(instance)
        return content.replace(value, "********") if value and len(value) >= 8 else content

    def secret_bearing_folders(self, instance: Instance) -> tuple[str, ...]:
        return ("logs",)  # the image tees the server's console into logs/<identity>_<date>.txt

    def consistency_note(self, instance: Instance) -> str:
        return (
            "the world was saved through WebRCON (server.save) right before the copy; the game is not paused, so a save "
            "during the copy can be in the backup as well"
        )

    # -- the world -------------------------------------------------------------------------------
    def save_now(self, client: AgentClient, instance: Instance) -> bool:
        client.game_action(instance.id, "save", {}, timeout=120)
        return True

    def world_reset_fields(self) -> tuple[dict[str, Any], ...]:
        return RESET_FIELDS

    def world_label(self, instance: Instance) -> str:
        return self.identity(instance)

    def world_paths(self, client: AgentClient, instance: Instance, purpose: str, params: dict[str, Any]) -> list[str]:
        folder = f"server/{self.identity(instance)}"
        if purpose != "reset":
            return [folder]  # an import replaces (and an export carries) the whole identity folder
        wipe_blueprints = bool(params.get("wipe_blueprints"))
        return [
            f"{folder}/{e['name']}"
            for e in _entries(client, instance, folder)
            if e.get("type") == "file"
            and (
                WIPE_FILES.fullmatch(str(e.get("name") or ""))
                or (wipe_blueprints and BLUEPRINTS.fullmatch(str(e.get("name") or "")))
            )
        ]

    def world_target(self, client: AgentClient, instance: Instance, root_name: str, params: dict[str, Any]) -> str:
        return f"server/{self.identity(instance)}"

    def prepare_world_reset(self, client: AgentClient, instance: Instance, values: dict[str, Any], seed: Any) -> str:
        """A new map: the chosen seed, else a random one (the world size stays what it is set to)."""
        from apps.instances.models import Instance as InstanceModel
        from apps.instances.services import bump_revision

        chosen = values.get("seed")
        if chosen is None:
            chosen = seed
        new_seed = int(chosen) if chosen is not None else random_seed()
        if not 0 <= new_seed <= MAX_SEED:
            raise ApiError(f"Rust seeds are 0 to {MAX_SEED}", code="invalid_setting")
        instance.refresh_from_db(fields=["game_options"])
        options = dict(instance.game_options or {})
        server = dict(options.get("server") or {})
        server["seed"] = new_seed
        options["server"] = server
        instance.game_options = options
        InstanceModel.objects.filter(pk=instance.pk).update(game_options=options)
        bump_revision(instance)
        return str(new_seed)

    def world_notes(self, instance: Instance) -> list[str]:
        return [
            "A world reset is a map wipe: the map, the saves and the world's databases are moved aside; blueprints only "
            "when you choose so. Owners and bans (cfg/) stay.",
        ]


def _read_text(client: AgentClient, instance: Instance, path: str) -> str | None:
    try:
        return client.read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return None
        raise


__all__ = ["GAME", "RustDefinition", "parse_cfg"]
