"""Project Zomboid (dedicated server on indifferentbroccoli/projectzomboid-server-docker).

What the panel knows about it (probe ``audit-2026-11-09/design/probe-6games.md`` section 4, observed on
``v1.2.0`` and Build 42.22):

* Image ``indifferentbroccoli/projectzomboid-server-docker:<pinned vX.Y.Z>``; it downloads the game
  (DepotDownloader, app 380870, about 7 GB) on the first start and validates it at every start. The
  branch is chosen once: ``public`` (Build 42) or ``legacy41`` (Build 41, ``SERVER_BRANCH``). Root +
  ``PUID``/``PGID``; the game itself runs as root, so the agent fixes the ownership after every stop.
* Two folders: ``game/`` (the game, left out of backups) and ``data/`` (``-cachedir``: the server's
  ``Server/<name>.ini``, its sandbox settings, the world ``Saves/Multiplayer/<name>/`` and the accounts
  database ``db/<name>.db``). The server name is chosen once: it names every one of these files.
* Ports: the game port and the RakNet port, both UDP (16261/16262), published 1:1 (``DEFAULT_PORT``,
  ``UDP_PORT``); RCON (27015/tcp) stays internal.
* Control: Source RCON (the existing ``rcon`` driver; the password is the control secret, which the agent
  injects as ``RCON_PASSWORD``).
  Saves with ``save``, players with ``players``; accounts, bans and admins live in the server's database
  and change through RCON only (``kickuser``, ``banuser``, ``unbanuser``, ``setaccesslevel``,
  ``removeuserfromwhitelist``). A whitelist entry is an account with a password (``adduser "name" "password"``):
  the console makes it, the panel's players page does not.
* The admin account ``admin`` gets the admin password (a generated secret the owner can show) at every
  start; the launcher prints it, the agent hides it in every log it hands out.
* ``Server/<name>.ini`` is edited with the ``kv`` codec; the image overwrites the ports, the RCON password and
  MaxPlayers at every start (managed keys), and the panel writes ``UPnP=false`` (no router changes) and
  ``BackupsOnStart=false`` (the panel takes backups) before every start.
* Memory: ``MEMORY_XMX_GB`` = the container's GB - 2 (at least 2).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError

from .base import ConfigFile, GameOption, PortSpec, SecretSpec
from .foldersaves import FolderSaves
from .steamgame import SteamGameDefinition

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "project-zomboid"
DEFAULT_REPOSITORY = "indifferentbroccoli/projectzomboid-server-docker"
IMAGE_TAG = "v1.2.0"
GAME_PORT = 16261
UDP_PORT = 16262
ADMIN_PASSWORD = "admin_password"
SERVER_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
PASSWORD_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
# account names the panel passes in RCON commands (inside double quotes)
PLAYER_NAME = r"^[A-Za-z0-9_.-]([A-Za-z0-9_. -]{0,30}[A-Za-z0-9_.-])?$"
BRANCHES = ("public", "legacy41")
# the game's own backups (data/backups/: zips that hold the ini with the RCON password) are switched off: at
# a start, at a version change (the first start counts as one: the end-to-end run found backups/version/
# backup_1.zip with BackupsOnStart=false) and periodically
PANEL_KEYS = {"UPnP": "false", "BackupsOnStart": "false", "BackupsOnVersionChange": "false", "BackupsPeriod": "0"}
MANAGED_KEYS = frozenset({"DefaultPort", "UDPPort", "RCONPort", "RCONPassword", "MaxPlayers", *PANEL_KEYS})
GAME_BACKUPS = "data/backups"
LOGS = ("data/server-console.txt",)
LOG_FOLDERS = ("data/Logs",)
MASK = "********"
# lines that hold a password whatever its value (also one the server had before: logs keep old lines):
# Build 41 logs every RCON login with its body ("RCON: ID=31 Type=3 Body='<password>' Socket[...]"),
# the ini keeps the RCON and the join password
_RCON_LOGIN = re.compile(r"(Type=3 Body=')[^']*(')")
_INI_SECRET = re.compile(r"^(\s*(?:RCONPassword|Password)\s*=)(.+?)(\s*)$", re.MULTILINE)
_PLAYERS_COUNT = re.compile(r"Players connected \((\d+)\)")

INI_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "PublicName", "type": "string", "label": "Server name (in the server browser)", "group": "Listing", "initial": True},
    {"key": "PublicDescription", "type": "string", "label": "Description", "group": "Listing", "nullable": True},
    {
        "key": "Public",
        "type": "bool",
        "label": "List in the in-game server browser",
        "group": "Listing",
        "default": False,
        "initial": True,
    },
    {
        "key": "Password",
        "type": "string",
        "label": "Join password",
        "help": "Players need it to join. Empty: no password.",
        "group": "Access",
        "secret": True,
        "nullable": True,
        "initial": True,
    },
    {
        "key": "Open",
        "type": "bool",
        "label": "Anyone may create an account (off: whitelist only)",
        "help": "Off: only accounts an admin created join (Players -> Whitelist).",
        "group": "Access",
        "default": True,
        "initial": True,
    },
    {"key": "PVP", "type": "bool", "label": "PvP", "group": "Gameplay", "default": True, "initial": True},
    {"key": "PauseEmpty", "type": "bool", "label": "Pause while nobody is online", "group": "Gameplay", "default": True},
    {"key": "GlobalChat", "type": "bool", "label": "Global chat", "group": "Gameplay", "default": True},
    {
        "key": "SaveWorldEveryMinutes",
        "type": "int",
        "label": "Save the world every (minutes, 0 = only on events)",
        "group": "Saving",
        "min": 0,
        "max": 1440,
        "default": 0,
    },
)


def ini_file(name: str) -> ConfigFile:
    return ConfigFile(
        id="server-ini",
        path=f"data/Server/{name}.ini",
        codec="kv",
        label="Server settings (Server/<name>.ini)",
        help="Read when the server starts. The ports, the RCON password and the player limit are set by the image "
        "at every start; UPnP and the game's own backups at start are switched off by the panel. Sandbox settings: "
        "Server/<name>_SandboxVars.lua in the file manager.",
        managed_keys=MANAGED_KEYS,
        secret_keys=("RCONPassword", "Password"),
        fields=INI_FIELDS,
    )


INI_TEMPLATE = ini_file("<server name>")
LAUNCH = ConfigFile(
    id="launch",
    path="(container settings)",
    codec="json",
    label="Launch settings",
    help="Kept by the panel and passed to the server at its start (restart to apply).",
    fields=({"key": "max_players", "type": "int", "label": "Max players", "min": 1, "max": 100, "default": 16, "initial": True},),
)
RESET_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "reset_accounts",
        "type": "bool",
        "label": "Also reset accounts, bans and the whitelist",
        "help": "Moves the server's database (db/<name>.db) aside as well. The admin account is made again at the next start.",
        "default": False,
    },
)


def parse_players(out: str) -> dict[str, Any]:
    """RCON ``players``: ``Players connected (N): `` then one ``-name`` line per player."""
    names = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("-") and line[1:].strip() and line[1:].strip() not in names:
            names.append(line[1:].strip()[:64])
    m = _PLAYERS_COUNT.search(out or "")
    online = int(m.group(1)) if m else (len(names) if names else None)  # (None: nothing recognised, unknown)
    return {"online": online, "max": None, "players": names}


class ZomboidDefinition(SteamGameDefinition):
    # its control port listens on the network: the agent runs it on its own network only (A-6, R-8)
    custom_network = False
    world_option_keys = ("server_name",)  # (E-5: the save folder is named after it)
    id = GAME
    label = "Project Zomboid"
    profile = GAME
    control = "rcon"
    repository = DEFAULT_REPOSITORY
    image_tag = IMAGE_TAG
    default_memory = 8192
    min_memory_mb = 4096
    default_stop_timeout = 120
    retryable_install_exits = frozenset({1})  # the image exits 1 when DepotDownloader fails
    capability_set = frozenset(
        {
            "console_commands",
            "broadcast",
            "players",
            "player_actions",
            "consistent_backups",
            "world_reset",
            "world_import_export",
            "config_files",
            "diagnostics",
            "query",  # the player count of the metrics through A2S (the agent's /ping): no RCON connection
        }
    )
    stop_words = frozenset({"quit"})
    refuse_stop_commands = True
    command_hint = 'Project Zomboid server commands without "/", e.g. players, save, servermsg "Hello", help.'
    flush_command = "save"
    player_name_pattern = PLAYER_NAME
    player_list_labels = (("whitelist", "Whitelist (accounts)"), ("ops", "Admins"), ("bans", "Bans"))
    virtual_configs = (LAUNCH,)
    file_configs = (INI_TEMPLATE,)
    options = (
        GameOption(
            "server_name",
            "Server name (files)",
            "string",
            "servertest",
            "Names the server's settings, world and database files (letters, digits, - and _). Chosen once.",
            pinned=True,
            pattern=SERVER_NAME_PATTERN,
            pattern_help="1 to 32 letters, digits, - and _",
        ),
        GameOption(
            "branch",
            "Game branch",
            "enum",
            "public",
            "Build 42 (Steam's public branch) or Build 41 (legacy41). Players need the same build. Chosen once.",
            pinned=True,
            options=BRANCHES,
            option_labels=(("public", "Build 42 (public)"), ("legacy41", "Build 41 (legacy41)")),
        ),
    )
    secret_specs = (
        SecretSpec(
            ADMIN_PASSWORD,
            "Admin password",
            "The password of the in-game account 'admin' (log in with it to get admin rights). Generated when the "
            "server is created; show it here. It is set again at every start.",
            generate=20,
            reveal=True,
            pattern=PASSWORD_PATTERN,
            pattern_help="8 to 64 letters, digits, - and _",
        ),
    )
    fixed_version = {
        "id": "public",
        "label": "Steam branch chosen at creation",
        "help": "The image checks the game at every start and runs the newest build of the server's branch (Build 42 "
        "public or Build 41 legacy41). There is no version to choose; 'update' means restart.",
        # "Update now" is a restart: the image downloads the newest build of the branch at every start
        "updates_on_restart": True,
    }
    notes = (
        "Dedicated server on the indifferentbroccoli/projectzomboid-server-docker image. The first start downloads "
        "the game (about 7 GB on disk) and takes a few minutes; every start validates the game files.",
        "Needs at least 4 GB of memory (8 GB by default); the Java heap is the container's memory minus 2 GB.",
        "Forward the game port and the RakNet port on UDP (16261 and 16262 by default).",
        "Log in as 'admin' with the admin password (Settings -> Secrets) to get admin rights in game.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this UDP port (Project Zomboid's default is 16261; Steam queries it too).",
            one_to_one=True,
        ),
        PortSpec(
            "udp",
            "RakNet (direct)",
            UDP_PORT,
            "udp",
            UDP_PORT,
            "The second UDP port players' clients use (default 16262).",
            one_to_one=True,
        ),
    )

    def __init__(self) -> None:
        self.saves = FolderSaves(
            self,
            folder="data/Saves",
            markers=(r"map_t\.bin",),
            marker_help="a zip of a world folder from Saves/Multiplayer/ with map_t.bin in it (same server name and game build)",
        )

    # -- data ------------------------------------------------------------------------------------
    def server_name(self, instance: Instance) -> str:
        name = str((instance.game_options or {}).get("server_name") or "servertest")
        return name if re.fullmatch(SERVER_NAME_PATTERN, name) else "servertest"

    def branch_of(self, options: dict[str, Any]) -> str:
        return str(options.get("branch") or "public")

    def config_files_for(self, instance: Instance) -> tuple[ConfigFile, ...]:
        return (LAUNCH, ini_file(self.server_name(instance)))

    def config_file(self, file_id: str) -> ConfigFile | None:
        return LAUNCH if file_id == "launch" else INI_TEMPLATE if file_id == "server-ini" else None

    def ini_path(self, instance: Instance) -> str:
        return f"data/Server/{self.server_name(instance)}.ini"

    def memory_heap_gb(self, instance: Instance) -> int:
        return max(2, int(instance.memory_mb) // 1024 - 2)

    def game_env(self, instance: Instance) -> dict[str, str]:
        # (RCON_PASSWORD and RCON_PORT are the agent's: it injects the control secret)
        env = {
            "SERVER_NAME": self.server_name(instance),
            "DEFAULT_PORT": str(self.host_port(instance, "game", GAME_PORT)),
            "UDP_PORT": str(self.host_port(instance, "udp", UDP_PORT)),
            "MAX_PLAYERS": str(self.config_values(instance, "launch")["max_players"]),
            "MEMORY_XMX_GB": str(self.memory_heap_gb(instance)),
        }
        if (instance.game_options or {}).get("branch") == "legacy41":
            env["SERVER_BRANCH"] = "legacy41"
        if password := instance.secret_values.get(ADMIN_PASSWORD):
            env["ADMIN_PASSWORD"] = password
        return env

    def initial_files(self, instance: Instance, params: dict[str, Any]) -> dict[str, str]:
        from . import codecs

        chosen = {k: v for k, v in ((params.get("settings") or {}).get("server-ini") or {}).items() if INI_TEMPLATE.field(k)}
        values = {"PublicName": instance.name[:64], **chosen, **PANEL_KEYS}
        text = "# Written by MCPanel before the first start; Project Zomboid adds every other setting.\n"
        return {self.ini_path(instance): codecs.get("kv").update(text, values)}

    def before_apply(self, instance: Instance, client: AgentClient) -> None:
        """``UPnP=false`` and ``BackupsOnStart=false`` in the server's ini (it may have been replaced)."""
        from . import codecs

        kv = codecs.get("kv")
        path = self.ini_path(instance)
        text = _read_text(client, instance, path)
        current = kv.parse(text or "")
        changes = {k: v for k, v in PANEL_KEYS.items() if str(current.get(k, "")).lower() != v}
        if changes or text is None:
            client.mkdir(instance.id, path.rsplit("/", 1)[0], parents=True)
            client.write_text(instance.id, path, kv.update(text or "", changes))

    masks_by_line = True  # (every rule of masked_text works on single lines)

    def masked_text(self, instance: Instance, path: str, content: str) -> str:
        """The ini holds the RCON password (the image writes it) and the join password; Build 41's logs hold
        every RCON login with the password: hidden in the editor and in every download of those files."""
        from apps.instances.spec import rcon_password

        for value in (rcon_password(instance), instance.secret_values.get(ADMIN_PASSWORD)):
            if value and len(value) >= 8:
                content = content.replace(value, MASK)
        content = _RCON_LOGIN.sub(lambda m: f"{m.group(1)}{MASK}{m.group(2)}", content)
        return _INI_SECRET.sub(lambda m: f"{m.group(1)}{MASK}{m.group(3)}", content)

    def unmask_text(self, instance: Instance, path: str, content: str, current: str) -> str:
        """A saved ini keeps its passwords where the editor showed the mask."""
        if not path.strip("/").endswith(".ini"):
            return content
        values = {m.group(1).strip().rstrip("=").strip(): m.group(2) for m in _INI_SECRET.finditer(current or "")}

        def restore(m: re.Match[str]) -> str:
            key = m.group(1).strip().rstrip("=").strip()
            if m.group(2) == MASK and key in values:
                return f"{m.group(1)}{values[key]}{m.group(3)}"
            return m.group(0)

        return _INI_SECRET.sub(restore, content)

    def secret_paths(self, instance: Instance) -> tuple[str, ...]:
        # the game's own backup zips (its ini with the RCON password inside, not maskable): never handed out
        return (*super().secret_paths(instance), GAME_BACKUPS)

    def secret_bearing_paths(self, instance: Instance) -> tuple[str, ...]:
        return (self.ini_path(instance), *LOGS)

    def secret_bearing_folders(self, instance: Instance) -> tuple[str, ...]:
        return LOG_FOLDERS

    # -- console and players -------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        return text.strip().lstrip("/").strip()

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        line = self.chat_text(text, message)
        if line:
            client.command(instance.id, f'servermsg "{line}"')

    def online_players(self, instance: Instance, *, max_age: int = 30) -> dict[str, Any]:
        from django.core.cache import cache

        from apps.nodes.client import AgentClient

        key = f"players:online:{instance.pk}"
        if max_age and (hit := cache.get(key)) is not None:
            return hit
        result = parse_players(AgentClient(instance.node).command(instance.id, "players", timeout=15))
        result["max"] = self.config_values(instance, "launch")["max_players"]
        cache.set(key, result, max_age or 1)
        return result

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        from apps.nodes.client import AgentClient

        running = bool(AgentClient(instance.node).status(instance.id).get("running"))
        result: dict[str, Any] = {
            "running": running,
            "labels": dict(self.player_list_labels),
            "whitelist": [],
            "ops": [],
            "bans": [],
            "ip_bans": [],
            "known_players": [],
            # the accounts, bans and admins are in the server's database: changed through RCON, not listed
            "lists_unknown": True,
            "id_hint": "Project Zomboid keeps accounts, admins and bans in its database: the panel changes them while the "
            "server runs but cannot list them.",
            # (through RCON: only while the server runs)
            "changes_while_stopped": False,
        }
        if running:
            try:
                result["online"] = self.online_players(instance, max_age=0)
            except ApiError as exc:
                result["online"] = {"error": str(exc.detail)}
        return result

    COMMANDS = {
        "kick": 'kickuser "{name}"',
        "ban": 'banuser "{name}"',
        "pardon": 'unbanuser "{name}"',
        "op": 'setaccesslevel "{name}" admin',
        "deop": 'setaccesslevel "{name}" {plain}',
        "whitelist_remove": 'removeuserfromwhitelist "{name}"',
    }
    # the access level that takes admin rights away: Build 42 has "user"; Build 41 (legacy41) refuses it
    # ("Access Level '0' unknown, list of access level: player, admin, moderator, overseer, gm, observer")
    # and removes the level with "none" (end-to-end run of 2026-11-11)
    PLAIN_LEVEL = {"public": "user", "legacy41": "none"}
    # what the game answers when it did not do it: its usage text ("Use: /adduser ..."), an unknown access
    # level or command, a user that does not exist (kick/ban/unban of a name it does not know)
    refusal_pattern = (
        r"(?i)\bunknown\b|doesn'?t exist|does not exist|\buse:|\bnot found\b|no such user|is not banned|isn'?t banned"
    )
    # adding to the whitelist creates an account, which needs a password (adduser "name" "password"; with
    # the name alone the game only answers its usage text): the console does it, or players create their
    # own while the ini's Open=true
    WHITELIST_ADD_REFUSED = (
        'Project Zomboid accounts need a password: create the account in the console with adduser "name" "password" '
        "and give the player both (or let players create their own accounts: Open=true in the server settings)"
    )

    def player_action(self, instance: Instance, action: str, name: str, reason: str = "") -> dict[str, Any]:
        from django.core.cache import cache

        from apps.nodes.client import AgentClient

        self.require(instance, "player_actions")
        if action == "whitelist_add":
            raise ApiError(self.WHITELIST_ADD_REFUSED, code="invalid_action")
        if action not in self.COMMANDS:
            raise ApiError("Unknown player action", code="invalid_action")
        name = self.validate_player_name(name)
        client = AgentClient(instance.node)
        if not client.status(instance.id).get("running"):
            raise ApiError(
                "The server is not running: Project Zomboid changes accounts, admins and bans through RCON",
                code="not_running",
                status_code=409,
            )
        branch = str((instance.game_options or {}).get("branch") or "public")
        command = self.COMMANDS[action].format(name=name, plain=self.PLAIN_LEVEL.get(branch, "user"))
        reason = self.chat_text(reason, limit=120)
        if reason and action in ("kick", "ban"):
            command += f' -r "{reason}"'
        output = client.command(instance.id, command)
        cache.delete(f"players:online:{instance.pk}")
        return {"via": "rcon", "output": self.checked_reply(output, said=(name,))}

    def consistency_note(self, instance: Instance) -> str:
        return (
            "the world was saved through RCON (save, answered once it was written) right before the copy; the game is "
            "not paused, so a save during the copy can be in the backup as well"
        )

    # -- the world -------------------------------------------------------------------------------
    def save_now(self, client: AgentClient, instance: Instance) -> bool:
        client.command(instance.id, "save", timeout=120)
        return True

    def world_reset_fields(self) -> tuple[dict[str, Any], ...]:
        return RESET_FIELDS

    def world_label(self, instance: Instance) -> str:
        return self.server_name(instance)

    def world_paths(self, client: AgentClient, instance: Instance, purpose: str, params: dict[str, Any]) -> list[str]:
        name = self.server_name(instance)
        folder = f"data/Saves/Multiplayer/{name}"
        if purpose == "export":
            return [folder]
        paths = [folder, f"{folder}_player"]
        if purpose == "reset" and params.get("reset_accounts"):
            paths.append(f"data/db/{name}.db")
        return paths

    def world_target(self, client: AgentClient, instance: Instance, root_name: str, params: dict[str, Any]) -> str:
        return f"data/Saves/Multiplayer/{self.server_name(instance)}"

    def world_notes(self, instance: Instance) -> list[str]:
        name = self.server_name(instance)
        return [
            f"The world is data/Saves/Multiplayer/{name}/; accounts and bans are in data/db/{name}.db and the sandbox "
            f"settings in data/Server/{name}_SandboxVars.lua (a reset keeps them unless you choose otherwise).",
        ]


def _read_text(client: AgentClient, instance: Instance, path: str) -> str | None:
    try:
        return client.read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return None
        raise


__all__ = ["GAME", "ZomboidDefinition", "parse_players"]
