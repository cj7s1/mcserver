"""Counter-Strike 2 (dedicated server on joedwards32/cs2).

What the panel knows about it (probe ``audit-2026-11-09/design/probe-6games.md`` section 5; the image was
inspected, the game was not downloaded on the probe's desktop):

* Image ``joedwards32/cs2:<pinned X.Y.Z>``; ``entry.sh`` runs SteamCMD (app 730) at every start, so the
  server follows Steam's public branch (the whole game, about 60 GB, lives in the server folder). The
  image works only as uid 1000:1000 (the agent's profile runs it so and owns the folder that way) and
  needs ``init`` (bash as PID 1 ignores SIGTERM).
* **entry.sh evals its start line**: every value the panel passes is checked against a strict pattern
  (letters, digits and a few signs, never quotes, ``$``, ``;``, ``/`` or backslashes), here and again in
  the agent's profile.
* Ports: the game on UDP (default 27015, ``CS2_PORT``, published 1:1) and, while CSTV is on, its port
  (default 27020/udp, ``TV_PORT``). RCON listens on the game port's number over TCP: never published.
* Control: Source RCON (``rcon`` driver with ``port_env``); players and the status through A2S (the
  agent's status query). No world, no saves: a backup keeps the configuration (``game/csgo/cfg/``).
* Settings: a virtual config (name, players, game mode, map, LAN, bots, CSTV) passed as variables;
  ``server.cfg`` is written by the image at every start and holds the RCON password (hidden in the
  editor; the agent refuses writes to it and to the ``pre.sh``/``post.sh`` hooks).
* Secrets: the RCON password is the panel's control secret; the join password and the Game Server
  Login Token (GSLT, app 730) are optional, but without a GSLT the server is not listed and players
  outside the LAN may not find it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError

from .base import ConfigFile, PortSpec, SecretSpec
from .steamgame import SteamGameDefinition

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "cs2"
DEFAULT_REPOSITORY = "joedwards32/cs2"
IMAGE_TAG = "5.0.1"
GAME_PORT = 27015
TV_PORT = 27020
JOIN_PASSWORD = "server_password"
GSLT = "gslt"
TV_PASSWORD = "tv_password"
SERVER_CFG = "game/csgo/cfg/server.cfg"
# values that reach entry.sh's eval and its sed expressions: no quotes, $, ;, /, \ or control characters
NAME_PATTERN = r"^[A-Za-z0-9._-][A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-]$|^[A-Za-z0-9._-]$"
MAP_PATTERN = r"^[a-z0-9_]{1,64}$"
ALNUM_PASSWORD = r"^[A-Za-z0-9]{1,64}$"
GSLT_PATTERN = r"^[A-F0-9]{32}$"
GAME_MODES = ["competitive", "casual", "deathmatch", "wingman", "armsrace"]
# player names the panel passes to RCON's kick (inside double quotes; the Source console splits at ;)
PLAYER_NAME = r'^[^";\\\x00-\x1f\x7f]{1,64}$'

SERVER_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "server_name",
        "type": "string",
        "label": "Server name",
        "pattern": NAME_PATTERN,
        "pattern_help": "1 to 64 letters, digits, spaces, . _ and - (no other signs: the image runs its start line "
        "through the shell)",
        "default": "Counter-Strike 2 server",
        "initial": True,
    },
    {"key": "max_players", "type": "int", "label": "Max players", "min": 1, "max": 64, "default": 10, "initial": True},
    {
        "key": "game_mode",
        "type": "enum",
        "label": "Game mode",
        "options": GAME_MODES,
        "default": "competitive",
        "initial": True,
        "group": "Game",
    },
    {
        "key": "start_map",
        "type": "string",
        "label": "Start map",
        "pattern": MAP_PATTERN,
        "pattern_help": "a map name such as de_inferno (lower-case letters, digits and _)",
        "default": "de_inferno",
        "initial": True,
        "group": "Game",
    },
    {
        "key": "map_group",
        "type": "string",
        "label": "Map group",
        "pattern": MAP_PATTERN,
        "pattern_help": "a map group such as mg_active (lower-case letters, digits and _)",
        "default": "mg_active",
        "group": "Game",
    },
    {
        "key": "lan",
        "type": "bool",
        "label": "LAN only",
        "help": "Only players on your local network can join.",
        "default": False,
        "group": "Network",
    },
    {
        "key": "cstv",
        "type": "bool",
        "label": "CSTV (spectators)",
        "help": "Publishes the CSTV port (UDP) while on.",
        "default": False,
        "group": "Network",
    },
    {
        "key": "bot_quota",
        "type": "int",
        "label": "Bots",
        "min": 0,
        "max": 64,
        "nullable": True,
        "group": "Bots",
        "help": "Empty: the game mode's default.",
    },
    {
        "key": "bot_difficulty",
        "type": "enum",
        "label": "Bot difficulty",
        "options": ["", "0", "1", "2", "3"],
        "help": "0 easy, 1 normal, 2 hard, 3 expert; empty: the game mode's default.",
        "default": "",
        "group": "Bots",
    },
    {
        "key": "bot_quota_mode",
        "type": "enum",
        "label": "Bot fill mode",
        "options": ["", "fill", "competitive", "normal"],
        "default": "",
        "group": "Bots",
    },
)

CONFIGS = (
    ConfigFile(
        id="server",
        path="(container settings)",
        codec="json",
        label="Server",
        help="Kept by the panel and passed to the server at its start (restart to apply). server.cfg is written "
        "by the image from these at every start.",
        fields=SERVER_FIELDS,
    ),
)

_STATUS_PLAYER = re.compile(r"^\s*\d+\s+.*?'(.+)'\s*$")


def parse_status(out: str) -> list[str]:
    """Player names of RCON ``status`` (lines ending in ``'name'``; bots and the CSTV slot left out)."""
    names: list[str] = []
    for line in (out or "").splitlines():
        if "BOT" in line or "[NoChan]" in line:
            continue
        if (m := _STATUS_PLAYER.match(line)) and m.group(1) and m.group(1) not in names:
            names.append(m.group(1)[:64])
    return names


class Cs2Definition(SteamGameDefinition):
    # its control port listens on the network: the agent runs it on its own network only (A-6, R-8)
    custom_network = False
    id = GAME
    label = "Counter-Strike 2"
    profile = GAME
    control = "rcon"
    repository = DEFAULT_REPOSITORY
    image_tag = IMAGE_TAG
    default_memory = 4096
    min_memory_mb = 2048
    default_stop_timeout = 30
    retryable_install_exits = frozenset({5, 8})  # SteamCMD: timed out, missing configuration
    first_start_timeout = 4 * 3600  # the first start downloads about 60 GB
    capability_set = frozenset(
        {"console_commands", "broadcast", "players", "player_actions", "query", "config_files", "diagnostics"}
    )
    stop_words = frozenset({"quit", "exit", "_restart"})
    # the Source console's refusals of a kick (a name nobody online has, an unknown command)
    refusal_pattern = r"(?i)\bnot found\b|can'?t (?:find|kick)|cannot (?:find|kick)|no (?:such )?(?:user|player)|unknown command"
    refuse_stop_commands = True
    command_hint = "Server console commands, e.g. status, say Hello, changelevel de_dust2, mp_restartgame 1."
    player_name_pattern = PLAYER_NAME
    player_list_labels = ()
    virtual_configs = CONFIGS
    secret_specs = (
        SecretSpec(
            GSLT,
            "Game Server Login Token (GSLT)",
            "Needed for the server to be listed and reachable for players outside your network: create one for app 730 "
            "(one token per server). Without it the server works on your LAN.",
            link="https://steamcommunity.com/dev/managegameservers",
            pattern=GSLT_PATTERN,
            pattern_help="32 characters, digits and A-F (as Steam shows it)",
            per_server=True,  # one token per server: a copy does not take it over
        ),
        SecretSpec(
            JOIN_PASSWORD,
            "Join password",
            "Players need it to join (connect <address>; password <password>). Empty: no password.",
            pattern=ALNUM_PASSWORD,
            pattern_help="1 to 64 letters and digits",
        ),
        SecretSpec(
            TV_PASSWORD,
            "CSTV password",
            "Spectators need it while CSTV is on. Empty: anyone may watch.",
            pattern=ALNUM_PASSWORD,
            pattern_help="1 to 64 letters and digits",
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
        "Dedicated server on the joedwards32/cs2 image. The first start downloads the whole game (about 60 GB on "
        "disk; check the free space first) and can take a long time; every start checks for an update.",
        "Needs at least 2 GB of memory (4 GB by default).",
        "Forward the game port on UDP (and the CSTV port while CSTV is on). RCON uses the same number on TCP: never "
        "forward the TCP port.",
        "A Game Server Login Token (GSLT, app 730, https://steamcommunity.com/dev/managegameservers) is needed for "
        "players outside your network; add it under Settings -> Secrets.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this UDP port (CS2's default is 27015). RCON listens on the same number over TCP inside "
            "the panel's network only.",
            one_to_one=True,
        ),
        PortSpec(
            "tv",
            "CSTV",
            TV_PORT,
            "udp",
            TV_PORT,
            "Spectators connect here (UDP); published only while CSTV is on.",
            one_to_one=True,
            when=("server", "cstv", True),
        ),
    )

    def initial_config(self, file_id: str, data: dict[str, Any]) -> dict[str, Any]:
        name = " ".join(re.sub(r"[^A-Za-z0-9 ._-]+", " ", str(data.get("name") or "")).split())[:64].strip()
        return {"server_name": name} if name and re.fullmatch(NAME_PATTERN, name) else {}

    def game_env(self, instance: Instance) -> dict[str, str]:
        values = self.config_values(instance, "server")
        secrets = instance.secret_values
        env = {
            "CS2_SERVERNAME": str(values["server_name"]),
            "CS2_PORT": str(self.host_port(instance, "game", GAME_PORT)),
            "CS2_MAXPLAYERS": str(values["max_players"]),
            "CS2_GAMEALIAS": str(values["game_mode"]),
            "CS2_STARTMAP": str(values["start_map"]),
            "CS2_MAPGROUP": str(values["map_group"]),
            "CS2_LAN": "1" if values["lan"] else "0",
            "TV_ENABLE": "1" if values["cstv"] else "0",
            # always the number the panel reserved for CSTV, also while it is off (nothing listens then): the
            # agent's default 27020 would otherwise be refused as a second name of a game port set to 27020
            "TV_PORT": str(self.host_port(instance, "tv", TV_PORT)),
        }
        if values["cstv"] and secrets.get(TV_PASSWORD):
            env["TV_PW"] = secrets[TV_PASSWORD]
        if values.get("bot_quota") is not None:
            env["CS2_BOT_QUOTA"] = str(values["bot_quota"])
        if values.get("bot_difficulty"):
            env["CS2_BOT_DIFFICULTY"] = str(values["bot_difficulty"])
        if values.get("bot_quota_mode"):
            env["CS2_BOT_QUOTA_MODE"] = str(values["bot_quota_mode"])
        if secrets.get(JOIN_PASSWORD):
            env["CS2_PW"] = secrets[JOIN_PASSWORD]
        if secrets.get(GSLT):
            env["SRCDS_TOKEN"] = secrets[GSLT]
        return env

    def masked_text(self, instance: Instance, path: str, content: str) -> str:
        """server.cfg holds the RCON, join and CSTV passwords (the image writes them): hidden in the editor."""
        from apps.instances.spec import rcon_password

        secrets = instance.secret_values
        for value in (rcon_password(instance), secrets.get(JOIN_PASSWORD), secrets.get(TV_PASSWORD), secrets.get(GSLT)):
            if value and len(value) >= 4:
                content = content.replace(value, "********")
        return content

    def secret_bearing_paths(self, instance: Instance) -> tuple[str, ...]:
        return (SERVER_CFG,)

    def secret_bearing_folders(self, instance: Instance) -> tuple[str, ...]:
        return ("game/csgo/logs",)  # the server's logs (sv_logfile): masked like server.cfg, never zipped

    # -- console and players -------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        return text.strip().lstrip("/").strip()

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        line = self.chat_text(text, message)
        if line:
            client.command(instance.id, f"say {line}")

    def online_players(self, instance: Instance, *, max_age: int = 20) -> dict[str, Any]:
        """The agent's status query (A2S): no RCON connection, nothing in the server's log."""
        from django.core.cache import cache

        from apps.nodes.client import AgentClient

        key = f"players:online:{instance.pk}"
        if max_age and (hit := cache.get(key)) is not None:
            return hit
        client = AgentClient(instance.node)
        try:
            answer = client.ping(instance.id)
        except ApiError as exc:
            if exc.error_code != "ping_unavailable":
                raise
            # an agent without the status query for CS2: RCON's status
            names = parse_status(client.command(instance.id, "status", timeout=15))
            answer = {"online": len(names), "max": self.config_values(instance, "server")["max_players"], "sample": names}
        names = [str(n)[:64] for n in answer.get("sample") or [] if isinstance(n, str) and n.strip()]
        result = {"online": answer.get("online", len(names)), "max": answer.get("max"), "players": names}
        cache.set(key, result, max_age or 1)
        return result

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        from apps.nodes.client import AgentClient

        running = bool(AgentClient(instance.node).status(instance.id).get("running"))
        result: dict[str, Any] = {
            "running": running,
            "labels": {},
            "whitelist": [],
            "ops": [],
            "bans": [],
            "ip_bans": [],
            "known_players": [],
            # a kick needs the running server (RCON)
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
        if action != "kick":
            raise ApiError("Counter-Strike 2 servers can kick players from the panel (bans: the console)", code="invalid_action")
        name = self.validate_player_name(name)
        client = AgentClient(instance.node)
        if not client.status(instance.id).get("running"):
            raise ApiError("The server is not running, so nobody can be kicked", code="not_running", status_code=409)
        output = client.command(instance.id, f'kick "{name}"')
        cache.delete(f"players:online:{instance.pk}")
        return {"via": "rcon", "output": self.checked_reply(output, said=(name,))}


__all__ = ["GAME", "Cs2Definition", "parse_status"]
