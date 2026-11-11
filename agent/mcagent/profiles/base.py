"""Runtime profiles: everything about a game's container that the panel cannot choose.

A profile is data shipped with the agent and selected by the spec's ``game`` (and ``variant``).
The panel only names the game; images, the user model, capabilities, mounts, injected and
reserved environment, the environment policy, publishable ports, the health check, the stop
signal, TTY/stdin and the control driver all come from here (docs/design/GAME-DEFINITIONS.md,
sections 5 and 9). So a compromised panel still cannot pick caps, mounts or users.
"""

from __future__ import annotations

import re
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
    """The readiness probe.

    Run by Docker: ``cmd`` (a fixed argv as Docker's health check, timed by the spec's
    ``healthcheck``) and ``image`` (the image's own HEALTHCHECK). Run by the agent
    (``docker_ops.agent_health``, reported as the container's ``health`` by ``describe``):
    ``tcp`` (a TCP connect to ``port`` of the container, re-checked at most every few seconds)
    and ``log`` (``pattern``, a regular expression, printed by the container since it started;
    once seen, the run stays healthy). An agent-side probe that has not succeeded reads
    "starting" for ``start_period_s`` after the start and "unhealthy" afterwards; the container
    gets no Docker health check (``NONE``, so an image HEALTHCHECK cannot interfere).
    ``udp-query`` and ``driver`` are named by the design but not implemented: a profile that
    names one is refused when it is registered.
    """

    kind: Literal["cmd", "image", "tcp", "udp-query", "log", "driver"]
    argv: tuple[str, ...] = ()
    port: int | None = None
    pattern: str | None = None
    start_period_s: int = 600


IMPLEMENTED_HEALTH = frozenset({"cmd", "image", "tcp", "log"})
# probes the agent runs itself (no Docker health check)
AGENT_SIDE_HEALTH = frozenset({"tcp", "log"})

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
    # Paths relative to the server folder that backups leave out (re-downloadable or scratch
    # data); the agent adds them to the panel's excludes (POST /backups).
    default_backup_excludes: tuple[str, ...] = ()
    # Mount AGENT_RUNTIME_JAVA_TRUSTSTORE into the container (a JVM reads it).
    java_truststore: bool = False
    # A regular expression the image tag must match in full (e.g. exact version tags only).
    tag_pattern: str | None = None
    # Variables the panel may set (they are allowed by env_policy), with the regular expression
    # their value must match in full; any other value is refused.
    env_values: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # Mount every VOLUME the image declares into the server folder (a declared path below a
    # profile mount: that subfolder of the mount; elsewhere: <server folder>/.volumes/<name>), or
    # a tmpfs for the paths in volume_tmpfs, so Docker never creates anonymous volumes outside the
    # server folder. Off for Minecraft: its only VOLUME (/data) is its mount.
    mount_image_volumes: bool = False
    volume_tmpfs: tuple[str, ...] = ()
    # Exit codes of an image-side download failure (SteamCMD) that the panel may retry while a
    # server is installing; informational (GET /v1/info).
    install_retry_exits: tuple[int, ...] = ()
    # Replace the control secret (and what log_redact_patterns match) in every console text the
    # agent hands out: for images that print it into their log (factoriotools' `set -x`).
    redact_secret_in_logs: bool = False
    # Regular expressions whose match is replaced by its first group + "********".
    log_redact_patterns: tuple[str, ...] = ()

    def env_error(self, key: str) -> str | None:
        """Why the spec may not set ``key``; None when it may."""
        if key.upper() in self.reserved_env:
            return f"Environment variable {key} is managed by the agent"
        return self.env_policy(key)

    def env_value_error(self, key: str, value: str) -> str | None:
        """Why the value of a variable the profile constrains is refused; None when it is fine."""
        pattern = self.env_values.get(key.upper())
        if pattern is not None and not re.fullmatch(pattern, value):
            return f"Environment variable {key} has a value this agent does not accept"
        return None

    def tag_error(self, tag: str) -> str | None:
        if self.tag_pattern is not None and not re.fullmatch(self.tag_pattern, tag):
            return f"Image tag '{tag}' is not a tag {self.label} servers can run"
        return None

    def port_error(self, container_port: int, protocol: str) -> str | None:
        """Why a spec may not publish this container port; None when it may."""
        for port, proto in self.never_publish:
            if port == container_port and proto in (None, protocol):
                return f"Container port {container_port}/{protocol} is never published"
        if not self.allow_extra_ports and (container_port, protocol) not in self.container_ports:
            return f"Container port {container_port}/{protocol} is not a port of {self.label}"
        return None
