"""Factorio (headless server on factoriotools/factorio, rootless tags): the second game definition.

What the panel knows about it (docs/design/GAME-DEFINITIONS.md 8.2, corrected by the spike of
2026-11-10):

* Image ``factoriotools/factorio:<exact version>-rootless`` (no ``stable``/``latest`` tags: client
  and server versions must match, so a server is pinned and updated on purpose). The rootless image
  runs as any uid with no capabilities; the agent's ``factorio`` profile sets the user and mounts the
  server folder at ``/factorio``.
* One named port: the game, 34197/udp. RCON (27015/tcp) is internal only: the agent's profile never
  publishes it.
* The control secret (``Instance.rcon_password``) is written to ``config/rconpw`` before every
  apply (``control_secret_file``); the image passes it to the server on its command line, the agent's
  ``rcon`` driver reads it from the same file.
* Saves are zip files in ``saves/``. The server creates ``saves/<save_name>.zip`` from
  ``config/map-gen-settings.json`` / ``map-settings.json`` when it does not exist
  (``GENERATE_NEW_SAVE`` with an explicit ``SAVE_NAME``) and loads the newest save
  (``LOAD_LATEST_SAVE``: after an unclean exit that is an autosave). A world reset moves every save
  aside and an import puts a zip under that name as the only save, so it is the newest one.
* Space Age is chosen once, at creation (``game_options.space_age``); the image enables it by default
  and switching it under an existing save silently converts the save at its next save.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError, UpstreamError

from .base import ConfigFile, GameDefinition, GameOption, PortSpec

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "factorio"
DEFAULT_REPOSITORY = "factoriotools/factorio"
TAG_SUFFIX = "-rootless"
GAME_PORT = 34197
SAVE_NAME = "world"
SAVES_DIR = "saves"
SET_ASIDE_DIR = "saves-replaced"
SECRET_FILE = "config/rconpw"
VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,4}$")
PLAYER_NAME = r"^[A-Za-z0-9_.-]{1,60}$"
HUB_TAGS = "https://hub.docker.com/v2/repositories/{repo}/tags"
LATEST_RELEASES = "https://factorio.com/api/latest-releases"
VERSIONS_TTL = 3600

# The 2.1 headless example (data/server-settings.example.json) without its comment keys, with
# visibility.public off: a public listing needs factorio.com credentials and shows the server to
# everyone (the image copies the example, which is public, when the file is missing).
SERVER_SETTINGS_DEFAULTS: dict[str, Any] = {
    "name": "",
    "description": "",
    "tags": [],
    "max_players": 0,
    "visibility": {"public": False, "lan": True},
    "username": "",
    "password": "",
    "token": "",
    "game_password": "",
    "require_user_verification": True,
    "max_upload_in_kilobytes_per_second": 0,
    "max_upload_slots": 5,
    "minimum_latency_in_ticks": 0,
    "max_heartbeats_per_second": 60,
    "ignore_player_limit_for_returning_players": False,
    "allow_commands": "admins-only",
    "autosave_interval": 10,
    "autosave_slots": 5,
    "afk_autokick_interval": 0,
    "auto_pause": True,
    "auto_pause_when_players_connect": False,
    "only_admins_can_pause_the_game": True,
    "autosave_only_on_server": True,
    "non_blocking_saving": False,
    "minimum_segment_size": 25,
    "minimum_segment_size_peer_count": 20,
    "maximum_segment_size": 100,
    "maximum_segment_size_peer_count": 10,
}

# data/map-gen-settings.example.json of 2.1 without comments ("seed": null = random).
MAP_GEN_DEFAULTS: dict[str, Any] = {
    "width": 0,
    "height": 0,
    "starting_area": 1,
    "peaceful_mode": False,
    "autoplace_controls": {
        "coal": {"frequency": 1, "size": 1, "richness": 1},
        "stone": {"frequency": 1, "size": 1, "richness": 1},
        "copper-ore": {"frequency": 1, "size": 1, "richness": 1},
        "iron-ore": {"frequency": 1, "size": 1, "richness": 1},
        "uranium-ore": {"frequency": 1, "size": 1, "richness": 1},
        "crude-oil": {"frequency": 1, "size": 1, "richness": 1},
        "water": {"frequency": 1, "size": 1},
        "trees": {"frequency": 1, "size": 1},
        "enemy-base": {"frequency": 1, "size": 1},
    },
    "cliff_settings": {"name": "cliff", "cliff_elevation_0": 10, "cliff_elevation_interval": 40, "richness": 1},
    "property_expression_names": {
        "control:moisture:frequency": "1",
        "control:moisture:bias": "0",
        "control:aux:frequency": "1",
        "control:aux:bias": "0",
    },
    "starting_points": [{"x": 0, "y": 0}],
    "seed": None,
}

SERVER_SETTINGS_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "name", "type": "string", "label": "Server name", "group": "Listing", "initial": True},
    {"key": "description", "type": "string", "label": "Description", "group": "Listing", "initial": True},
    {
        "key": "visibility.public",
        "type": "bool",
        "label": "List publicly on the Factorio server browser",
        "help": "Needs a factorio.com username and token below. Off by default: only players who know the address join.",
        "group": "Listing",
        "default": False,
        "initial": True,
    },
    {"key": "visibility.lan", "type": "bool", "label": "Announce on the local network", "group": "Listing", "default": True},
    {"key": "username", "type": "string", "label": "factorio.com username (public listing)", "group": "Listing"},
    {
        "key": "token",
        "type": "string",
        "label": "factorio.com token (public listing)",
        "group": "Listing",
        "secret": True,
        "help": "From https://factorio.com/profile. Stored in the server folder (config/server-settings.json).",
    },
    {
        "key": "game_password",
        "type": "string",
        "label": "Game password",
        "help": "Players need it to join. Empty: no password.",
        "group": "Access",
        "secret": True,
        "initial": True,
    },
    {
        "key": "require_user_verification",
        "type": "bool",
        "label": "Only players with a factorio.com account",
        "group": "Access",
        "default": True,
    },
    {
        "key": "max_players",
        "type": "int",
        "label": "Max players",
        "help": "0 = unlimited; admins can always join.",
        "group": "Access",
        "min": 0,
        "max": 65535,
        "default": 0,
        "initial": True,
    },
    {
        "key": "allow_commands",
        "type": "enum",
        "label": "Who may use console commands in game",
        "group": "Access",
        "options": ["admins-only", "true", "false"],
        "default": "admins-only",
    },
    {
        "key": "autosave_interval",
        "type": "int",
        "label": "Autosave every (minutes)",
        "group": "Saving",
        "min": 1,
        "max": 1440,
        "default": 10,
        "initial": True,
    },
    {"key": "autosave_slots", "type": "int", "label": "Autosave slots", "group": "Saving", "min": 1, "max": 100, "default": 5},
    {
        "key": "auto_pause",
        "type": "bool",
        "label": "Pause the game while nobody is online",
        "group": "Gameplay",
        "default": True,
    },
    {
        "key": "afk_autokick_interval",
        "type": "int",
        "label": "Kick idle players after (minutes, 0 = never)",
        "group": "Gameplay",
        "min": 0,
        "max": 10000,
        "default": 0,
    },
    {
        "key": "only_admins_can_pause_the_game",
        "type": "bool",
        "label": "Only admins can pause",
        "group": "Gameplay",
        "default": True,
    },
)

MAP_GEN_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "seed",
        "type": "int",
        "label": "Map seed",
        "help": "Empty: random. Used only when a new map is created (first start or a world reset).",
        "nullable": True,
        "min": 0,
        "max": 4294967295,
        "initial": True,
        "restart": False,
    },
    {
        "key": "width",
        "type": "int",
        "label": "Map width (tiles, 0 = infinite)",
        "min": 0,
        "max": 2000000,
        "default": 0,
        "restart": False,
    },
    {
        "key": "height",
        "type": "int",
        "label": "Map height (tiles, 0 = infinite)",
        "min": 0,
        "max": 2000000,
        "default": 0,
        "restart": False,
    },
    {
        "key": "peaceful_mode",
        "type": "bool",
        "label": "Peaceful mode",
        "default": False,
        "initial": True,
        "restart": False,
    },
    {
        "key": "starting_area",
        "type": "number",
        "label": "Starting area size (multiplier)",
        "min": 0.16,
        "max": 6,
        "default": 1,
        "restart": False,
    },
)

MAP_SETTINGS_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "pollution.enabled", "type": "bool", "label": "Pollution", "default": True, "restart": False},
    {"key": "enemy_evolution.enabled", "type": "bool", "label": "Enemy evolution", "default": True, "restart": False},
    {"key": "enemy_expansion.enabled", "type": "bool", "label": "Enemy expansion", "default": True, "restart": False},
)

CONFIG_FILES = (
    ConfigFile(
        id="server-settings",
        path="config/server-settings.json",
        codec="json",
        label="Server settings",
        help="Applied when the server starts.",
        secret_keys=("game_password", "token", "password"),
        fields=SERVER_SETTINGS_FIELDS,
    ),
    ConfigFile(
        id="map-gen-settings",
        path="config/map-gen-settings.json",
        codec="json",
        label="New map settings",
        help="Used only when a new map is created: the first start and a world reset. They do not change the current map.",
        fields=MAP_GEN_FIELDS,
    ),
    ConfigFile(
        id="map-settings",
        path="config/map-settings.json",
        codec="json",
        label="Map rules for new maps",
        help="Used only when a new map is created (the file is the image's example until the first start).",
        fields=MAP_SETTINGS_FIELDS,
    ),
)

# Player list files (JSON arrays; the ban list holds names or {"username", "reason"} objects)
LIST_FILES = {
    "whitelist": "config/server-whitelist.json",
    "ops": "config/server-adminlist.json",
    "bans": "config/server-banlist.json",
}
ONLINE_LINE = re.compile(r"^\s*(\S.*?)\s+\(online\)\s*$")
COUNT_LINE = re.compile(r"(?:Online players|Players) \((\d+)\):")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


class FactorioDefinition(GameDefinition):
    id = GAME
    label = "Factorio"
    profile = GAME
    control = "rcon"
    memory_model = "plain"
    capability_set = frozenset(
        {
            "console_commands",
            "broadcast",
            "players",
            "player_actions",
            "consistent_backups",
            "world_reset",
            "world_import_export",
            "software_update",
            "config_files",
        }
    )
    stop_words = frozenset({"/quit"})
    keeps_command_slash = True
    refuse_stop_commands = True
    command_hint = "Commands start with / (for example /players online); a line without / is a chat message."
    flush_command = "/server-save"
    notes = (
        "Headless server on the factoriotools/factorio image, pinned to an exact version: players need the same version.",
        "Players connect over UDP; forward the UDP port on your router for players outside your network.",
        "No account is needed to run it; a public listing needs a factorio.com token.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this UDP port (Factorio's default is 34197).",
        ),
    )
    options = (
        GameOption(
            "space_age",
            "Space Age",
            "bool",
            False,
            "Enables the Space Age expansion (and Quality, Elevated rails). Players need to own it to join. "
            "Chosen once: switching it under an existing save converts the save.",
            pinned=True,
        ),
    )
    min_memory_mb = 1024
    default_stop_timeout = 120
    player_name_pattern = PLAYER_NAME
    player_list_labels = (("whitelist", "Whitelist"), ("ops", "Admins"), ("bans", "Bans"))
    control_secret_file = SECRET_FILE
    # (the agent's factorio profile leaves out temp/ itself; interrupted saves never belong in a backup)
    default_backup_excludes = ("*.tmp.zip",)
    allowed_user_env = frozenset({"TZ"})

    def __init__(self) -> None:
        self.saves = FactorioSaves(self)

    # -- data ------------------------------------------------------------------------------------
    def default_memory_mb(self) -> int:
        return 2048

    def config_files(self) -> tuple[ConfigFile, ...]:
        return CONFIG_FILES

    def variant(self, instance: Instance) -> str:
        return ""

    # -- versions and image ----------------------------------------------------------------------
    @property
    def has_versions(self) -> bool:
        return True

    def versions(self, variant: str | None = None) -> list[dict[str, Any]]:
        from apps.catalog.providers import cached

        repo = self.runtime_repository(DEFAULT_REPOSITORY)
        return cached(f"catalog:factorio:versions:{repo}", VERSIONS_TTL, lambda: self._fetch_versions(repo))

    def _fetch_versions(self, repo: str) -> list[dict[str, Any]]:
        from apps.catalog.providers import _get

        exact: set[str] = set()
        stable_tags: set[str] = set()
        url: str | None = HUB_TAGS.format(repo=repo.removeprefix("docker.io/"))
        params: dict[str, Any] | None = {"page_size": 100, "name": TAG_SUFFIX.lstrip("-")}
        for _page in range(10):
            if not url:
                break
            data = _get(url, params=params) or {}
            for tag in data.get("results") or []:
                name = str(tag.get("name") or "")
                if not name.endswith(TAG_SUFFIX):
                    continue
                base = name[: -len(TAG_SUFFIX)]
                if VERSION_RE.match(base):
                    exact.add(base)
                elif base.startswith("stable-") and VERSION_RE.match(base[7:]):
                    stable_tags.add(base[7:])
            url, params = data.get("next"), None
        if not exact:
            raise UpstreamError("Docker Hub lists no Factorio versions for this image", code="upstream_error")
        latest_stable = None
        try:
            releases = _get(LATEST_RELEASES) or {}
            latest_stable = str(((releases.get("stable") or {}).get("headless")) or "") or None
        except UpstreamError:
            latest_stable = None  # factorio.com unreachable: the image's stable tags still say it
        if latest_stable not in exact:
            latest_stable = max(stable_tags & exact, key=version_key, default=None)
        ordered = sorted(exact, key=version_key, reverse=True)
        return [
            {
                "id": v,
                "label": v,
                "stable": v in stable_tags or v == latest_stable,
                "latest": v == latest_stable,
            }
            for v in ordered
        ]

    def latest_stable(self) -> str | None:
        return next((v["id"] for v in self.versions() if v["latest"]), None)

    def validate_version(self, version: str, variant: str | None = None) -> str:
        version = str(version or "").strip()
        if not VERSION_RE.match(version):
            raise ApiError("Choose an exact Factorio version such as 2.1.24", code="invalid_version")
        known = {v["id"] for v in self.versions()}
        if version not in known:
            raise ApiError(f"Factorio {version} has no rootless image tag", code="invalid_version")
        return version

    def image(self, instance: Instance) -> str:
        repo = self.runtime_repository(DEFAULT_REPOSITORY)
        return f"{repo}:{instance.minecraft_version}{TAG_SUFFIX}"

    # -- creating a server -----------------------------------------------------------------------
    def create_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        version = self.validate_version(data.get("version") or "")
        options = self.validate_options(data.get("options"))
        options["save_name"] = SAVE_NAME
        return {
            "server_type": "",
            "minecraft_version": version,
            "loader_version": "",
            "game_options": options,
            "memory_mb": int(data.get("memory_mb") or self.default_memory_mb()),
            "memory_overhead_mb": 0,
            "stop_timeout": self.default_stop_timeout,
            "jvm_flags_preset": "none",
            "jvm_args": "",
        }

    def initial_files(self, instance: Instance, params: dict[str, Any]) -> dict[str, str]:
        from . import codecs

        settings = params.get("settings") or {}
        server = codecs.get("json").update(
            json.dumps(SERVER_SETTINGS_DEFAULTS), {"name": instance.name[:100], **settings.get("server-settings", {})}
        )
        map_gen = codecs.get("json").update(json.dumps(MAP_GEN_DEFAULTS), settings.get("map-gen-settings", {}))
        files = {"config/server-settings.json": server, "config/map-gen-settings.json": map_gen}
        if settings.get("map-settings"):
            # the image writes its example when the file is missing; with changes the panel writes
            # only them, and Factorio fills in its defaults for the rest
            files["config/map-settings.json"] = codecs.get("json").render(settings["map-settings"])
        return files

    # -- container -------------------------------------------------------------------------------
    def save_name(self, instance: Instance) -> str:
        name = str((instance.game_options or {}).get("save_name") or SAVE_NAME)
        return name if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) else SAVE_NAME

    def game_env(self, instance: Instance) -> dict[str, str]:
        options = instance.game_options or {}
        return {
            "DLC_SPACE_AGE": "true" if options.get("space_age") else "false",
            # create saves/<SAVE_NAME>.zip when it is missing (first start, after a world reset), and
            # load the newest save: after an unclean exit (kill, crash, host power loss) the newest
            # state is an autosave (_autosaveN.zip), not <SAVE_NAME>.zip. A reset or an import moves
            # every save aside while the server is stopped, so the new map / the uploaded save is the
            # only one (and therefore the newest) at the next start; a restore brings back the files
            # with their times, so the newest save of the backup is loaded.
            "GENERATE_NEW_SAVE": "true",
            "LOAD_LATEST_SAVE": "true",
            "SAVE_NAME": self.save_name(instance),
        }

    # -- console ---------------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        # Factorio's commands start with "/" (a line without one is a chat message): keep it
        return text.strip()

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        line = text if not message else f"{text} - {message}"
        # a chat line: never a command, single line
        line = " ".join(line.split()).lstrip("/").strip()[:500]
        if line:
            client.command(instance.id, line)

    def _rcon(self, instance: Instance, command: str) -> str:
        from apps.nodes.client import AgentClient

        return AgentClient(instance.node).command(instance.id, command)

    def online_players(self, instance: Instance, *, max_age: int = 30) -> dict[str, Any]:
        """``/players online`` (cached: every RCON connection is a line in Factorio's log)."""
        from django.core.cache import cache

        key = f"players:online:{instance.pk}"
        if max_age and (hit := cache.get(key)) is not None:
            return hit
        result = parse_players(self._rcon(instance, "/players online"))
        cache.set(key, result, max_age)
        return result

    # -- players ---------------------------------------------------------------------------------
    def _running(self, instance: Instance) -> bool:
        from apps.nodes.client import AgentClient

        return bool(AgentClient(instance.node).status(instance.id).get("running"))

    def _read_list(self, instance: Instance, path: str) -> list[Any]:
        from apps.nodes.client import AgentClient

        try:
            content = AgentClient(instance.node).read_text(instance.id, path)["content"]
        except ApiError as exc:
            if exc.error_code == "not_found":
                return []
            raise
        try:
            data = json.loads(content or "[]")
        except ValueError as exc:
            raise ApiError(f"{path} is not valid JSON; fix it in the file manager", code="invalid_json") from exc
        return data if isinstance(data, list) else []

    def _write_list(self, instance: Instance, path: str, data: list[Any]) -> None:
        from apps.nodes.client import AgentClient

        AgentClient(instance.node).write_text(instance.id, path, json.dumps(data, indent=2) + "\n")

    @staticmethod
    def _entry_name(entry: Any) -> str:
        if isinstance(entry, dict):
            return str(entry.get("username") or entry.get("name") or "")
        return str(entry)

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        running = self._running(instance)
        result: dict[str, Any] = {"running": running, "labels": dict(self.player_list_labels)}
        for key in ("whitelist", "ops"):
            result[key] = [
                {"name": self._entry_name(e)} for e in self._read_list(instance, LIST_FILES[key]) if self._entry_name(e)
            ]
        result["bans"] = [
            {"name": self._entry_name(e), "reason": str(e.get("reason") or "") if isinstance(e, dict) else ""}
            for e in self._read_list(instance, LIST_FILES["bans"])
            if self._entry_name(e)
        ]
        result["ip_bans"] = []
        result["known_players"] = []
        if running:
            try:
                result["online"] = self.online_players(instance)
            except ApiError as exc:
                result["online"] = {"error": str(exc.detail)}
        return result

    COMMANDS = {
        "whitelist_add": "/whitelist add {name}",
        "whitelist_remove": "/whitelist remove {name}",
        "op": "/promote {name}",
        "deop": "/demote {name}",
        "ban": "/ban {name} {reason}",
        "pardon": "/unban {name}",
        "kick": "/kick {name} {reason}",
    }

    def player_action(self, instance: Instance, action: str, name: str, reason: str = "") -> dict[str, Any]:
        self.require(instance, "player_actions")
        if action not in self.COMMANDS:
            raise ApiError("Unknown player action", code="invalid_action")
        name = self.validate_player_name(name)
        reason = " ".join(re.sub(r"[\r\n]", " ", reason or "").split())[:200]
        if self._running(instance):
            command = self.COMMANDS[action].format(name=name, reason=reason).strip()
            output = self._rcon(instance, command)
            from django.core.cache import cache

            cache.delete(f"players:online:{instance.pk}")
            return {"via": "rcon", "output": output}
        if action == "kick":
            raise ApiError("The server is not running, so nobody can be kicked", code="not_running", status_code=409)
        key = {"whitelist_add": "whitelist", "whitelist_remove": "whitelist", "op": "ops", "deop": "ops"}.get(action, "bans")
        path = LIST_FILES[key]
        entries = self._read_list(instance, path)
        lowered = name.lower()
        if action in ("whitelist_remove", "deop", "pardon"):
            kept = [e for e in entries if self._entry_name(e).lower() != lowered]
            if len(kept) == len(entries):
                raise ApiError(f"{name} is not in {path}", code="not_listed", status_code=404)
            self._write_list(instance, path, kept)
            return {"via": "file", "output": f"Removed {name} from {path}"}
        if any(self._entry_name(e).lower() == lowered for e in entries):
            return {"via": "file", "output": f"{name} is already in {path}"}
        entries.append({"username": name, "reason": reason} if action == "ban" else name)
        self._write_list(instance, path, entries)
        return {"via": "file", "output": f"Added {name} to {path} (applies when the server starts)"}

    # -- software --------------------------------------------------------------------------------
    software_keys = ("game", "version", "space_age", "image")

    def consistency_note(self, instance: Instance) -> str:
        return (
            "the map was saved right before the copy (/server-save, waited for 'Saving finished'); the game is not "
            "paused, so an autosave during the copy can be in the backup as well"
        )

    def software_state(self, instance: Instance) -> dict[str, Any]:
        return {
            "game": self.id,
            "version": instance.minecraft_version,
            "image": instance.image,
            "space_age": bool((instance.game_options or {}).get("space_age")),
        }

    def describe_software(self, state: dict[str, Any]) -> str:
        text = f"Factorio {state.get('version') or '?'}"
        return text + (" with Space Age" if state.get("space_age") else "")

    def set_software(self, instance: Instance, state: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        from apps.instances.services import bump_revision

        version = state.get("version")
        if not version or version == instance.minecraft_version:
            return {}
        changes = {"minecraft_version": (instance.minecraft_version, version)}
        instance.minecraft_version = str(version)
        instance.image = self.image(instance)
        instance.save(update_fields=["minecraft_version", "image"])
        bump_revision(instance)
        return changes


def parse_players(out: str) -> dict[str, Any]:
    """``/players online``: ``Online players (N):`` then one ``  name (online)`` line per player."""
    m = COUNT_LINE.search(out or "")
    names: list[str] = []
    for line in (out or "").splitlines():
        if (pm := ONLINE_LINE.match(line)) and re.fullmatch(PLAYER_NAME, pm.group(1)) and pm.group(1) not in names:
            names.append(pm.group(1))
    online = int(m.group(1)) if m else len(names)
    return {"online": online, "max": None, "players": names}


class FactorioSaves:
    """The save files of a Factorio server (the ``saves`` model of the definition; design 8.2).

    The newest ``saves/*.zip`` is the map the server runs (``LOAD_LATEST_SAVE``; ``saves/<save_name>.zip``
    until an autosave is newer). A world reset or an import moves every
    ``saves/*.zip`` aside into ``saves-replaced/<UTC stamp>/`` (kept in the server folder, so they
    are in backups and can be imported again), then the server creates a new map (reset) or loads
    the uploaded zip (import) at its next start.
    """

    kind = "saves"
    extension = ".zip"

    def __init__(self, definition: FactorioDefinition) -> None:
        self.definition = definition

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "extension": self.extension,
            "folder": SAVES_DIR,
            "set_aside_folder": SET_ASIDE_DIR,
            "reset_fields": [dict(f) for f in MAP_GEN_FIELDS if f["key"] in ("seed", "peaceful_mode")],
        }

    def current(self, instance: Instance) -> str:
        return self.definition.save_name(instance)

    def current_path(self, instance: Instance) -> str:
        """Where an imported save is put (every other save was moved aside first)."""
        return f"{SAVES_DIR}/{self.current(instance)}{self.extension}"

    def _save_entries(self, client: AgentClient, instance: Instance) -> list[dict[str, Any]]:
        return [
            e
            for e in _entries(client, instance, SAVES_DIR)
            if e.get("type") == "file"
            and str(e.get("name") or "").endswith(self.extension)
            and not str(e.get("name") or "").endswith(".tmp.zip")
        ]

    def loaded(self, client: AgentClient, instance: Instance, entries: list[dict[str, Any]] | None = None) -> str:
        """The save the server loads at its next start (and saves into while it runs): the newest one
        (``LOAD_LATEST_SAVE``); ``<save_name>`` when there is none yet or on a tie."""
        name = self.current(instance)
        files = self._save_entries(client, instance) if entries is None else entries
        if not files:
            return name
        newest = max(
            files,
            key=lambda e: (e.get("modified") or 0, str(e.get("name")) == f"{name}{self.extension}"),
        )
        return str(newest["name"])[: -len(self.extension)]

    def export_path(self, client: AgentClient, instance: Instance) -> str:
        return f"{SAVES_DIR}/{self.loaded(client, instance)}{self.extension}"

    def list(self, client: AgentClient, instance: Instance) -> dict[str, Any]:
        entries = self._save_entries(client, instance)
        current = self.loaded(client, instance, entries)
        saves = []
        for entry in entries:
            name = str(entry.get("name") or "")
            stem = name[: -len(self.extension)]
            saves.append(
                {
                    "name": stem,
                    "path": f"{SAVES_DIR}/{name}",
                    "size": entry.get("size"),
                    "modified": entry.get("modified"),
                    "current": stem == current,
                }
            )
        set_aside = []
        for folder in _entries(client, instance, SET_ASIDE_DIR):
            if folder.get("type") != "directory":
                continue
            files = [str(e.get("name")) for e in _entries(client, instance, f"{SET_ASIDE_DIR}/{folder.get('name')}")]
            set_aside.append({"folder": f"{SET_ASIDE_DIR}/{folder.get('name')}", "files": sorted(files)})
        set_aside.sort(key=lambda f: f["folder"], reverse=True)
        saves.sort(key=lambda s: (not s["current"], s["name"]))
        return {
            "current": current,
            "saves": saves,
            "set_aside": set_aside,
            "options": {k: v for k, v in (instance.game_options or {}).items() if k != "save_name"},
            "reset_fields": self.describe()["reset_fields"],
        }

    def set_aside(self, client: AgentClient, instance: Instance, stamp: str) -> list[str]:
        """Move every save zip (and leftover .tmp.zip) into saves-replaced/<stamp>/; returns the names."""
        names = [
            str(e.get("name"))
            for e in _entries(client, instance, SAVES_DIR)
            if e.get("type") == "file" and str(e.get("name") or "").endswith(self.extension)
        ]
        if not names:
            return []
        target = f"{SET_ASIDE_DIR}/{stamp}"
        client.mkdir(instance.id, target, parents=True)
        for name in names:
            client.rename(instance.id, f"{SAVES_DIR}/{name}", f"{target}/{name}")
        return names

    def write_map_gen(self, client: AgentClient, instance: Instance, values: dict[str, Any]) -> None:
        """Merge ``values`` (validated map-gen fields) into config/map-gen-settings.json."""
        from . import codecs

        cfg = self.definition.config_file("map-gen-settings")
        assert cfg is not None
        try:
            current = client.read_text(instance.id, cfg.path)["content"]
        except ApiError as exc:
            if exc.error_code != "not_found":
                raise
            current = json.dumps(MAP_GEN_DEFAULTS)
        client.write_text(instance.id, cfg.path, codecs.get("json").update(current, values))


def _entries(client: AgentClient, instance: Instance, path: str) -> list[dict[str, Any]]:
    try:
        answer = client.list_files(instance.id, path)
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return []
        raise
    return list(answer.get("entries") or [])
