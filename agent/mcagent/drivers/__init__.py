"""Control drivers, and which one a container gets.

A driver is chosen from the container's labels (``io.mcpanel.game`` / ``io.mcpanel.variant``,
none on a Minecraft container) through its runtime profile's ``control``. A container of a game
this agent has no profile for (created by a newer agent before a rollback) gets the ``none``
driver: a plain ``docker stop``, no commands, no saving pause.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .base import ControlDriver, NoControlDriver, Report, Send
from .minecraft import MinecraftDriver
from .rcon import RconCommands, RconDriver, RconStep

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..profiles import RuntimeProfile

__all__ = [
    "ControlDriver",
    "MinecraftDriver",
    "NoControlDriver",
    "RconCommands",
    "RconDriver",
    "RconStep",
    "Report",
    "Send",
    "for_container",
    "for_labels",
    "for_profile",
    "for_status",
    "get",
]

# control id -> factory taking the profile's control_options
FACTORIES: dict[str, Callable[[dict[str, Any]], ControlDriver]] = {
    NoControlDriver.id: lambda _options: NoControlDriver(),
    MinecraftDriver.id: lambda _options: MinecraftDriver(),
    RconDriver.id: RconDriver.from_options,
}
_NONE = NoControlDriver()


def get(control: str, options: dict[str, Any] | None = None) -> ControlDriver:
    """A driver by its id (``none``, ``minecraft-rcon``, ``rcon`` with options)."""
    try:
        factory = FACTORIES[control]
    except KeyError:
        raise ValueError(f"Unknown control driver {control!r}") from None
    return factory(dict(options or {}))


def for_profile(profile: RuntimeProfile) -> ControlDriver:
    return get(profile.control, dict(profile.control_options))


def for_labels(labels: dict[str, str] | None) -> ControlDriver:
    from .. import profiles

    profile = profiles.for_labels(labels)
    return _NONE if profile is None else for_profile(profile)


def for_container(c: Container) -> ControlDriver:
    return for_labels(c.labels)


def for_status(status: dict[str, Any]) -> ControlDriver:
    """The driver of the container ``docker_ops.status`` / ``describe`` described: its ``game`` and
    ``variant`` (reported only for a container that is not Minecraft)."""
    from .. import profiles

    labels = {}
    if status.get("game"):
        labels[profiles.LABEL_GAME] = str(status["game"])
    if status.get("variant"):
        labels[profiles.LABEL_VARIANT] = str(status["variant"])
    return for_labels(labels)
