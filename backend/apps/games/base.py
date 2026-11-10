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
    """A configuration file of the game the panel can edit with a form."""

    path: str
    codec: str  # java-properties | json | ini | kv
    label: str = ""
    managed_keys: frozenset[str] = field(default_factory=frozenset)
    secret_keys: tuple[str, ...] = ()


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

    # -- data -------------------------------------------------------------------------------
    def default_memory_mb(self) -> int:
        return 2048

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
        """The game in ``GET /api/catalog/games``."""
        return {
            "id": self.id,
            "label": self.label,
            "variants": [v.as_dict() for v in self.variants],
            "capabilities": sorted(self.capabilities()),
            "memory_model": self.memory_model,
            "default_memory_mb": self.default_memory_mb(),
            "notes": list(self.notes),
        }

    # -- container -------------------------------------------------------------------------
    def build_spec(self, instance: Instance) -> dict[str, Any]:
        """The agent's container spec; hashed for "restart required"."""
        raise NotImplementedError(f"{self.id} has no container spec")

    def container_memory_mb(self, instance: Instance) -> int:
        """The container's memory limit (``plain``: what was set, plus an explicit overhead)."""
        return int(instance.memory_mb) + int(instance.memory_overhead_mb or 0)

    def initial_files(self, instance: Instance, params: dict[str, Any]) -> dict[str, str]:
        """Files written into a new server's folder before its first start (the install job)."""
        return {}

    # -- console -------------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        return text

    def is_stop_command(self, text: str) -> bool:
        """A console line that would make the server exit: the panel runs its Stop instead
        (Docker's restart policy would start the server again at once)."""
        return self.normalise_command(text.strip()).strip().lower() in self.stop_words

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
