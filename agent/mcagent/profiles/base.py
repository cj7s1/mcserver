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
    runtime uid/gid), for images that run unprivileged. A literal ``uid``/``gid`` (CS2's image
    works only as 1000:1000) also makes it the owner of the server folder: the agent gives the
    folder that owner before every create and start and writes its files as that owner
    (``RuntimeProfile.literal_owner``, ``docker_ops.server_owner``).
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
    once seen, the run stays healthy), ``udp-query`` (``payload`` sent as one datagram to
    ``port``/udp of the container; healthy when a reply arrives whose bytes, read as latin-1,
    match ``pattern``) and ``driver`` (the control driver's ``ready()``, e.g. an HTTP API's health
    function); ``tcp``, ``udp-query`` and ``driver`` are re-checked at most every few seconds.
    An agent-side probe that has not succeeded reads "starting" for ``start_period_s`` after the
    start and "unhealthy" afterwards; the container gets no Docker health check (``NONE``, so an
    image HEALTHCHECK cannot interfere).

    ``a2s`` (Valve's Source query, ``mcagent.a2s``): A2S_INFO to ``port``/udp of the container, with
    the challenge handshake; healthy on a valid ``I`` reply. ``port_env``: a variable of the
    container that names the port instead (games whose ports are set 1:1 through the environment;
    ``port`` is the fallback when the variable is missing). Re-checked at most every few seconds.
    """

    kind: Literal["cmd", "image", "tcp", "udp-query", "log", "driver", "a2s"]
    argv: tuple[str, ...] = ()
    port: int | None = None
    pattern: str | None = None
    start_period_s: int = 600
    # udp-query: the datagram sent to the port
    payload: bytes = b""
    # a2s: the container variable that holds the query port (the value of `port` without it)
    port_env: str | None = None


IMPLEMENTED_HEALTH = frozenset({"cmd", "image", "tcp", "log", "udp-query", "driver", "a2s"})
# probes the agent runs itself (no Docker health check)
AGENT_SIDE_HEALTH = frozenset({"tcp", "log", "udp-query", "driver", "a2s"})

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
    # The container's process, fixed by the profile (None: the image's own). Never taken from a
    # spec: an image whose default entrypoint needs privileges can be run unprivileged by naming
    # its server binary here (TShock). ``working_dir`` replaces the image's WORKDIR.
    entrypoint: tuple[str, ...] | None = None
    command: tuple[str, ...] | None = None
    working_dir: str | None = None
    # Exit codes of a correct, saving stop of the game (Terraria's image exits 143 after SIGTERM);
    # informational for the panel (GET /v1/info, only when it differs from (0,)).
    clean_exit_codes: tuple[int, ...] = (0,)
    # Variables that name a container port the image listens on, with its protocol (Satisfactory's
    # SERVERMESSAGINGPORT: the server advertises that port to players, so container and host port
    # must be the same number). A spec may publish the container port such a variable names (its
    # value must also match env_values).
    env_ports: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # Only the agent's instance network (AGENT_INSTANCE_NETWORK): no host network, no other network
    # a spec may name (Satisfactory: the driver reaches the API there, and an unclaimed server must
    # not be reachable from anywhere else; since the review of 2026-11-11 (A-6) every game but
    # Minecraft whose control port is a network listener: never_publish holds only there).
    instance_network_only: bool = False
    # The image's own start-up `chown -R` cannot give the files the runtime gid (Satisfactory: when
    # the gid exists in the image, steam keeps its primary group 1000, so files written during a run
    # are uid:1000): the agent gives the server folder the runtime uid:gid after every stop and
    # before every backup (docker_ops.fix_ownership).
    fix_ownership: bool = False
    # Docker's restart policy for every container of the profile, whatever the spec says (None: the
    # spec's). Satisfactory: "no", so only the agent's start path (and its start wrapper) starts it.
    restart_policy: str | None = None
    # Run before every create and every start the agent makes (Satisfactory: writes the start
    # wrapper, the container's entrypoint, into the server folder).
    prepare_start: Callable[[Settings, Any], None] | None = None
    # Files the agent writes for the container and mounts read-only into it, from the agent's state
    # folder (out of reach of the game and the file API): ``(profile, settings, spec) -> {container path:
    # content}``. Project Zomboid: the image's rcon.yml, which its own scripts cannot write as uid 1000
    # (its SIGTERM trap saves and quits through it).
    agent_files: Callable[[RuntimeProfile, Settings, InstanceSpec], Mapping[str, str]] | None = None
    # Docker's init process (tini) as PID 1, for images whose shell entrypoint ignores SIGTERM
    # (CS2: bash as PID 1 does not forward it, so every stop would wait for the timeout).
    init: bool = False
    # Image references by digest (``<repository>@sha256:<hex>``) a spec may use, for images
    # without usable version tags (Rust: only ``latest``): the repository must still be allowed
    # and the digest must be one of these. Without any, digests are refused (tags only).
    image_digests: tuple[str, ...] = ()
    # The digests a version tag is known to point to (``{tag: (sha256:<hex>, ...)}``; A-8): a container
    # is created from that tag only when the local image is one of them (its RepoDigests or, with
    # Docker's containerd image store, its id), checked after any pull. Tags not listed are not checked.
    tag_digests: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    # Container variables whose values (at least 4 characters) are replaced by ``********`` in every
    # log text the agent hands out (GET logs, the stream, console history, last-run records,
    # console replies): passwords and tokens the image prints (PZ's launcher logs -adminpassword).
    redact_env: tuple[str, ...] = ()
    # Paths relative to the server folder that the file API never writes, uploads, renames onto
    # or deletes (CS2: pre.sh/post.sh, sourced as code at every start; server.cfg, generated).
    protected_paths: tuple[str, ...] = ()
    # Further ports an ``env_ports`` variable names, as (offset, protocol) from its value
    # (Valheim: the query port is the game port + 1, crossplay + 2).
    env_port_offsets: Mapping[str, tuple[tuple[int, str], ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # The Docker stop timeout (seconds) of a stop while the server has not become ready in its current run
    # (an agent-side readiness probe that never succeeded: the image is still downloading or installing the
    # game, so there is no world to save): SIGTERM, then SIGKILL after this many seconds, without the
    # driver's save. None (every game before the six): the usual stop. The images of PZ (DepotDownloader)
    # and Rust (SteamCMD) defer their SIGTERM trap until the download ends (end-to-end run: 61 s, 4.8 GB).
    not_ready_stop_timeout: int | None = None
    # A spec that sets a variable the agent injects is refused (400 ``env_not_allowed``) instead of
    # having its value silently replaced: the six newer games, whose fixed variables switch off
    # what downloads or runs code (CS2_ADDITIONAL_ARGS, RUST_OXIDE_ENABLED, ...). Defaults the agent
    # injects only when the spec lacks them (1:1 ports, the control secret) are never both.
    strict_env: bool = False
    # Variables a spec must set (non-empty): Valheim's join password, without which the image falls back to
    # its own default password "secret" (I-3 / E-4). 400 env_not_allowed when missing.
    required_env: tuple[str, ...] = ()

    @property
    def literal_owner(self) -> tuple[int, int] | None:
        """(uid, gid) the profile fixes for its container whatever the agent's runtime ids are (CS2's
        image works only as 1000:1000); None when the server runs as the runtime uid/gid."""
        if self.user.kind == "fixed" and self.user.uid is not None and self.user.gid is not None:
            return int(self.user.uid), int(self.user.gid)
        return None

    def env_named_ports(self, env: Mapping[str, str] | None) -> list[tuple[int, str, str]]:
        """(port, protocol, name) of every container port the ``env_ports`` variables of ``env``
        name, with the ports ``env_port_offsets`` derive from them."""
        out: list[tuple[int, str, str]] = []
        for key, value in (env or {}).items():
            proto = self.env_ports.get(key)
            if proto is None or not str(value).isdigit():
                continue
            port = int(value)
            out.append((port, proto, key))
            for offset, derived in self.env_port_offsets.get(key, ()):
                out.append((port + offset, derived, f"{key}+{offset}"))
        return out

    def env_port_error(self, env: Mapping[str, str] | None) -> str | None:
        """Why the ports the ``env_ports`` variables of ``env`` name are refused: such a port must
        not be one of the profile's own container ports (Satisfactory: a messaging port 7777/tcp
        would be the HTTPS API's) or one it never publishes (an internal port such as an RCON or
        REST port), and not be named twice. None when they are fine."""
        seen: dict[tuple[int, str], str] = {}
        for port, proto, key in self.env_named_ports(env):
            if "+" in key and not 1 <= port <= 65535:  # a derived port (the value itself has a pattern)
                return f"{key} names the port {port}, which does not exist"
            if (port, proto) in self.container_ports:
                return f"{key}={port} collides with the server's own port {port}/{proto}"
            for internal, internal_proto in self.never_publish:
                if internal == port and internal_proto in (None, proto):
                    return f"{key}={port} collides with the server's internal port {port}/{proto}"
            if (port, proto) in seen:
                return f"{key} and {seen[(port, proto)]} name the same port {port}/{proto}"
            seen[(port, proto)] = key
        return None

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

    def port_error(
        self, container_port: int, protocol: str, env: Mapping[str, str] | None = None
    ) -> str | None:
        """Why a spec may not publish this container port; None when it may. ``env``: the spec's
        environment, for the ports ``env_ports`` variables name."""
        for port, proto in self.never_publish:
            if port == container_port and proto in (None, protocol):
                return f"Container port {container_port}/{protocol} is never published"
        named = {(port, proto) for port, proto, _key in self.env_named_ports(env)}
        if (
            not self.allow_extra_ports
            and (container_port, protocol) not in self.container_ports
            and (container_port, protocol) not in named
        ):
            return f"Container port {container_port}/{protocol} is not a port of {self.label}"
        return None
