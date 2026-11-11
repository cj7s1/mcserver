"""Terraria: vanilla and tModLoader on PassiveLemon's image, TShock on ryshe's (the third game).

What the panel knows about it (docs/design/GAME-DEFINITIONS.md 8.3/8.4, corrected by the spike of
2026-11-10 and the probes of this release):

* Variants (``Instance.server_type``): ``vanilla``, ``tmodloader`` and ``tshock``; one agent profile
  per variant (game ``terraria``). Images are pinned to exact tags: PassiveLemon's
  ``terraria-<version>`` / ``tmodloader-<version>`` and ryshe's ``tshock-<terraria>-<tshock>``.
  tModLoader is built on Terraria 1.4.4.9: vanilla clients on 1.4.5 cannot join it.
* One named port, the game: 7777/tcp. TShock's REST API (7878) stays internal (never published).
* ``serverconfig.txt`` (``kv``) is written by the panel; the server reads it (PassiveLemon:
  ``SERVERCONFIG=1``; TShock: ``-config``). The panel owns ``world``, ``worldpath``, ``worldname``,
  ``port``, ``banlist`` and tModLoader's ``modpack``/``modpath`` and writes them before every apply,
  together with ``autocreate`` (a vanilla server needs a preset world, or it stops at an interactive
  world picker).
* The world is ``Worlds/<world name>.wld`` (tModLoader adds ``.twld``); the name is chosen at
  creation (``game_options.world_name``). A reset moves the world files aside and sets size,
  difficulty and seed explicitly, so the server creates the new world at its next start; an import
  puts an uploaded ``.wld`` under that name.
* Console: PassiveLemon's ``inject`` script through the agent's exec primitive (vanilla,
  tModLoader); TShock's REST API with a token the panel generates (the control secret,
  ``Instance.rcon_password``, written into ``config.json`` before every apply), its console (stdin)
  as the fallback. ``exit`` (and TShock's ``off``) would end the server: the panel's Stop instead.
* Stop: PassiveLemon's script saves on SIGTERM and exits 143 (a clean stop for this image), TShock
  saves on SIGINT and exits 0.
* TShock's first start prints a one-time ``/setup`` code; the panel instead creates the owner
  account over REST (setup step ``owner_account``, password from the secret ``owner_password``).
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError, ConflictError, UpstreamError

from .base import ConfigFile, GameDefinition, GameOption, PortSpec, SecretSpec, SetupStep, Variant

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

GAME = "terraria"
VANILLA, TMODLOADER, TSHOCK = "vanilla", "tmodloader", "tshock"
VARIANT_IDS = (VANILLA, TMODLOADER, TSHOCK)
PASSIVELEMON_REPOSITORY = "passivelemon/terraria-docker"
TSHOCK_REPOSITORY = "ryshe/terraria"
GAME_PORT = 7777
WORLDS_DIR = "Worlds"
SET_ASIDE_DIR = "Worlds-replaced"
MODPACK = "mcpanel"
TMODLOADER_TERRARIA = "1.4.4.9"
CONFIG_FILE = "serverconfig.txt"
TSHOCK_CONFIG = "config.json"
REST_USER = "mcpanel"
REST_PORT = 7878
DATA_DIR = {VANILLA: "/opt/terraria/config", TMODLOADER: "/opt/terraria/config", TSHOCK: "/config"}
TAG_PREFIX = {VANILLA: "terraria-", TMODLOADER: "tmodloader-", TSHOCK: "tshock-"}
VERSION_RE = {
    VANILLA: re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?$"),
    TMODLOADER: re.compile(r"^\d{4}\.\d{1,2}\.\d{1,3}\.\d{1,3}$"),
    # TShock 6 and later: the layout the agent's zero-capability profile was built and tested on
    TSHOCK: re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}-(?:[6-9]|\d{2})\.\d{1,3}\.\d{1,3}$"),
}
VERSION_EXAMPLE = {VANILLA: "1.4.5.8", TMODLOADER: "2026.09.3.1", TSHOCK: "1.4.5.8-6.2.1"}
HUB_TAGS = "https://hub.docker.com/v2/repositories/{repo}/tags"
VERSIONS_TTL = 3600
SETUP_READY_WAIT = 600
WORLD_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
# Terraria character names: up to 20 characters; quotes and backslashes would break the commands
PLAYER_NAME = r'(?=.{1,20}$)[^\s"\\/\x00-\x1f][^"\\\x00-\x1f\x7f]*(?<!\s)'
SIZES = {"1": "Small", "2": "Medium", "3": "Large"}
DIFFICULTIES = {"0": "Classic", "1": "Expert", "2": "Master", "3": "Journey"}

VARIANTS = (
    Variant(VANILLA, "Terraria (vanilla)"),
    Variant(TMODLOADER, "tModLoader"),
    Variant(TSHOCK, "TShock"),
)
VARIANT_NOTES: dict[str, tuple[str, ...]] = {
    VANILLA: (
        "The official dedicated server (PassiveLemon's image), pinned to an exact version: players need the same version.",
        "Commands go to its console; there is no remote admin protocol.",
    ),
    TMODLOADER: (
        f"tModLoader is built on Terraria {TMODLOADER_TERRARIA}: players need the tModLoader client (Steam); vanilla "
        "clients on newer Terraria versions cannot join.",
        "The first start of every new container downloads the .NET runtime from Microsoft (internet needed, slower start).",
        "Mods: put .tmod files and enabled.json into ModPacks/mcpanel/Mods/ with the file manager (a mod manager comes later).",
    ),
    TSHOCK: (
        "TShock adds accounts, permissions, bans and a REST API; it runs without any Linux capability.",
        "The panel creates the in-game owner account for you (no /setup code needed): log in with /login.",
    ),
}
VARIANT_MEMORY = {VANILLA: 1536, TMODLOADER: 3072, TSHOCK: 1536}

# --- serverconfig.txt --------------------------------------------------------------------------------
SIZE_FIELD = {
    "key": "autocreate",
    "type": "enum",
    "label": "World size (for a new world)",
    "options": ["1", "2", "3"],
    "help": "1 = small, 2 = medium, 3 = large. Used only when a new world is created (the first start or a world reset).",
    "default": "2",
    "initial": True,
    "restart": False,
}
DIFFICULTY_FIELD = {
    "key": "difficulty",
    "type": "enum",
    "label": "Difficulty (for a new world)",
    "options": ["0", "1", "2", "3"],
    "help": "0 = classic, 1 = expert, 2 = master, 3 = journey. Used only when a new world is created.",
    "default": "0",
    "initial": True,
    "restart": False,
}
SEED_FIELD = {
    "key": "seed",
    "type": "string",
    "label": "World seed (for a new world)",
    "help": "Empty: random. Text or a number; also Terraria's secret seeds. Used only when a new world is created.",
    "nullable": True,
    "initial": True,
    "restart": False,
}
SERVERCONFIG_FIELDS: tuple[dict[str, Any], ...] = (
    SIZE_FIELD,
    DIFFICULTY_FIELD,
    SEED_FIELD,
    {"key": "maxplayers", "type": "int", "label": "Max players", "min": 1, "max": 255, "default": 8, "initial": True},
    {
        "key": "password",
        "type": "string",
        "label": "Server password",
        "help": "Players need it to join. Empty: no password.",
        "secret": True,
        "nullable": True,
        "initial": True,
    },
    {"key": "motd", "type": "string", "label": "Message of the day", "nullable": True, "initial": True},
    {
        "key": "secure",
        "type": "enum",
        "label": "Cheat protection",
        "options": ["0", "1"],
        "help": "1 = kick players the server thinks are cheating.",
        "default": "1",
    },
    {"key": "language", "type": "enum", "label": "Server language", "options": ["en-US", "de-DE", "it-IT", "fr-FR", "es-ES", "ru-RU", "zh-Hans", "pt-BR", "pl-PL"], "default": "en-US"},
)  # fmt: skip
# TShock reads the world settings from serverconfig.txt; players, password and the rest come from config.json
TSHOCK_SERVERCONFIG_FIELDS = (SIZE_FIELD, DIFFICULTY_FIELD, SEED_FIELD)
SERVERCONFIG_MANAGED = frozenset({"world", "worldpath", "worldname", "port", "banlist", "modpack", "modpath", "upnp"})
TSHOCK_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": "Settings.ServerName", "type": "string", "label": "Server name", "nullable": True, "initial": True},
    {
        "key": "Settings.ServerPassword",
        "type": "string",
        "label": "Server password",
        "help": "Players need it to join. Empty: no password.",
        "secret": True,
        "nullable": True,
        "initial": True,
    },
    {"key": "Settings.MaxSlots", "type": "int", "label": "Max players", "min": 1, "max": 255, "default": 8, "initial": True},
    {"key": "Settings.RequireLogin", "type": "bool", "label": "Players must log in to an account", "default": False},
    {"key": "Settings.EnableWhitelist", "type": "bool", "label": "Whitelist (whitelist.txt, by IP)", "default": False},
    {"key": "Settings.SpawnProtection", "type": "bool", "label": "Spawn protection", "default": True},
    {"key": "Settings.AutoSave", "type": "bool", "label": "Autosave", "default": True},
    {"key": "Settings.BackupInterval", "type": "int", "label": "TShock's own world backups every (minutes, 0 = off)", "min": 0, "max": 10080, "default": 10, "help": "Kept in backups/ (not in the panel's backups)."},
)  # fmt: skip
TSHOCK_MANAGED = frozenset(
    {
        "Settings.RestApiEnabled",
        "Settings.RestApiPort",
        "Settings.ApplicationRestTokens",
        "Settings.LogRest",
        "Settings.EnableTokenEndpointAuthentication",
        "Settings.ServerPort",
    }
)


def _serverconfig(variant: str) -> ConfigFile:
    return ConfigFile(
        id="serverconfig",
        path=CONFIG_FILE,
        codec="kv",
        label="Server config (serverconfig.txt)",
        help="Read when the server starts. World, port and paths are managed by MCPanel.",
        managed_keys=SERVERCONFIG_MANAGED,
        secret_keys=("password",),
        fields=TSHOCK_SERVERCONFIG_FIELDS if variant == TSHOCK else SERVERCONFIG_FIELDS,
        variants=(variant,),
    )


SERVERCONFIG_FILES = {v: _serverconfig(v) for v in VARIANT_IDS}
TSHOCK_FILE = ConfigFile(
    id="tshock",
    path=TSHOCK_CONFIG,
    codec="json",
    label="TShock (config.json)",
    help="Read when the server starts. The REST API settings and the game port are managed by MCPanel.",
    managed_keys=TSHOCK_MANAGED,
    secret_keys=("Settings.ServerPassword",),
    fields=TSHOCK_FIELDS,
    variants=(TSHOCK,),
)
RESET_FILE = ConfigFile(
    id="world-reset",
    path=CONFIG_FILE,
    codec="kv",
    label="New world",
    fields=(
        {**SIZE_FIELD, "label": "World size", "help": "1 = small, 2 = medium, 3 = large."},
        {**DIFFICULTY_FIELD, "label": "Difficulty"},
        {**SEED_FIELD, "key": "world_seed", "label": "World seed", "help": "Empty: random. Text or a number."},
    ),
)

# --- players ---------------------------------------------------------------------------------------------
_PLAYING_COUNT = re.compile(r"(\d+) players? connected", re.IGNORECASE)
_PLAYING_LINE = re.compile(r"^(?::\s*)?(\S.*?) \((?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+)(?::\d+)?\)\s*$")
_TSHOCK_EXISTS = re.compile(r"already exists", re.IGNORECASE)


def version_key(version: str) -> tuple[int, ...]:
    """Every number of a version in order (``1.4.5.8-6.2.1`` -> (1, 4, 5, 8, 6, 2, 1))."""
    return tuple(int(n) for n in re.findall(r"\d+", str(version)))


def parse_playing(out: str) -> dict[str, Any]:
    """Terraria's ``playing``: one ``name (ip:port)`` line per player, then ``N players connected.``
    (or ``No players connected.``). Lines of other output in between are ignored."""
    names: list[str] = []
    count = None
    for raw in (out or "").splitlines():
        line = raw.strip()
        if "no players connected" in line.lower():
            count = 0
            continue
        if m := _PLAYING_COUNT.search(line):
            count = int(m.group(1))
            continue
        if (m := _PLAYING_LINE.match(line)) and re.fullmatch(PLAYER_NAME, m.group(1)) and m.group(1) not in names:
            names.append(m.group(1))
    return {"online": len(names) if count is None else count, "max": None, "players": names}


def parse_banlist(text: str) -> list[str]:
    """Names in Terraria's banlist.txt (``//name`` lines, each followed by the banned address)."""
    return [line[2:].strip() for line in (text or "").splitlines() if line.startswith("//") and line[2:].strip()]


