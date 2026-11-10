"""Runtime profiles: everything about a game's container that the panel cannot choose.

A profile is data shipped with the agent and selected by the spec's ``game`` (and ``variant``).
The panel only names the game; images, the user model, capabilities, mounts, injected and
reserved environment, the environment policy, publishable ports, the health check, the stop
signal, TTY/stdin and the control driver all come from here (docs/design/GAME-DEFINITIONS.md,
sections 5 and 9). So a compromised panel still cannot pick caps, mounts or users.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec


@dataclass(frozen=True)
class UserModel:
    """How the server ends up running as the agent's runtime uid/gid.

    ``drop_privileges_env``: the image starts as root and drops to the uid/gid given in the
    environment variables ``uid_env`` / ``gid_env`` (itzg: ``UID`` / ``GID``); the agent injects
    them. ``fixed``: the agent sets the container's ``user`` to ``uid:gid`` (None = the agent's
    runtime uid/gid), for images that run unprivileged.
    """

    kind: Literal["drop_privileges_env", "fixed"]
    uid_env: str | None = None
    gid_env: str | None = None
    uid: int | None = None
    gid: int | None = None


@dataclass(frozen=True)
class Mount:
    """A bind mount of the server folder (``subdir`` "." = the folder itself) at ``container_path``."""

    subdir: str
    container_path: str


@dataclass(frozen=True)
class Health:
    """The readiness probe. Only ``cmd`` (a fixed argv run by Docker's health check) and
    ``image`` (the image's own HEALTHCHECK) are implemented; the agent-side probes the design
    names (``tcp``, ``udp-query``, ``log``, ``driver``) come with the first game that needs them,
    and a profile that names one is refused when it is registered."""

    kind: Literal["cmd", "image", "tcp", "udp-query", "log", "driver"]
    argv: tuple[str, ...] = ()
    port: int | None = None
    pattern: str | None = None


IMPLEMENTED_HEALTH = frozenset({"cmd", "image"})

EnvPolicy = Callable[[str], str | None]
InjectedEnv = Callable[["RuntimeProfile", "Settings", "InstanceSpec"], dict[str, str]]


def no_env_policy(_key: str) -> str | None:
    return None


@dataclass(frozen=True)
class RuntimeProfile:
    game: str
    label: str
    # The variants this profile serves; () = only a spec without a variant.
    variants: tuple[str, ...]
    # Allowed image repositories (any tag matching schemas.IMAGE_TAG_RE, no digests). For
    # Minecraft AGENT_ALLOWED_IMAGES replaces them; for other games AGENT_GAME_<ID>_IMAGES.
    images: tuple[str, ...]
    user: UserModel
    cap_add: tuple[str, ...]
    mounts: tuple[Mount, ...]
    # The environment the agent sets after the spec's own (it always wins), in order.
    injected_env: InjectedEnv
    # Keys a spec may never set (the agent injects them or they would undo what it injects).
    reserved_env: frozenset[str]
    # Why a variable of the spec is refused (None: allowed). Re-validates the panel's policy.
    env_policy: EnvPolicy
    # (container port, protocol) a spec may publish; with allow_extra_ports any other port as well.
    container_ports: tuple[tuple[int, str], ...]
    allow_extra_ports: bool
    # (container port, protocol or None for both) that are never published, whatever the spec says.
    never_publish: tuple[tuple[int, str | None], ...]
    health: Health
    stop_signal: str
    tty: bool
    stdin_open: bool
    hostname_prefix: str
    fetch_hosts: tuple[str, ...]
    # The control driver (mcagent.drivers) and its options.
    control: str
    control_options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    default_backup_excludes: tuple[str, ...] = ()
    # Mount AGENT_RUNTIME_JAVA_TRUSTSTORE into the container (a JVM reads it).
    java_truststore: bool = False

    def env_error(self, key: str) -> str | None:
        """Why the spec may not set ``key``; None when it may."""
        if key.upper() in self.reserved_env:
            return f"Environment variable {key} is managed by the agent"
        return self.env_policy(key)

    def port_error(self, container_port: int, protocol: str) -> str | None:
        """Why a spec may not publish this container port; None when it may."""
        for port, proto in self.never_publish:
            if port == container_port and proto in (None, protocol):
                return f"Container port {container_port}/{protocol} is never published"
        if not self.allow_extra_ports and (container_port, protocol) not in self.container_ports:
            return f"Container port {container_port}/{protocol} is not a port of {self.label}"
        return None
