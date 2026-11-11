"""Docker container lifecycle for game server instances.

Only containers carrying the ``io.mcpanel.managed`` label are ever touched.
All containers are created from an :class:`InstanceSpec`, which deliberately
cannot express privileged mode, extra capabilities, devices or extra mounts: what the
container gets beyond the spec comes from the runtime profile of its game
(:mod:`mcagent.profiles`), and how the agent talks to the server from its control driver
(:mod:`mcagent.drivers`). Games: Minecraft and Factorio (profiles/).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
from docker.errors import APIError, ImageNotFound, NotFound

# _create_container_args is docker-py's own (private) conversion from containers.create()
# arguments to the low-level API call; see create_container() for why it is used directly.
from docker.models.containers import Container, _create_container_args

from . import drivers, lastrun, profiles
from .config import Settings, get_settings
from .drivers.minecraft import GAME_PORT, RCON_PORT, SAVE_BEFORE_STOP, STOP_COMMAND
from .errors import AgentError
from .profiles import LABEL_GAME, LABEL_VARIANT, RuntimeProfile
from .rcon import RconError, RconUnavailable, run_command_sync
from .schemas import InstanceSpec

__all__ = ["GAME_PORT", "LABEL_GAME", "LABEL_VARIANT", "RCON_PORT", "AgentError"]

log = logging.getLogger(__name__)

LABEL_MANAGED = "io.mcpanel.managed"
LABEL_INSTANCE = "io.mcpanel.instance"
LABEL_NAME = "io.mcpanel.name"
LABEL_SPEC_HASH = "io.mcpanel.spec-hash"
LABEL_STOP_TIMEOUT = "io.mcpanel.stop-timeout"
# GAME_PORT / RCON_PORT (Minecraft's) live in drivers/minecraft.py; LABEL_GAME / LABEL_VARIANT
# (only on containers that are not Minecraft) in profiles/__init__.py.

# How this agent turns a spec into a container, for what the spec itself does not show. The panel
# keeps the same list (backend/apps/instances/spec.py, CONTAINER_FORMAT) and mixes the number the
# agent reports (GET /v1/info and the result of an apply) into the hash behind "restart
# required", so a container created by an agent that is older than the panel keeps reading
# restart-required until an up-to-date agent has recreated it. Raise it together with the panel's.
#   1  agents before the shutdown-safety fix (they report no number)
#   2  the container gets Docker's stop timeout (Config.StopTimeout), see build_create_kwargs
CONTAINER_FORMAT = 2

# Graceful stop (see graceful_stop). Seconds.
RCON_PROBE_TIMEOUT = 3.0  # connect + login: "does RCON answer quickly?"
RCON_STOP_INTERVAL = 2.0  # between tries to deliver `stop` while `docker stop` is in progress
RCON_STOP_COMMAND_TIMEOUT = 5.0  # waiting for the server's answer to `stop`


_client_lock = threading.Lock()
_client: docker.DockerClient | None = None


def client() -> docker.DockerClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = docker.from_env(timeout=120)
        return _client


def container_name(instance_id: UUID) -> str:
    return f"mcpanel-{instance_id.hex[:12]}"


REPLACEMENT_SUFFIX = "-replacement"


def replacement_name(instance_id: UUID) -> str:
    """The name a new container gets while the one it replaces still exists (see apply)."""
    return container_name(instance_id) + REPLACEMENT_SUFFIX


def remove_stale_replacement(instance_id: UUID) -> bool:
    """Remove a container left under :func:`replacement_name` by an apply that was interrupted.

    Only a container that carries this server's labels and has never run is removed.
    """
    try:
        c = client().containers.get(replacement_name(instance_id))
    except NotFound:
        return False
    labels = c.labels or {}
    if labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_INSTANCE) != str(instance_id):
        raise AgentError(
            "A container with the agent's replacement name exists but is not managed by MCPanel",
            "foreign_container",
            409,
        )
    if c.status not in ("created", "exited", "dead"):
        raise AgentError("A replacement container of this server is running", "instance_busy", 409)
    try:
        c.remove(v=False, force=False)
    except NotFound:
        pass
    log.warning("Removed the replacement container %s an interrupted apply left behind", c.name)
    return True


def rename_container(c: Container, name: str) -> None:
    try:
        c.rename(name)
    except APIError as exc:
        raise AgentError(
            f"Docker could not rename the new container: {exc.explanation or exc}", "rename_failed", 502
        ) from exc


def instance_dir(settings: Settings, instance_id: UUID) -> Path:
    return settings.instances_dir / str(instance_id)


def host_instance_dir(settings: Settings, instance_id: UUID) -> Path:
    return settings.host_instances_dir / str(instance_id)


# --- images ----------------------------------------------------------------------
def split_image(image: str) -> tuple[str, str]:
    if "@" in image:
        raise AgentError("Image digests are not supported; use a tag", "invalid_image")
    repo, _, tag = image.rpartition(":")
    if not repo or "/" in tag:
        repo, tag = image, "latest"
    return repo, tag


def validate_image(settings: Settings, image: str, profile: RuntimeProfile | None = None) -> str:
    """``repository:tag`` of an image the profile (default: Minecraft's) allows."""
    repo, tag = split_image(image)
    profile = profile or profiles.get(profiles.DEFAULT_GAME)
    assert profile is not None
    if repo not in profiles.allowed_images(settings, profile):
        raise AgentError(
            f"Image repository '{repo}' is not in the agent allow-list", "image_not_allowed", 403
        )
    from .schemas import IMAGE_TAG_RE

    if not IMAGE_TAG_RE.match(tag):
        raise AgentError("Invalid image tag", "invalid_image")
    if (reason := profile.tag_error(tag)) is not None:
        raise AgentError(reason, "invalid_image")
    return f"{repo}:{tag}"


def image_present(image: str) -> bool:
    try:
        client().images.get(image)
        return True
    except ImageNotFound:
        return False


def local_image_id(image: str) -> str | None:
    """The id (sha256:...) the tag points to on this host; None when it is not here."""
    try:
        return client().images.get(image).id
    except ImageNotFound:
        return None


def pull_image_bounded(image: str, timeout: float, progress: Any = None) -> None:
    """:func:`pull_image`, given up after ``timeout`` seconds (504 ``pull_timeout``).

    A pull that is given up keeps running in the Docker daemon (and in a daemon thread here,
    until Docker ends the stream); the tag then moves whenever it completes.
    """
    box: dict[str, BaseException] = {}
    given_up = threading.Event()

    def report(p: float, m: str) -> None:
        if progress is not None and not given_up.is_set():  # the operation has moved on
            progress(p, m)

    def run() -> None:
        try:
            pull_image(image, report)
        except BaseException as exc:
            box["error"] = exc

    t = threading.Thread(target=run, name="image-pull", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        given_up.set()
        raise AgentError(f"Pulling {image} did not finish within {int(timeout)} seconds", "pull_timeout", 504)
    if "error" in box:
        raise box["error"]


def host_cpus() -> int | None:
    """How many CPUs Docker can give a container on this host (None when Docker does not say)."""
    try:
        cpus = client().info().get("NCPU")
    except Exception as exc:
        log.debug("Docker did not report the CPU count: %s", exc)
        return None
    return cpus if isinstance(cpus, int) and cpus > 0 else None


def _image_repos(attrs: dict[str, Any]) -> set[str]:
    """Repositories an image came from (an untagged image still has its RepoDigests)."""
    names = {ref.split("@", 1)[0] for ref in attrs.get("RepoDigests") or [] if ref and "@" in ref}
    names |= {
        ref.rsplit(":", 1)[0] for ref in attrs.get("RepoTags") or [] if ref and not ref.startswith("<none>")
    }
    return names


def _repo_of(reference: str | None) -> str | None:
    try:
        return split_image(reference)[0] if reference else None
    except AgentError:
        return None


def remove_superseded_images(
    settings: Settings, candidates: set[str | None] | dict[str | None, str | None]
) -> list[str]:
    """Remove runtime images that nothing needs any more; returns the ids removed.

    Only an image of an allow-listed repository (of any game) that no longer has any
    tag (its tag was moved to a newer pull: a dangling image) and that no container uses, running
    or not. A tagged image is never removed, and nothing is forced. Never raises.

    ``candidates`` may map each image id to the reference the caller knew it by (the tag it was
    pulled under, the image of the server's container). Docker's containerd image store (the
    default of new Docker 29 installs) reports a dangling image with neither RepoTags nor
    RepoDigests, so its repository can only come from there; an image Docker names a
    repository for is judged by Docker's names alone.
    """
    removed: list[str] = []
    allowed = profiles.all_allowed_images(settings)
    known = candidates if isinstance(candidates, dict) else dict.fromkeys(candidates)
    for image_id in sorted(i for i in known if i):
        try:
            img = client().images.get(image_id)
            attrs = img.attrs or {}
            tags = [t for t in attrs.get("RepoTags") or [] if t and not t.startswith("<none>")]
            repos = _image_repos(attrs)
            if not repos and (repo := _repo_of(known[image_id])):
                repos = {repo}
            if tags or not (repos & allowed):
                continue
            if client().containers.list(all=True, filters={"ancestor": image_id}, ignore_removed=True):
                continue
            client().images.remove(image_id, noprune=False)
        except NotFound:
            continue
        except Exception as exc:  # in use after all (409), Docker unreachable: try again next time
            log.info("Superseded image %s was not removed: %s", image_id, exc)
            continue
        log.info("Removed superseded runtime image %s", image_id)
        removed.append(image_id)
    return removed


# pull="newer": a tag is looked up at the registry at most once per this many seconds (a moved
# tag means a full new itzg build, ~1.2 GB, at the next start that creates a container).
PULL_CHECK_INTERVAL = 24 * 3600
PULL_CHECK_FILE = "image-checks.json"
_pull_checks_lock = threading.Lock()


def _read_pull_checks(settings: Settings) -> dict[str, float]:
    try:
        data = json.loads((settings.state_dir / PULL_CHECK_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, int | float)}


def recently_checked(settings: Settings, image: str, now: float | None = None) -> float | None:
    """When ``image`` (a tag) was last pulled successfully, if that is less than
    :data:`PULL_CHECK_INTERVAL` ago; else None. A time in the future (the clock was set back)
    does not count."""
    now = time.time() if now is None else now
    with _pull_checks_lock:
        at = _read_pull_checks(settings).get(image)
    if at is None or not 0 <= now - at < PULL_CHECK_INTERVAL:
        return None
    return at


def remember_pull(settings: Settings, image: str, now: float | None = None) -> None:
    """Record a successful pull of ``image`` (a failed one is never recorded). Never raises."""
    now = time.time() if now is None else now
    path = settings.state_dir / PULL_CHECK_FILE
    with _pull_checks_lock:
        checks = {k: v for k, v in _read_pull_checks(settings).items() if 0 <= now - v < PULL_CHECK_INTERVAL}
        checks[image] = now
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(checks, fh)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Could not record the image check of %s in %s: %s", image, path, exc)


def pull_image(image: str, progress: Any = None) -> None:
    repo, tag = split_image(image)
    api = client().api
    layers: dict[str, tuple[int, int]] = {}
    for event in api.pull(repo, tag=tag, stream=True, decode=True):
        if "error" in event:
            raise AgentError(f"Image pull failed: {event['error']}", "pull_failed", 502)
        detail = event.get("progressDetail") or {}
        if event.get("id") and detail.get("total"):
            layers[event["id"]] = (detail.get("current", 0), detail["total"])
        if progress and layers:
            cur = sum(c for c, _ in layers.values())
            tot = sum(t for _, t in layers.values()) or 1
            progress(min(cur / tot, 1.0), f"Pulling {image}: {event.get('status', '')}")


# --- networks ----------------------------------------------------------------------
def ensure_network(settings: Settings) -> None:
    name = settings.instance_network
    try:
        client().networks.get(name)
    except NotFound:
        log.info("Creating instance network %s", name)
        client().networks.create(name, driver="bridge", labels={LABEL_MANAGED: "true"})


# --- containers ----------------------------------------------------------------------
def get_container(instance_id: UUID) -> Container | None:
    try:
        c = client().containers.get(container_name(instance_id))
    except NotFound:
        return None
    if c.labels.get(LABEL_MANAGED) != "true" or c.labels.get(LABEL_INSTANCE) != str(instance_id):
        raise AgentError(
            "A container with the expected name exists but is not managed by MCPanel",
            "foreign_container",
            409,
        )
    return c


def no_container() -> AgentError:
    return AgentError("The instance has no container; apply its configuration first", "no_container", 404)


def require_container(instance_id: UUID) -> Container:
    c = get_container(instance_id)
    if c is None:
        raise no_container()
    return c


def container_is_gone(exc: Exception) -> bool:
    """Docker answered a request about a container with "No such container": it was removed
    between the lookup and this request (a stopped server's container is retired, or someone
    ran ``docker rm``). A 404 about something else (a network that is missing) does not count.
    """
    return isinstance(exc, NotFound) and "no such container" in str(exc.explanation or exc).lower()


def label_safe_name(name: str) -> str:
    """The server name as stored in the ``io.mcpanel.name`` label: without ``=``.

    Unraid decides which containers are its own with a plain text search for
    ``net.unraid.docker.managed=`` over the names and all label text of ``docker ps``
    (rc.docker), label values included. Containers that match are stopped by Unraid itself
    at array stop, with Unraid's timeout, and then count as manually stopped. A value
    without ``=`` can never produce that text, whatever the server is called.
    """
    return name[:100].replace("=", "-")


def _validate_env(profile: RuntimeProfile, spec: InstanceSpec, injected: dict[str, str]) -> None:
    """The profile's environment policy, re-validated here (never rely on the panel). A key the
    agent injects itself is not refused: the injected value replaces it."""
    for key, value in spec.env.items():
        if key in injected:
            continue
        reason = profile.env_error(key)
        if reason is None:
            reason = profile.env_value_error(key, value)
        if reason is not None:
            raise AgentError(reason, "env_not_allowed", 400)


def _mount_source(settings: Settings, instance_id: UUID, subdir: str) -> str:
    base = host_instance_dir(settings, instance_id)
    return str(base if subdir in ("", ".") else base / subdir)


# Folder of the server folder that holds the image VOLUMEs outside the profile's mounts.
IMAGE_VOLUMES_DIR = ".volumes"
IMAGE_VOLUME_TMPFS_OPTIONS = "rw,nosuid,nodev"


def _norm_container_path(path: str) -> str:
    parts = [p for p in str(path).split("/") if p not in ("", ".")]
    if ".." in parts:
        raise AgentError(f"The image declares an invalid volume path {path!r}", "invalid_image", 400)
    return "/" + "/".join(parts)


def image_volume_plan(
    profile: RuntimeProfile, image_volumes: list[str] | None
) -> tuple[dict[str, str], list[str]]:
    """Where each VOLUME the image declares goes: ({container path: path relative to the server
    folder}, [container paths that get a tmpfs]). A path that is exactly a profile mount needs
    nothing; one below a mount is that subfolder of the mount; one in ``volume_tmpfs`` a tmpfs;
    any other one ``.volumes/<path with "/" as "_">``. Docker creates an anonymous volume for
    every declared path that has no mount of its own, so every one gets one."""
    binds: dict[str, str] = {}
    tmpfs: list[str] = []
    if not profile.mount_image_volumes or not image_volumes:
        return binds, tmpfs
    mounts = {_norm_container_path(m.container_path): m.subdir for m in profile.mounts}
    tmpfs_paths = {_norm_container_path(t) for t in profile.volume_tmpfs}
    for raw in sorted(image_volumes):
        path = _norm_container_path(raw)
        if path in mounts or path == "/":
            continue
        if path in tmpfs_paths:
            tmpfs.append(path)
            continue
        rel = None
        for target, subdir in sorted(mounts.items(), key=lambda kv: -len(kv[0])):
            if path.startswith(target.rstrip("/") + "/"):
                below = path[len(target.rstrip("/")) + 1 :]
                rel = below if subdir in ("", ".") else f"{subdir.rstrip('/')}/{below}"
                break
        if rel is None:
            rel = f"{IMAGE_VOLUMES_DIR}/{path.strip('/').replace('/', '_')}"
        binds[path] = rel
    return binds, tmpfs


def build_create_kwargs(
    settings: Settings,
    instance_id: UUID,
    spec: InstanceSpec,
    spec_hash: str,
    image_volumes: list[str] | None = None,
) -> dict[str, Any]:
    """The arguments of ``containers.create()``: what the spec asks for (validated) plus what the
    runtime profile of its game fixes. For Minecraft exactly what the agent created before
    multi-game support (tests/test_games_pins.py).

    ``image_volumes``: the VOLUMEs the image declares (``create_container`` inspects the image
    for profiles with ``mount_image_volumes``); each gets a mount (``image_volume_plan``)."""
    profile = profiles.for_spec(spec)
    image = validate_image(settings, spec.image, profile)
    network = spec.network or settings.instance_network
    if network not in settings.effective_networks():
        raise AgentError(f"Network '{network}' is not permitted by agent policy", "network_not_allowed", 403)
    if spec.memory_limit_mb > settings.max_memory_mb:
        raise AgentError(
            f"Memory limit exceeds agent maximum of {settings.max_memory_mb} MB", "memory_limit", 400
        )
    injected = profile.injected_env(profile, settings, spec)
    _validate_env(profile, spec, injected)

    env: dict[str, str] = dict(settings.runtime_extra_env)
    env.update(spec.env)
    env.update(injected)

    volumes: dict[str, dict[str, str]] = {
        _mount_source(settings, instance_id, m.subdir): {"bind": m.container_path, "mode": "rw"}
        for m in profile.mounts
    }
    if settings.runtime_ca_bundle:
        volumes[str(settings.runtime_ca_bundle)] = {"bind": "/mcpanel/certs/ca-bundle.crt", "mode": "ro"}
    if settings.runtime_java_truststore and profile.java_truststore:
        volumes[str(settings.runtime_java_truststore)] = {"bind": "/mcpanel/certs/cacerts", "mode": "ro"}
    volume_binds, volume_tmpfs = image_volume_plan(profile, image_volumes)
    for target, rel in volume_binds.items():
        volumes[_mount_source(settings, instance_id, rel)] = {"bind": target, "mode": "rw"}

    ports: dict[str, Any] = {}
    if network != "host":
        for p in spec.ports:
            if not (settings.port_min <= p.host_port <= settings.port_max):
                raise AgentError(
                    f"Host port {p.host_port} is outside the permitted range {settings.port_min}-{settings.port_max}",
                    "port_not_allowed",
                    403,
                )
            if (reason := profile.port_error(p.container_port, p.protocol)) is not None:
                raise AgentError(reason, "port_not_allowed", 403)
            key = f"{p.container_port}/{p.protocol}"
            ports.setdefault(key, []).append((p.host_ip, p.host_port))

    labels = {
        LABEL_MANAGED: "true",
        LABEL_INSTANCE: str(instance_id),
        LABEL_NAME: label_safe_name(spec.name),
        LABEL_SPEC_HASH: spec_hash,
        LABEL_STOP_TIMEOUT: str(spec.stop_timeout),
    }
    labels.update(profiles.labels_for(spec))  # none for Minecraft
    kwargs: dict[str, Any] = {
        "image": image,
        "name": container_name(instance_id),
        "hostname": f"{profile.hostname_prefix}{instance_id.hex[:8]}",
        "environment": env,
        "labels": labels,
        "volumes": volumes,
        "ports": ports,
        "network": network,
        "mem_limit": f"{spec.memory_limit_mb}m",
        "memswap_limit": f"{spec.memory_limit_mb}m",
        "pids_limit": settings.pids_limit,
        "restart_policy": {
            "Name": spec.restart_policy,
            **({"MaximumRetryCount": 3} if spec.restart_policy == "on-failure" else {}),
        },
        "stop_signal": profile.stop_signal,
        # Docker's own stop timeout (Config.StopTimeout): what the server gets when Docker stops
        # it without the panel (host shutdown, daemon restart, `docker restart`); without it that
        # is Docker's default of 10 s. Capped by AGENT_SHUTDOWN_STOP_TIMEOUT because Unraid kills
        # the Docker daemon 30 s after asking it to stop, and the daemon waits
        # max(StopTimeout of its containers) + 5 s for them. Stops from the panel pass the full
        # spec.stop_timeout themselves (kept in the stop-timeout label as the agent's default).
        # Not an argument of docker-py's containers.create(): create_container() takes it out.
        "stop_timeout": min(spec.stop_timeout, settings.shutdown_stop_timeout),
        "stdin_open": profile.stdin_open,
        "tty": profile.tty,
        "detach": True,
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        # Only what the profile's image needs (Minecraft: the itzg entrypoint starts as root to
        # fix ownership, then drops to UID/GID).
        "cap_add": list(profile.cap_add),
        "log_config": {
            "Type": "json-file",
            "Config": {"max-size": settings.log_max_size, "max-file": str(settings.log_max_files)},
        },
    }
    if profile.user.kind == "fixed":
        uid = settings.runtime_uid if profile.user.uid is None else profile.user.uid
        gid = settings.runtime_gid if profile.user.gid is None else profile.user.gid
        kwargs["user"] = f"{uid}:{gid}"
    hc = spec.healthcheck
    if profile.health.kind == "cmd":
        kwargs["healthcheck"] = {
            "test": ["CMD", *profile.health.argv],
            "interval": hc.interval_s * 1_000_000_000,
            "timeout": hc.timeout_s * 1_000_000_000,
            "start_period": hc.start_period_s * 1_000_000_000,
            "retries": hc.retries,
        }
    elif profile.health.kind in profiles.AGENT_SIDE_HEALTH:
        # the agent probes readiness itself (agent_health); no image HEALTHCHECK may interfere
        kwargs["healthcheck"] = {"test": ["NONE"]}
    # kind "image": the image's own HEALTHCHECK applies (profiles.register refuses other kinds)
    if volume_tmpfs:
        kwargs["tmpfs"] = dict.fromkeys(volume_tmpfs, IMAGE_VOLUME_TMPFS_OPTIONS)
    if spec.cpu_limit:
        kwargs["nano_cpus"] = int(spec.cpu_limit * 1e9)
    return kwargs


def declared_volumes(image: str) -> list[str]:
    """The VOLUMEs of a local image (its Config.Volumes)."""
    try:
        attrs = client().images.get(image).attrs or {}
    except ImageNotFound as exc:
        raise AgentError(f"Image {image} is not present", "image_missing", 409) from exc
    return sorted(((attrs.get("Config") or {}).get("Volumes") or {}).keys())


def prepare_image_volumes(settings: Settings, instance_id: UUID, binds: dict[str, str]) -> None:
    """Create the server-folder subfolders the image volumes are bound to (owned by the runtime
    uid/gid; no symbolic link is followed), so Docker does not create them as root."""
    from .safefs import PathError, SafeRoot

    root = SafeRoot(instance_dir(settings, instance_id), settings.runtime_uid, settings.runtime_gid)
    for rel in binds.values():
        try:
            if not root.exists(rel):
                root.mkdir(rel, parents=True)
            elif root.stat(rel).type != "directory":
                raise AgentError(
                    f"{rel} in the server folder is not a folder; the image needs it for a volume",
                    "invalid_path",
                    409,
                )
        except PathError as exc:
            raise AgentError(
                f"Cannot prepare {rel} for the image's volume: {exc}", exc.code, exc.status
            ) from exc


def create_container(
    settings: Settings, instance_id: UUID, spec: InstanceSpec, spec_hash: str, *, name: str | None = None
) -> Container:
    """Create the container (stopped). ``name`` replaces the usual name (used while the old
    container of the server still exists; see :func:`replacement_name`). For a profile with
    ``mount_image_volumes`` the image is inspected for its VOLUMEs first (never for Minecraft)."""
    profile = profiles.for_spec(spec)
    image_volumes = None
    if profile.mount_image_volumes:
        image_volumes = declared_volumes(validate_image(settings, spec.image, profile))
        prepare_image_volumes(settings, instance_id, image_volume_plan(profile, image_volumes)[0])
    kwargs = build_create_kwargs(settings, instance_id, spec, spec_hash, image_volumes)
    if name is not None:
        if name not in (container_name(instance_id), replacement_name(instance_id)):
            raise AgentError("Invalid container name", "internal", 500)
        kwargs["name"] = name
    if kwargs["network"] == settings.instance_network:
        ensure_network(settings)
    # docker-py's high-level containers.create() cannot set Config.StopTimeout: it rejects a
    # stop_timeout keyword with a TypeError (docker 7.x), only the low-level create_container()
    # has it. So this does exactly what containers.create() does internally (same argument
    # conversion, same API call, same lookup of the result) plus stop_timeout.
    # tests/test_docker_ops.py compares the two requests on the wire.
    stop_timeout = kwargs.pop("stop_timeout")
    docker_client = client()
    try:
        low_level = _create_container_args(
            {**kwargs, "command": None, "version": docker_client.api.api_version}
        )
        created = docker_client.api.create_container(**low_level, stop_timeout=stop_timeout)
        return docker_client.containers.get(created["Id"])
    except APIError as exc:
        raise AgentError(
            f"Docker refused to create the container: {exc.explanation or exc}", "create_failed", 502
        ) from exc


def remove_container(
    instance_id: UUID, *, timeout: int = 60, settings: Settings | None = None, save_last_run: bool = True
) -> bool:
    """Stop (gracefully) and remove the container. Returns False when there was none.

    With ``save_last_run`` the last-run record of a container that has run is saved first, so
    the console history survives a recreation or an archive too. Unlike :func:`retire` that is
    best effort here: the caller asked for the container to go (it is being replaced or the
    server archived), so a record that cannot be written is a warning in the agent log.
    """
    c = get_container(instance_id)
    if c is None:
        return False
    settings = settings or get_settings()
    try:
        graceful_stop(settings, c, timeout)  # its warnings are in the agent log
    except NotFound as exc:
        if container_is_gone(exc):
            return False  # removed between the lookup and the stop: there is none any more
        raise
    if save_last_run:
        try:
            save_last_run_record(settings, instance_id, c)
        except Exception as exc:
            log.warning(
                "The last-run record of %s was not saved before its container was removed: %s", c.name, exc
            )
    try:
        c.remove(v=False)
    except NotFound:
        pass  # removed by someone else meanwhile: the result is the same
    return True


# --- retiring the container of a stopped server ---------------------------------------------------
# States in which a container is not running and Docker will not start it by itself (a container
# waiting for its restart policy is "restarting").
RETIRABLE_STATES = ("created", "exited", "dead")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

_instance_locks_guard = threading.Lock()
_instance_locks: dict[UUID, threading.Lock] = {}


@contextlib.contextmanager
def instance_lock(instance_id: UUID, wait: float | None = None) -> Iterator[None]:
    """The per-server lock around every sequence that removes, creates or starts its container.

    Retiring a container (inspect, save the last-run record, remove) must not interleave with a
    start, a restart (stop, then start of the same container) or an apply (remove, create,
    start). ``wait`` is how long to wait for the lock (None: as long as it takes); when it runs
    out the answer is 409 ``instance_busy``. A kill and a stop never take it: they only act on a
    running container, which a retire refuses.
    """
    with _instance_locks_guard:
        lock = _instance_locks.setdefault(instance_id, threading.Lock())
    if not lock.acquire(timeout=-1 if wait is None else wait):
        raise AgentError("Another container operation is in progress for this server", "instance_busy", 409)
    try:
        yield
    finally:
        lock.release()


def _timestamp(value: Any) -> datetime | None:
    """A Docker timestamp (RFC 3339 with up to nine fraction digits) as an aware datetime."""
    m = _TS_RE.match(str(value or ""))
    if not m or m.group(1).startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(m.group(1) + (m.group(2) or "")[:7] + m.group(3).replace("Z", "+00:00"))
    except ValueError:
        return None


def stopped_seconds(described: dict[str, Any], now: datetime | None = None) -> float | None:
    """How long a container that is not running has been in that state; None when Docker does not say.

    Since it finished, or since it was created when it never ran. Both times and "now" come
    from the Docker host, so this does not depend on the panel's clock.
    """
    since = _timestamp(described.get("finished_at")) if described.get("started_at") else None
    since = since or _timestamp(described.get("created_at"))
    if since is None:
        return None
    return ((now or datetime.now(UTC)) - since).total_seconds()


def read_console_tail(c: Container) -> tuple[list[str], bool]:
    """The end of the container's console output, bounded and cleaned (see lastrun.collect_tail)."""
    stream = c.logs(
        stream=True, follow=False, stdout=True, stderr=True, timestamps=False, tail=lastrun.MAX_LINES
    )
    try:
        return lastrun.collect_tail(stream)
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def save_last_run_record(settings: Settings, instance_id: UUID, c: Container) -> bool:
    """Write the last-run record of a container that is not running. Returns False (and writes
    nothing, keeping an older record) when the container never ran. Raises when the record cannot
    be built or written."""
    described = describe(c)
    if not lastrun.has_run(described):
        return False
    lines, truncated = read_console_tail(c)
    redact = log_redactor(settings, c) if (getattr(c, "labels", None) or {}).get(LABEL_GAME) else None
    if redact is not None:
        lines = [redact(line) for line in lines]
    lastrun.write(settings, instance_id, lastrun.build(instance_id, described, lines, truncated))
    return True


def retire(settings: Settings, instance_id: UUID, settle_seconds: float = 0.0) -> dict[str, Any]:
    """Remove the container of a stopped server, after saving its last-run record.

    The caller holds :func:`instance_lock`. In order:

    * no container: nothing to do, which is success (``retired`` false, ``reason`` "no_container");
    * running, restarting or paused: refused with 409 ``container_running``. Only a container
      Docker will not start again by itself is retired;
    * in that state for less than ``settle_seconds``: refused with 409 ``not_settled``, so a
      container that was stopped a moment ago (by a job that may start it again) is left alone;
    * it has run: the last-run record is written. If that fails the container STAYS and the
      error is 500 ``last_run_failed``. A container that never ran has nothing to record, and an
      older record is kept;
    * the container is removed. Already gone (someone else removed it meanwhile) is success.
    """
    c = get_container(instance_id)
    if c is None:
        return {"retired": False, "reason": "no_container", "recorded": False}
    try:
        c.reload()
    except NotFound:  # removed between the two calls
        return {"retired": False, "reason": "no_container", "recorded": False}
    described = describe(c)
    state = described.get("status")
    if (
        described.get("running")
        or described.get("restarting")
        or state in ("running", "restarting", "paused")
    ):
        raise AgentError(
            "The server's container is running; only the container of a stopped server is retired",
            "container_running",
            409,
        )
    if state not in RETIRABLE_STATES:
        raise AgentError(f"The container is in state '{state}'; try again later", "container_busy", 409)
    stopped_for = stopped_seconds(described)
    if stopped_for is not None and stopped_for < settle_seconds:
        raise AgentError(
            f"The container stopped {max(stopped_for, 0):.0f} s ago; it is retired after {settle_seconds:.0f} s",
            "not_settled",
            409,
        )
    try:
        recorded = save_last_run_record(settings, instance_id, c)
    except Exception as exc:
        log.exception("Could not save the last-run record of %s; its container is kept", c.name)
        raise AgentError(
            f"The last-run record of the server could not be saved ({type(exc).__name__}: {exc}), "
            "so its container was kept",
            "last_run_failed",
            500,
        ) from exc
    try:
        c.remove(v=False)
    except NotFound:
        pass  # removed by someone else meanwhile: the result is the same
    except APIError as exc:
        text = str(exc.explanation or exc).lower()
        if exc.status_code == 409 and "running" in text:
            # started from outside the agent since the check above (docker start, Unraid's tab)
            raise AgentError(
                "The server's container was started meanwhile; it was not removed", "container_running", 409
            ) from exc
        if not (exc.status_code == 409 and "removal" in text and "in progress" in text):
            raise
    log.info(
        "Retired the container of %s (%s, exit code %s%s)",
        instance_id,
        state,
        described.get("exit_code"),
        "" if recorded else ", never started: no record",
    )
    return {"retired": True, "reason": "", "recorded": recorded}


_BUILTIN_NETWORKS = frozenset({"host", "none", "bridge"})


def reattach_recreated_networks(c: Container) -> list[str]:
    """Point a stopped container at the current network of every name it is attached to.

    Docker lets a network be removed while only *stopped* containers use it (for example
    ``docker compose down`` of the MCPanel stack while every server is stopped, or
    ``docker network prune``). A container that has run before still holds the old
    network ID, so once a network with the same name exists again ``start`` fails with
    "network <id> not found". Disconnecting and reconnecting by name repairs that; nothing
    else about the container changes. Returns the names of the repaired networks.
    """
    c.reload()
    if c.status not in ("created", "exited", "dead"):
        return []
    networks = (c.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    repaired: list[str] = []
    for name, endpoint in networks.items():
        old_id = (endpoint or {}).get("NetworkID") or ""
        if not old_id or name in _BUILTIN_NETWORKS:
            continue  # never started (resolved by name at start) or a built-in network
        try:
            current = client().networks.get(name)
        except NotFound:
            continue  # nothing to reattach to; start() reports the missing network
        if current.name != name or current.id == old_id:
            continue
        log.warning("Network %s was recreated; reconnecting container %s to it", name, c.name)
        try:
            client().api.disconnect_container_from_network(c.id, name, force=True)
            client().api.connect_container_to_network(c.id, name)
        except APIError as exc:
            raise AgentError(
                f"Could not reconnect the container to network '{name}': {exc.explanation or exc}",
                "start_failed",
                502,
            ) from exc
        repaired.append(name)
    return repaired


def start(instance_id: UUID) -> None:
    c = require_container(instance_id)
    try:
        reattach_recreated_networks(c)
        c.start()
    except APIError as exc:
        if container_is_gone(exc):
            # removed between the lookup and this request: the same answer as without a container,
            # which the panel answers by creating one (404 no_container, not 502)
            raise no_container() from exc
        text = str(exc.explanation or exc)
        code = (
            "port_conflict"
            if ("port is already allocated" in text or "address already in use" in text)
            else "start_failed"
        )
        raise AgentError(
            f"Docker could not start the container: {text}", code, 409 if code == "port_conflict" else 502
        ) from exc


def default_stop_timeout(c: Container) -> int:
    try:
        return int(c.labels.get(LABEL_STOP_TIMEOUT, "60"))
    except ValueError:
        return 60


def flush_timeout(stop_timeout: int) -> float:
    """How long the save before a stop (Minecraft: ``save-all flush``) may take: half the stop
    timeout, 10 to 60 s."""
    return float(min(60, max(10, stop_timeout / 2)))


@dataclass
class StopResult:
    """What a graceful stop did besides ``docker stop``; the panel logs it."""

    # The JSON keys are kept for every game (older panels read them): flushed = the server answered
    # the driver's save before the stop (Minecraft: `save-all flush`); stop_sent_via_rcon = the
    # agent delivered the stop command itself (RCON was not up at first); false without RCON.
    flushed: bool = False
    stop_sent_via_rcon: bool = False
    warnings: list[str] = field(default_factory=list)
    # Exit code of the run this stop ended: 0 = the server shut down by itself, anything else =
    # it was ended (137: Docker's SIGKILL at the timeout) or failed while stopping. None when
    # nothing was stopped (the container was not running) or Docker did not say. After /restart
    # the container status describes the new run, so this is the only place it is reported.
    exit_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "flushed": self.flushed,
            "stop_sent_via_rcon": self.stop_sent_via_rcon,
            "warnings": list(self.warnings),
            "exit_code": self.exit_code,
        }


def _flush_before_stop(
    target: tuple[str, int, str], bound: float, result: StopResult, command: str = SAVE_BEFORE_STOP
) -> bool:
    """``command`` (Minecraft: ``save-all flush``) if RCON answers quickly. Returns whether RCON
    answered (accepted the login). Used by the RCON drivers (drivers/rcon.py).

    Never raises: whatever goes wrong here is a warning and the stop goes on.
    """
    host, port, password = target
    try:
        run_command_sync(host, port, password, command, timeout=bound, connect_timeout=RCON_PROBE_TIMEOUT)
    except RconUnavailable as exc:
        result.warnings.append(
            "The world was not flushed before the stop: RCON is not available "
            f"(the server is probably still starting): {exc}"
        )
        return False
    except RconError as exc:
        result.warnings.append(f"{command} before the stop failed, the world may not be saved: {exc}")
        return True
    except Exception as exc:
        log.exception("Unexpected error while flushing the world before a stop")
        result.warnings.append(f"The world was not flushed before the stop: {exc}")
        return False  # unknown; the courier below then makes sure `stop` arrives
    result.flushed = True
    return True


class _RconStopCourier:
    """Delivers ``stop`` through the agent's own RCON client while ``docker stop`` is in progress.

    (The command is the RCON driver's; Minecraft's is ``stop``.)

    For a server that had not opened RCON yet when the stop began: the itzg runner
    (mc-server-runner) tries ``rcon-cli stop`` once when it gets SIGTERM, and its console
    fallback cannot work in these containers (their stdin is a read-only pipe). The stop
    would be lost: the server finishes starting and is killed STOP_DURATION later without
    saving. So a helper thread keeps trying every RCON_STOP_INTERVAL seconds and sends
    ``stop`` once, as soon as the server accepts an RCON login.

    The first try comes one interval after the thread was started, which is right before
    ``docker stop`` is sent: Docker must have registered the stop before the server exits, or
    the restart policy would start it again. The thread never touches the caller's Container
    object, and it sends nothing after :meth:`finish` returned (a restart may start the server
    again right after it).
    """

    def __init__(
        self, container_id: str, name: str, target: tuple[str, int, str], command: str = STOP_COMMAND
    ) -> None:
        self._container_id = container_id
        self._target = target
        self._command = command
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._sent = False
        self._warning: str | None = None
        self._thread = threading.Thread(target=self._run, name=f"rcon-stop-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _container_running(self) -> bool:
        try:
            state = client().api.inspect_container(self._container_id).get("State") or {}
        except NotFound:
            return False
        except Exception:
            # Docker did not say. `docker stop` returning ends the loop in any case, and the
            # RCON login only fits this server, so trying is the safer side.
            return True
        return bool(state.get("Running"))

    def _run(self) -> None:
        host, port, password = self._target
        try:
            while not self._done.wait(RCON_STOP_INTERVAL):
                if not self._container_running() or self._done.is_set():
                    return
                try:
                    run_command_sync(
                        host,
                        port,
                        password,
                        self._command,
                        timeout=RCON_STOP_COMMAND_TIMEOUT,
                        connect_timeout=RCON_PROBE_TIMEOUT,
                    )
                except RconUnavailable:
                    continue  # RCON is not open yet
                except RconError as exc:
                    # Logged in and sent. A server that is shutting down often drops the
                    # connection before it answers, so this counts as delivered.
                    with self._lock:
                        self._sent = True
                        self._warning = (
                            f"{self._command} was sent through RCON but the server did not confirm it: {exc}"
                        )
                    return
                with self._lock:
                    self._sent = True
                return
        except Exception:
            log.exception("RCON stop helper failed")

    def finish(self, result: StopResult) -> None:
        """Called when ``docker stop`` has returned (or failed): end the loop and collect the outcome."""
        self._done.set()
        # A try that is in flight is bounded by the two RCON timeouts (+1 s backstop); wait for
        # it, so that nothing is sent after this returns.
        self._thread.join(RCON_PROBE_TIMEOUT + RCON_STOP_COMMAND_TIMEOUT + 3.0)
        with self._lock:
            result.stop_sent_via_rcon = self._sent
            if self._warning:
                result.warnings.append(self._warning)


def graceful_stop(settings: Settings, c: Container, timeout: int | None) -> StopResult:
    """Stop a container the way the panel means it, through the control driver of its game.

    The driver saves first and makes sure the stop arrives where it can (Minecraft,
    ``drivers.rcon.RconDriver.stop``: ``save-all flush`` when RCON answers quickly, then
    ``docker stop``, and ``stop`` through RCON as soon as it answers when it did not at first); a
    driver without a stop of its own gets a plain ``docker stop`` with the timeout. A failure to
    save is a warning, never an error.

    A container that is not running is left alone. Docker errors of ``docker stop`` propagate.
    """
    result = StopResult()
    c.reload()
    if c.status not in ("running", "restarting", "paused"):
        return result
    timeout = timeout or default_stop_timeout(c)
    if not drivers.for_container(c).stop(settings, c, timeout, result):
        c.stop(timeout=timeout)
    result.exit_code = _stopped_exit_code(c)
    for warning in result.warnings:
        log.warning("Stopping %s: %s", c.name, warning)
    return result


def _stopped_exit_code(c: Container) -> int | None:
    """Exit code of the run ``docker stop`` just ended; None when Docker does not say.

    Never raises: the stop itself has happened, its exit code is only reported.
    """
    try:
        c.reload()
        state = c.attrs.get("State") or {}
    except Exception as exc:
        log.warning("Could not read the exit code of %s after its stop: %s", c.name, exc)
        return None
    if state.get("Running"):
        return None
    code = state.get("ExitCode")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def stop(instance_id: UUID, timeout: int | None, settings: Settings | None = None) -> StopResult:
    c = require_container(instance_id)
    try:
        return graceful_stop(settings or get_settings(), c, timeout)
    except NotFound as exc:
        if container_is_gone(exc):
            # A stop takes no lock: the container of a server that was not running can be retired
            # between the lookup and the first request about it. That is "no container" (404),
            # which the panel's stop takes as stopped, and not a Docker error (502).
            raise no_container() from exc
        raise


def kill(instance_id: UUID) -> None:
    """Kill the server process. A server that is not running, with or without a container, is left alone."""
    c = get_container(instance_id)
    if c is None:
        return
    try:
        c.reload()
    except NotFound:  # removed meanwhile (a stopped server's container is retired): nothing runs
        return
    if c.status in ("running", "restarting", "paused"):
        c.kill()


def _parse_ts(value: str | None) -> str | None:
    if not value or value.startswith("0001-"):
        return None
    return value


def describe(c: Container) -> dict[str, Any]:
    attrs = c.attrs
    state = attrs.get("State", {})
    health = state.get("Health") or {}
    net_settings = attrs.get("NetworkSettings", {}) or {}
    networks = net_settings.get("Networks") or {}
    ip = None
    for net in networks.values():
        if net.get("IPAddress"):
            ip = net["IPAddress"]
            break
    ports = []
    for key, bindings in (net_settings.get("Ports") or {}).items():
        cport, _, proto = key.partition("/")
        for b in bindings or []:
            ports.append(
                {
                    "container_port": int(cport),
                    "protocol": proto,
                    "host_ip": b.get("HostIp"),
                    "host_port": int(b.get("HostPort")),
                }
            )
    health_log = health.get("Log") or []
    described = {
        "exists": True,
        "id": c.id,
        "name": c.name,
        "image": attrs.get("Config", {}).get("Image"),
        # what the tag pointed to when the container was created (sha256:...)
        "image_id": attrs.get("Image") or None,
        "status": state.get("Status"),
        "running": bool(state.get("Running")),
        "restarting": bool(state.get("Restarting")),
        "exit_code": state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled")),
        "error": state.get("Error") or None,
        "started_at": _parse_ts(state.get("StartedAt")),
        "finished_at": _parse_ts(state.get("FinishedAt")),
        "health": health.get("Status"),
        "health_output": (health_log[-1].get("Output", "")[-500:] if health_log else None),
        "restart_count": attrs.get("RestartCount", 0),
        "spec_hash": c.labels.get(LABEL_SPEC_HASH),
        "ip_address": ip,
        "ports": ports,
        "created_at": attrs.get("Created"),
    }
    # the game of a container that is not Minecraft (a Minecraft container has no such label)
    labels = c.labels or {}
    if labels.get(LABEL_GAME):
        described["game"] = labels[LABEL_GAME]
        if labels.get(LABEL_VARIANT):
            described["variant"] = labels[LABEL_VARIANT]
        profile = profiles.for_labels(labels)
        if profile is not None and profile.health.kind in profiles.AGENT_SIDE_HEALTH:
            described["health"], described["health_output"] = agent_health(c, profile, described)
    return described


# --- agent-side readiness probes (profiles' health kinds "tcp" and "log") ----------------------------
HEALTH_TCP_CACHE_SECONDS = 5.0
HEALTH_TCP_TIMEOUT = 1.0
HEALTH_LOG_TAIL = 5000  # lines read per look at the log (only what is new since the last look)
HEALTH_LOG_OVERLAP = 2.0  # seconds read again at every look (Docker's `since` is a timestamp)
_health_lock = threading.Lock()
# (container id, StartedAt) -> state of the probe for that run
_health_state: dict[tuple[str, str], dict[str, Any]] = {}
_HEALTH_STATE_MAX = 512


def _health_entry(key: tuple[str, str]) -> dict[str, Any]:
    with _health_lock:
        entry = _health_state.get(key)
        if entry is None:
            if len(_health_state) >= _HEALTH_STATE_MAX:
                _health_state.clear()  # old runs; a probe just looks again
            entry = _health_state[key] = {}
        return entry


def _probe_tcp(c: Container, port: int, settings: Settings) -> bool:
    import socket

    host: str | None
    target_port = port
    info_ports = (c.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    if settings.rcon_mode == "published":
        host = None
        for b in info_ports.get(f"{port}/tcp") or []:
            if b.get("HostPort"):
                host, target_port = "127.0.0.1", int(b["HostPort"])
                break
    elif (c.attrs.get("HostConfig") or {}).get("NetworkMode") == "host":
        host = "127.0.0.1"
    else:
        host = None
        for net in ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}).values():
            if net.get("IPAddress"):
                host = net["IPAddress"]
                break
    if host is None:
        return False
    try:
        with socket.create_connection((host, target_port), timeout=HEALTH_TCP_TIMEOUT):
            return True
    except OSError:
        return False


def _log_matches(c: Container, pattern: re.Pattern[str], since: float | None) -> bool:
    kwargs: dict[str, Any] = {"stdout": True, "stderr": True, "timestamps": False, "tail": HEALTH_LOG_TAIL}
    if since is not None:
        kwargs["since"] = since
    text = c.logs(**kwargs)
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return any(pattern.search(line) for line in re.split(r"[\r\n]+", text))


def agent_health(
    c: Container, profile: RuntimeProfile, described: dict[str, Any], now: float | None = None
) -> tuple[str | None, str | None]:
    """(health, health_output) of a container whose profile's readiness probe the agent runs.

    None while the container is not running. "healthy" once the probe succeeded: ``log`` stays
    healthy for the rest of the run (the line was printed), ``tcp`` is checked again after
    HEALTH_TCP_CACHE_SECONDS. Before that "starting" until ``start_period_s`` after the start,
    then "unhealthy" (what Docker reports for its own checks, so state.py reads it alike).
    Never raises: a probe that cannot be run counts as not succeeded."""
    if not described.get("running"):
        return None, None
    health = profile.health
    started_raw = str(described.get("started_at") or "")
    started = _timestamp(started_raw)
    now = time.time() if now is None else now
    key = (str(c.id), started_raw)
    entry = _health_entry(key)
    ok = False
    try:
        if health.kind == "log":
            if entry.get("ok"):
                ok = True
            else:
                look_from = entry.get("checked")
                if look_from is None and started is not None:
                    look_from = started.timestamp()
                since = None if look_from is None else max(0.0, look_from - HEALTH_LOG_OVERLAP)
                ok = _log_matches(c, re.compile(str(health.pattern)), since)
                entry["checked"] = now
                entry["ok"] = ok
        elif health.kind == "tcp":
            at = entry.get("at")
            if at is not None and 0 <= now - at < HEALTH_TCP_CACHE_SECONDS:
                ok = bool(entry.get("ok"))
            else:
                ok = _probe_tcp(c, int(health.port or 0), get_settings())
                entry["ok"], entry["at"] = ok, now
    except Exception as exc:  # Docker or socket trouble: not ready (yet)
        log.debug("readiness probe of %s failed: %s", c.name, exc)
        ok = False
    what = f"agent probe {health.kind}: " + (
        f"log line /{health.pattern}/" if health.kind == "log" else f"TCP port {health.port}"
    )
    if ok:
        return "healthy", f"{what} ok"
    running_for = None if started is None else now - started.timestamp()
    if running_for is not None and running_for > health.start_period_s:
        return "unhealthy", f"{what} not seen within {health.start_period_s} s"
    return "starting", f"{what} not seen yet"


# --- waiting for a log line (saves that report their end in the log) -----------------------------------
LOG_WAIT_POLL = 0.25


def wait_for_log(c: Container, pattern: str, since: float, timeout: float) -> bool:
    """Whether the container prints a line matching ``pattern`` after ``since`` (epoch seconds)
    within ``timeout`` seconds. Polls the log; bounded."""
    compiled = re.compile(pattern)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if _log_matches(c, compiled, since):
                return True
        except NotFound:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(LOG_WAIT_POLL)


# --- secrets in console output -------------------------------------------------------------------------
REDACTED = "********"


def log_redactor(settings: Settings, c: Container) -> Any:
    """A function that hides the control secret in console text of container ``c``, or None when
    its profile prints none (Minecraft). The secret (and whatever the profile's
    log_redact_patterns match) becomes ``********``."""
    profile = profiles.for_labels(c.labels)
    if profile is None or not profile.redact_secret_in_logs:
        return None
    try:
        secret = drivers.for_profile(profile).secret(settings, c)
    except Exception as exc:
        log.debug("control secret of %s not readable for redaction: %s", c.name, exc)
        secret = None
    patterns = [re.compile(p) for p in profile.log_redact_patterns]

    def redact(text: str) -> str:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
        for pattern in patterns:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
        return text

    return redact


def status(instance_id: UUID, settings: Settings | None = None) -> dict[str, Any]:
    """The container's state. Without a container: ``exists`` false, and ``last_run`` (the
    last-run record without its console lines) when the server has run before."""
    c = get_container(instance_id)
    if c is not None:
        try:
            c.reload()
        except NotFound:  # removed between the two calls
            c = None
    if c is None:
        answer: dict[str, Any] = {"exists": False}
        last_run = lastrun.summary(settings or get_settings(), instance_id)
        if last_run is not None:
            answer["last_run"] = last_run
        return answer
    return describe(c)


def list_managed() -> list[dict[str, Any]]:
    out = []
    # ignore_removed: docker-py lists the ids and then inspects each container. One that is
    # removed in between (a retire, an apply) must not fail the whole list: the panel would mark
    # every server of the node "unknown" for a pass.
    for c in client().containers.list(
        all=True, filters={"label": f"{LABEL_MANAGED}=true"}, ignore_removed=True
    ):
        if (c.name or "").endswith(REPLACEMENT_SUFFIX):
            continue  # an apply's new container before it takes over the name: not the server's yet
        info = describe(c)
        info["instance_id"] = c.labels.get(LABEL_INSTANCE)
        out.append(info)
    return out


def published_ports() -> list[dict[str, Any]]:
    """Host ports published by *any* container on this Docker host (for conflict checks)."""
    used = []
    for c in client().containers.list(all=False, ignore_removed=True):
        for key, bindings in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
            _cport, _, proto = key.partition("/")
            for b in bindings or []:
                used.append(
                    {
                        "host_ip": b.get("HostIp"),
                        "host_port": int(b.get("HostPort")),
                        "protocol": proto,
                        "container": c.name,
                        "managed": c.labels.get(LABEL_MANAGED) == "true",
                        "instance_id": c.labels.get(LABEL_INSTANCE),
                    }
                )
    return used


def stats(instance_id: UUID) -> dict[str, Any]:
    c = get_container(instance_id)
    if c is None:  # a stopped server has no container
        return {"running": False}
    try:
        c.reload()
    except NotFound:
        return {"running": False}
    if c.status != "running":
        return {"running": False}
    s = c.stats(stream=False)
    cpu = s.get("cpu_stats", {})
    pre = s.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get(
        "total_usage", 0
    )
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    cpu_percent = (cpu_delta / sys_delta) * online * 100.0 if sys_delta > 0 and cpu_delta >= 0 else 0.0
    mem = s.get("memory_stats", {})
    mem_stats = mem.get("stats", {})
    cache = mem_stats.get("inactive_file", mem_stats.get("total_inactive_file", mem_stats.get("cache", 0)))
    mem_usage = max(mem.get("usage", 0) - cache, 0)
    rx = tx = 0
    for net in (s.get("networks") or {}).values():
        rx += net.get("rx_bytes", 0)
        tx += net.get("tx_bytes", 0)
    blk_read = blk_write = 0
    for entry in s.get("blkio_stats", {}).get("io_service_bytes_recursive") or []:
        if entry.get("op", "").lower() == "read":
            blk_read += entry.get("value", 0)
        elif entry.get("op", "").lower() == "write":
            blk_write += entry.get("value", 0)
    return {
        "running": True,
        "timestamp": datetime.now().astimezone().isoformat(),
        "cpu_percent": round(cpu_percent, 2),
        "cpu_count": online,
        "memory_bytes": mem_usage,
        "memory_limit_bytes": mem.get("limit", 0),
        "network_rx_bytes": rx,
        "network_tx_bytes": tx,
        "block_read_bytes": blk_read,
        "block_write_bytes": blk_write,
        "pids": s.get("pids_stats", {}).get("current"),
    }


def logs(
    instance_id: UUID, tail: int = 500, since: int | None = None, settings: Settings | None = None
) -> str:
    c = require_container(instance_id)
    return _container_log(c, tail, since, settings)


def _container_log(
    c: Container, tail: int, since: int | None = None, settings: Settings | None = None
) -> str:
    kwargs: dict[str, Any] = {"tail": tail, "timestamps": False, "stdout": True, "stderr": True}
    if since:
        kwargs["since"] = since
    text = c.logs(**kwargs).decode("utf-8", errors="replace")
    redact = (
        log_redactor(settings or get_settings(), c)
        if (getattr(c, "labels", None) or {}).get(LABEL_GAME)
        else None
    )
    return redact(text) if redact is not None else text


def previous_run_lines(settings: Settings, instance_id: UUID, c: Container | None) -> list[str]:
    """Console lines of the last-run record that belong in front of the log of container ``c``.

    Empty when there is no record, or when the record describes ``c`` itself (its removal
    failed after the record was written): those lines are in the container's own log.
    Never raises; the record must not be able to break the console.
    """
    try:
        record = lastrun.load(settings, instance_id)
        if record is None or (c is not None and record.get("container_id") == c.id):
            return []
        return lastrun.console_lines(record)
    except Exception:
        log.exception("Reading the last-run record of %s failed", instance_id)
        return []


def _is_gone(exc: APIError) -> bool:
    """Docker's answer to a read of the log of a container that was removed, or is being
    removed, under the request (404, or 409 "... is dead or marked for removal")."""
    return isinstance(exc, NotFound) or exc.status_code in (404, 409)


def console_history(
    instance_id: UUID,
    tail: int = 500,
    since: int | None = None,
    settings: Settings | None = None,
    previous: bool = True,
) -> list[str]:
    """The last ``tail`` lines of console output: the container log, with the end of the
    previous run (the last-run record) in front of it while the log is shorter than ``tail``.

    Without a container the answer is the last-run record alone; without either it is
    404 ``no_container`` as before. A container that is removed while it is read (a stopped
    server's container is retired) counts as "without a container". ``since`` reads the current
    container only, and so does ``previous=False``: the output of this container's own run and
    nothing of the run before it (the panel's diagnostics ask like that, or a server that was
    repaired and recreated would still be diagnosed with the problem of its previous run).
    """
    settings = settings or get_settings()
    c = get_container(instance_id)
    lines: list[str] = []
    if c is not None:
        try:
            lines = _container_log(c, tail, since, settings).splitlines()
        except APIError as exc:
            if not _is_gone(exc):
                raise
            c = None  # removed between the two calls: answer like for a server without one
    if c is None:
        record_lines = [] if since else previous_run_lines(settings, instance_id, None)
        if not record_lines and lastrun.summary(settings, instance_id) is None:
            raise AgentError(
                "The instance has no container; apply its configuration first", "no_container", 404
            )
        return record_lines[-tail:]
    if previous and not since and len(lines) < tail:
        record_lines = previous_run_lines(settings, instance_id, c)
        if record_lines:
            lines = record_lines[-(tail - len(lines)) :] + lines
    return lines


def count_log_lines(c: Container, tail: int) -> int:
    """How many of the last ``tail`` lines the container's log holds (counted, not kept)."""
    stream = c.logs(stream=True, follow=False, stdout=True, stderr=True, timestamps=False, tail=tail)
    count = 0
    last = b"\n"
    try:
        for chunk in stream:
            if chunk:
                count += chunk.count(b"\n")
                last = chunk[-1:]
    finally:
        with contextlib.suppress(Exception):
            stream.close()
    return count if last == b"\n" else count + 1


def running_container(instance_id: UUID) -> Container:
    """The server's container, reloaded and running; 409 ``not_running`` otherwise (a server without
    a container is "not running" like one whose container is stopped)."""
    c = get_container(instance_id)
    if c is None:
        raise AgentError("The server is not running", "not_running", 409)
    try:
        c.reload()
    except NotFound as exc:
        raise AgentError("The server is not running", "not_running", 409) from exc
    if c.status != "running":
        raise AgentError("The server is not running", "not_running", 409)
    return c


def rcon_endpoint(settings: Settings, instance_id: UUID) -> tuple[str, int, str]:
    """Return (host, port, password) for the instance's RCON listener.

    The password is read from the container's own configuration so it never has to
    travel in agent API requests. Where RCON listens and which variable holds the password is
    the container's control driver's (Minecraft: 25575, ``RCON_PASSWORD``).
    """
    return _rcon_target(settings, running_container(instance_id))


def game_endpoint(settings: Settings, instance_id: UUID) -> tuple[str, int]:
    """(host, port) of the running server's status query as the agent reaches it (Minecraft: the
    server list ping on the game port); 409 ``ping_unavailable`` for a game without one."""
    c = running_container(instance_id)
    return drivers.for_container(c).query_endpoint(settings, c)


def _rcon_target(settings: Settings, c: Container) -> tuple[str, int, str]:
    """(host, port, password) from the attributes of a running container that was just reloaded."""
    driver = drivers.for_container(c)
    if not isinstance(driver, drivers.RconDriver):
        raise AgentError("This server's game has no RCON", "rcon_unavailable", 409)
    return driver.endpoint(settings, c)


def docker_info() -> dict[str, Any]:
    info = client().info()
    version = client().version()
    return {
        "server_version": version.get("Version"),
        "api_version": version.get("ApiVersion"),
        "os": info.get("OperatingSystem"),
        "kernel": info.get("KernelVersion"),
        "architecture": info.get("Architecture"),
        "cpus": info.get("NCPU"),
        "memory_bytes": info.get("MemTotal"),
        "storage_driver": info.get("Driver"),
        "cgroup_version": info.get("CgroupVersion"),
        "containers_running": info.get("ContainersRunning"),
        "name": info.get("Name"),
    }