class TerrariaDefinition(GameDefinition):
    id = GAME
    label = "Terraria"
    variants = VARIANTS
    profile = GAME
    control = "console"
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
            "query",
        }
    )
    stop_words = frozenset({"exit", "exit-nosave", "off", "off-nosave"})
    keeps_command_slash = False
    refuse_stop_commands = True
    command_hint = "Terraria console commands, e.g. playing, say Hello, save, time (TShock: any of its commands)."
    flush_command = "save"
    notes = (
        "Three flavours: the official server, tModLoader (mods) and TShock (accounts, permissions, REST API).",
        "Players connect over TCP 7777; forward the TCP port on your router for players outside your network.",
    )
    ports = (
        PortSpec("game", "Game", GAME_PORT, "tcp", GAME_PORT, "Players connect to this TCP port (Terraria's default is 7777)."),
    )
    options = (
        GameOption(
            "world_name",
            "World name",
            "string",
            "world",
            "The world file's name (Worlds/<name>.wld): letters, digits, - and _. Chosen once.",
            pinned=True,
        ),
        GameOption(
            "owner_account",
            "TShock owner account",
            "string",
            "owner",
            "The in-game account the panel creates in the owner group (log in with /login <account> <password>).",
            pinned=True,
            variants=(TSHOCK,),
        ),
    )
    secret_specs = (
        SecretSpec(
            "owner_password",
            "TShock owner password",
            "The password of the owner account the panel creates in TShock (at least 4 characters). Stored encrypted.",
            required=False,
            variants=(TSHOCK,),
        ),
    )
    setup_steps = (
        SetupStep(
            "owner_account",
            "Create the TShock owner account",
            "Creates the in-game owner account over TShock's REST API, so the one-time /setup code is not needed.",
            kind="secret",
            secret="owner_password",
        ),
    )
    min_memory_mb = 1024
    default_stop_timeout = 60
    player_name_pattern = PLAYER_NAME
    player_list_labels = (("bans", "Bans (by IP)"),)
    default_backup_excludes = ("*.tmp", "Worlds/*.bad")
    allowed_user_env = frozenset({"TZ"})
    clean_exit_codes = frozenset({0, 143})

    def __init__(self) -> None:
        self.saves = TerrariaSaves(self)

    # -- variant data ---------------------------------------------------------------------------------
    def variant(self, instance: Instance) -> str:
        variant = str(instance.server_type or "")
        return variant if variant in VARIANT_IDS else VANILLA

    def agent_variant(self, instance: Instance) -> str | None:
        return self.variant(instance)

    def default_memory_mb(self) -> int:
        return VARIANT_MEMORY[VANILLA]

    def default_memory_mb_for(self, variant: str | None) -> int:
        return VARIANT_MEMORY.get(str(variant or ""), self.default_memory_mb())

    def capabilities(self, instance: Instance | None = None) -> frozenset[str]:
        if instance is None:
            return self.capability_set
        if self.variant(instance) == TSHOCK:
            return self.capability_set
        return self.capability_set - {"query"}

    def config_files(self) -> tuple[ConfigFile, ...]:
        return (*SERVERCONFIG_FILES.values(), TSHOCK_FILE)

    def config_files_for(self, instance: Instance) -> tuple[ConfigFile, ...]:
        variant = self.variant(instance)
        return (SERVERCONFIG_FILES[variant], TSHOCK_FILE) if variant == TSHOCK else (SERVERCONFIG_FILES[variant],)

    def config_file(self, file_id: str) -> ConfigFile | None:
        # create requests name the generic file ids; the wizard sends only the variant's fields
        if file_id == "serverconfig":
            return SERVERCONFIG_FILES[VANILLA]
        if file_id == "tshock":
            return TSHOCK_FILE
        return None

    def secret_specs_for(self, instance: Instance) -> tuple[SecretSpec, ...]:
        return self.secret_specs if self.variant(instance) == TSHOCK else ()

    def setup_status(self, instance: Instance) -> dict[str, Any] | None:
        if self.variant(instance) != TSHOCK:
            return None
        return super().setup_status(instance)

    def catalog_entry(self) -> dict[str, Any]:
        entry = super().catalog_entry()
        variants = []
        for v in VARIANTS:
            variants.append(
                {
                    **v.as_dict(),
                    "default_memory_mb": VARIANT_MEMORY[v.id],
                    "notes": list(VARIANT_NOTES[v.id]),
                    "capabilities": sorted(self.capability_set - ({"query"} if v.id != TSHOCK else set())),
                    "stop_words": sorted({"exit", "exit-nosave"} | ({"off", "off-nosave"} if v.id == TSHOCK else set())),
                    "player_list_labels": {} if v.id == TSHOCK else dict(self.player_list_labels),
                    "game_version": TMODLOADER_TERRARIA if v.id == TMODLOADER else None,
                }
            )
        entry["variants"] = variants
        return entry

    # -- versions and image ----------------------------------------------------------------------------
    @property
    def has_versions(self) -> bool:
        return True

    def _repository(self, variant: str) -> str:
        if variant == TSHOCK:
            return TSHOCK_REPOSITORY
        return self.runtime_repository(PASSIVELEMON_REPOSITORY)

    def versions(self, variant: str | None = None) -> list[dict[str, Any]]:
        from apps.catalog.providers import cached

        variant = variant if variant in VARIANT_IDS else VANILLA
        repo = self._repository(variant)
        return cached(f"catalog:terraria:versions:{variant}:{repo}", VERSIONS_TTL, lambda: self._fetch_versions(repo, variant))

    def _fetch_versions(self, repo: str, variant: str) -> list[dict[str, Any]]:
        from apps.catalog.providers import _get

        prefix = TAG_PREFIX[variant]
        pattern = VERSION_RE[variant]
        digests: dict[str, str] = {}
        url: str | None = HUB_TAGS.format(repo=repo.removeprefix("docker.io/"))
        params: dict[str, Any] | None = {"page_size": 100, "name": prefix.rstrip("-")}
        for _page in range(10):
            if not url:
                break
            data = _get(url, params=params) or {}
            for tag in data.get("results") or []:
                name = str(tag.get("name") or "")
                if name.startswith(prefix):
                    digests[name[len(prefix) :]] = str(tag.get("digest") or "")
            url, params = data.get("next"), None
        exact = {v: d for v, d in digests.items() if pattern.match(v)}
        if not exact:
            raise UpstreamError(
                f"Docker Hub lists no {self.variant_label(variant)} versions for this image", code="upstream_error"
            )
        latest_digest = digests.get("latest") or ""
        pre_digest = digests.get("latest-pre") or ""

        def stable(v: str) -> bool:
            if variant == TMODLOADER:
                # tModLoader's x.y.3.z releases are stable, x.y.2.z previews
                return v.split(".")[2] == "3" and not (pre_digest and exact[v] == pre_digest and exact[v] != latest_digest)
            return True

        stables = [v for v in exact if stable(v)]
        latest = next((v for v in exact if latest_digest and exact[v] == latest_digest and stable(v)), None)
        if latest is None and stables:
            latest = max(stables, key=version_key)
        return [
            {
                "id": v,
                "label": self.version_label(variant, v),
                "stable": stable(v),
                "latest": v == latest,
            }
            for v in sorted(exact, key=version_key, reverse=True)
        ]

    @staticmethod
    def variant_label(variant: str) -> str:
        return next((v.label for v in VARIANTS if v.id == variant), variant)

    @staticmethod
    def version_label(variant: str, version: str) -> str:
        if variant == TSHOCK and "-" in version:
            terraria, tshock = version.split("-", 1)
            return f"TShock {tshock} (Terraria {terraria})"
        if variant == TMODLOADER:
            return f"{version} (Terraria {TMODLOADER_TERRARIA})"
        return version

    def validate_version(self, version: str, variant: str | None = None) -> str:
        variant = variant if variant in VARIANT_IDS else VANILLA
        version = str(version or "").strip()
        if not VERSION_RE[variant].match(version):
            raise ApiError(
                f"Choose an exact {self.variant_label(variant)} version such as {VERSION_EXAMPLE[variant]}",
                code="invalid_version",
            )
        if version not in {v["id"] for v in self.versions(variant)}:
            raise ApiError(f"{self.variant_label(variant)} {version} has no image tag", code="invalid_version")
        return version

    def version_key(self, version: str) -> tuple[int, ...]:
        return version_key(version)

    def image(self, instance: Instance) -> str:
        variant = self.variant(instance)
        return f"{self._repository(variant)}:{TAG_PREFIX[variant]}{instance.minecraft_version}"

    # -- creating a server ------------------------------------------------------------------------------
    def create_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        variant = str(data.get("variant") or "")
        if variant not in VARIANT_IDS:
            raise ApiError("Choose a Terraria variant: vanilla, tmodloader or tshock", code="invalid_variant")
        version = self.validate_version(data.get("version") or "", variant)
        options = self.validate_options(data.get("options"))
        world = str(options.get("world_name") or "world")
        if not WORLD_NAME_RE.match(world):
            raise ApiError("The world name may use letters, digits, - and _ (at most 40)", code="invalid_option")
        out = {"world_name": world}
        if variant == TSHOCK:
            account = str(options.get("owner_account") or "owner")
            if not ACCOUNT_RE.match(account):
                raise ApiError("The owner account may use letters, digits, ., - and _ (at most 32)", code="invalid_option")
            out["owner_account"] = account
        if variant == TMODLOADER:
            out["modpack"] = MODPACK
        return {
            "server_type": variant,
            "minecraft_version": version,
            "loader_version": "",
            "game_options": out,
            "memory_mb": int(data.get("memory_mb") or self.default_memory_mb_for(variant)),
            "memory_overhead_mb": 0,
            "stop_timeout": self.default_stop_timeout,
            "jvm_flags_preset": "none",
            "jvm_args": "",
        }

    def game_env(self, instance: Instance) -> dict[str, str]:
        # tModLoader's start script exits (0) without MODPACK, even when serverconfig.txt names the pack;
        # the other image variables are refused by the agent (serverconfig.txt carries the settings)
        if self.variant(instance) == TMODLOADER:
            return {"MODPACK": MODPACK}
        return {}

    def world_name(self, instance: Instance) -> str:
        name = str((instance.game_options or {}).get("world_name") or "world")
        return name if WORLD_NAME_RE.match(name) else "world"

    def managed_serverconfig(self, instance: Instance) -> dict[str, Any]:
        """The keys of serverconfig.txt the panel owns (written before every apply)."""
        variant = self.variant(instance)
        data = DATA_DIR[variant]
        world = self.world_name(instance)
        values: dict[str, Any] = {
            "world": f"{data}/{WORLDS_DIR}/{world}.wld",
            "worldpath": f"{data}/{WORLDS_DIR}",
            "worldname": world,
            "port": str(GAME_PORT),
            "upnp": "0",
        }
        if variant != TSHOCK:
            values["banlist"] = f"{data}/banlist.txt"
        if variant == TMODLOADER:
            values["modpack"] = f"{data}/ModPacks/{MODPACK}/Mods/enabled.json"
            values["modpath"] = f"{data}/ModPacks/{MODPACK}/Mods/"
        return values

    def initial_files(self, instance: Instance, params: dict[str, Any]) -> dict[str, str]:
        from . import codecs

        variant = self.variant(instance)
        settings = params.get("settings") or {}
        cfg = SERVERCONFIG_FILES[variant]
        defaults = {f["key"]: f["default"] for f in cfg.fields if "default" in f}
        chosen = {k: v for k, v in (settings.get("serverconfig") or {}).items() if cfg.field(k) is not None}
        text = "# Written by MCPanel. World, port and paths are managed by the panel; edit the rest freely.\n"
        files = {CONFIG_FILE: codecs.get("kv").update(text, {**defaults, **chosen, **self.managed_serverconfig(instance)})}
        if variant == TMODLOADER:
            files[f"ModPacks/{MODPACK}/Mods/enabled.json"] = "[]\n"
        if variant == TSHOCK:
            values = {k: v for k, v in (settings.get("tshock") or {}).items() if TSHOCK_FILE.field(k) is not None}
            files[TSHOCK_CONFIG] = codecs.get("json").update("{}", {**values, **self.rest_settings(instance)})
        return files

    # -- before every apply ---------------------------------------------------------------------------------
    def rest_settings(self, instance: Instance) -> dict[str, Any]:
        """TShock's REST settings the panel owns: on, internal port, no request logging, and one token
        (the control secret) for the panel's user."""
        from apps.instances.spec import rcon_password

        return {
            "Settings.RestApiEnabled": True,
            "Settings.RestApiPort": REST_PORT,
            "Settings.LogRest": False,
            "Settings.EnableTokenEndpointAuthentication": False,
            "Settings.ServerPort": GAME_PORT,
            "Settings.ApplicationRestTokens": {rcon_password(instance): {"Username": REST_USER, "UserGroupName": "superadmin"}},
        }

    def before_apply(self, instance: Instance, client: AgentClient) -> None:
        """Write the panel-owned keys: serverconfig.txt's world/port/paths (and autocreate when it is
        missing: a vanilla server without a world to create stops at an interactive picker), and
        TShock's REST settings with the current token. Nothing is logged."""
        from . import codecs

        variant = self.variant(instance)
        kv = codecs.get("kv")
        text = _read_text(client, instance, CONFIG_FILE)
        current = kv.parse(text or "")
        changes = {k: v for k, v in self.managed_serverconfig(instance).items() if current.get(k) != v}
        if not str(current.get("autocreate") or "").strip():
            changes["autocreate"] = SIZE_FIELD["default"]
        if changes or text is None:
            client.write_text(instance.id, CONFIG_FILE, kv.update(text or "", changes))
        client.mkdir(instance.id, WORLDS_DIR, parents=True)
        if variant == TSHOCK:
            js = codecs.get("json")
            raw = _read_text(client, instance, TSHOCK_CONFIG)
            try:
                new = js.update(raw or "{}", self.rest_settings(instance))
            except ValueError as exc:
                raise ConflictError(
                    f"{TSHOCK_CONFIG} is not valid JSON, so the panel cannot set up TShock's REST API; fix or delete it "
                    "in the file manager",
                    code="invalid_file",
                ) from exc
            if raw is None or js.parse(raw) != js.parse(new):
                client.write_text(instance.id, TSHOCK_CONFIG, new)
        if variant == TMODLOADER:
            enabled = f"ModPacks/{MODPACK}/Mods/enabled.json"
            if _read_text(client, instance, enabled) is None:
                client.mkdir(instance.id, f"ModPacks/{MODPACK}/Mods", parents=True)
                client.write_text(instance.id, enabled, "[]\n")

    def masked_text(self, instance: Instance, path: str, content: str) -> str:
        """The text editor's view of a server file: TShock's REST token hidden in config.json."""
        from apps.instances.spec import rcon_password

        if self.variant(instance) == TSHOCK and path.strip("/") == TSHOCK_CONFIG:
            secret = rcon_password(instance)
            if secret and len(secret) >= 8:
                return content.replace(secret, "********")
        return content

    # -- setup: the TShock owner account ----------------------------------------------------------------
    def run_setup_step(self, instance: Instance, step: SetupStep, ctx: Any) -> str:
        from apps.nodes.client import AgentClient

        if step.id != "owner_account" or self.variant(instance) != TSHOCK:
            return super().run_setup_step(instance, step, ctx)
        password = instance.secret_values.get("owner_password") or ""
        if len(password) < 4:
            raise ApiError("Set the TShock owner password (at least 4 characters) first", code="secret_missing")
        account = str((instance.game_options or {}).get("owner_account") or "owner")
        client = AgentClient(instance.node)
        deadline = time.monotonic() + SETUP_READY_WAIT
        while True:
            status = client.status(instance.id)
            if status.get("running") and status.get("health") == "healthy":
                break
            if not status.get("running") and not status.get("restarting"):
                raise ConflictError(
                    "Start the server first: the owner account is created over TShock's REST API", code="not_running"
                )
            if time.monotonic() > deadline:
                raise ConflictError(f"TShock did not become ready within {SETUP_READY_WAIT // 60} minutes", code="not_ready")
            ctx.progress(0.2, "Waiting for TShock to be ready")
            ctx.sleep_until(time.monotonic() + 3)
        result = _game_action(client, instance, "owner_account", {"user": account, "password": password, "group": "owner"})
        how = "created" if result.get("created") else "updated (it existed)"
        return (
            f"Account '{account}' {how} in the owner group; log in in game with /login {account} <password>. "
            "TShock turns its one-time /setup code off at its next start."
        )

    # -- console -------------------------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        # Terraria's console takes commands without "/" (TShock's REST adds it again)
        return text.strip().lstrip("/").strip()

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        line = text if not message else f"{text} - {message}"
        line = " ".join(line.split()).lstrip("/-").strip().rstrip(";")[:200]
        if line:
            client.command(instance.id, f"say {line}")

    def online_players(self, instance: Instance, *, max_age: int = 55) -> dict[str, Any]:
        """TShock: its status (no console line); vanilla/tModLoader: the console's ``playing`` (cached)."""
        from django.core.cache import cache

        from apps.nodes.client import AgentClient

        key = f"players:online:{instance.pk}"
        if max_age and (hit := cache.get(key)) is not None:
            return hit
        client = AgentClient(instance.node)
        if self.variant(instance) == TSHOCK:
            answer = client.ping(instance.id)
            names = [str(n) for n in answer.get("sample") or [] if isinstance(n, str)]
            result = {"online": answer.get("online", len(names)), "max": answer.get("max"), "players": names}
        else:
            result = parse_playing(client.command(instance.id, "playing", timeout=10))
        cache.set(key, result, max_age or 1)
        return result

    # -- players ---------------------------------------------------------------------------------------------
    def _running(self, instance: Instance) -> bool:
        from apps.nodes.client import AgentClient

        return bool(AgentClient(instance.node).status(instance.id).get("running"))

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        running = self._running(instance)
        tshock = self.variant(instance) == TSHOCK
        result: dict[str, Any] = {
            "running": running,
            "labels": {} if tshock else dict(self.player_list_labels),
            "whitelist": [],
            "ops": [],
            "bans": [],
            "ip_bans": [],
            "known_players": [],
        }
        if not tshock:
            from apps.nodes.client import AgentClient

            text = _read_text(AgentClient(instance.node), instance, "banlist.txt") or ""
            result["bans"] = [{"name": n, "reason": ""} for n in parse_banlist(text)]
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
        tshock = self.variant(instance) == TSHOCK
        allowed = {"kick", "ban"} if tshock else {"kick", "ban", "pardon"}
        if action not in allowed:
            raise ApiError(
                f"{self.variant_label(self.variant(instance))} servers do not support this player action", code="invalid_action"
            )
        name = self.validate_player_name(name)
        reason = " ".join(re.sub(r"[\r\n]", " ", reason or "").split())[:120].rstrip(";")
        client = AgentClient(instance.node)
        if action == "pardon":
            text = _read_text(client, instance, "banlist.txt") or ""
            lines = text.splitlines()
            kept: list[str] = []
            skip = False
            removed = False
            for line in lines:
                if skip:
                    skip = False
                    continue
                if line.startswith("//") and line[2:].strip().lower() == name.lower():
                    removed = skip = True
                    continue
                kept.append(line)
            if not removed:
                raise ApiError(f"{name} is not in banlist.txt", code="not_listed", status_code=404)
            client.write_text(instance.id, "banlist.txt", "\n".join(kept) + ("\n" if kept else ""))
            return {"via": "file", "output": f"Removed {name} (and the banned address) from banlist.txt"}
        if not self._running(instance):
            raise ApiError(
                "The server is not running: Terraria bans and kicks players who are online", code="not_running", status_code=409
            )
        if tshock:
            command = f'kick "{name}" {reason}' if action == "kick" else f'ban add "{name}" {reason}'
        else:
            command = f"{action} {name}"
        output = client.command(instance.id, command.strip())
        cache.delete(f"players:online:{instance.pk}")
        return {"via": "console", "output": output}

    # -- software -----------------------------------------------------------------------------------------------
    def software_state(self, instance: Instance) -> dict[str, Any]:
        return {
            "game": self.id,
            "variant": self.variant(instance),
            "version": instance.minecraft_version,
            "image": instance.image,
        }

    def describe_software(self, state: dict[str, Any]) -> str:
        variant = str(state.get("variant") or VANILLA)
        version = str(state.get("version") or "?")
        if variant == TSHOCK:
            return self.version_label(TSHOCK, version)
        if variant == TMODLOADER:
            return f"tModLoader {version}"
        return f"Terraria {version}"

    def set_software(self, instance: Instance, state: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        from apps.instances.services import bump_revision

        if state.get("variant") and state["variant"] != self.variant(instance):
            raise ConflictError(
                "That backup is of another Terraria variant; restore it into a server of the same variant",
                code="software_mismatch",
            )
        version = state.get("version")
        if not version or version == instance.minecraft_version:
            return {}
        changes = {"minecraft_version": (instance.minecraft_version, version)}
        instance.minecraft_version = str(version)
        instance.image = self.image(instance)
        instance.save(update_fields=["minecraft_version", "image"])
        bump_revision(instance)
        return changes


def _read_text(client: AgentClient, instance: Instance, path: str) -> str | None:
    try:
        return client.read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code == "not_found":
            return None
        raise


def _game_action(client: AgentClient, instance: Instance, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """``POST /v1/instances/<id>/game/<action>`` (agents with ``game_actions``); the params are never logged."""
    method = getattr(client, "game_action", None)
    if method is not None:
        return method(instance.id, action, params)
    answer = client._json(
        "POST", f"/v1/instances/{instance.id}/game/{action}", json={"params": params, "timeout": 60}, timeout=90
    )
    return answer.get("result") or {}


class TerrariaSaves:
    """The worlds of a Terraria server (the ``saves`` model of the definition).

    ``Worlds/<world name>.wld`` is the world the server runs (tModLoader keeps its mod data next to it
    in ``.twld``; both rotate ``.bak`` files). A reset or an import moves every world file in
    ``Worlds/`` aside into ``Worlds-replaced/<UTC stamp>/`` (kept in the server folder, so they are in
    backups and can be imported again); the server then creates a new world with the size, difficulty
    and seed of the reset (always set explicitly) or loads the uploaded one at its next start.
    """

    kind = "worlds"
    extension = ".wld"
    content_type = "application/octet-stream"

    def __init__(self, definition: TerrariaDefinition) -> None:
        self.definition = definition

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "extension": self.extension,
            "folder": WORLDS_DIR,
            "set_aside_folder": SET_ASIDE_DIR,
            "reset_fields": [dict(f) for f in RESET_FILE.fields],
        }

    def reset_config(self, instance: Instance) -> ConfigFile:
        return RESET_FILE

    def current(self, instance: Instance) -> str:
        return self.definition.world_name(instance)

    def current_path(self, instance: Instance) -> str:
        return f"{WORLDS_DIR}/{self.current(instance)}{self.extension}"

    def list(self, client: AgentClient, instance: Instance) -> dict[str, Any]:
        from .factorio import _entries

        current = self.current(instance)
        worlds = []
        for entry in _entries(client, instance, WORLDS_DIR):
            name = str(entry.get("name") or "")
            if entry.get("type") != "file" or not name.endswith(self.extension):
                continue
            stem = name[: -len(self.extension)]
            worlds.append(
                {
                    "name": stem,
                    "path": f"{WORLDS_DIR}/{name}",
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
        worlds.sort(key=lambda s: (not s["current"], s["name"]))
        return {
            "current": current,
            "saves": worlds,
            "set_aside": set_aside,
            "options": {"variant": self.definition.variant(instance), **(instance.game_options or {})},
            "reset_fields": self.describe()["reset_fields"],
        }

    @staticmethod
    def _is_world_file(name: str) -> bool:
        return bool(re.search(r"\.(wld|twld)(\.bak\d*)?$", name)) or name.endswith((".wld.bad", ".twld.bad"))

    def set_aside(self, client: AgentClient, instance: Instance, stamp: str) -> list[str]:
        """Move every world file (``*.wld``, ``*.twld`` and their backups) into Worlds-replaced/<stamp>/."""
        from .factorio import _entries

        names = [
            str(e.get("name"))
            for e in _entries(client, instance, WORLDS_DIR)
            if e.get("type") == "file" and self._is_world_file(str(e.get("name") or ""))
        ]
        if not names:
            return []
        target = f"{SET_ASIDE_DIR}/{stamp}"
        client.mkdir(instance.id, target, parents=True)
        for name in names:
            client.rename(instance.id, f"{WORLDS_DIR}/{name}", f"{target}/{name}")
        return names

    def prepare_reset(self, client: AgentClient, instance: Instance, values: dict[str, Any], seed: Any) -> str:
        """The new world's size, difficulty and seed in serverconfig.txt (size always written: an
        autocreate smaller than an existing world must never meet it). Returns the seed it set."""
        from . import codecs

        kv = codecs.get("kv")
        text = _read_text(client, instance, CONFIG_FILE) or ""
        current = kv.parse(text)
        world_seed = values.get("world_seed")
        if world_seed in (None, "") and seed is not None:
            world_seed = str(seed)
        changes = {
            "autocreate": str(values.get("autocreate") or current.get("autocreate") or SIZE_FIELD["default"]),
            "difficulty": str(values.get("difficulty") or current.get("difficulty") or DIFFICULTY_FIELD["default"]),
            "seed": str(world_seed).strip() if world_seed not in (None, "") else None,
            **self.definition.managed_serverconfig(instance),
        }
        client.write_text(instance.id, CONFIG_FILE, kv.update(text, changes))
        return changes["seed"] or "random"

    def validate_upload(self, upload: Any) -> str | None:
        """Why an upload is not a Terraria world (None: it looks like one): the file header
        (version, the magic ``relogic`` and file type 2, a world)."""
        name = str(getattr(upload, "name", "") or "")
        if not name.lower().endswith(self.extension):
            return f"Upload a {self.extension} world file"
        try:
            upload.seek(0)
            head = upload.read(12)
        except (OSError, ValueError):
            return "The file could not be read"
        finally:
            try:
                upload.seek(0)
            except (OSError, ValueError):
                pass
        if len(head) < 12 or head[4:11] != b"relogic" or head[11] != 2:
            return "The file is not a Terraria world (.wld) file"
        return None


__all__ = ["GAME", "TerrariaDefinition", "TerrariaSaves", "parse_banlist", "parse_playing", "version_key"]
