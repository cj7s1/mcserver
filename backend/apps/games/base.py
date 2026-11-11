"""The game definition: what the panel knows about one game (docs/design/GAME-DEFINITIONS.md, section 4).

A definition is a Python object with data plus a few methods. Everything that differs between
games on the panel side is asked from the instance's definition (``apps.games.for_instance``):
the container spec, the capabilities that decide which features exist for a server, the memory
model, the console's stop words and command normalisation, in-game broadcasts, the online player
list, log analysis and the software record of backups. The agent has the matching runtime
profile for each game (``profile``) and refuses a game it does not know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from apps.core.errors import ConflictError

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

DEFAULT_GAME = "minecraft"

# Capability names (design 4.2). The panel and the UI read them; a definition declares its set,
# possibly per instance (Minecraft: content and modpacks depend on the server type).
CAPABILITIES: tuple[str, ...] = (
    "console_commands",  # commands through the control channel (console, schedules)
    "broadcast",  # in-game messages, e.g. the countdown before a restart/stop
    "players",  # online player list
    "player_actions",  # whitelist / op / ban / kick
    "query",  # status and player count without a console command
    "consistent_backups",  # the game saves (and pauses saving) around a backup
    "world_reset",
    "world_import_export",
    "software_update",  # pick a version of the game / loader
    "content",  # mods/plugins manager
    "modpacks",
    "jvm",  # heap and JVM flags
    "eula",  # the owner must accept the game's EULA when creating a server
    "properties",  # the server.properties editor
    "diagnostics",  # known problems found in the console log
)

# Capabilities of games other than Minecraft that Minecraft does not have (kept out of CAPABILITIES so
# a Minecraft server's capability list stays what it was).
EXTRA_CAPABILITIES: tuple[str, ...] = (
    "config_files",  # the "Game settings" editor: the definition's config files rendered from their schema
)
ALL_CAPABILITIES: tuple[str, ...] = CAPABILITIES + EXTRA_CAPABILITIES

# jvm: memory_mb is the Java heap; the container gets heap + overhead.
# plain: memory_mb is the container limit (plus an explicit memory_overhead_mb, if any).
MEMORY_MODELS = ("jvm", "plain")


@dataclass(frozen=True)
class Variant:
    """A flavour of a game (Minecraft: the server type; Terraria: vanilla/tModLoader/TShock)."""

    id: str
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


@dataclass(frozen=True)
class ConfigFile:
    """A configuration file of the game the panel can edit with a form.

    ``fields`` is the form's schema: dicts with ``key`` (dotted for nested JSON objects, "section.key"
    for INI), ``type`` (string, int, number, bool, enum, list), ``label`` and optionally ``help``,
    ``group``, ``default``, ``min``, ``max``, ``options`` (enum), ``nullable`` (empty = null),
    ``secret`` (masked), ``restart`` (default True: takes effect at the next start) and ``initial``
    (asked in the create wizard). Keys outside the schema are kept as they are in the file.
    ``managed_keys`` are never written through the form; ``secret_keys`` are masked when read.
    """

    path: str
    codec: str  # java-properties | json | ini | kv (apps/games/codecs.py)
    label: str = ""
    managed_keys: frozenset[str] = field(default_factory=frozenset)
    secret_keys: tuple[str, ...] = ()
    id: str = ""  # the file's id in the API; default: the file name without its extension
    fields: tuple[dict[str, Any], ...] = ()
    help: str = ""
    # the game variants that have this file (empty: every variant); the wizard shows only those
    variants: tuple[str, ...] = ()

    @property
    def file_id(self) -> str:
        if self.id:
            return self.id
        name = self.path.rsplit("/", 1)[-1]
        return name.rsplit(".", 1)[0] if "." in name else name

    def field(self, key: str) -> dict[str, Any] | None:
        return next((f for f in self.fields if f["key"] == key), None)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "id": self.file_id,
            "path": self.path,
            "codec": self.codec,
            "label": self.label or self.path,
            "help": self.help,
            "fields": [dict(f) for f in self.fields],
            "managed_keys": sorted(self.managed_keys),
            "secret_keys": list(self.secret_keys),
        }
        if self.variants:
            data["variants"] = list(self.variants)
        return data


@dataclass(frozen=True)
class PortSpec:
    """A named port of a game (design 5.3). The agent's runtime profile must allow the same container
    port and protocol; the panel picks the host port (``default_host_port`` when it is free)."""

    name: str
    label: str
    container_port: int
    protocol: str  # tcp | udp
    default_host_port: int
    help: str = ""
    # the name of another port of the game that this one always shares its host port number with
    # (Satisfactory: the HTTPS API on 7777/tcp and the game on 7777/udp); the panel never asks for it
    same_port_as: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = {
            "name": self.name,
            "label": self.label,
            "container_port": self.container_port,
            "protocol": self.protocol,
            "default_host_port": self.default_host_port,
            "help": self.help,
        }
        if self.same_port_as:
            data["same_port_as"] = self.same_port_as
        return data


@dataclass(frozen=True)
class SecretSpec:
    """A value only the owner can provide (a token), stored encrypted in ``Instance.secrets``."""

    key: str
    label: str
    help: str = ""
    required: bool = False
    link: str = ""
    variants: tuple[str, ...] = ()  # the variants that have it (empty: every variant)
    # required only when the option (key, value) is chosen (Satisfactory: the client password when
    # access is "password")
    required_when: tuple[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {"key": self.key, "label": self.label, "help": self.help, "required": self.required, "link": self.link}
        if self.variants:
            data["variants"] = list(self.variants)
        if self.required_when:
            data["required_when"] = {"option": self.required_when[0], "value": self.required_when[1]}
        return data


@dataclass(frozen=True)
class SetupStep:
    """A first-start step (design 4.5). ``kind``: ``auto`` (the ``setup`` job runs it), ``secret`` (it
    waits for the secret ``secret`` to be set, then runs automatically) or ``manual`` (the owner does
    something outside the panel and marks it done)."""

    id: str
    label: str
    help: str = ""
    kind: str = "auto"
    secret: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "help": self.help, "kind": self.kind, "secret": self.secret}


@dataclass(frozen=True)
class GameOption:
    """A choice made when a server is created; ``pinned`` options cannot be changed afterwards
    (Factorio's Space Age: switching it under an existing save converts the save)."""

    key: str
    label: str
    type: str  # bool | enum | string | int
    default: Any
    help: str = ""
    pinned: bool = True
    options: tuple[str, ...] = ()
    variants: tuple[str, ...] = ()  # the variants that have it (empty: every variant)
    # what the form shows for each value of an enum ((value, label), ...)
    option_labels: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "help": self.help,
            "pinned": self.pinned,
        }
        if self.options:
            data["options"] = list(self.options)
        if self.variants:
            data["variants"] = list(self.variants)
        if self.option_labels:
            data["option_labels"] = dict(self.option_labels)
        return data


