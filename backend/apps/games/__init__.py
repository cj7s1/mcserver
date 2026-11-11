"""Game definitions and their registry (docs/design/GAME-DEFINITIONS.md).

``get(id)``, ``all()`` and ``for_instance(instance)`` return the definition objects
(``base.GameDefinition``). Built-in games: Minecraft (``minecraft.MinecraftDefinition``), Factorio
(``factorio.FactorioDefinition``), Terraria (``terraria.TerrariaDefinition``) and Satisfactory
(``satisfactory.SatisfactoryDefinition``). They are registered on first use, so importing this package
loads no models. To add a game: a module with a ``GameDefinition`` subclass, registered in
``_load`` below, and the matching runtime profile in the agent (agent/mcagent/profiles/).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apps.core.errors import ConflictError

from .base import (
    ALL_CAPABILITIES,
    CAPABILITIES,
    DEFAULT_GAME,
    EXTRA_CAPABILITIES,
    MEMORY_MODELS,
    CapabilityUnavailable,
    ConfigFile,
    GameDefinition,
    GameOption,
    PortSpec,
    SecretSpec,
    SetupStep,
    SetupWaiting,
    Variant,
)

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

__all__ = [
    "ALL_CAPABILITIES",
    "CAPABILITIES",
    "DEFAULT_GAME",
    "EXTRA_CAPABILITIES",
    "GAMES_FEATURE",
    "MEMORY_MODELS",
    "CapabilityUnavailable",
    "ConfigFile",
    "GameDefinition",
    "GameOption",
    "PortSpec",
    "SecretSpec",
    "SetupStep",
    "SetupWaiting",
    "UnknownGame",
    "Variant",
    "all",
    "capabilities",
    "for_instance",
    "get",
    "get_or_none",
    "ids",
    "register",
    "require_agent_support",
    "unregister",
]

# The agent's name, in GET /v1/info ("features"), for runtime profiles per game (its "games" map).
GAMES_FEATURE = "games"

_registry: dict[str, GameDefinition] = {}
_loaded = False


class UnknownGame(ConflictError):
    """A server of a game this panel has no definition for (a newer release created it)."""

    default_code = "unknown_game"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from .factorio import FactorioDefinition
    from .minecraft import MinecraftDefinition
    from .satisfactory import SatisfactoryDefinition
    from .terraria import TerrariaDefinition

    _registry.setdefault(DEFAULT_GAME, MinecraftDefinition())
    _registry.setdefault(FactorioDefinition.id, FactorioDefinition())
    _registry.setdefault(SatisfactoryDefinition.id, SatisfactoryDefinition())
    _registry.setdefault(TerrariaDefinition.id, TerrariaDefinition())


def register(definition: GameDefinition) -> GameDefinition:
    """Add a definition (tests register extra games; replacing a built-in one is refused)."""
    _load()
    if not definition.id or definition.id in _registry:
        raise ValueError(f"game {definition.id!r} is already registered")
    _registry[definition.id] = definition
    return definition


BUILT_IN = (DEFAULT_GAME, "factorio", "terraria", "satisfactory")


def unregister(game_id: str) -> None:
    if game_id in BUILT_IN:
        raise ValueError(f"the built-in {game_id} definition cannot be removed")
    _registry.pop(game_id, None)


def get_or_none(game_id: str | None) -> GameDefinition | None:
    _load()
    return _registry.get(game_id or DEFAULT_GAME)


def get(game_id: str | None) -> GameDefinition:
    definition = get_or_none(game_id)
    if definition is None:
        raise UnknownGame(f"This panel does not know the game '{game_id}'; update the panel", details={"game": game_id})
    return definition


def all() -> list[GameDefinition]:
    """Every known game, Minecraft first."""
    _load()
    return list(_registry.values())


def ids() -> list[str]:
    return [d.id for d in all()]


def for_instance(instance: Instance) -> GameDefinition:
    """The definition of the server's game (409 ``unknown_game`` for a game this panel lacks)."""
    return get(getattr(instance, "game", None) or DEFAULT_GAME)


def capabilities(instance: Instance) -> frozenset[str]:
    """The server's capabilities; none for a game this panel does not know."""
    definition = get_or_none(getattr(instance, "game", None) or DEFAULT_GAME)
    return definition.capabilities(instance) if definition is not None else frozenset()


def require_agent_support(game_id: str | None, client: AgentClient, variant: str | None = None) -> None:
    """Refuse to create or start a server of a non-Minecraft game on an agent without its runtime
    profile (409 ``agent_lacks_game``).

    An agent from before game profiles ignores the ``game`` of a spec and would start any image
    as a Minecraft server (itzg environment, /data mount, the Minecraft health check), so such a
    server needs an agent that reports the ``games`` feature with this game in its ``games`` map.
    Minecraft runs on every agent.
    """
    game = game_id or DEFAULT_GAME
    if game == DEFAULT_GAME:
        return
    if client.supports(GAMES_FEATURE) and game in client.games():
        # a variant needs its own profile (Terraria: vanilla, tModLoader, TShock)
        if not variant or variant in ((client.games().get(game) or {}).get("variants") or []):
            return
    definition = get_or_none(game)
    label = definition.label if definition is not None and definition.label else game
    raise ConflictError(
        f"The agent of node '{client.node.name}' cannot run {label} servers; update the agent on that node first",
        code="agent_lacks_game",
        details={"game": game, "node": client.node.name},
    )


def catalog() -> list[dict[str, Any]]:
    """``GET /api/catalog/games``."""
    return [d.catalog_entry() for d in all()]
