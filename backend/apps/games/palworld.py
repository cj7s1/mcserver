"""Palworld (dedicated server on thijsvanloef/palworld-server-docker).

What the panel knows about it (probe ``audit-2026-11-09/design/probe-6games.md`` section 1, observed on
``v2.8.1`` and game v1.0.5):

* Image ``thijsvanloef/palworld-server-docker:<pinned vX.Y.Z>``; it downloads the game (SteamCMD app
  2394010, about 4.6 GB) on the first start and checks for an update at every start, so the server
  follows Steam's public branch (no version to pick). Root + ``PUID``/``PGID`` (the agent's profile).
* One named port, the game on UDP (default 8211), published 1:1 (``PORT``; ``PUBLIC_PORT`` is the
  same number, what a community listing advertises). The REST API (8212/tcp) stays internal: the
  agent's ``palworld-api`` driver reaches it with Basic ``admin:<admin password>``.
* Settings: the image writes ``PalWorldSettings.ini`` from its environment at every start, so the
  editor is a virtual config the panel keeps (``server`` and ``gameplay``) and passes as variables.
* The admin password (a generated secret the owner can show: players type it in game to become admins,
  and it is the REST API's password) goes to the image as ``ADMIN_PASSWORD``; the agent's driver reads it
  back from the container's environment. The join password is optional.
* Players: the REST API's list, kick/ban/unban by user id (``steam_<SteamID64>``); the ban list is
  ``Pal/Saved/SaveGames/banlist.txt``. No whitelist; admins are whoever knows the admin password.
* The world is a folder ``Pal/Saved/SaveGames/0/<world id>/``; ``GameUserSettings.ini``
  (``DedicatedServerName``) names the one the server loads. A reset moves it aside and clears the
  name (a new world gets a new id); an import puts a world folder there and names it.
* Stop: SIGTERM, the image saves through the REST API first (exit 143); the driver saves before too.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError

from .base import ConfigFile, PortSpec, SecretSpec
from .foldersaves import FolderSaves
from .steamgame import SAFE_NAME_HELP, SAFE_NAME_PATTERN, SteamGameDefinition, safe_name

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "palworld"
DEFAULT_REPOSITORY = "thijsvanloef/palworld-server-docker"
IMAGE_TAG = "v2.8.1"
GAME_PORT = 8211
QUERY_PORT = 27015  # inside the container only (never published)
ADMIN_PASSWORD = "admin_password"
JOIN_PASSWORD = "server_password"
SAVES_ROOT = "Pal/Saved/SaveGames/0"
BANLIST = "Pal/Saved/SaveGames/banlist.txt"
GAME_USER_SETTINGS = "Pal/Saved/Config/LinuxServer/GameUserSettings.ini"
GUS_SECTION = "[/Script/Pal.PalGameLocalSettings]"
WORLD_ID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
# a Palworld user id as the REST API and the ban list name it: platform_id (steam_76561198...)
USER_ID = r"^[a-z0-9]{2,16}_[A-Za-z0-9]{1,64}$"
# what the image writes into the ini as quoted text: no quotes, no line breaks (compile-settings.sh)
TEXT_PATTERN = r'^[^"\\\x00-\x1f\x7f]{0,256}$'
PASSWORD_PATTERN = r"^[A-Za-z0-9_-]{12,64}$"
JOIN_PATTERN = r"^[A-Za-z0-9_.,!?@#%+=:-]{1,64}$"

SERVER_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "server_name",
        "type": "string",
        "label": "Server name",
        "pattern": SAFE_NAME_PATTERN,
        "pattern_help": SAFE_NAME_HELP,
        "default": "Palworld server",
        "initial": True,
    },
    {
        "key": "description",
        "type": "string",
        "label": "Description",
        "pattern": TEXT_PATTERN,
        "pattern_help": "no double quotes or backslashes, at most 256 characters",
        "default": "",
        "initial": True,
    },
    {"key": "max_players", "type": "int", "label": "Max players", "min": 1, "max": 32, "default": 16, "initial": True},
    {
        "key": "community",
        "type": "bool",
        "label": "List on the community server browser",
        "help": "Off: only players who know the address (and the join password) join.",
        "default": False,
        "initial": True,
    },
)


def _rate(key: str, env: str, label: str, lo: float = 0.1, hi: float = 5.0, group: str = "Rates") -> dict[str, Any]:
    return {"key": key, "env": env, "type": "number", "label": label, "min": lo, "max": hi, "default": 1.0, "group": group}


# Gameplay settings the image turns into PalWorldSettings.ini (env name per field in "env", left out of
# the catalog). Numbers go to the image as decimals ("1.500000"), booleans as True/False.
GAMEPLAY: tuple[dict[str, Any], ...] = (
    {
        "key": "difficulty",
        "env": "DIFFICULTY",
        "type": "enum",
        "label": "Difficulty",
        "options": ["None", "Casual", "Normal", "Hard"],
        "default": "None",
        "group": "World",
    },
    {
        "key": "death_penalty",
        "env": "DEATH_PENALTY",
        "type": "enum",
        "label": "Death penalty",
        "help": "None, Item (drop items), ItemAndEquipment, All (also Pals).",
        "options": ["None", "Item", "ItemAndEquipment", "All"],
        "default": "Item",
        "group": "World",
    },
    {"key": "pvp", "env": "IS_PVP", "type": "bool", "label": "PvP", "default": False, "group": "World"},
    {
        "key": "player_damage",
        "env": "ENABLE_PLAYER_TO_PLAYER_DAMAGE",
        "type": "bool",
        "label": "Players can damage each other",
        "default": False,
        "group": "World",
    },
    {
        "key": "friendly_fire",
        "env": "ENABLE_FRIENDLY_FIRE",
        "type": "bool",
        "label": "Friendly fire",
        "default": False,
        "group": "World",
    },
    {"key": "raids", "env": "ENABLE_INVADER_ENEMY", "type": "bool", "label": "Raids on bases", "default": True, "group": "World"},
    {
        "key": "fast_travel",
        "env": "ENABLE_FAST_TRAVEL",
        "type": "bool",
        "label": "Fast travel",
        "default": True,
        "group": "World",
    },
    {
        "key": "hardcore",
        "env": "HARDCORE",
        "type": "bool",
        "label": "Hardcore (death is permanent)",
        "default": False,
        "group": "World",
    },
    {"key": "pal_lost", "env": "PAL_LOST", "type": "bool", "label": "Pals are lost on death", "default": False, "group": "World"},
    _rate("exp_rate", "EXP_RATE", "Experience rate", 0.1, 20.0),
    _rate("capture_rate", "PAL_CAPTURE_RATE", "Pal capture rate", 0.5, 2.0),
    _rate("spawn_rate", "PAL_SPAWN_NUM_RATE", "Pal appearance rate", 0.5, 3.0),
    _rate("day_speed", "DAYTIME_SPEEDRATE", "Day time speed"),
    _rate("night_speed", "NIGHTTIME_SPEEDRATE", "Night time speed"),
    _rate("pal_attack", "PAL_DAMAGE_RATE_ATTACK", "Damage from Pals"),
    _rate("pal_defense", "PAL_DAMAGE_RATE_DEFENSE", "Damage to Pals"),
    _rate("player_attack", "PLAYER_DAMAGE_RATE_ATTACK", "Damage from players"),
    _rate("player_defense", "PLAYER_DAMAGE_RATE_DEFENSE", "Damage to players"),
    _rate("player_hunger", "PLAYER_STOMACH_DECREASE_RATE", "Player hunger rate"),
    _rate("player_stamina", "PLAYER_STAMINA_DECREASE_RATE", "Player stamina use"),
    _rate("work_speed", "WORK_SPEED_RATE", "Pal work speed"),
    _rate("drop_rate", "ENEMY_DROP_ITEM_RATE", "Enemy item drop rate", 0.5, 3.0),
    _rate("gather_rate", "COLLECTION_DROP_RATE", "Gathered item rate", 0.5, 3.0),
    {
        "key": "egg_hatching_hours",
        "env": "PAL_EGG_DEFAULT_HATCHING_TIME",
        "type": "number",
        "label": "Hours to hatch a huge egg",
        "min": 0,
        "max": 240,
        "default": 72.0,
        "group": "Rates",
    },
    {
        "key": "guild_max_players",
        "env": "GUILD_PLAYER_MAX_NUM",
        "type": "int",
        "label": "Max players per guild",
        "min": 1,
        "max": 100,
        "default": 20,
        "group": "Limits",
    },
    {
        "key": "base_camps_per_guild",
        "env": "BASE_CAMP_MAX_NUM_IN_GUILD",
        "type": "int",
        "label": "Bases per guild",
        "min": 1,
        "max": 10,
        "default": 4,
        "group": "Limits",
    },
    {
        "key": "base_camp_workers",
        "env": "BASE_CAMP_WORKER_MAX_NUM",
        "type": "int",
        "label": "Pals working per base",
        "min": 1,
        "max": 50,
        "default": 15,
        "group": "Limits",
    },
    {
        "key": "autosave_seconds",
        "env": "AUTO_SAVE_SPAN",
        "type": "number",
        "label": "Autosave every (seconds)",
        "min": 30,
        "max": 3600,
        "default": 30.0,
        "group": "Saving",
    },
)
GAMEPLAY_ENV = {f["key"]: f["env"] for f in GAMEPLAY}
GAMEPLAY_FIELDS = tuple({k: v for k, v in f.items() if k != "env"} for f in GAMEPLAY)

CONFIGS = (
    ConfigFile(
        id="server",
        path="(container settings)",
        codec="json",
        label="Server",
        help="Kept by the panel and passed to the server at its start (restart to apply).",
        fields=SERVER_FIELDS,
    ),
    ConfigFile(
        id="gameplay",
        path="(container settings)",
        codec="json",
        label="Gameplay (PalWorldSettings.ini)",
        help="The image writes PalWorldSettings.ini from these at every start (restart to apply); edits to the "
        "file itself are overwritten.",
        fields=GAMEPLAY_FIELDS,
    ),
)


def env_value(field: dict[str, Any], value: Any) -> str:
    """A setting as the image wants it in PalWorldSettings.ini: decimals with six places, True/False."""
    if field["type"] == "bool":
        return "True" if value else "False"
    if field["type"] == "number":
        return f"{float(value):.6f}"
    return str(value)


def parse_banlist(text: str) -> list[str]:
    """User ids of ``banlist.txt`` (``userid,playerUID`` per line)."""
    out = []
    for line in (text or "").splitlines():
        user = line.split(",", 1)[0].strip()
        if user and re.fullmatch(USER_ID, user) and user not in out:
            out.append(user)
    return out


def set_dedicated_server_name(text: str, name: str) -> str:
    """``GameUserSettings.ini`` with ``DedicatedServerName=<name>`` (the world the server loads; "" =
    none, a new world). The section name has dots, so this is a line edit, not the ini codec."""
    lines = (text or "").splitlines()
    out, done = [], False
    for line in lines:
        if line.strip().startswith("DedicatedServerName="):
            if not done:
                out.append(f"DedicatedServerName={name}")
                done = True
            continue
        out.append(line)
    if not done:
        if GUS_SECTION in [ln.strip() for ln in out]:
            at = [ln.strip() for ln in out].index(GUS_SECTION) + 1
            out.insert(at, f"DedicatedServerName={name}")
        else:
            out += [GUS_SECTION, f"DedicatedServerName={name}"]
    return "\n".join(out) + "\n"


def dedicated_server_name(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip().startswith("DedicatedServerName="):
            return line.split("=", 1)[1].strip()
    return ""


class PalworldDefinition(SteamGameDefinition):
    id = GAME
    label = "Palworld"
    profile = GAME
    control = "palworld-api"
    repository = DEFAULT_REPOSITORY
    image_tag = IMAGE_TAG
    default_memory = 16384
    min_memory_mb = 8192
    default_stop_timeout = 60
    capability_set = frozenset(
        {
            "players",
            "player_actions",
            "broadcast",
            "consistent_backups",
            "world_reset",
            "world_import_export",
            "config_files",
            "diagnostics",
        }
    )
    flush_command = "save"  # what the job log names (the driver saves through the REST API)
    # a user id (steam_<SteamID64>) or the name of a player who is online (resolved to the id)
    player_name_pattern = r"^[^\x00-\x1f\x7f]{1,64}$"
    player_list_labels = (("bans", "Bans (user ids)"),)
    virtual_configs = CONFIGS
    secret_specs = (
        SecretSpec(
            ADMIN_PASSWORD,
            "Admin password",
            "Players become admins in game with /AdminPassword <password>; the panel's REST API access uses it as "
            "well. Generated when the server is created; show it here.",
            generate=24,
            reveal=True,
            pattern=PASSWORD_PATTERN,
            pattern_help="12 to 64 letters, digits, - and _",
        ),
        SecretSpec(
            JOIN_PASSWORD,
            "Join password",
            "Players need it to join. Empty: anyone who knows the address joins.",
            pattern=JOIN_PATTERN,
            pattern_help="1 to 64 letters, digits and _ . , ! ? @ # % + = : -",
        ),
    )
    fixed_version = {
        "id": "public",
        "label": "Steam public branch",
        "help": "Players' game clients update themselves, so the server always runs the current public build: the "
        "image checks for an update at every start. There is no version to choose; 'update' means restart.",
        # "Update now" is a restart: the image downloads the newest build of the branch at every start
        "updates_on_restart": True,
    }
    notes = (
        "Dedicated server on the thijsvanloef/palworld-server-docker image. The first start downloads the game "
        "(about 4.6 GB on disk) and takes a minute or two; every start checks for an update.",
        "Needs at least 8 GB of memory (16 GB by default; big worlds and many players need more).",
        "Forward the game port on UDP. The REST API the panel uses stays inside the panel's network.",
        "Admins: players type /AdminPassword <password> in game; the admin password is under Settings -> Secrets.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this UDP port (Palworld's default is 8211). The server listens on the same number "
            "inside its container.",
            one_to_one=True,
        ),
    )

    def __init__(self) -> None:
        self.saves = FolderSaves(
            self,
            folder="Pal/Saved/SaveGames",
            markers=(r"Level\.sav",),
            marker_help="a zip of a world folder from Pal/Saved/SaveGames/0/ with Level.sav in it",
            export_exclude=("backup",),
        )

    # -- settings --------------------------------------------------------------------------------
    def initial_config(self, file_id: str, data: dict[str, Any]) -> dict[str, Any]:
        if file_id == "server":
            return {"server_name": safe_name(str(data.get("name") or ""), "Palworld server")}
        return {}

    def game_env(self, instance: Instance) -> dict[str, str]:
        port = self.host_port(instance, "game", GAME_PORT)
        server = self.config_values(instance, "server")
        env = {
            "PORT": str(port),
            "PUBLIC_PORT": str(port),
            # the Steam query port stays inside the container (Palworld answers no query on it), but it must not be
            # the game port's number: the agent's default 27015 would be refused for a game port of 27015
            "QUERY_PORT": str(QUERY_PORT if port != QUERY_PORT else QUERY_PORT + 1),
            "PLAYERS": str(server["max_players"]),
            "SERVER_NAME": str(server["server_name"]),
            "COMMUNITY": "true" if server["community"] else "false",
        }
        if server.get("description"):
            env["SERVER_DESCRIPTION"] = str(server["description"])
        secrets = instance.secret_values
        if secrets.get(ADMIN_PASSWORD):
            env["ADMIN_PASSWORD"] = secrets[ADMIN_PASSWORD]
        if secrets.get(JOIN_PASSWORD):
            env["SERVER_PASSWORD"] = secrets[JOIN_PASSWORD]
        gameplay = self.config_values(instance, "gameplay")
        for field in GAMEPLAY:
            env[field["env"]] = env_value(field, gameplay[field["key"]])
        return env

    def masked_text(self, instance: Instance, path: str, content: str) -> str:
        """PalWorldSettings.ini holds the admin and join passwords: hidden in the editor."""
        for key in (ADMIN_PASSWORD, JOIN_PASSWORD):
            value = instance.secret_values.get(key)
            if value and len(value) >= 4:
                content = content.replace(value, "********")
        return content

    def secret_bearing_paths(self, instance: Instance) -> tuple[str, ...]:
        return ("Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",)

    # -- players ---------------------------------------------------------------------------------
    def _client(self, instance: Instance) -> AgentClient:
        from apps.nodes.client import AgentClient

        return AgentClient(instance.node)

    def _players(self, client: AgentClient, instance: Instance) -> list[dict[str, Any]]:
        answer = client.game_action(instance.id, "players", {}, timeout=30)
        raw = answer.get("players") if isinstance(answer, dict) else None
        out = []
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("accountName") or "").strip()
            user = str(entry.get("userId") or entry.get("userid") or "").strip()
            if name or user:
                out.append({"name": name[:64] or user, "id": user})
        return out

    def online_players(self, instance: Instance, *, max_age: int = 30) -> dict[str, Any]:
        """The REST API's player list (cached: every REST call is a line in Palworld's log)."""
        from django.core.cache import cache

        key = f"players:online:{instance.pk}"
        if max_age and (hit := cache.get(key)) is not None:
            return hit
        players = self._players(self._client(instance), instance)
        result = {
            "online": len(players),
            "max": self.config_values(instance, "server")["max_players"],
            "players": [p["name"] for p in players],
            "entries": players,
        }
        cache.set(key, result, max_age or 1)
        return result

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        client = self._client(instance)
        running = bool(client.status(instance.id).get("running"))
        bans = parse_banlist(_read_text(client, instance, BANLIST) or "")
        result: dict[str, Any] = {
            "running": running,
            "labels": dict(self.player_list_labels),
            "whitelist": [],
            "ops": [],
            "bans": [{"name": b, "reason": ""} for b in bans],
            "ip_bans": [],
            "known_players": [],
            "id_hint": "Bans and kicks use the player's user id (steam_<SteamID64>, shown with the online players).",
            # kicks and bans need the running server (its REST API); only an unban works while it is stopped
            "changes_while_stopped": False,
        }
        if running:
            try:
                result["online"] = self.online_players(instance, max_age=0)
            except ApiError as exc:
                result["online"] = {"error": str(exc.detail)}
        return result

    def _user_id(self, client: AgentClient, instance: Instance, name: str, *, running: bool) -> str:
        """A user id as given, or the id of the online player with that name."""
        name = name.strip()
        if re.fullmatch(USER_ID, name):
            return name
        if running:
            for player in self._players(client, instance):
                if player["name"].lower() == name.lower() and player["id"]:
                    return player["id"]
        raise ApiError(
            "Use the player's user id (steam_<SteamID64>), or the name of a player who is online",
            code="invalid_player_name",
        )

    def player_action(self, instance: Instance, action: str, name: str, reason: str = "") -> dict[str, Any]:
        from django.core.cache import cache

        self.require(instance, "player_actions")
        if action not in ("kick", "ban", "pardon"):
            raise ApiError(
                "Palworld servers can kick, ban and unban players (no whitelist, no admin list)", code="invalid_action"
            )
        client = self._client(instance)
        running = bool(client.status(instance.id).get("running"))
        user = self._user_id(client, instance, self.validate_player_name(name), running=running)
        message = self.chat_text(reason, limit=120)
        if not running:
            if action != "pardon":
                raise ApiError(
                    "The server is not running: Palworld kicks and bans through its REST API", code="not_running", status_code=409
                )
            text = _read_text(client, instance, BANLIST) or ""
            kept = [ln for ln in text.splitlines() if ln.split(",", 1)[0].strip() != user]
            if len(kept) == len(text.splitlines()):
                raise ApiError(f"{user} is not banned", code="not_listed", status_code=404)
            client.write_text(instance.id, BANLIST, "\n".join(kept) + ("\n" if kept else ""))
            return {"via": "file", "output": f"Removed {user} from banlist.txt"}
        params: dict[str, Any] = {"userid": user}
        if message and action != "pardon":
            params["message"] = message
        client.game_action(instance.id, {"pardon": "unban"}.get(action, action), params, timeout=30)
        cache.delete(f"players:online:{instance.pk}")
        done = {"kick": "Kicked", "ban": "Banned", "pardon": "Unbanned"}[action]
        return {"via": "api", "output": f"{done} {user}"}

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        line = self.chat_text(text, message)
        if line:
            client.game_action(instance.id, "announce", {"message": line}, timeout=30)

    def consistency_note(self, instance: Instance) -> str:
        return (
            "the world was saved through the server's REST API right before the copy; the game is not paused, so an "
            "autosave during the copy can be in the backup as well"
        )

    # -- the world -------------------------------------------------------------------------------
    def save_now(self, client: AgentClient, instance: Instance) -> bool:
        client.game_action(instance.id, "save", {}, timeout=120)
        return True

    def _world_id(self, client: AgentClient, instance: Instance) -> str:
        """The world the server loads: GameUserSettings.ini's DedicatedServerName when that folder exists,
        else the newest world folder."""
        named = dedicated_server_name(_read_text(client, instance, GAME_USER_SETTINGS) or "")
        folders = [e for e in _entries(client, instance, SAVES_ROOT) if e.get("type") == "directory" and str(e.get("name") or "")]
        if named and any(str(e["name"]) == named for e in folders):
            return named
        if not folders:
            return ""
        return str(max(folders, key=lambda e: e.get("modified") or 0)["name"])

    def world_paths(self, client: AgentClient, instance: Instance, purpose: str, params: dict[str, Any]) -> list[str]:
        world = self._world_id(client, instance)
        return [f"{SAVES_ROOT}/{world}"] if world else []

    def world_target(self, client: AgentClient, instance: Instance, root_name: str, params: dict[str, Any]) -> str:
        name = root_name if WORLD_ID_RE.fullmatch(root_name or "") else uuid.uuid4().hex.upper()
        return f"{SAVES_ROOT}/{name}"

    def after_import(self, client: AgentClient, instance: Instance, target: str, params: dict[str, Any]) -> None:
        name = target.rsplit("/", 1)[-1]
        text = _read_text(client, instance, GAME_USER_SETTINGS) or ""
        client.mkdir(instance.id, GAME_USER_SETTINGS.rsplit("/", 1)[0], parents=True)
        client.write_text(instance.id, GAME_USER_SETTINGS, set_dedicated_server_name(text, name))

    def prepare_world_reset(self, client: AgentClient, instance: Instance, values: dict[str, Any], seed: Any) -> str:
        text = _read_text(client, instance, GAME_USER_SETTINGS)
        if text is not None and dedicated_server_name(text):
            client.write_text(instance.id, GAME_USER_SETTINGS, set_dedicated_server_name(text, ""))
        return ""

    def world_notes(self, instance: Instance) -> list[str]:
        return [
            "The world folder also holds the game's own rolling backups (backup/); a download leaves them out.",
        ]


def _read_text(client: AgentClient, instance: Instance, path: str) -> str | None:
    try:
        return client.read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return None
        raise


def _entries(client: AgentClient, instance: Instance, path: str) -> list[dict[str, Any]]:
    from .foldersaves import _entries as entries

    return entries(client, instance, path)


__all__ = ["GAME", "PalworldDefinition", "parse_banlist", "set_dedicated_server_name"]