class SetupWaiting(Exception):
    """A setup step cannot run yet (``GameDefinition.run_setup_step``); the message says why."""


class CapabilityUnavailable(ConflictError):
    """The feature does not exist for this server's game (409 ``unsupported_by_game``)."""

    default_code = "unsupported_by_game"


class GameDefinition:
    """Base class: a game without any optional feature. Subclasses set the data and override
    what their game supports."""

    id: str = ""
    label: str = ""
    variants: tuple[Variant, ...] = ()
    profile: str = ""  # the agent's runtime profile id
    control: str | None = None  # the agent's control driver id (None: plain docker stop, no console)
    memory_model: str = "plain"
    capability_set: frozenset[str] = frozenset()
    stop_words: frozenset[str] = frozenset()
    # console command that makes a running server write everything to disk (the world download)
    flush_command: str | None = None
    notes: tuple[str, ...] = ()
    default_backup_excludes: tuple[str, ...] = ()

    # -- multi-game phase 2: what a game other than Minecraft declares ---------------------------
    ports: tuple[PortSpec, ...] = ()  # named ports; the first one is the server's main port
    options: tuple[GameOption, ...] = ()  # choices pinned at creation (Instance.game_options)
    secret_specs: tuple[SecretSpec, ...] = ()  # values only the owner can provide (Instance.secrets)
    setup_steps: tuple[SetupStep, ...] = ()  # first-start steps (Instance.setup_state, the setup job)
    # commands keep a leading "/" (Factorio's console commands start with one; Minecraft's are stripped)
    keeps_command_slash: bool = False
    command_hint: str = ""  # shown next to the console input
    # POST /command and schedule commands refuse the stop words (a server that exits by itself is
    # started again by Docker's restart policy): the console turns them into the Stop job instead
    refuse_stop_commands: bool = False
    min_memory_mb: int = 512
    default_stop_timeout: int = 90
    player_name_pattern: str = r"^[A-Za-z0-9_]{1,16}$"
    player_list_labels: tuple[tuple[str, str], ...] = ()  # ("ops", "Admins"), ... for the players page
    # a server file the panel writes the control secret (Instance.rcon_password) into before every
    # apply (Factorio: config/rconpw, which the image passes to the server); the agent's driver reads it
    control_secret_file: str | None = None
    # Exit codes of the image's own download step (SteamCMD) that are worth another try while the
    # server was never ready; the first start retries up to install_attempts times.
    retryable_install_exits: frozenset[int] = frozenset()
    install_attempts: int = 3
    # Exit codes of a correct, saving stop (Terraria's image exits 143 after its SIGTERM save)
    clean_exit_codes: frozenset[int] = frozenset({0})
    # the save files of the game (non-Minecraft games; Minecraft has instances/world.py)
    saves: Any = None
    # environment variables an owner may add (Settings -> Environment) for this game; all others are
    # refused (images of other games have variables that download code or run scripts)
    allowed_user_env: frozenset[str] = frozenset({"TZ"})
    # a game without versions to choose: {"id", "label", "help"} of what it runs (the catalog says so
    # and the wizard asks for no version; Satisfactory follows Steam's stable branch)
    fixed_version: dict[str, str] | None = None

    # -- data -------------------------------------------------------------------------------
    def default_memory_mb(self) -> int:
        return 2048

    def default_memory_mb_for(self, variant: str | None) -> int:
        """The default memory of a new server of ``variant`` (tModLoader needs more than vanilla)."""
        return self.default_memory_mb()

    def config_files(self) -> tuple[ConfigFile, ...]:
        """The files the config editor shows (the first one is the main file)."""
        return ()

    def capabilities(self, instance: Instance | None = None) -> frozenset[str]:
        """The features this game has; for ``instance``, the ones this server has."""
        return self.capability_set

    def has(self, instance: Instance | None, capability: str) -> bool:
        return capability in self.capabilities(instance)

    def require(self, instance: Instance, capability: str, message: str | None = None) -> None:
        """409 ``unsupported_by_game`` when ``instance`` lacks ``capability``."""
        if not self.has(instance, capability):
            raise CapabilityUnavailable(
                message or f"{self.label or self.id} servers do not support this ({capability})",
                details={"game": self.id, "capability": capability},
            )

    def variant(self, instance: Instance) -> str:
        return str(instance.server_type or "")

    def catalog_entry(self) -> dict[str, Any]:
        """The game in ``GET /api/catalog/games``. The keys of phase 2 are present only when the game
        has something to say there (Minecraft's entry is what it was)."""
        entry: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "variants": [v.as_dict() for v in self.variants],
            "capabilities": sorted(self.capabilities()),
            "memory_model": self.memory_model,
            "default_memory_mb": self.default_memory_mb(),
            "notes": list(self.notes),
        }
        if self.ports:
            entry["ports"] = [p.as_dict() for p in self.ports]
        files = [f for f in self.config_files() if f.fields] if self.id != DEFAULT_GAME else []
        if files:
            entry["config_files"] = [f.as_dict() for f in files]
        if self.options:
            entry["options"] = [o.as_dict() for o in self.options]
        if self.secret_specs:
            entry["secrets"] = [s.as_dict() for s in self.secret_specs]
        if self.setup_steps:
            entry["setup_steps"] = [s.as_dict() for s in self.setup_steps]
        if self.id != DEFAULT_GAME and self.stop_words:
            entry["stop_words"] = sorted(self.stop_words)
        if self.keeps_command_slash:
            entry["keeps_command_slash"] = True
        if self.command_hint:
            entry["command_hint"] = self.command_hint
        if self.has_versions:
            entry["has_versions"] = True
        if self.fixed_version:
            # no version to pick: the server follows a branch (Satisfactory: stable, updated at every start)
            entry["fixed_version"] = dict(self.fixed_version)
        if self.id != DEFAULT_GAME and self.ports:
            entry["min_memory_mb"] = self.min_memory_mb
            entry["default_stop_timeout"] = self.default_stop_timeout
            if self.has(None, "players"):
                entry["player_name_pattern"] = self.player_name_pattern
                entry["player_list_labels"] = dict(self.player_list_labels)
        if self.saves is not None:
            entry["world"] = self.saves.describe()
        return entry

    # -- versions and image ----------------------------------------------------------------------
    @property
    def has_versions(self) -> bool:
        """Whether the game has a version catalog (``versions``)."""
        return False

    def versions(self, variant: str | None = None) -> list[dict[str, Any]]:
        """``[{"id", "label", "stable", "latest"}]``, newest first (raises UpstreamError when the
        source cannot be reached)."""
        raise CapabilityUnavailable(f"{self.label} has no version catalog", details={"game": self.id})

    def validate_version(self, version: str, variant: str | None = None) -> str:
        """The exact version to pin; 400 when the catalog does not have it."""
        return version

    def image(self, instance: Instance) -> str:
        """``repository:tag`` of the runtime image of the server (per-game channel rule)."""
        raise NotImplementedError(f"{self.id} has no runtime image")

    def runtime_repository(self, default: str) -> str:
        """The image repository of this game: SystemSetting ``runtime_images[<game>].repository``, else
        ``default`` (Minecraft has its own two settings)."""
        from apps.core.models import SystemSetting

        configured = SystemSetting.get("runtime_images") or {}
        entry = configured.get(self.id) if isinstance(configured, dict) else None
        repo = entry.get("repository") if isinstance(entry, dict) else None
        return str(repo) if repo else default

    # -- options, secrets, environment -----------------------------------------------------------
    def default_options(self) -> dict[str, Any]:
        return {o.key: o.default for o in self.options}

    def validate_options(self, values: dict[str, Any] | None) -> dict[str, Any]:
        """The pinned options of a new server: defaults plus what the request chose (400 otherwise)."""
        from apps.core.errors import ApiError

        out = self.default_options()
        for key, value in (values or {}).items():
            option = next((o for o in self.options if o.key == key), None)
            if option is None:
                raise ApiError(f"Unknown option '{key}' for {self.label}", code="invalid_option")
            if option.type == "bool" and not isinstance(value, bool):
                raise ApiError(f"{option.label} must be true or false", code="invalid_option")
            if option.type == "enum" and value not in option.options:
                raise ApiError(f"{option.label} must be one of {', '.join(option.options)}", code="invalid_option")
            if option.type == "int" and (isinstance(value, bool) or not isinstance(value, int)):
                raise ApiError(f"{option.label} must be a whole number", code="invalid_option")
            if option.type == "string" and not isinstance(value, str):
                raise ApiError(f"{option.label} must be text", code="invalid_option")
            out[key] = value
        return out

    def env_override_error(self, key: str) -> str | None:
        """Why an owner may not set this extra environment variable (None: allowed)."""
        if str(key).upper() in self.allowed_user_env:
            return None
        return f"{key} is not allowed for {self.label} servers (allowed: {', '.join(sorted(self.allowed_user_env)) or 'none'})"

    def secret_spec(self, key: str) -> SecretSpec | None:
        return next((s for s in self.secret_specs if s.key == key), None)

    # -- config files ----------------------------------------------------------------------------
    def config_file(self, file_id: str) -> ConfigFile | None:
        return next((f for f in self.config_files() if f.file_id == file_id), None)

    def config_files_for(self, instance: Instance) -> tuple[ConfigFile, ...]:
        """The config files of this server (a game whose variants have different files narrows them)."""
        return self.config_files()

    def config_file_for(self, instance: Instance, file_id: str) -> ConfigFile | None:
        return next((f for f in self.config_files_for(instance) if f.file_id == file_id), None)

    def secret_specs_for(self, instance: Instance) -> tuple[SecretSpec, ...]:
        """The secrets of this server (per variant)."""
        return self.secret_specs

    # -- setup steps -----------------------------------------------------------------------------
    def setup_status(self, instance: Instance) -> dict[str, Any] | None:
        """``Instance.setup`` in the API: null without setup steps."""
        if not self.setup_steps:
            return None
        state = (instance.setup_state or {}).get("steps") or {}
        steps = []
        next_step = None
        for step in self.setup_steps:
            entry = state.get(step.id) or {}
            status = entry.get("status") or "pending"
            if status != "done" and next_step is None:
                next_step = step.id
            steps.append({**step.as_dict(), "status": status, "message": entry.get("message", "")})
        return {"needed": next_step is not None, "next": next_step, "steps": steps}

    def run_setup_step(self, instance: Instance, step: SetupStep, ctx: Any) -> str:
        """Run one automatic setup step (the ``setup`` job); returns what to record as its message.
        Raise :class:`SetupWaiting` when the step cannot run yet (the server is stopped): the step stays
        pending with that message and the job ends without failing."""
        raise NotImplementedError(f"{self.id} has no setup step {step.id}")

    def secrets_changed(self, instance: Instance, keys: list[str]) -> None:
        """The owner changed these secrets (Settings); a game whose setup step applies one marks that
        step pending again (Satisfactory: the client password)."""
        return None

    # -- the control secret file -------------------------------------------------------------------
    def control_secret_value(self, instance: Instance) -> str | None:
        """What the panel writes into ``control_secret_file`` before every apply: the control secret
        (Factorio's RCON password); None writes nothing (Satisfactory before its claim)."""
        from apps.instances.spec import rcon_password

        return rcon_password(instance)

    # -- config "files" the panel keeps itself or reads from the game's API ----------------------------
    def is_virtual_config(self, cfg: ConfigFile) -> bool:
        """Whether ``cfg`` is not a file of the server folder but values the definition reads and
        writes itself (``virtual_config_values`` / ``write_virtual_config``)."""
        return False

    def virtual_config_values(self, client: AgentClient, instance: Instance, cfg: ConfigFile) -> dict[str, Any] | None:
        """The values of a virtual config; None leaves it out of the Game settings page for now."""
        return None

    def write_virtual_config(
        self, client: AgentClient, instance: Instance, cfg: ConfigFile, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply validated changes of a virtual config; returns its values afterwards."""
        raise NotImplementedError

    # -- creating a server -------------------------------------------------------------------------
    def create_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        """Instance fields of a new server of this game from the validated create request (B5)."""
        raise CapabilityUnavailable(f"{self.label} servers cannot be created by this panel", details={"game": self.id})

    def validate_initial_settings(self, settings: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        """``settings`` of a create request: {file id: {key: value}}, validated against the schema."""
        from apps.core.errors import ApiError

        from . import codecs

        out: dict[str, dict[str, Any]] = {}
        for file_id, values in (settings or {}).items():
            cfg = self.config_file(str(file_id))
            if cfg is None or not isinstance(values, dict):
                raise ApiError(f"Unknown settings file '{file_id}'", code="invalid_setting")
            try:
                out[cfg.file_id] = codecs.validate_values(cfg, values)
            except ValueError as exc:
                raise ApiError(str(exc), code="invalid_setting") from exc
        return out

    # -- players ---------------------------------------------------------------------------------
    def validate_player_name(self, name: str) -> str:
        import re

        from apps.core.errors import ApiError

        name = (name or "").strip()
        if not re.fullmatch(self.player_name_pattern, name):
            raise ApiError(f"That is not a valid {self.label} player name", code="invalid_player_name")
        return name

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        raise NotImplementedError

    def player_action(self, instance: Instance, action: str, name: str, reason: str = "") -> dict[str, Any]:
        self.require(instance, "player_actions")
        raise NotImplementedError

    # -- container -------------------------------------------------------------------------
    def build_spec(self, instance: Instance) -> dict[str, Any]:
        """The agent's container spec; hashed for "restart required".

        The generic spec of a game with named ports: the image of ``image()``, the environment of
        ``build_env()``, the container memory of the memory model, the named ports, the control
        secret and ``game`` (the agent's runtime profile; Minecraft overrides all of this and never
        sends ``game``)."""
        from apps.instances.spec import rcon_password

        spec: dict[str, Any] = {
            "name": instance.name,
            "image": self.image(instance),
            "env": self.build_env(instance),
            "memory_limit_mb": instance.container_memory_mb,
            "ports": self.spec_ports(instance),
            "restart_policy": instance.restart_policy,
            "stop_timeout": instance.stop_timeout,
            "rcon_password": rcon_password(instance),
            "game": self.profile or self.id,
        }
        variant = self.agent_variant(instance)
        if variant:
            spec["variant"] = variant
        if instance.cpu_limit:
            spec["cpu_limit"] = instance.cpu_limit
        if instance.network:
            spec["network"] = instance.network
        return spec

    def agent_variant(self, instance: Instance) -> str | None:
        """The agent profile's variant for this server (None: the profile without variants)."""
        return None

    def game_env(self, instance: Instance) -> dict[str, str]:
        """The environment the panel sets for the image (the agent's profile allows exactly these)."""
        return {}

    def build_env(self, instance: Instance) -> dict[str, str]:
        """Owner variables the game allows (``allowed_user_env``), the panel's timezone, then the
        game's own variables (they always win)."""
        from django.conf import settings

        from apps.core.models import SystemSetting

        env = {str(k): str(v) for k, v in (instance.env or {}).items() if self.env_override_error(str(k)) is None}
        tz = SystemSetting.get("timezone") or settings.TIME_ZONE
        if tz and "TZ" not in env and "TZ" in self.allowed_user_env:
            env["TZ"] = tz
        env.update(self.game_env(instance))
        return env

    def spec_ports(self, instance: Instance) -> list[dict[str, Any]]:
        """The named ports of the server as the agent's spec lists them (definition order)."""
        from apps.core.errors import ConflictError

        configured = {str(p.get("name")): p for p in instance.ports or [] if isinstance(p, dict)}
        out = []
        for port in self.ports:
            entry = configured.get(port.name)
            if entry is None:
                raise ConflictError(
                    f"The server has no host port for its {port.label} port; set it in Network",
                    code="port_missing",
                    details={"port": port.name},
                )
            out.append(
                {
                    "container_port": port.container_port,
                    "host_port": int(entry["host_port"]),
                    "protocol": port.protocol,
                    "host_ip": entry.get("host_ip") or instance.host_ip,
                }
            )
        return out

    def container_memory_mb(self, instance: Instance) -> int:
        """The container's memory limit (``plain``: what was set, plus an explicit overhead)."""
        return int(instance.memory_mb) + int(instance.memory_overhead_mb or 0)

    def initial_files(self, instance: Instance, params: dict[str, Any]) -> dict[str, str]:
        """Files written into a new server's folder before its first start (the install job)."""
        return {}

    def before_apply(self, instance: Instance, client: AgentClient) -> None:
        """Write what the panel owns in the server's files before every apply (after the control
        secret file): Terraria's managed serverconfig.txt keys and TShock's REST token."""
        return None

    def masked_text(self, instance: Instance, path: str, content: str) -> str:
        """A server file as the text editor shows it (secrets the game keeps inside a file hidden)."""
        return content

    def version_key(self, version: str) -> tuple[int, ...]:
        """Sort key of an exact version (newer is larger); ValueError when it has no numbers."""
        import re

        parts = tuple(int(n) for n in re.findall(r"\d+", str(version)))
        if not parts:
            raise ValueError(f"not a version: {version!r}")
        return parts

    # -- console -------------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        return text

    def is_stop_command(self, text: str) -> bool:
        """A console line that would make the server exit: the panel runs its Stop instead
        (Docker's restart policy would start the server again at once)."""
        return self.normalise_command(text.strip()).strip().lower() in self.stop_words

    @property
    def stop_word(self) -> str:
        """The stop word messages name (Minecraft: ``stop``)."""
        return sorted(self.stop_words)[0] if self.stop_words else "stop"

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        """Show ``text`` (and the operator's ``message``) to the players in game; raises ApiError."""
        self.require(instance, "broadcast")
        raise NotImplementedError

    def online_players(self, instance: Instance) -> dict[str, Any]:
        """``{"online", "max", "players"}`` of a running server; raises ApiError when unreachable."""
        self.require(instance, "players")
        raise NotImplementedError

    def analyze_logs(self, instance: Instance, lines: list[str]) -> list[Any]:
        """Known problems found in console lines (``diagnostics.Issue`` objects)."""
        return []

    # -- software record of backups ------------------------------------------------------------
    def software_state(self, instance: Instance) -> dict[str, Any]:
        return {"game": self.id}

    def describe_software(self, state: dict[str, Any]) -> str:
        return self.label or self.id

    def set_software(self, instance: Instance, state: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        """Put the software of a backup record (``software_state``) back; returns {field: (old, new)}."""
        return {}
