"""Hollow Knight Multiplayer (HKMP server on maximalmax90/hkmpserver).

What the panel knows about it (probe ``audit-2026-11-09/design/probe-6games.md`` section 3, observed on
``v2.4.3-2``):

* Image ``maximalmax90/hkmpserver:<pinned vX.Y.Z-N>``: the HKMP server (mono, 1.2 MB) comes with the
  image, so the image tag is the version (a panel release changes it); nothing is downloaded at start.
  Root + ``PUID``/``PGID``.
* One port, UDP (default 2222, ``PORT``, published 1:1). The image's HEALTHCHECK says when it is ready.
* No world: players keep their own saves. The server's files are ``serversettings.json`` (game rules,
  edited with the json codec), ``whitelist.json``, ``banned.json`` and ``authorized.json``; the server
  writes them itself, so edit them while it is stopped.
* Console: the image's ``hkmp`` helper (``docker exec``) through the agent's console driver, which
  refuses ``^``, ``\\`` and control characters (the helper types into ``screen``, which interprets
  them). Commands: ``list``, ``announce``, ``kick``, ``ban``/``banip``, ``unban``/``unbanip``,
  ``whitelist on|off|add|remove``, ``auth``/``deauth`` (admins), ``exit`` (the panel's Stop instead).
* Stop: SIGTERM, the image sends ``exit`` (exit 0, under a second).
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError

from .base import ConfigFile, PortSpec
from .steamgame import SteamGameDefinition

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "hkmp"
DEFAULT_REPOSITORY = "maximalmax90/hkmpserver"
IMAGE_TAG = "v2.4.3-2"
HKMP_VERSION = "2.4.3"
GAME_PORT = 2222
# what the console helper may receive as a player argument (probe 3.7): no spaces, quotes, ^ or \
PLAYER_NAME = r"^[A-Za-z0-9_.:@+/=-]{1,64}$"
_ONLINE = re.compile(r"Online players \((\d+)\):\s*(.*)$")

SETTINGS_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "IsPvpEnabled", "type": "bool", "label": "PvP", "default": False, "group": "Rules", "initial": True},
    {
        "key": "IsBodyDamageEnabled",
        "type": "bool",
        "label": "Body damage (touching other players hurts)",
        "default": True,
        "group": "Rules",
    },
    {"key": "TeamsEnabled", "type": "bool", "label": "Teams", "default": False, "group": "Rules", "initial": True},
    {"key": "AllowSkins", "type": "bool", "label": "Skins", "default": True, "group": "Rules"},
    {"key": "AllowParries", "type": "bool", "label": "Parries", "default": True, "group": "Rules"},
    {"key": "DisplayNames", "type": "bool", "label": "Show player names", "default": True, "group": "Map and names"},
    {
        "key": "AlwaysShowMapIcons",
        "type": "bool",
        "label": "Always show players on the map",
        "default": False,
        "group": "Map and names",
    },
    {
        "key": "OnlyBroadcastMapIconWithWaywardCompass",
        "type": "bool",
        "label": "Show a player on the map only with the Wayward Compass",
        "default": True,
        "group": "Map and names",
    },
    *(
        {
            "key": f"{name}Damage",
            "type": "int",
            "label": f"{label} damage",
            "min": 0,
            "max": 10,
            "default": default,
            "group": "Damage (PvP)",
        }
        for name, label, default in (
            ("Nail", "Nail", 1),
            ("GreatSlash", "Great Slash", 2),
            ("DashSlash", "Dash Slash", 2),
            ("CycloneSlash", "Cyclone Slash", 1),
            ("VengefulSpirit", "Vengeful Spirit", 1),
            ("ShadeSoul", "Shade Soul", 2),
            ("DesolateDive", "Desolate Dive", 1),
            ("DescendingDark", "Descending Dark", 2),
            ("HowlingWraith", "Howling Wraiths", 1),
            ("AbyssShriek", "Abyss Shriek", 2),
        )
    ),
)
SETTINGS = ConfigFile(
    id="serversettings",
    path="serversettings.json",
    codec="json",
    label="Server settings (serversettings.json)",
    help="Read when the server starts. The server writes this file itself (also the console's set command): edit it "
    "while the server is stopped.",
    fields=SETTINGS_FIELDS,
)
WHITELIST = ConfigFile(
    id="whitelist",
    path="whitelist.json",
    codec="json",
    label="Whitelist (whitelist.json)",
    help="Read when the server starts; while it runs, the console's whitelist on/off changes it.",
    fields=({"key": "enabled", "type": "bool", "label": "Only whitelisted players may join", "default": False},),
)
LAUNCH = ConfigFile(
    id="launch",
    path="(container settings)",
    codec="json",
    label="Launch settings",
    help="Kept by the panel and passed to the server at its start (restart to apply).",
    fields=(
        {
            "key": "full_sync",
            "type": "bool",
            "label": "Full synchronisation",
            "help": "HKMP 2.4.3 and later: the server keeps the world state (enemies, breakables) in sync between players.",
            "default": False,
            "initial": True,
        },
    ),
)


def parse_list(out: str) -> dict[str, Any]:
    """``list``: ``[INFO] Online players (N): name1, name2``."""
    for line in (out or "").splitlines():
        if m := _ONLINE.search(line):
            names = [n.strip()[:64] for n in m.group(2).split(",") if n.strip()]
            return {"online": int(m.group(1)), "max": None, "players": names}
    raise ApiError("The server's player list could not be read", code="unexpected_reply", status_code=502)


def _json_list(text: str | None, key: str) -> list[str]:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return []
    values = data.get(key) if isinstance(data, dict) else None
    out = []
    for value in values or []:
        if isinstance(value, dict):  # (an entry object: its key or name)
            value = value.get("authKey") or value.get("auth_key") or value.get("name") or value.get("username") or ""
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


class HkmpDefinition(SteamGameDefinition):
    id = GAME
    label = "Hollow Knight (HKMP)"
    profile = GAME
    control = "console"
    repository = DEFAULT_REPOSITORY
    image_tag = IMAGE_TAG
    branch = HKMP_VERSION
    default_memory = 512
    min_memory_mb = 256
    default_stop_timeout = 40
    clean_exit_codes = frozenset({0})
    capability_set = frozenset({"console_commands", "broadcast", "players", "player_actions", "config_files", "diagnostics"})
    stop_words = frozenset({"exit"})
    refuse_stop_commands = True
    command_hint = "HKMP server commands without /, e.g. list, announce Hello, kick <name>, whitelist on."
    player_name_pattern = PLAYER_NAME
    player_list_labels = (("whitelist", "Whitelist"), ("ops", "Authorized (admins)"), ("bans", "Bans"))
    virtual_configs = (LAUNCH,)
    file_configs = (SETTINGS, WHITELIST)
    fixed_version = {
        "id": HKMP_VERSION,
        "label": f"HKMP {HKMP_VERSION}",
        "help": "The HKMP server comes with the panel's runtime image; a panel release updates it. Players need the "
        "same HKMP version in their game.",
    }
    notes = (
        "HKMP (Hollow Knight Multiplayer) server on the maximalmax90/hkmpserver image: small (about 25 MB of memory), "
        "ready in seconds, nothing to download.",
        "Forward the UDP port (HKMP's default is 2222). Players need Hollow Knight with the HKMP mod of the same version.",
        "There is no shared world: every player keeps their own save.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this UDP port (HKMP's default is 2222).",
            one_to_one=True,
        ),
    )

    def game_env(self, instance: Instance) -> dict[str, str]:
        return {
            "PORT": str(self.host_port(instance, "game", GAME_PORT)),
            "FULL_SYNC": "true" if self.config_values(instance, "launch")["full_sync"] else "false",
        }

    def initial_files(self, instance: Instance, params: dict[str, Any]) -> dict[str, str]:
        from . import codecs

        chosen = (params.get("settings") or {}).get("serversettings") or {}
        if not chosen:
            return {}  # the server writes its defaults at the first start
        defaults = {f["key"]: f["default"] for f in SETTINGS_FIELDS}
        return {SETTINGS.path: codecs.get("json").update("{}", {**defaults, **chosen})}

    # -- console and players -------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        return text.strip().lstrip("/").strip()

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        line = self.chat_text(text, message)
        if line:
            client.command(instance.id, f"announce {line}")

    def online_players(self, instance: Instance, *, max_age: int = 55) -> dict[str, Any]:
        """The console's ``list`` (cached: every command is a line in the server's log)."""
        from django.core.cache import cache

        from apps.nodes.client import AgentClient

        key = f"players:online:{instance.pk}"
        if max_age and (hit := cache.get(key)) is not None:
            return hit
        result = parse_list(AgentClient(instance.node).command(instance.id, "list", timeout=10))
        cache.set(key, result, max_age or 1)
        return result

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        from apps.nodes.client import AgentClient

        client = AgentClient(instance.node)
        running = bool(client.status(instance.id).get("running"))
        whitelist = _read_text(client, instance, "whitelist.json")
        banned = _read_text(client, instance, "banned.json")
        authorized = _read_text(client, instance, "authorized.json")
        result: dict[str, Any] = {
            "running": running,
            "labels": dict(self.player_list_labels),
            "whitelist": [{"name": n} for n in _json_list(whitelist, "pre-listed") + _json_list(whitelist, "approved")],
            "ops": [{"name": n} for n in _json_list(authorized, "approved")],
            "bans": [{"name": n, "reason": ""} for n in _json_list(banned, "approved")],
            "ip_bans": [{"ip": n, "reason": ""} for n in _json_list(banned, "ip-addresses")],
            "known_players": [],
            "id_hint": "HKMP lists hold auth keys (and names on the whitelist's pre-list); actions take a player's "
            "name, auth key or IP address.",
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
        target = self.validate_player_name(name)
        ip = _is_ip(target)
        commands = {
            "kick": "kick",
            "ban": "banip" if ip else "ban",
            "pardon": "unbanip" if ip else "unban",
            "whitelist_add": "whitelist add",
            "whitelist_remove": "whitelist remove",
            "op": "auth",
            "deop": "deauth",
        }
        if action not in commands:
            raise ApiError("Unknown player action", code="invalid_action")
        client = AgentClient(instance.node)
        if not client.status(instance.id).get("running"):
            raise ApiError(
                "The server is not running: HKMP changes its lists through its console (start the server, or edit the "
                "JSON files while it is stopped)",
                code="not_running",
                status_code=409,
            )
        output = client.command(instance.id, f"{commands[action]} {target}")
        cache.delete(f"players:online:{instance.pk}")
        return {"via": "console", "output": output}

    def consistency_note(self, instance: Instance) -> str:
        return ""


def _read_text(client: AgentClient, instance: Instance, path: str) -> str | None:
    try:
        return client.read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return None
        raise


__all__ = ["GAME", "HkmpDefinition", "parse_list"]
