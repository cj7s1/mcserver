"""HTTP/WebSocket API of the MCPanel node agent.

The agent is the only component with Docker socket access. It must only be
reachable on the internal Compose network; every request needs the shared bearer
token. The API is intentionally narrow: it manages containers created from a
validated spec and files inside instance directories — nothing else on the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import shutil
import stat
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import docker.errors
import requests.exceptions
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketState

from . import (
    __version__,
    backups,
    claimguard,
    claimmarker,
    docker_ops,
    drivers,
    hashcache,
    lastrun,
    maintenance,
    netguard,
    profiles,
    slp,
    storage,
    tls,
)
from .auth import require_token, websocket_authorized
from .backups import Restic, restic_available
from .config import Settings, get_settings
from .docker_ops import AgentError
from .operations import Operation, registry
from .rcon import RconError, run_command
from .safefs import PathError, SafeRoot
from .schemas import (
    ApplySpecRequest,
    BackupRequest,
    CloneRequest,
    CommandRequest,
    DeleteDataRequest,
    DeleteRequest,
    ExtractRequest,
    ForgetRequest,
    GameActionRequest,
    MkdirRequest,
    PrepareInstanceRequest,
    RenameRequest,
    RestorePathRequest,
    RestoreRequest,
    RetireRequest,
    SnapshotRequest,
    StopRequest,
    WorldReplaceRequest,
    WorldResetRequest,
    WriteTextRequest,
)

logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("mcagent")


class SafeJSONResponse(JSONResponse):
    """JSON that cannot fail on a file name that is not valid UTF-8.

    Python hands such names over with lone surrogates, which UTF-8 cannot encode: one odd file
    name would turn a whole folder listing into a 500. They become U+FFFD here (the listings
    also flag such entries, see ``safefs.Entry.as_dict``).
    """

    def render(self, content: Any) -> bytes:
        text = json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"))
        try:
            return text.encode("utf-8")
        except UnicodeEncodeError:
            return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace").encode("utf-8")


app = FastAPI(
    title="MCPanel Agent",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url="/v1/openapi.json",
    default_response_class=SafeJSONResponse,
)
TEXT_LIMIT = 5 * 1024 * 1024
# What this agent can do beyond the first API version, for panels that must work with older
# agents too (GET /v1/info, "features"):
#   retire      POST /instances/{id}/retire and DELETE /instances/{id}/last-run exist, and the status,
#               list, logs and log-stream answers carry the last-run record of a server without a container
#   pull_newer  apply accepts pull="newer" and reports image_id_before, image_id, pulled, warnings
#               and images_removed (superseded runtime images it removed)
#   ping        GET /instances/{id}/ping: server list ping (players online, max, sample, version, motd)
#   clone_level_name  clone takes level_name and layout and leaves out exactly that world
#   games       the spec takes game/variant (absent = minecraft) and selects a runtime profile; GET
#               /v1/info lists the profiles under "games"; a spec of a game without a profile is
#               refused with 400 unknown_game
#   game_actions POST /instances/{id}/game/{action}: operations of a game's own control API through its
#               driver (Satisfactory: claim, client_password, new_game, sessions, save, upload_save, ...);
#               /ping answers through the driver's own query for such games
#   claim_guard the agent never runs a published Satisfactory server unclaimed: apply, /start and /restart
#               answer 409 server_unclaimed (no claim file / token, or the running server is unclaimed) or
#               409 claim_unverified (its API did not answer or answered unexpectedly); a loop stops a
#               running published server that turns out unclaimed, and the status (and each entry of
#               GET /instances, plus "guard_stops" there for servers without a container) carries
#               guard_stop {code, message, at} for the run it stopped
#   archive_excludes  GET /files/archive takes exclude= (repeatable, relative to the server folder): those
#               files and folders are left out of the zip
#   extract_protected  POST /files/extract takes "protected" (files and folders relative to the server
#               folder): an entry that would write one of them, or something inside one, is skipped
#               and reported (protected_count, protected) instead
FEATURES = [
    "retire",
    "pull_newer",
    "ping",
    "clone_level_name",
    "games",
    "game_actions",
    "claim_guard",
    "archive_excludes",
    "extract_protected",
    "claim_marker",
    # backup/restore requests may name the server's game (for a server without container or last-run record)
    "backup_game_hint",
    # a stop may say the server never became ready in this run (StopRequest.not_ready)
    "stop_not_ready",
]
# How long a request waits for the per-server lock (docker_ops.instance_lock) before it answers
# 409 instance_busy. A retire gives up quickly: the panel simply asks again on its next pass.
LIFECYCLE_LOCK_WAIT = 60.0
OPERATION_LOCK_WAIT = 300.0
RETIRE_LOCK_WAIT = 3.0
UPLOAD_LIMIT = int(os.environ.get("AGENT_MAX_UPLOAD_BYTES", str(4 * 1024 * 1024 * 1024)))


# Subnets of the Minecraft instance network(s); None until they could be read from Docker.
_blocked_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None
# Until then: what the guard saw the last time Docker answered, and the subnets this process is
# attached to now (netguard.provisional_verdict). Set at start-up and by the guard loop.
_remembered_networks: dict[str, list[ipaddress.IPv4Network | ipaddress.IPv6Network]] | None = None
_attached_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
GUARD_RETRY_SECONDS = 2.0  # while the subnets are unknown (Docker not reachable)
GUARD_REFRESH_SECONDS = 60.0  # afterwards (a network that is recreated can get another subnet)
# Answered while the subnets are unknown, from any address: it reveals nothing.
_GUARD_EXEMPT_PATHS = frozenset({"/v1/health"})


def _load_blocked_networks(settings: Settings) -> bool:
    """Read the subnets of the Minecraft instance network(s): requests from there are refused.

    Minecraft containers run third-party code and share a network with the agent (needed
    for RCON); they must never be able to talk to the agent API, token or not. A network
    that does not exist has no members and blocks nothing; any other Docker error leaves
    the previous list in place and returns False (the caller tries again). What was read is
    stored (``netguard.remember``) for a later start at which Docker does not answer.
    """
    global _blocked_networks
    nets = []
    for name in sorted({settings.instance_network, *settings.allowed_networks} - {"host"}):
        try:
            net = docker_ops.client().networks.get(name)
        except docker.errors.NotFound:
            continue
        except Exception as exc:  # Docker not reachable (yet)
            log.debug("cannot inspect network %s: %s", name, exc)
            return False
        for cfg in (net.attrs.get("IPAM") or {}).get("Config") or []:
            with contextlib.suppress(ValueError, TypeError):
                nets.append(ipaddress.ip_network(cfg.get("Subnet"), strict=False))
    if _blocked_networks is None or set(nets) != set(_blocked_networks):
        log.info(
            "Refusing agent API requests from instance networks: %s",
            ", ".join(map(str, nets)) or "(none yet)",
        )
    _blocked_networks = nets
    netguard.remember(settings.state_dir, nets, netguard.attached_subnets())
    return True


def _prepare_guard(settings: Settings) -> None:
    """Start-up, before the first request: what the guard goes by until Docker answers."""
    global _blocked_networks, _remembered_networks, _attached_networks
    _blocked_networks = None
    _remembered_networks = netguard.recall(settings.state_dir)
    _attached_networks = netguard.attached_subnets()


def _guard_loop(settings: Settings, stop: threading.Event) -> None:
    """Load the instance-network subnets, retrying until Docker answers, then keep them current."""
    global _attached_networks
    warned = False
    while not stop.is_set():
        try:
            docker_ops.ensure_network(settings)
        except Exception as exc:
            log.debug("Could not ensure instance network: %s", exc)
        loaded = _load_blocked_networks(settings)
        if not loaded and _blocked_networks is None:
            _attached_networks = netguard.attached_subnets()
        if not loaded and not warned:
            log.warning(
                "Docker is not reachable: until it is, the agent refuses API requests from networks it "
                "cannot tell apart from the instance networks (retrying every %.0f s)",
                GUARD_RETRY_SECONDS,
            )
            warned = True
        if loaded and warned:
            log.info("Docker is reachable again; the instance-network guard is up to date")
            warned = False
        stop.wait(GUARD_REFRESH_SECONDS if _blocked_networks is not None else GUARD_RETRY_SECONDS)


def _source_verdict(client: Any, path: str = "") -> str | None:
    """None when the request may pass; "blocked" for an address on an instance network;
    "pending" while those networks are not known yet and the address cannot be told apart
    from one (see ``netguard``: loopback, the health probe and the panel's own network pass)."""
    client_ip = client[0] if client else None
    try:
        ip = ipaddress.ip_address(client_ip) if client_ip else None
    except ValueError:
        ip = None
    if ip is None:  # a Unix socket or an in-process client: not from a container network
        return None
    nets = _blocked_networks
    if nets is None:
        if ip.is_loopback or path in _GUARD_EXEMPT_PATHS:
            return None
        return netguard.provisional_verdict(ip, _remembered_networks, _attached_networks)
    return "blocked" if any(ip in net for net in nets) else None


class _RefuseInstanceNetworks:
    """Refuse HTTP requests *and* WebSocket handshakes from instance networks (pure ASGI).

    A ``@app.middleware("http")`` function never sees WebSocket connections, so the
    log stream would otherwise be reachable from a Minecraft container. While the instance
    networks are not known (Docker was not reachable yet), a request from an address that
    cannot be told apart from them is refused with 503 ``agent_starting`` (``netguard``).
    """

    def __init__(self, asgi_app: Any) -> None:
        self.app = asgi_app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            verdict = _source_verdict(scope.get("client"), scope.get("path", ""))
            if verdict:
                client = (scope.get("client") or ["?"])[0]
                if verdict == "blocked":
                    log.warning(
                        "Refused agent API %s from instance network address %s", scope["type"], client
                    )
                    body, status, ws_code = {"code": "forbidden_source", "message": "Forbidden"}, 403, 4403
                else:
                    body = {
                        "code": "agent_starting",
                        "message": "The node agent cannot reach Docker yet, so it cannot tell which "
                        "networks may use it; try again shortly",
                    }
                    status, ws_code = 503, 1013
                if scope["type"] == "http":
                    response = JSONResponse({"error": body}, status_code=status)
                    await response(scope, receive, send)
                else:  # closing before accept: the handshake is answered with HTTP 403
                    await WebSocket(scope, receive, send).close(code=ws_code)
                return
        await self.app(scope, receive, send)


app.add_middleware(_RefuseInstanceNetworks)


@app.exception_handler(AgentError)
async def _agent_error(_request: Request, exc: AgentError) -> JSONResponse:
    return SafeJSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)


@app.exception_handler(requests.exceptions.ConnectionError)
async def _docker_connection_error(_request: Request, _exc: Exception) -> JSONResponse:
    # docker-py surfaces a vanished/unreachable daemon socket as a requests ConnectionError
    return JSONResponse(
        {
            "error": {
                "code": "docker_unavailable",
                "message": "The Docker daemon on this node is not reachable",
            }
        },
        status_code=503,
    )


@app.exception_handler(docker.errors.DockerException)
async def _docker_error(_request: Request, exc: docker.errors.DockerException) -> JSONResponse:
    if isinstance(exc, docker.errors.APIError):
        return JSONResponse(
            {"error": {"code": "docker_error", "message": f"Docker error: {exc.explanation or exc}"}},
            status_code=502,
        )
    return JSONResponse(
        {
            "error": {
                "code": "docker_unavailable",
                "message": "The Docker daemon on this node is not reachable",
            }
        },
        status_code=503,
    )


@app.exception_handler(PathError)
async def _path_error(_request: Request, exc: PathError) -> JSONResponse:
    return SafeJSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)


# Background work of this process (source guard, start-up maintenance); stopped at shutdown.
_background_stop = threading.Event()
SHUTDOWN_RESTIC_GRACE = 4.0  # seconds restic children get to exit (and drop their locks) at shutdown
SHUTDOWN_SAVE_ON_TIMEOUT = 3.0  # per server, all at once


def _spawn(name: str, target: Any, *args: Any) -> threading.Thread:
    t = threading.Thread(target=target, args=args, name=name, daemon=True)
    t.start()
    return t


def _busy(instance_id: UUID) -> bool:
    return registry.active_for(instance_id) is not None


@app.on_event("startup")
async def _startup() -> None:
    settings = get_settings()
    started_at = time.time()
    for d in (
        settings.instances_dir,
        settings.backups_dir,
        settings.staging_dir,
        settings.trash_dir,
        settings.state_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    # nobody but root traverses the data root (server folders hold RCON passwords)
    await run_in_threadpool(maintenance.restrict_data_root, settings)
    # runs seen ready before a restart stay ready (stops keep their save, the log probe needs no new look)
    await run_in_threadpool(docker_ops.load_ready_marks, settings)
    if not settings.resolve_token():
        log.error("AGENT_TOKEN is not configured; all requests will be rejected")
    # Before the first request: restic commands wait until their repository has been unlocked.
    repos = await run_in_threadpool(backups.prepare_startup_unlock, settings)
    _background_stop.clear()
    await run_in_threadpool(_prepare_guard, settings)
    try:
        await run_in_threadpool(docker_ops.ensure_network, settings)
        await run_in_threadpool(_load_blocked_networks, settings)
    except Exception as exc:  # docker not reachable yet; the guard loop keeps trying
        log.warning("Could not ensure instance network: %s", exc)
    _spawn("agent-source-guard", _guard_loop, settings, _background_stop)
    _spawn("restic-startup-unlock", backups.startup_unlock, settings, repos)
    _spawn("leftover-sweep", maintenance.sweep_leftovers, settings, started_at, _busy)
    _spawn("saving-recovery", _saving_recovery_loop, settings, _background_stop)
    _spawn("claim-guard", claimguard.run, settings, _background_stop)


# At most one background pass per folder (four at a time) for /files/hashes calls.
_hash_jobs = hashcache.HashJobs(workers=4)


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _hash_jobs
    _background_stop.set()
    _hash_jobs.shutdown()  # stop background hashing so the process can exit promptly
    _hash_jobs = hashcache.HashJobs(workers=4)
    settings = get_settings()
    # World saving first (in parallel threads), while restic children get SIGINT and so remove
    # their locks: the operation threads that started them die with this process.
    saving = _spawn("shutdown-save-on", _resume_all_saving, settings, SHUTDOWN_SAVE_ON_TIMEOUT)
    await run_in_threadpool(backups.stop_children, SHUTDOWN_RESTIC_GRACE)
    await run_in_threadpool(saving.join, SHUTDOWN_SAVE_ON_TIMEOUT + 2)


def _root(settings: Settings, instance_id: UUID) -> SafeRoot:
    """The server folder for the file API: the claim marker and the start wrapper cannot be written
    through it, and a change under ``saved/`` removes the claim marker (``claimmarker.GuardedRoot``).
    For a game with protected paths (CS2's start hooks) those are refused too, and files are
    written as the game's owner (CS2: 1000:1000; every other game: the runtime uid/gid)."""
    uid, gid = docker_ops.server_owner(settings, instance_id)
    return claimmarker.GuardedRoot(
        docker_ops.instance_dir(settings, instance_id),
        uid,
        gid,
        settings=settings,
        instance_id=instance_id,
        protected=docker_ops.server_protected_paths(settings, instance_id),
    )


def _with_game_protected(settings: Settings, instance_id: UUID, protected: Any) -> Any:
    """The panel's protected paths of an extraction plus the game's own (none for most games: the
    panel's list is passed on unchanged then)."""
    own = docker_ops.server_protected_paths(settings, instance_id)
    if not own:
        return protected
    return [*(protected or []), *(p for p in own if p not in (protected or []))]


def _spec_hash(spec: Any) -> str:
    return hashlib.sha256(json.dumps(spec.hash_payload(), sort_keys=True).encode()).hexdigest()[:16]


# --- health / host -------------------------------------------------------------------
@app.get("/v1/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Unauthenticated liveness probe (reveals no host details)."""
    try:
        await run_in_threadpool(lambda: docker_ops.client().ping())
        docker_ok = True
    except Exception:
        docker_ok = False
    return {"status": "ok" if docker_ok else "degraded", "docker": docker_ok, "version": __version__}


def _tls_fingerprint(settings: Settings) -> str | None:
    try:
        return tls.server_fingerprint(settings)
    except (tls.TlsConfigError, OSError, ValueError):
        return None


@app.get("/v1/info", dependencies=[Depends(require_token)])
async def info(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    docker_info = await run_in_threadpool(docker_ops.docker_info)
    caps = await run_in_threadpool(storage.capabilities, settings)
    return {
        "version": __version__,
        # what this agent does when it creates a container (docker_ops.CONTAINER_FORMAT)
        "container_format": docker_ops.CONTAINER_FORMAT,
        "features": list(FEATURES),
        "docker": docker_info,
        "restic": await run_in_threadpool(restic_available, settings),
        "storage": caps,
        "tls": {"mode": settings.tls, "fingerprint": await run_in_threadpool(_tls_fingerprint, settings)},
        "policy": {
            "allowed_images": settings.allowed_images,
            "networks": sorted(settings.effective_networks()),
            "port_min": settings.port_min,
            "port_max": settings.port_max,
            "max_memory_mb": settings.max_memory_mb,
            "runtime_uid": settings.runtime_uid,
            "runtime_gid": settings.runtime_gid,
        },
        # the runtime profiles: {game: {"variants": [...], "images": [...], "control": driver}}
        "games": profiles.info(settings),
    }


@app.get("/v1/ports", dependencies=[Depends(require_token)])
async def ports() -> dict[str, Any]:
    return {"ports": await run_in_threadpool(docker_ops.published_ports)}


# --- instances: lifecycle ------------------------------------------------------------
@app.get("/v1/instances", dependencies=[Depends(require_token)])
async def list_instances(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Every managed container, and under ``last_runs`` the last-run record (without console
    lines) of every server that has one, by instance id: a stopped server has no container."""
    containers = await run_in_threadpool(docker_ops.list_managed)
    last_runs = await run_in_threadpool(lastrun.summaries, settings)
    answer: dict[str, Any] = {"containers": containers, "last_runs": last_runs}
    # the claim guard's reason for servers without a container (feature claim_guard; only when any)
    stops = await run_in_threadpool(
        _guard_stops_without_container, settings, {str(c.get("instance_id")) for c in containers}
    )
    if stops:
        answer["guard_stops"] = stops
    return answer


def _guard_stops_without_container(settings: Settings, with_container: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        names = os.listdir(claimguard.directory(settings))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name[:-5] in with_container:
            continue
        try:
            instance_id = UUID(name[:-5])
        except ValueError:
            continue
        stopped = claimguard.for_status(settings, instance_id, None)
        if stopped is not None:
            out[str(instance_id)] = stopped
    return out


@app.post("/v1/instances/{instance_id}/prepare", dependencies=[Depends(require_token)])
async def prepare_instance(
    instance_id: UUID, body: PrepareInstanceRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Create the instance directory and write initial files that do not exist yet."""

    def _do() -> dict[str, Any]:
        from .snapshots import ensure_instance_dir

        path = docker_ops.instance_dir(settings, instance_id)
        created = not path.exists()
        ensure_instance_dir(settings, path)
        with contextlib.suppress(PermissionError):
            os.chown(path, settings.runtime_uid, settings.runtime_gid)
        root = _root(settings, instance_id)
        written = []
        for rel, content in body.files.items():
            if not root.exists(rel):
                parent = "/".join(rel.split("/")[:-1])
                if parent:
                    root.mkdir(parent, parents=True)
                root.write_bytes(rel, content.encode())
                written.append(rel)
        return {"created": created, "written": written}

    return await run_in_threadpool(_do)


PULL_NEWER_TIMEOUT = 180.0  # pull="newer": how long the check for a newer image may take


def _pull_for_apply(
    op: Operation, image: str, pull: str, warnings: list[str], settings: Settings | None = None
) -> bool:
    """Make the image available as ``pull`` asks; returns whether a pull succeeded.

    ``newer`` asks the registry at most once per tag per ``docker_ops.PULL_CHECK_INTERVAL``
    (24 h): every successful pull of the tag is remembered in the state folder, a failed check
    is not (the next start tries again)."""
    settings = settings or get_settings()
    present = docker_ops.local_image_id(image) is not None

    def progress(p: float, m: str) -> None:
        op.update(0.05 + p * 0.8, m)

    if pull == "never":
        if not present:
            raise AgentError(f"Image {image} is not present and pulling is disabled", "image_missing", 409)
        return False
    if pull == "always" or not present:
        op.update(0.05, f"Pulling image {image}")
        docker_ops.pull_image(image, progress)
        docker_ops.remember_pull(settings, image)
        return True
    if pull == "newer":
        checked = docker_ops.recently_checked(settings, image)
        if checked is not None:
            log.info(
                "Apply: %s was checked for a newer build %d minutes ago; the image on this host is used",
                image,
                (time.time() - checked) // 60,
            )
            return False
        op.update(0.05, f"Checking for a newer {image}")
        try:
            docker_ops.pull_image_bounded(image, PULL_NEWER_TIMEOUT, progress)
            docker_ops.remember_pull(settings, image)
            return True
        except Exception as exc:  # no internet, registry error, timeout: the local image will do
            text = str(exc) or type(exc).__name__
            warnings.append(f"Could not check for a newer {image} ({text}); the image on this host is used")
            log.warning("Apply: %s", warnings[-1])
    return False


@app.put("/v1/instances/{instance_id}/container", dependencies=[Depends(require_token)])
async def apply_spec(
    instance_id: UUID, body: ApplySpecRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """(Re)create the container from a spec as an operation. World data is never touched.

    A container that exists is replaced only once its successor has been created (under a
    temporary name, renamed afterwards): a spec Docker refuses leaves the old container as it was.
    The result says which image the server ran before (``image_id_before``: the old container's,
    else the one of its last run, else what the tag pointed to before a pull) and runs now
    (``image_id``), whether a pull succeeded (``pulled``), and lists superseded runtime images it
    removed (``images_removed``) and ``warnings``.
    """
    profile = profiles.for_spec(body.spec)  # 400 unknown_game
    image = docker_ops.validate_image(settings, body.spec.image, profile)
    docker_ops.build_create_kwargs(settings, instance_id, body.spec, "validate")  # validate before queuing
    registry.assert_lifecycle_allowed(instance_id)

    def _apply(op: Operation) -> dict[str, Any]:
        warnings: list[str] = []
        tag_before = docker_ops.local_image_id(image)
        pulled = _pull_for_apply(op, image, body.pull, warnings, settings)
        if body.spec.cpu_limit:
            cpus = docker_ops.host_cpus()
            if cpus is not None and body.spec.cpu_limit > cpus:
                raise AgentError(
                    f"The CPU limit {body.spec.cpu_limit:g} is more than the {cpus} CPUs of this host",
                    "cpu_limit",
                    400,
                )
        from .snapshots import ensure_instance_dir

        path = docker_ops.instance_dir(settings, instance_id)
        ensure_instance_dir(settings, path)
        with contextlib.suppress(PermissionError):
            os.chown(path, settings.runtime_uid, settings.runtime_gid)
        # one step for a retire: the container it looks at is not replaced under it
        with docker_ops.instance_lock(instance_id, wait=OPERATION_LOCK_WAIT):
            existing = docker_ops.get_container(instance_id)
            was_running = False
            if existing is not None:
                try:
                    existing.reload()
                except docker.errors.NotFound:
                    existing = None  # removed from outside the agent between the two requests
            previous_image = (existing.attrs.get("Image") or None) if existing is not None else None
            previous_ref = (
                ((existing.attrs.get("Config") or {}).get("Image")) if existing is not None else None
            )
            if previous_image is None:
                last = lastrun.summary(settings, instance_id) or {}
                previous_image, previous_ref = last.get("image_id"), last.get("image")
            docker_ops.remove_stale_replacement(instance_id)
            if body.spec.ports and (body.spec.network or settings.instance_network) != "host":
                # e.g. Satisfactory: a server is never published before it is claimed
                drivers.for_profile(profile).publish_guard(settings, instance_id, existing)
            if existing is not None:
                was_running = existing.status in ("running", "restarting")
                op.update(0.88, "Creating the new container")
                c = docker_ops.create_container(
                    settings,
                    instance_id,
                    body.spec,
                    _spec_hash(body.spec),
                    name=docker_ops.replacement_name(instance_id),
                )
                try:
                    op.update(0.9, "Stopping and removing previous container (data is kept)")
                    docker_ops.remove_container(
                        instance_id, timeout=body.spec.stop_timeout, settings=settings
                    )
                    docker_ops.rename_container(c, docker_ops.container_name(instance_id))
                except BaseException:
                    with contextlib.suppress(Exception):
                        c.remove(v=False, force=True)
                    raise
            else:
                op.update(0.92, "Creating container")
                c = docker_ops.create_container(settings, instance_id, body.spec, _spec_hash(body.spec))
            # a new container: what the claim guard recorded about the old one's run is history now
            claimguard.clear(settings, instance_id)
            if body.start or was_running:
                op.update(0.96, "Starting container")
                docker_ops.start(instance_id)
            c.reload()
            described = docker_ops.describe(c)
        image_id = described.get("image_id")
        removed: list[str] = []
        try:
            # each with the reference it was known by (see remove_superseded_images)
            candidates = {previous_image: previous_ref, tag_before: image}
            candidates = {k: v for k, v in candidates.items() if k and k != image_id}
            if candidates:
                removed = docker_ops.remove_superseded_images(settings, candidates)
        except Exception as exc:  # tidying up never fails the apply
            log.warning("Removing superseded runtime images failed: %s", exc)
        return {
            "container": described,
            "recreated": existing is not None,
            # how this container was created; the panel stores it with the spec hash
            "container_format": docker_ops.CONTAINER_FORMAT,
            "image_id_before": previous_image or tag_before,
            "image_id": image_id,
            "pulled": pulled,
            "images_removed": removed,
            "warnings": warnings,
        }

    op = registry.start("apply", [instance_id], _apply)
    return {"operation": op.as_dict()}


@app.delete("/v1/instances/{instance_id}/container", dependencies=[Depends(require_token)])
async def remove_container(
    instance_id: UUID, timeout: int = Query(60, ge=1, le=900), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Stop and remove the container (archive, migration). The last-run record of a container
    that has run is saved first, best effort (``docker_ops.remove_container``). A replacement
    container an interrupted apply left (``<name>-replacement``) goes too: nothing of the server
    stays behind, and an older agent after a rollback never finds it."""
    registry.assert_lifecycle_allowed(instance_id)

    def _remove() -> bool:
        with docker_ops.instance_lock(instance_id, wait=LIFECYCLE_LOCK_WAIT):
            docker_ops.remove_stale_replacement(instance_id)
            return docker_ops.remove_container(instance_id, timeout=timeout, settings=settings)

    return {"removed": await run_in_threadpool(_remove)}


@app.post("/v1/instances/{instance_id}/retire", dependencies=[Depends(require_token)])
async def retire(
    instance_id: UUID, body: RetireRequest | None = None, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Remove the container of a stopped server after saving its last-run record.

    One step under the per-server lock (``docker_ops.retire``): a running container is refused
    (409 ``container_running``), one that stopped less than ``settle_seconds`` ago too
    (409 ``not_settled``); the record is written before the container is removed, and when it
    cannot be written the container stays (500 ``last_run_failed``). No container at all is
    success. The answer says what happened and carries ``last_run``, the record without its
    console lines (null when the server has none).
    """
    registry.assert_lifecycle_allowed(instance_id)
    settle = body.settle_seconds if body else RetireRequest().settle_seconds

    def _retire() -> dict[str, Any]:
        with docker_ops.instance_lock(instance_id, wait=RETIRE_LOCK_WAIT):
            registry.assert_lifecycle_allowed(instance_id)
            result = docker_ops.retire(settings, instance_id, settle)
        result["last_run"] = lastrun.summary(settings, instance_id)
        return result

    return await run_in_threadpool(_retire)


@app.delete("/v1/instances/{instance_id}/last-run", dependencies=[Depends(require_token)])
async def forget_last_run(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Delete the last-run record (the server moved to another node; its history went with it)."""
    return {"deleted": await run_in_threadpool(lastrun.delete, settings, instance_id)}


@app.get("/v1/instances/{instance_id}/status", dependencies=[Depends(require_token)])
async def instance_status(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """The container's state; for a server without a container ``exists`` is false and
    ``last_run`` holds its last-run record without the console lines (absent: it never ran).
    ``guard_stop`` (``{"code", "message", "at"}``, absent otherwise): the claim guard stopped the
    container's latest run, or the last run (``claimguard``; codes ``server_unclaimed``,
    ``claim_unverified``)."""
    st = await run_in_threadpool(docker_ops.status, instance_id)
    active = registry.active_for(instance_id)
    st["operation"] = active.as_dict() if active else None
    return st


@app.post("/v1/instances/{instance_id}/start", dependencies=[Depends(require_token)])
async def start(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Start the existing container. A server without one (404 ``no_container``) is started
    with an apply, which creates it. 409 ``server_unclaimed`` / ``claim_unverified``: a container
    with published ports of a server that is not claimed (``docker_ops.start_guard``); 409
    ``volume_source_invalid``: a folder it mounts from the server folder was replaced."""
    registry.assert_lifecycle_allowed(instance_id)

    def _start() -> None:
        with docker_ops.instance_lock(instance_id, wait=LIFECYCLE_LOCK_WAIT):
            docker_ops.start(instance_id)

    await run_in_threadpool(_start)
    return await run_in_threadpool(docker_ops.status, instance_id)


@app.post("/v1/instances/{instance_id}/stop", dependencies=[Depends(require_token)])
async def stop(
    instance_id: UUID, body: StopRequest | None = None, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Graceful stop (``docker_ops.graceful_stop``): flush, ``docker stop``, RCON ``stop`` if needed.

    Answers with the container status as before, plus ``stop``: what the stop did
    (``flushed``, ``stop_sent_via_rcon``, ``warnings``, ``exit_code`` of the run it ended; null
    when the container was not running). It can take the stop timeout plus the flush
    (``docker_ops.flush_timeout``) plus a few seconds.
    """
    registry.assert_lifecycle_allowed(instance_id)
    result = await run_in_threadpool(
        lambda: docker_ops.stop(
            instance_id, body.timeout if body else None, settings, not_ready=bool(body and body.not_ready)
        )
    )
    status = await run_in_threadpool(docker_ops.status, instance_id)
    status["stop"] = result.as_dict()
    return status


@app.post("/v1/instances/{instance_id}/restart", dependencies=[Depends(require_token)])
async def restart(
    instance_id: UUID, body: StopRequest | None = None, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """The graceful stop of ``/stop``, then start; the answer carries the same ``stop`` field.

    The container status in the answer describes the new run (``exit_code`` is 0 again), so the
    exit code of the run that was stopped is in ``stop.exit_code``.
    """
    registry.assert_lifecycle_allowed(instance_id)

    def _restart() -> docker_ops.StopResult:
        # stop and start of the same container: a retire must not remove it in between
        with docker_ops.instance_lock(instance_id, wait=LIFECYCLE_LOCK_WAIT):
            # refused before the stop when the start would be (server folder only: a hung server's
            # API must not keep it from being restarted), so a refused restart leaves it running
            docker_ops.start_guard(
                settings, instance_id, docker_ops.require_container(instance_id), live=False
            )
            stopped = docker_ops.stop(
                instance_id, body.timeout if body else None, settings, not_ready=bool(body and body.not_ready)
            )
            docker_ops.start(instance_id)
            return stopped

    result = await run_in_threadpool(_restart)
    status = await run_in_threadpool(docker_ops.status, instance_id)
    status["stop"] = result.as_dict()
    return status


@app.post("/v1/instances/{instance_id}/kill", dependencies=[Depends(require_token)])
async def kill(instance_id: UUID) -> dict[str, Any]:
    """Kill the server process. Takes no lock and removes nothing, so it also works while a
    stop or a restart is in progress; a server that is not running is left alone."""
    await run_in_threadpool(docker_ops.kill, instance_id)
    return await run_in_threadpool(docker_ops.status, instance_id)


@app.get("/v1/instances/{instance_id}/stats", dependencies=[Depends(require_token)])
async def stats(instance_id: UUID) -> dict[str, Any]:
    return await run_in_threadpool(docker_ops.stats, instance_id)


PING_TIMEOUT = 5.0


@app.get("/v1/instances/{instance_id}/ping", dependencies=[Depends(require_token)])
async def ping(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Server list ping of the running server (what the game's server list shows; no RCON login,
    so nothing appears in the server's console): ``online``, ``max``, ``sample`` (player names),
    ``version``, ``motd`` (plain text) and ``latency_ms``. 409 ``not_running`` without a running
    container, 504 ``ping_timeout`` after 5 s, 502 ``ping_failed`` when it does not answer the
    protocol (still starting)."""
    # the container's driver says where its query listens (409 ping_unavailable without one); the
    # query itself is the server list ping, the only one there is so far (Minecraft's)
    target = await run_in_threadpool(docker_ops.game_endpoint, settings, instance_id)
    host, port = target
    own_query = getattr(target, "query", None)  # a driver with a query of its own (drivers.QueryTarget)
    if own_query is not None:
        try:
            return await asyncio.wait_for(own_query(PING_TIMEOUT), PING_TIMEOUT + 1)
        except TimeoutError as exc:
            raise AgentError(
                "The server did not answer the status query in time", "ping_timeout", 504
            ) from exc
    try:
        return await asyncio.wait_for(slp.ping(host, port, timeout=PING_TIMEOUT), PING_TIMEOUT)
    except TimeoutError as exc:
        raise AgentError(
            "The server did not answer the server list ping in time", "ping_timeout", 504
        ) from exc
    except (OSError, slp.SlpError, asyncio.IncompleteReadError) as exc:
        raise AgentError(
            f"The server did not answer the server list ping: {exc}", "ping_failed", 502
        ) from exc


@app.get("/v1/instances/{instance_id}/disk", dependencies=[Depends(require_token)])
async def disk(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return await run_in_threadpool(storage.instance_usage, settings, instance_id)


@app.post("/v1/instances/{instance_id}/command", dependencies=[Depends(require_token)])
async def command(
    instance_id: UUID, body: CommandRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """One console command through the container's control driver (Minecraft: RCON); its output."""
    c = await run_in_threadpool(docker_ops.running_container, instance_id)
    try:
        output = await drivers.for_container(c).command(settings, c, body.command, body.timeout)
    except RconError as exc:
        raise AgentError(str(exc), "rcon_failed", 502) from exc
    return {"output": output}


GAME_ACTION_TIMEOUT = 600.0


@app.delete("/v1/instances/{instance_id}/claim-marker", dependencies=[Depends(require_token)])
async def forget_claim(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Remove the claim marker (feature ``claim_marker``): the panel lost track of the claim
    (``mark_unclaimed``), so no published start happens before its setup verified the claim again on
    an unpublished container (``claimmarker``). Answers whether there was one."""
    removed = await run_in_threadpool(
        claimmarker.remove, settings, instance_id, "the panel asked for the claim to be verified again"
    )
    return {"removed": removed}


@app.post("/v1/instances/{instance_id}/game/{action}", dependencies=[Depends(require_token)])
async def game_action(
    instance_id: UUID,
    action: str = PathParam(pattern=r"^[a-z][a-z0-9_]{0,31}$"),
    body: GameActionRequest | None = None,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """An operation of the running server's own control API, through its driver (feature
    ``game_actions``; Satisfactory: ``claim``, ``verify_claim``, ``client_password``, ``new_game``,
    ``sessions``, ``save``, ``upload_save``, ``load_game``, ``options``, ``apply_options``, ``status``;
    the ones that change the game's settings file take ``admin_password`` and write the claim
    marker again, see ``claimmarker``). The driver
    validates ``params``; 409 ``not_running`` without a running server, 409 ``action_unsupported``
    for an operation its game does not have. Bounded by ``timeout`` (at most 600 s)."""
    request = body or GameActionRequest()
    c = await run_in_threadpool(docker_ops.running_container, instance_id)
    driver = drivers.for_container(c)
    timeout = min(request.timeout, GAME_ACTION_TIMEOUT)
    result = await run_in_threadpool(driver.game_action, settings, c, action, dict(request.params), timeout)
    return {"result": result}


@app.delete("/v1/instances/{instance_id}/data", dependencies=[Depends(require_token)])
async def delete_data(
    instance_id: UUID, body: DeleteDataRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Permanently delete an instance's container, last-run record, files, snapshots and backup repository."""
    from .snapshots import snapshot_provider

    if body.confirm != instance_id:
        raise AgentError("Confirmation id does not match", "confirmation_mismatch", 400)

    def _delete(op: Operation) -> dict[str, Any]:
        op.update(0.1, "Removing container")
        with docker_ops.instance_lock(instance_id, wait=OPERATION_LOCK_WAIT):
            docker_ops.remove_stale_replacement(instance_id)  # left by an interrupted apply
            docker_ops.remove_container(instance_id, timeout=30, settings=settings, save_last_run=False)
        record_warning = None
        with contextlib.suppress(OSError):
            docker_ops.forget_server_game(settings, instance_id)  # only some games have one
        try:
            lastrun.delete(settings, instance_id)
        except OSError as exc:
            record_warning = (
                f"Could not delete the saved console output of the server's last run "
                f"({lastrun.path(settings, instance_id)}): {exc}"
            )
        # Snapshots go before the files. On btrfs this never fails the delete: a snapshot or a
        # folder left by a rollback that cannot be removed stays on disk and is named in the
        # result (warnings, leftovers), and the server is deleted all the same. On ZFS it
        # destroys the server's dataset itself (with its history datasets), because a mount point
        # cannot be renamed into the trash; if ZFS refuses, the operation fails, what was not
        # destroyed is still there and the delete can be retried.
        op.update(0.2, "Deleting filesystem snapshots")
        snapshots = snapshot_provider(settings, instance_id).destroy_all()
        warnings = list(snapshots.get("warnings") or [])
        leftovers = list(snapshots.get("leftovers") or [])
        if record_warning:
            warnings.append(record_warning)
            leftovers.append(str(lastrun.path(settings, instance_id)))

        def remove(tree: Path, what: str) -> None:
            shutil.rmtree(tree, ignore_errors=True)
            if os.path.lexists(tree):
                leftovers.append(str(tree))
                warnings.append(
                    f"Could not delete all of {what}: the rest is in {tree}. It still uses disk space; "
                    "delete it on the host"
                )

        path = docker_ops.instance_dir(settings, instance_id)
        if path.exists():
            op.update(0.3, "Deleting instance files")
            trash = settings.trash_dir / f"{instance_id}-{int(time.time())}"
            path.rename(trash)
            remove(trash, "the server's files")
        hashcache.HashCache(settings.hash_cache_dir).forget(instance_id)
        repo = settings.backups_dir / "restic" / str(instance_id)
        if repo.exists():
            op.update(0.8, "Deleting backup repository")
            remove(repo, "the backup repository")
        for warning in warnings:
            log.warning("Deleting %s: %s", instance_id, warning)
        return {
            "deleted": True,
            "snapshots_deleted": snapshots["snapshots"],
            "warnings": warnings,
            # paths (as the agent sees them) of what is still on disk
            "leftovers": leftovers,
        }

    op = registry.start("delete_data", [instance_id], _delete)
    return {"operation": op.as_dict()}


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    """Give ``root`` and everything below it to ``uid:gid`` (links themselves, never followed).

    A copy made by the agent (root) belongs to root; the server runs as the runtime user and
    could not write into it. The itzg entrypoint only fixes ownership when the top folder's
    owner differs, which apply_spec sets, so the copy must be right by itself.
    """
    try:
        os.chown(root, uid, gid, follow_symlinks=False)
        for _dirpath, dirnames, filenames, dir_fd in os.fwalk(root, follow_symlinks=False):
            for name in (*dirnames, *filenames):
                with contextlib.suppress(FileNotFoundError):
                    os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
    except PermissionError as exc:  # an agent that does not run as root (development)
        log.warning("Could not give the copied files to %s:%s: %s", uid, gid, exc)


_LEGACY_WORLD_FOLDERS = frozenset({"world", "world_nether", "world_the_end"})


def _holds_a_world(path: str, name: str) -> bool:
    """A top-level entry a clone without worlds leaves out besides the current world's folders:
    the agent's world staging folders (``.mcpanel-world-*``: the world a failed reset or replace
    moved aside, an import being extracted) and every folder that directly holds a ``level.dat``
    (an older world, a world kept under another name). Links are never followed."""
    if name.startswith(".mcpanel-world-"):
        return True
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        return stat.S_ISREG(os.lstat(os.path.join(path, "level.dat")).st_mode)
    except OSError:
        return False


@app.post("/v1/instances/{instance_id}/clone", dependencies=[Depends(require_token)])
async def clone(
    instance_id: UUID, body: CloneRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Copy a stopped server's folder into a new server's folder (an operation).

    With ``exclude_worlds`` the world is left out: with ``level_name`` (and ``layout``) exactly
    the folders of that world at the top of the server folder (``world.world_paths``), without
    it the folders world, world_nether and world_the_end wherever they are (older panels); in
    both cases also every top-level folder that directly holds a ``level.dat`` and the agent's
    ``.mcpanel-world-*`` folders (:func:`_holds_a_world`). The
    copy belongs to the runtime user like every other server file; FIFOs, sockets and devices
    are never copied. The result lists the left-out folders as ``skipped_worlds``.
    """
    from . import world

    src = docker_ops.instance_dir(settings, instance_id)
    dst = docker_ops.instance_dir(settings, body.target_instance_id)
    level_skips: dict[str, set[str]] | None = None
    if body.exclude_worlds and body.level_name is not None:
        parts = world.level_parts(body.level_name)  # refuse bad names before starting an operation
        level_skips = {}
        for _role, rel in world.world_paths(parts, body.layout or "vanilla"):
            *parent, name = rel.split("/")
            level_skips.setdefault("/".join(parent), set()).add(name)
    if not src.exists():
        raise AgentError("Source instance has no data directory", "not_found", 404)
    if dst.exists() and any(dst.iterdir()):
        raise AgentError("Target instance directory is not empty", "target_not_empty", 409)
    status = await run_in_threadpool(docker_ops.status, instance_id)
    if status.get("running"):
        raise AgentError("Stop the source server before cloning it", "source_running", 409)

    def _clone(op: Operation) -> dict[str, Any]:
        from .snapshots import create_empty_dir, prepare_replacement, remove_tree

        skipped_worlds: list[str] = []
        src_text = str(src)

        def ignore(directory: str, names: list[str]) -> set[str]:
            rel = os.path.relpath(directory, src_text)
            rel = "" if rel == "." else rel.replace(os.sep, "/")
            if level_skips is not None:
                worlds = level_skips.get(rel, set()) & set(names)
            elif body.exclude_worlds:
                worlds = _LEGACY_WORLD_FOLDERS & set(names)
            else:
                worlds = set()
            if body.exclude_worlds and rel == "":
                worlds |= {n for n in names if _holds_a_world(os.path.join(directory, n), n)}
            skipped_worlds.extend(sorted("/".join(p for p in (rel, n) if p) for n in worlds))
            skipped = set(worlds)
            for name in names:  # FIFOs, sockets and devices (a mod can mkfifo) are never copied
                try:
                    mode = os.lstat(os.path.join(directory, name)).st_mode
                except OSError:
                    continue
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                    skipped.add(name)
            return skipped

        def result(extra: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {"bytes": storage.dir_size(dst), **extra}
            if body.exclude_worlds:
                out["skipped_worlds"] = skipped_worlds
            return out

        op.update(0.05, "Copying files")
        # The clone gets the same kind of folder a new server would: subvolume / dataset / plain.
        strategy = prepare_replacement(settings, dst)
        if strategy == "in_place":  # the target's own ZFS dataset: copy inside it, swap entries
            with _staging_inside(dst, "clone") as (root_fd, stage_fd, stage_name):
                # through the descriptor: renaming the staging folder cannot redirect the copy
                copy = f"/proc/self/fd/{stage_fd}/c"
                shutil.copytree(src, copy, symlinks=True, ignore=ignore)
                op.update(0.9, "Setting the owner of the copied files")
                _chown_tree(Path(copy), settings.runtime_uid, settings.runtime_gid)
                _swap_in_staged(root_fd, stage_fd, "c", stage_name)
            return result({})
        warnings: list[str] = []
        if strategy == "subvolume":  # a new subvolume next to the target, renamed into place
            staging = dst.with_name(f"{body.target_instance_id}.mcpanel-clone-{secrets.token_hex(6)}")
        else:
            staging = settings.staging_dir / f"clone-{body.target_instance_id}"
            remove_tree(staging)  # left over from an interrupted clone
        try:
            if strategy == "subvolume" and not create_empty_dir(staging, subvolume=True):
                warnings.append(
                    "Could not create a btrfs subvolume; the clone is a plain directory "
                    "(convert it to enable snapshots)"
                )
            shutil.copytree(src, staging, symlinks=True, ignore=ignore, dirs_exist_ok=True)
            op.update(0.9, "Setting the owner of the copied files")
            _chown_tree(staging, settings.runtime_uid, settings.runtime_gid)
            if dst.exists():
                if any(dst.iterdir()):
                    raise AgentError("Target instance directory is not empty", "target_not_empty", 409)
                remove_tree(dst)  # the empty folder (or subvolume) made when the target was created
            staging.rename(dst)
        except BaseException:
            remove_tree(staging)
            raise
        return result({"warnings": warnings} if warnings else {})

    def _clone_unverified(op: Operation) -> dict[str, Any]:
        try:
            return _clone(op)
        finally:
            # a copied claim marker describes the source's verification, not the clone's (its own
            # setup verifies the copied claim before its ports are published)
            claimmarker.remove(settings, body.target_instance_id, "the server folder was cloned")

    op = registry.start("clone_target", [instance_id, body.target_instance_id], _clone_unverified)
    return {"operation": op.as_dict()}


# --- logs ----------------------------------------------------------------------------
@app.get("/v1/instances/{instance_id}/logs", dependencies=[Depends(require_token)])
async def get_logs(
    instance_id: UUID,
    tail: int = Query(500, ge=1, le=20000),
    since: int | None = None,
    previous: bool = True,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The last ``tail`` console lines (``docker_ops.console_history``): the container log, with
    the end of the previous run in front of it; for a server without a container, the end of
    its last run. ``previous=false`` leaves the previous run out when there is a container."""
    return {
        "lines": await run_in_threadpool(
            docker_ops.console_history, instance_id, tail, since, settings, previous
        )
    }


# Console output is produced by the Minecraft process (third-party code): every buffer
# between Docker and the WebSocket is bounded, and a client that cannot keep up loses the
# oldest lines (with a notice) instead of growing the agent's memory.
LOG_LINE_MAX_BYTES = 16 * 1024
LOG_BUFFER_MAX_LINES = 5000
LOG_BUFFER_MAX_BYTES = 4 * 1024 * 1024
LOG_BATCH_MAX_LINES = 500
LOG_BATCH_MAX_BYTES = 512 * 1024  # keeps one WebSocket message far below the panel's 8 MiB limit
LOG_TRUNCATED_SUFFIX = " [mcpanel: line truncated]"
LOG_PING_SECONDS = 15.0  # a ping goes out when nothing else did for this long
LOG_RETRY_SECONDS = 2.0  # after a Docker error, before the pump tries again


class _LineSplitter:
    """Split a byte stream into lines of at most ``max_bytes`` (longer lines are truncated)."""

    def __init__(self, max_bytes: int = LOG_LINE_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self._buf = bytearray()
        self._skipping = False  # inside a line that was already emitted truncated

    @staticmethod
    def _decode(data: bytes | bytearray) -> str:
        return bytes(data).decode("utf-8", errors="replace").rstrip("\r")

    def _add(self, data: bytes, lines: list[str]) -> None:
        if self._skipping or not data:
            return
        room = self.max_bytes - len(self._buf)
        if len(data) <= room:
            self._buf += data
            return
        self._buf += data[:room]
        lines.append(self._decode(self._buf) + LOG_TRUNCATED_SUFFIX)
        self._buf.clear()
        self._skipping = True

    def feed(self, chunk: bytes) -> list[str]:
        lines: list[str] = []
        pos = 0
        while pos < len(chunk):
            nl = chunk.find(b"\n", pos)
            self._add(chunk[pos : len(chunk) if nl == -1 else nl], lines)
            if nl == -1:
                break
            if self._skipping:
                self._skipping = False
            else:
                lines.append(self._decode(self._buf))
            self._buf.clear()
            pos = nl + 1
        return lines

    def flush(self) -> list[str]:
        """The pending partial line (the stream ended without a final newline)."""
        line = None if self._skipping or not self._buf else self._decode(self._buf)
        self._buf.clear()
        self._skipping = False
        return [line] if line is not None else []


class _LogBuffer:
    """Bounded hand-off from the Docker log thread to the WebSocket sender.

    ``put`` never blocks and schedules at most one wake-up on the event loop at a time.
    Beyond ``max_lines``/``max_bytes`` the oldest log lines are dropped and counted; the
    client gets a notice line in their place. Control messages are never dropped.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self._loop = loop
        self.max_lines = LOG_BUFFER_MAX_LINES if max_lines is None else max_lines
        self.max_bytes = LOG_BUFFER_MAX_BYTES if max_bytes is None else max_bytes
        self._lock = threading.Lock()
        self._items: deque[tuple[dict[str, Any], int]] = deque()
        self._held: list[dict[str, Any]] = []  # control messages displaced by dropping
        self._lines = 0
        self._bytes = 0
        self._dropped = 0
        self._wakeup = asyncio.Event()
        self._wake_pending = False

    def __len__(self) -> int:
        with self._lock:
            return len(self._items) + len(self._held)

    def put(self, msg: dict[str, Any]) -> None:
        size = len(msg["line"]) if msg.get("type") == "log" else 0
        with self._lock:
            self._items.append((msg, size))
            if msg.get("type") == "log":
                self._lines += 1
                self._bytes += size
            while self._lines > self.max_lines or self._bytes > self.max_bytes:
                old, old_size = self._items.popleft()
                if old.get("type") != "log":
                    self._held.append(old)
                    continue
                self._lines -= 1
                self._bytes -= old_size
                self._dropped += 1
            if self._wake_pending:
                return
            self._wake_pending = True
        with contextlib.suppress(RuntimeError):  # the loop is gone: the connection has ended
            self._loop.call_soon_threadsafe(self._wakeup.set)

    def wake(self) -> None:
        """Wake a waiting :meth:`get_batch` (event-loop thread only)."""
        self._wakeup.set()

    async def get_batch(self, timeout: float) -> list[dict[str, Any]] | None:
        """Messages in order (None if nothing arrived within ``timeout``)."""
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
        except TimeoutError:
            return None
        with self._lock:
            self._wakeup.clear()
            self._wake_pending = False
            out = self._held
            self._held = []
            if self._dropped:
                out.append(
                    {
                        "type": "log",
                        "line": f"[mcpanel] {self._dropped} console line(s) dropped: "
                        "the console could not keep up with the server output",
                    }
                )
                self._dropped = 0
            lines = batch_bytes = 0
            while self._items and lines < LOG_BATCH_MAX_LINES and batch_bytes < LOG_BATCH_MAX_BYTES:
                msg, size = self._items.popleft()
                if msg.get("type") == "log":
                    lines += 1
                    batch_bytes += size
                    self._lines -= 1
                    self._bytes -= size
                out.append(msg)
            if self._items:  # more is waiting: come back without sleeping
                self._wakeup.set()
                self._wake_pending = True
        return out


def _previous_run_head(settings: Settings, instance_id: UUID, c: Any, tail: int) -> list[str]:
    """The lines of the last-run record a console shows in front of the first ``tail`` lines of
    container ``c``: as many as the container's own log leaves free. Never raises."""
    try:
        previous = docker_ops.previous_run_lines(settings, instance_id, c)
        if not previous:
            return []
        room = tail - docker_ops.count_log_lines(c, tail)
        return previous[-room:] if room > 0 else []
    except Exception as exc:
        log.debug("previous run of %s not shown: %s", instance_id, exc)
        return []


@app.websocket("/v1/instances/{instance_id}/logs/stream")
async def stream_logs(websocket: WebSocket, instance_id: UUID, tail: int = 200) -> None:
    """Live console output. It opens with the last ``tail`` lines: of the container's log, with
    the end of the previous run (the last-run record) in front while that log is shorter, or of
    the last run alone while the server has no container (state ``missing``)."""
    settings = get_settings()
    if not await websocket_authorized(websocket, settings):
        return
    await websocket.accept()
    buffer = _LogBuffer(asyncio.get_running_loop())
    stop_event = threading.Event()
    current_stream: dict[str, Any] = {}
    tail = max(0, min(tail, 5000))
    emit = buffer.put

    def pump() -> None:
        first = True
        last_state = None
        since: int | None = None
        attached_id: str | None = None  # the container whose log was read last
        failing: str | None = None  # the Docker error reported last (each streak once)

        def docker_failed(exc: Exception) -> None:
            nonlocal failing
            text = str(exc) or type(exc).__name__
            if text != failing:
                emit({"type": "error", "message": text})
                failing = text
            stop_event.wait(LOG_RETRY_SECONDS)

        while not stop_event.is_set():
            try:
                c = docker_ops.get_container(instance_id)
            except Exception as exc:  # Docker unreachable for a moment: keep trying, the console stays
                docker_failed(exc)
                continue
            if c is None:
                failing = None
                if last_state != "missing":
                    emit({"type": "state", "state": "missing"})
                    last_state = "missing"
                if first:
                    # a stopped server has no container: the console opens with the end of its last run
                    first = False
                    if tail > 0:
                        for line in docker_ops.previous_run_lines(settings, instance_id, None)[-tail:]:
                            emit({"type": "log", "line": line})
                stop_event.wait(2)
                continue
            try:
                c.reload()
            except docker.errors.NotFound:  # removed between the two calls
                stop_event.wait(1)
                continue
            except Exception as exc:
                docker_failed(exc)
                continue
            failing = None
            state = c.status
            container_id = getattr(c, "id", None)
            if state != last_state:
                emit({"type": "state", "state": state})
                last_state = state
            if state != "running" and not first and container_id == attached_id:
                stop_event.wait(2)
                continue
            kwargs: dict[str, Any] = {"stream": True, "follow": True, "stdout": True, "stderr": True}
            if first:
                kwargs["tail"] = tail if tail > 0 else 0
                if tail > 0:
                    # the end of the previous run fills what the new container's log leaves of the tail
                    for line in _previous_run_head(settings, instance_id, c, tail):
                        emit({"type": "log", "line": line})
            elif container_id != attached_id:
                # a container created since the last read (a stopped server gets a new one at
                # every start): everything it has printed so far is new
                kwargs["tail"] = "all"
            else:
                kwargs["since"] = since or int(time.time())
                kwargs["tail"] = "all"
            first = False
            attached_id = container_id
            try:
                stream = c.logs(**kwargs)
            except Exception as exc:
                emit({"type": "error", "message": f"Cannot attach to logs: {exc}"})
                stop_event.wait(2)
                continue
            current_stream["s"] = stream
            splitter = _LineSplitter()
            # a game whose image prints its control secret: hidden in every line (None: Minecraft)
            redact = (
                docker_ops.log_redactor(settings, c)
                if (getattr(c, "labels", None) or {}).get(profiles.LABEL_GAME)
                else None
            )
            try:
                for chunk in stream:
                    if stop_event.is_set():
                        break
                    for line in splitter.feed(chunk):
                        emit({"type": "log", "line": redact(line) if redact is not None else line})
            except Exception as exc:  # stream broke (container stopped / daemon restarted)
                log.debug("log stream for %s ended: %s", instance_id, exc)
            finally:
                for line in splitter.flush():
                    emit({"type": "log", "line": redact(line) if redact is not None else line})
                with contextlib.suppress(Exception):
                    stream.close()
                current_stream.pop("s", None)
            since = int(time.time())
            # the container stopped (stream ended); report and wait for it to come back
            stop_event.wait(1)
        emit({"type": "closed"})

    def guarded_pump() -> None:
        try:
            pump()
        except Exception as exc:  # whatever went wrong, the console is told and can reconnect
            log.exception("log stream for %s failed", instance_id)
            emit({"type": "error", "message": f"Log stream failed: {type(exc).__name__}"})
            emit({"type": "closed"})

    thread = threading.Thread(target=guarded_pump, daemon=True, name=f"logs-{instance_id.hex[:8]}")
    thread.start()

    async def receiver() -> None:
        try:
            while True:
                await websocket.receive_text()  # we ignore client messages; detect disconnect
        except WebSocketDisconnect:
            pass
        finally:
            buffer.wake()  # let the sender loop notice the disconnect now, not at the next ping

    recv_task = asyncio.create_task(receiver())
    try:
        pump_ended = False
        while not recv_task.done() and not pump_ended:
            batch = await buffer.get_batch(timeout=LOG_PING_SECONDS)
            if batch is None:
                if not thread.is_alive():  # the pump is gone without a word: let the relay reconnect
                    break
                await websocket.send_json({"type": "ping"})
                continue
            lines: list[str] = []
            for m in batch:  # consecutive log lines go out as one message, in order
                if m.get("type") == "log":
                    lines.append(m["line"])
                    continue
                if lines:
                    await websocket.send_json({"type": "logs", "lines": lines})
                    lines = []
                await websocket.send_json(m)
                # the pump has ended: close the socket, so the panel's relay connects again
                pump_ended = pump_ended or m.get("type") == "closed"
            if lines:
                await websocket.send_json({"type": "logs", "lines": lines})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stop_event.set()
        s = current_stream.get("s")
        if s is not None:
            with contextlib.suppress(Exception):
                s.close()
        recv_task.cancel()
        if websocket.application_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


# --- files ---------------------------------------------------------------------------
@app.get("/v1/instances/{instance_id}/files/list", dependencies=[Depends(require_token)])
async def files_list(
    instance_id: UUID, path: str = "", settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    root = _root(settings, instance_id)
    entries = await run_in_threadpool(root.list, path)
    return {"path": "/".join(p for p in path.split("/") if p), "entries": [e.as_dict() for e in entries]}


@app.get("/v1/instances/{instance_id}/files/stat", dependencies=[Depends(require_token)])
async def files_stat(
    instance_id: UUID, path: str, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    return (await run_in_threadpool(_root(settings, instance_id).stat, path)).as_dict()


LATIN1_FALLBACK_SUFFIXES = (".properties",)  # Java reads these as ISO-8859-1 when they are not UTF-8


def _decode_text(path: str, data: bytes) -> tuple[str, str]:
    """(text, encoding) of a file for the editor; never lossy.

    A file that is not valid UTF-8 would be damaged by an editor that saves UTF-8 (every odd
    byte becomes U+FFFD for good), so it is refused with 415 ``not_utf8``, except ``*.properties``
    files, which are read (and written back) as ISO-8859-1 like Java does.
    """
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        if path.lower().endswith(LATIN1_FALLBACK_SUFFIXES):
            return data.decode("iso-8859-1"), "iso-8859-1"
        raise AgentError(
            "This file is not UTF-8 text; editing it here would damage it. Download it and use an "
            "editor that knows its encoding",
            "not_utf8",
            415,
        ) from exc


@app.get("/v1/instances/{instance_id}/files/text", dependencies=[Depends(require_token)])
async def files_read_text(
    instance_id: UUID, path: str, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """The text of a file and its ``encoding`` (``utf-8``, or ``iso-8859-1`` for a
    ``*.properties`` file that is not UTF-8). Other non-UTF-8 files: 415 ``not_utf8``."""
    root = _root(settings, instance_id)
    data = await run_in_threadpool(root.read_bytes, path, TEXT_LIMIT)
    if b"\x00" in data[:8192]:
        raise AgentError("This looks like a binary file and cannot be edited as text", "binary_file", 415)
    content, encoding = _decode_text(path, data)
    entry = await run_in_threadpool(root.stat, path)
    return {
        "path": entry.path,
        "content": content,
        "encoding": encoding,
        "size": entry.size,
        "modified": entry.modified,
    }


def _write_encoding(root: SafeRoot, path: str, requested: str | None) -> str:
    """The encoding a save uses: the requested one, else the one :func:`_decode_text` read."""
    if requested is not None:
        return requested
    if not path.lower().endswith(LATIN1_FALLBACK_SUFFIXES):
        return "utf-8"
    try:
        current = root.read_bytes(path, TEXT_LIMIT)
    except PathError:
        return "utf-8"  # a new file (or one that cannot be read): UTF-8
    try:
        current.decode("utf-8")
    except UnicodeDecodeError:
        return "iso-8859-1"
    return "utf-8"


@app.put("/v1/instances/{instance_id}/files/text", dependencies=[Depends(require_token)])
async def files_write_text(
    instance_id: UUID, body: WriteTextRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    root = _root(settings, instance_id)
    encoding = await run_in_threadpool(_write_encoding, root, body.path, body.encoding)
    try:
        data = body.content.encode(encoding)
    except UnicodeEncodeError:
        if body.encoding is not None:
            raise AgentError(
                f"The text contains characters that {encoding} cannot store", "not_encodable", 400
            ) from None
        encoding, data = "utf-8", body.content.encode("utf-8")  # Java reads a UTF-8 .properties too
    size = await run_in_threadpool(root.write_bytes, body.path, data, overwrite=not body.create_only)
    return {"path": body.path, "size": size, "encoding": encoding}


DOWNLOAD_CHUNK = 256 * 1024


class ClosingStreamingResponse(StreamingResponse):
    """A streamed answer whose source is closed as soon as the answer ends, also when the client
    goes away part-way (Starlette leaves a sync iterator it was reading suspended: its
    ``finally`` would only run when the garbage collector gets to it, minutes later, keeping the
    file open or restic running).

    ``content`` is a sync generator; ``on_close`` runs after it was closed (for what was opened
    before the generator started, which closing a generator that never ran does not reach). By
    then no thread is running the generator: Starlette awaits each step it hands to a thread.
    """

    def __init__(self, content: Any, *, on_close: Any = None, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._source = content
        self._on_close = on_close

    def _close_source(self) -> None:
        try:
            close = getattr(self._source, "close", None)
            if close is not None:
                close()
        finally:
            if self._on_close is not None:
                self._on_close()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await run_in_threadpool(self._close_source)
            except Exception as exc:  # never hides why the answer ended
                log.warning("Closing a streamed download failed: %s", exc)


def _once(fn: Any) -> Any:
    lock = threading.Lock()
    done = False

    def run() -> None:
        nonlocal done
        with lock:
            if done:
                return
            done = True
        fn()

    return run


@app.get("/v1/instances/{instance_id}/files/download", dependencies=[Depends(require_token)])
async def files_download(
    instance_id: UUID, path: str, settings: Settings = Depends(get_settings)
) -> StreamingResponse:
    """The file as it was when it was opened: exactly ``Content-Length`` bytes, also when the
    server is still writing to it (latest.log)."""
    root = _root(settings, instance_id)
    entry = await run_in_threadpool(root.stat, path)
    if entry.type != "file":
        raise AgentError("Only regular files can be downloaded", "not_a_file", 400)
    opened = root.open_read(path)
    fh, size = await run_in_threadpool(opened.__enter__)  # size: fstat of the open file
    close = _once(lambda: opened.__exit__(None, None, None))

    def _iter() -> Any:
        try:
            remaining = size
            while remaining > 0:
                chunk = fh.read(min(DOWNLOAD_CHUNK, remaining))
                if not chunk:
                    break  # shrank meanwhile: the client sees a short body and fails the download
                remaining -= len(chunk)
                yield chunk
        finally:
            close()

    # closed when the answer ends, also when the client goes away (not when the GC gets to it)
    return ClosingStreamingResponse(
        _iter(), on_close=close, media_type="application/octet-stream", headers={"Content-Length": str(size)}
    )


@app.get("/v1/instances/{instance_id}/files/archive", dependencies=[Depends(require_token)])
async def files_archive(
    instance_id: UUID,
    path: list[str] = Query(default=[""]),
    exclude: list[str] | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Download one or more directories (e.g. a world and its dimensions) as a zip streamed on the fly.
    ``exclude`` (repeatable, at most ``MAX_ZIP_EXCLUDES``; feature ``archive_excludes``): paths
    relative to the server folder whose files (and, for a folder, everything below it) are left
    out, e.g. a game's control-secret files."""
    from .archive import MAX_ZIP_FOLDERS, stream_zip

    if len(path) > MAX_ZIP_FOLDERS:
        raise AgentError(f"At most {MAX_ZIP_FOLDERS} folders can be zipped at once", "invalid_path", 400)
    excluded = list(exclude) if isinstance(exclude, list) else []  # a direct call leaves the Query default
    root = _root(settings, instance_id)
    for p in path:
        entry = await run_in_threadpool(root.stat, p)
        if entry.type != "directory":
            raise AgentError("Only folders can be downloaded as a zip", "not_a_directory", 400)
    gen = await run_in_threadpool(
        lambda: stream_zip(root, path[0] if len(path) == 1 else path, exclude=excluded)
    )
    return ClosingStreamingResponse(gen, media_type="application/zip")  # stops the zip producer


@app.put("/v1/instances/{instance_id}/files/upload", dependencies=[Depends(require_token)])
async def files_upload(
    request: Request,
    instance_id: UUID,
    path: str,
    overwrite: bool = False,
    sha256: str | None = Query(default=None, pattern="^[0-9a-fA-F]{64}$"),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Store the request body at ``path``. With ``sha256`` the file is only committed when the
    received bytes match it (the panel sends the hash of a file it downloaded and inspected)."""
    root = _root(settings, instance_id)
    writer = await run_in_threadpool(root.open_writer, path, overwrite=overwrite, max_bytes=UPLOAD_LIMIT)
    digest = hashlib.sha256()
    try:
        async for chunk in request.stream():
            if chunk:
                digest.update(chunk)
                await run_in_threadpool(writer.write, chunk)
        if sha256 and digest.hexdigest() != sha256.lower():
            raise AgentError("Uploaded file failed SHA-256 verification", "hash_mismatch", 422)
        size = await run_in_threadpool(writer.commit)
    except BaseException:
        await run_in_threadpool(writer.abort)
        raise
    return {"path": path, "size": size, "sha256": digest.hexdigest()}


@app.post("/v1/instances/{instance_id}/files/mkdir", dependencies=[Depends(require_token)])
async def files_mkdir(
    instance_id: UUID, body: MkdirRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    await run_in_threadpool(_root(settings, instance_id).mkdir, body.path, parents=body.parents)
    return {"path": body.path}


@app.post("/v1/instances/{instance_id}/files/rename", dependencies=[Depends(require_token)])
async def files_rename(
    instance_id: UUID, body: RenameRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    await run_in_threadpool(
        _root(settings, instance_id).rename, body.source, body.destination, overwrite=body.overwrite
    )
    return {"source": body.source, "destination": body.destination}


@app.post("/v1/instances/{instance_id}/files/delete", dependencies=[Depends(require_token)])
async def files_delete(
    instance_id: UUID, body: DeleteRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    root = _root(settings, instance_id)
    deleted = []
    for p in body.paths:
        await run_in_threadpool(root.delete, p, recursive=body.recursive)
        deleted.append(p)
    return {"deleted": deleted}


@app.get("/v1/instances/{instance_id}/files/search", dependencies=[Depends(require_token)])
async def files_search(
    instance_id: UUID,
    q: str = Query(..., min_length=2, max_length=200),
    path: str = "",
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    results = await run_in_threadpool(_root(settings, instance_id).search, path, q)
    return {"results": results}


@app.get("/v1/instances/{instance_id}/files/hashes", dependencies=[Depends(require_token)])
async def files_hashes(
    instance_id: UUID,
    path: str,
    algorithm: str = Query("sha1", pattern="^(sha1|sha512)$"),
    wait: float | None = Query(None, ge=0, le=60),
    suffix: list[str] = Query(default=[], max_length=8),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Hash every regular file directly inside ``path`` (used to identify mods/plugins).

    Digests are cached on disk per file (name, size, mtime, ctime, inode), so only new or changed
    files are read. Without ``wait`` the call returns once every file is hashed, or fails with
    503 ``hashing_busy`` after ``hashcache.BLOCKING_DEADLINE`` seconds (one pass per folder runs in
    the background however many callers wait for it). With ``wait`` it returns after at most
    about that many seconds; files not read yet then have ``hash: null``, ``complete`` is false,
    and hashing continues in the background. Files larger than ``hashcache.HASH_MAX_FILE_BYTES`` are
    never read: ``hash: null``, ``too_large: true``. ``suffix`` (repeatable, e.g. ``.jar``)
    limits the answer to names ending in one of them.
    """
    for s in suffix:
        if not s or len(s) > 32 or "/" in s:
            raise AgentError("Invalid suffix", "invalid_suffix", 400)
    root = _root(settings, instance_id)
    cache = hashcache.HashCache(settings.hash_cache_dir)
    try:
        result = await run_in_threadpool(
            functools.partial(
                hashcache.hash_directory,
                root,
                cache,
                _hash_jobs,
                instance_id,
                path,
                algorithm,
                wait=wait,
                suffixes=tuple(suffix),
            )
        )
    except PathError as exc:
        if exc.code != "not_found":
            raise
        result = {"files": [], "complete": True, "pending": 0}
    except hashcache.HashingCancelled as exc:
        raise AgentError("The agent is shutting down", "shutting_down", 503) from exc
    except hashcache.HashingBusy as exc:
        raise AgentError(
            f"Reading the files in {path or 'the server folder'} takes longer than "
            f"{int(hashcache.BLOCKING_DEADLINE)} seconds; it goes on in the background, try again later",
            "hashing_busy",
            503,
        ) from exc
    return {"algorithm": algorithm, **result}


@app.post("/v1/instances/{instance_id}/files/fetch", dependencies=[Depends(require_token)])
async def files_fetch(
    instance_id: UUID, body: dict[str, Any], settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Download a file from an allow-listed provider URL into the instance, verifying its hash."""
    from .fetch import fetch_into

    root = _root(settings, instance_id)
    return await run_in_threadpool(fetch_into, root, body)


@app.post("/v1/instances/{instance_id}/files/extract", dependencies=[Depends(require_token)])
async def files_extract(
    instance_id: UUID, body: ExtractRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Safely extract an archive that is already inside the instance (import server/world/server pack).

    ``protected`` (feature ``extract_protected``): entries that would write one of these paths (or
    inside one) are skipped and reported (``protected_count``, ``protected``); refused up front (400)
    when a path is not a safe relative path."""
    from .archive import extract, protected_paths

    protected_paths(body.protected)  # an invalid list is refused before anything starts
    status = await run_in_threadpool(docker_ops.status, instance_id)
    if status.get("running"):
        raise AgentError("Stop the server before extracting archives into it", "must_stop", 409)
    root = _root(settings, instance_id)
    entry = await run_in_threadpool(root.stat, body.path)
    if entry.type != "file":
        raise AgentError("The archive must be a regular file", "not_a_file", 400)

    def _do(op: Operation) -> dict[str, Any]:
        op.update(0.01, f"Extracting {body.path}")
        try:
            return extract(
                root,
                body.path,
                body.destination,
                strip=body.strip,
                overwrite=body.overwrite,
                progress=lambda p, m: op.update(p * 0.98, m),
                # the game's own protected paths (CS2's start hooks) are skipped like the panel's
                protected=_with_game_protected(settings, instance_id, body.protected),
            )
        finally:
            # also when the extraction failed: an uploaded archive must not stay in the folder
            # (and in every later backup)
            if body.delete_archive:
                with contextlib.suppress(PathError, OSError):
                    root.delete(body.path)

    op = registry.start("import", [instance_id], _do)
    return {"operation": op.as_dict()}


# --- worlds --------------------------------------------------------------------------
@app.get("/v1/instances/{instance_id}/world", dependencies=[Depends(require_token)])
async def world_info(
    instance_id: UUID,
    level_name: str = Query("world", min_length=1, max_length=200),
    layout: Literal["vanilla", "bukkit"] = "vanilla",
    sizes: bool = True,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The world folders of ``level_name`` (with bukkit-style dimension folders) and their state.

    ``sizes=false`` skips measuring the folders (no walk over every file of the world).
    """
    from . import world

    return await run_in_threadpool(world.info, _root(settings, instance_id), level_name, layout, sizes=sizes)


@app.post("/v1/instances/{instance_id}/world/replace", dependencies=[Depends(require_token)])
async def world_replace(
    instance_id: UUID, body: WorldReplaceRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Replace the world with the one found in an archive inside the instance (server must be stopped).

    The new world is extracted into a staging folder, the old world folders are moved aside,
    the new ones renamed into place and the old ones deleted only afterwards; any failure
    puts the old world back. ``delete_archive`` removes the archive whatever the outcome.
    """
    from . import world

    status = await run_in_threadpool(docker_ops.status, instance_id)
    if status.get("running"):
        raise AgentError("Stop the server before replacing its world", "must_stop", 409)
    parts = world.level_parts(body.level_name)  # refuse bad names before starting an operation
    world.check_archive_location(body.archive, parts, body.layout)
    root = _root(settings, instance_id)
    entry = await run_in_threadpool(root.stat, body.archive)
    if entry.type != "file":
        raise AgentError("The world archive must be a regular file", "not_a_file", 400)
    inst_dir = docker_ops.instance_dir(settings, instance_id)

    def _do(op: Operation) -> dict[str, Any]:
        try:
            # Checked again now that the operation blocks power actions: a start that slipped in
            # between the check above and the registration would otherwise run on a world being swapped.
            if docker_ops.status(instance_id).get("running"):
                raise AgentError("Stop the server before replacing its world", "must_stop", 409)
            return world.replace(
                root,
                body.archive,
                body.level_name,
                body.layout,
                free_bytes=lambda: shutil.disk_usage(inst_dir).free,
                progress=lambda p, m: op.update(p, m),
            )
        finally:
            if body.delete_archive:
                with contextlib.suppress(PathError, OSError):
                    root.delete(body.archive)

    op = registry.start("world_replace", [instance_id], _do)
    return {"operation": op.as_dict()}


def _container_live(status: dict[str, Any]) -> bool:
    """Running, or in a restart loop: Docker starts such a container again by itself."""
    return bool(status.get("running") or status.get("restarting") or status.get("status") == "restarting")


@app.post("/v1/instances/{instance_id}/world/reset", dependencies=[Depends(require_token)])
async def world_reset(
    instance_id: UUID, body: WorldResetRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Delete the world so the server generates a new one on its next start (server must be stopped).

    Only folders that look like a world are deleted, and only if they are exactly the folders in
    ``expect``. They are moved aside first (undone on any failure) and deleted afterwards. A failed
    operation's error code says whether the world was touched (see ``world.reset``).
    """
    from . import world

    if body.confirm != instance_id:
        raise AgentError("Confirmation id does not match", "confirmation_mismatch", 400)
    world.level_parts(body.level_name)  # refuse bad names before starting an operation
    status = await run_in_threadpool(docker_ops.status, instance_id)
    if _container_live(status):
        raise AgentError("Stop the server before resetting its world", "must_stop", 409)
    root = _root(settings, instance_id)

    def _do(op: Operation) -> dict[str, Any]:
        # Checked again now that the operation blocks power actions: a start that slipped in between
        # the check above and the registration would otherwise run on a world being deleted.
        try:
            live = _container_live(docker_ops.status(instance_id))
        except Exception as exc:
            raise AgentError(
                f"The state of the server's container could not be checked ({exc}). Nothing was changed.",
                "world_reset_failed",
                503,
            ) from exc
        if live:
            raise AgentError("Stop the server before resetting its world", "must_stop", 409)
        return world.reset(
            root,
            body.level_name,
            body.layout,
            expect=body.expect,
            keep=body.keep,
            progress=lambda p, m: op.update(p, m),
        )

    op = registry.start("world_reset", [instance_id], _do)
    return {"operation": op.as_dict()}


# --- backups -------------------------------------------------------------------------
@app.get("/v1/instances/{instance_id}/backups", dependencies=[Depends(require_token)])
async def list_backups(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    r = Restic(settings, instance_id)
    return {"snapshots": await run_in_threadpool(r.snapshots)}


GameHint = tuple[str | None, str | None]


def _server_profile(
    settings: Settings, instance_id: UUID, hint: GameHint = (None, None)
) -> profiles.RuntimeProfile | None:
    """The runtime profile of the server's game (its container's labels, or the game its last-run
    record names); None for an unknown game and when Docker cannot be asked. A server with neither
    (created stopped, its never-started container retired) takes the game the panel names in its
    request (``hint`` = (game, variant)), as long as this agent has a profile of it; else Minecraft."""
    try:
        status = docker_ops.status(instance_id, settings)
    except Exception as exc:
        log.debug("the game of %s could not be determined: %s", instance_id, exc)
        return None
    if status.get("exists"):
        info = status
    elif status.get("last_run"):
        info = status["last_run"]
    else:
        game, variant = hint
        hinted = profiles.get(game, variant) if game else None
        return hinted or profiles.get(profiles.DEFAULT_GAME)
    return profiles.get(info.get("game") or profiles.DEFAULT_GAME, info.get("variant") or None)


def _profile_backup_excludes(
    settings: Settings, instance_id: UUID, hint: GameHint = (None, None)
) -> list[str]:
    """The default backup excludes of the server's game (``_server_profile``), as absolute paths
    below the server folder; none for Minecraft, for an unknown game and when Docker cannot be
    asked."""
    profile = _server_profile(settings, instance_id, hint)
    if profile is None:
        return []
    source = docker_ops.instance_dir(settings, instance_id)
    return [str(source / rel) for rel in profile.default_backup_excludes]


def _carry_over_excluded(
    settings: Settings, instance_id: UUID, current: Path, restored: Path, hint: GameHint = (None, None)
) -> list[str]:
    """Move what the game's profile leaves out of backups (re-downloadable game files: Satisfactory's
    2.8 GB ``gamefiles/``, Palworld's ``PalServer.sh`` and ``*.so``, CS2's ``game/**/*.vpk``) from the
    server folder into the restored tree, which does not have it, so a restore does not throw it away
    (the image would download it again at the next start, and Palworld's image even takes a missing
    ``PalServer.sh`` for "not installed"). Folders, files and the matches of glob patterns (``*`` and
    ``**``, as restic matched them when it left them out) are carried; symbolic links never, and
    nothing is moved through a link (a link the game made inside its folder could point anywhere):
    every component of the source and of the destination's parents must be a real folder. A rename
    only: across file systems (a btrfs subvolume or ZFS dataset per server) they are left behind and
    the next start downloads them; that is a warning, never an error."""
    warnings: list[str] = []
    root = docker_ops.instance_dir(settings, instance_id)
    for absolute in _profile_backup_excludes(settings, instance_id, hint):
        pattern = os.path.relpath(absolute, root)
        if pattern.startswith("..") or os.path.isabs(pattern):
            continue
        for rel in _excluded_matches(current, pattern):
            src, dst = current / rel, restored / rel
            if os.path.lexists(dst) or not _real_parents(current, rel, include_last=True):
                continue
            try:
                if not _make_real_parents(restored, rel):
                    continue
                os.rename(src, dst)
            except OSError as exc:
                warnings.append(
                    f"{rel} (not in backups) could not be kept and is downloaded again at the next start: {exc}"
                )
    return warnings


def _excluded_matches(current: Path, pattern: str) -> list[str]:
    """Paths below ``current`` (relative, sorted) that an exclude ``pattern`` names: the path itself, or
    the matches of its ``*``/``?``/``[...]``/``**`` parts. Symbolic links are never followed or returned."""
    if not any(ch in pattern for ch in "*?["):
        return (
            [pattern] if os.path.lexists(current / pattern) and not (current / pattern).is_symlink() else []
        )
    try:
        found = current.glob(pattern, recurse_symlinks=False)
        out = sorted({str(p.relative_to(current)) for p in found if not p.is_symlink()})
    except (OSError, ValueError):
        return []
    return out


def _real_parents(base: Path, rel: str, *, include_last: bool = False) -> bool:
    """Whether every folder on the way from ``base`` to ``rel`` (and ``rel`` itself with
    ``include_last``) exists as a real directory or file, never a symbolic link."""
    parts = Path(rel).parts
    path = base
    for i, part in enumerate(parts):
        path = path / part
        last = i == len(parts) - 1
        if last and not include_last:
            break
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or (not last and not stat.S_ISDIR(st.st_mode)):
            return False
    return True


def _make_real_parents(base: Path, rel: str) -> bool:
    """Create the missing folders of ``rel``'s parent below ``base``; False when one of them is a link or
    not a folder (the restored tree comes from a backup of the server folder: a link there is the game's)."""
    path = base
    for part in Path(rel).parts[:-1]:
        path = path / part
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            os.mkdir(path)
            continue
        if not stat.S_ISDIR(st.st_mode):
            return False
    return True


@app.post("/v1/instances/{instance_id}/backups", dependencies=[Depends(require_token)])
async def create_backup(
    instance_id: UUID, body: BackupRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    source = docker_ops.instance_dir(settings, instance_id)
    if not source.exists():
        raise AgentError("Instance has no data directory", "not_found", 404)
    restic = Restic(settings, instance_id)

    def _backup(op: Operation) -> dict[str, Any]:
        hint = (body.game, body.variant)
        excludes = list(body.excludes) + _profile_backup_excludes(settings, instance_id, hint)
        with _world_saving_paused(settings, instance_id, body.consistency, op, "backup", 0.02) as saving:
            profile = _server_profile(settings, instance_id, hint)
            if profile is not None and profile.fix_ownership:
                # the backup keeps the owners: the files the save just wrote get the runtime uid:gid
                # (Satisfactory's are <uid>:1000 while it runs); what the backup leaves out is not walked
                if (
                    warning := docker_ops.fix_ownership(
                        settings, instance_id, tuple(profile.default_backup_excludes)
                    )
                ) is not None:
                    saving.warnings.append(warning)
            op.update(0.06, "Running restic backup")
            summary = restic.backup(
                source,
                [f"instance:{instance_id}", f"backup:{body.backup_id}", *body.tags],
                excludes,
                lambda p, m: op.update(0.06 + p * 0.9, m),
            )
        summary["warnings"] = saving.warnings + summary.get("warnings", [])
        summary["consistent"] = saving.consistent
        summary["saving"] = saving.how  # how it was made consistent (the panel's job log says so)
        return summary

    op = registry.start("backup", [instance_id], _backup)
    return {"operation": op.as_dict()}


@app.post("/v1/instances/{instance_id}/backups/restore", dependencies=[Depends(require_token)])
async def restore_backup(
    instance_id: UUID, body: RestoreRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    restic = Restic(settings, instance_id)
    target_id = body.target_instance_id or instance_id
    target_dir = docker_ops.instance_dir(settings, target_id)
    status = await run_in_threadpool(docker_ops.status, target_id)
    if status.get("running"):
        raise AgentError("Stop the target server before restoring", "target_running", 409)
    if body.target_instance_id and target_dir.exists() and any(target_dir.iterdir()):
        raise AgentError("Target instance directory is not empty", "target_not_empty", 409)

    def _restore(op: Operation) -> dict[str, Any]:
        from .snapshots import create_empty_dir, prepare_replacement, remove_tree

        def progress(p: float, m: str) -> None:
            op.update(0.02 + p * 0.9, m)

        strategy = prepare_replacement(settings, target_dir)
        if strategy == "in_place":
            # The server's own ZFS dataset (a mount point cannot be renamed): restore into a
            # hidden folder inside it, then swap the entries; the dataset and its snapshots stay.
            op.update(0.02, "Restoring snapshot into a staging folder inside the server folder")
            with _staging_inside(target_dir, "restore") as (root_fd, stage_fd, stage_name):
                # restic gets the staging folder as an inherited descriptor (see restore-path)
                result = restic.restore(
                    body.snapshot_id, Path(f"/proc/self/fd/{stage_fd}/r"), progress, pass_fds=(stage_fd,)
                )
                op.update(0.94, "Swapping restored files into place")
                _swap_in_staged(root_fd, stage_fd, "r", stage_name)
            return result

        stamp = int(time.time())
        warnings: list[str] = []
        # A btrfs subvolume stays a subvolume (or snapshots would silently stop working): the
        # backup is restored into a new subvolume next to it (same parent, so the swap is a
        # rename) and the old one is deleted after the swap.
        subvolume = strategy == "subvolume"
        if subvolume:
            staging = target_dir.with_name(f"{target_id}.mcpanel-restore-{stamp}")
            old = target_dir.with_name(f"{target_id}.mcpanel-old-{stamp}")
        else:
            staging = settings.staging_dir / f"restore-{target_id}-{stamp}"
            old = settings.trash_dir / f"pre-restore-{target_id}-{stamp}"
        op.update(0.02, "Restoring snapshot into staging area")
        try:
            if subvolume and not create_empty_dir(staging, subvolume=True):
                warnings.append(
                    "Could not create a btrfs subvolume; the server folder was restored as a plain "
                    "directory (convert it to enable snapshots again)"
                )
            result = restic.restore(body.snapshot_id, staging, progress)
        except BaseException:
            remove_tree(staging)
            raise
        if target_id == instance_id and target_dir.exists():
            warnings.extend(
                _carry_over_excluded(settings, instance_id, target_dir, staging, (body.game, body.variant))
            )
        op.update(0.94, "Swapping restored files into place")
        moved = False
        try:
            if target_dir.exists():
                target_dir.rename(old)
                moved = True
            staging.rename(target_dir)
        except BaseException:
            if moved:
                old.rename(target_dir)
            remove_tree(staging)
            raise
        if moved:
            remove_tree(old)
        # restic creates the staging folder itself (as root): the server folder must belong to the
        # runtime uid/gid again, as prepare and apply leave it, or an image that runs without
        # root (Factorio's rootless image) cannot create or rename files at its top level
        with contextlib.suppress(PermissionError, FileNotFoundError):
            os.chown(target_dir, settings.runtime_uid, settings.runtime_gid)
        if warnings:
            result["warnings"] = warnings
        return result

    def _restore_unverified(op: Operation) -> dict[str, Any]:
        claimmarker.remove(settings, target_id, "a restore of the server folder began")
        try:
            return _restore(op)
        finally:
            # a restored marker (from the backup) is not this run's verification
            claimmarker.remove(settings, target_id, "the server folder was restored")

    kinds = [instance_id] if target_id == instance_id else [instance_id, target_id]
    op = registry.start("restore", kinds, _restore_unverified)
    return {"operation": op.as_dict()}


@app.post("/v1/instances/{instance_id}/backups/forget", dependencies=[Depends(require_token)])
async def forget_backups(
    instance_id: UUID, body: ForgetRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    restic = Restic(settings, instance_id)
    op = registry.start("forget", [instance_id], lambda _op: restic.forget(body.snapshot_ids, body.prune))
    return {"operation": op.as_dict()}


@app.post("/v1/instances/{instance_id}/backups/check", dependencies=[Depends(require_token)])
async def check_backups(
    instance_id: UUID,
    read_data_subset: str | None = Query(None, pattern=r"^\d{1,3}(\.\d+)?%$|^\d+/\d+$"),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    restic = Restic(settings, instance_id)
    op = registry.start("check", [instance_id], lambda _op: restic.check(read_data_subset))
    return {"operation": op.as_dict()}


@app.get("/v1/instances/{instance_id}/backups/stats", dependencies=[Depends(require_token)])
async def backup_stats(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return await run_in_threadpool(Restic(settings, instance_id).stats)


@app.get("/v1/instances/{instance_id}/backups/{snapshot_id}/files", dependencies=[Depends(require_token)])
async def backup_files(
    instance_id: UUID,
    snapshot_id: str = PathParam(pattern=r"^[a-f0-9]{8,64}$"),
    path: str = "",
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    from .safefs import split_path

    parts = split_path(path)
    entries = await run_in_threadpool(Restic(settings, instance_id).ls, snapshot_id, parts)
    return {"path": "/".join(parts), "entries": entries}


@app.get("/v1/instances/{instance_id}/backups/{snapshot_id}/download", dependencies=[Depends(require_token)])
async def backup_download(
    instance_id: UUID,
    snapshot_id: str = PathParam(pattern=r"^[a-f0-9]{8,64}$"),
    path: str = "",
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    from .safefs import split_path

    parts = split_path(path)
    if not parts:
        raise AgentError("Choose a file", "invalid_path", 400)
    restic = Restic(settings, instance_id)
    # A failure before the first byte (a lock, a missing path, a folder) is a proper error answer.
    stream, first = await run_in_threadpool(restic.open_dump, snapshot_id, parts, DOWNLOAD_CHUNK)

    def _iter() -> Any:
        done = False
        try:
            if first:
                yield first
            while chunk := stream.read(DOWNLOAD_CHUNK):
                yield chunk
            code = stream.finish()
            done = True
            if code != 0:
                # restic failed part-way: abort the chunked response, so the download fails
                # instead of ending as a complete-looking, truncated file
                log.warning(
                    "restic dump of %s failed after streaming: exit %s %s", path, code, stream.stderr[-300:]
                )
                raise AgentError(f"restic dump failed (exit code {code})", "restic_failed", 500)
        finally:
            if not done:  # the client went away: stop restic so that it removes its lock
                stream.abort()

    # stream.abort only stops a restic that has not finished (also when _iter never started)
    return ClosingStreamingResponse(_iter(), on_close=stream.abort, media_type="application/octet-stream")


@app.post(
    "/v1/instances/{instance_id}/backups/{snapshot_id}/restore-path", dependencies=[Depends(require_token)]
)
async def backup_restore_path(
    instance_id: UUID,
    body: RestorePathRequest,
    snapshot_id: str = PathParam(pattern=r"^[a-f0-9]{8,64}$"),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Restore a single file or folder from a snapshot over the current one (server must be stopped)."""
    from .safefs import split_path

    parts = split_path(body.path)
    if not parts:
        raise AgentError("Choose a file or folder (use a full restore for everything)", "invalid_path", 400)
    status = await run_in_threadpool(docker_ops.status, instance_id)
    if status.get("running"):
        raise AgentError("Stop the server before restoring files into it", "must_stop", 409)
    restic = Restic(settings, instance_id)

    def _do(op: Operation) -> dict[str, Any]:
        try:
            return _restore_path(op)
        finally:
            if parts[0] == claimmarker.FOLDER:
                claimmarker.remove(settings, instance_id, "a restore of .mcpanel/ from a backup")
            claimmarker.files_changed(settings, instance_id, [body.path], "a restore from a backup")

    def _restore_path(op: Operation) -> dict[str, Any]:
        claimmarker.files_changed(settings, instance_id, [body.path], "a restore from a backup")
        root = _root(settings, instance_id)
        name = parts[-1]
        parent_rel = "/".join(parts[:-1])
        anchor_rel = _deepest_existing_dir(root, parts[:-1])
        stage_name = f".mcpanel-tmp-restore-{secrets.token_hex(6)}"  # excluded from backups
        with root.dir_fd(anchor_rel) as anchor_fd:
            # Stage inside the destination's deepest existing folder: same filesystem, btrfs
            # subvolume or ZFS dataset as the destination (folders created below it inherit it),
            # so the final rename cannot fail with EXDEV as one from <data root>/staging does.
            os.mkdir(stage_name, 0o700, dir_fd=anchor_fd)
            stage_fd = os.open(stage_name, _DIR_FLAGS, dir_fd=anchor_fd)
            try:
                op.update(0.1, f"Restoring {body.path} from backup")
                # restic gets the staging directory as an inherited descriptor: it writes into
                # that directory even if names inside the instance are changed meanwhile.
                restic.restore_path(
                    snapshot_id, parts, Path(f"/proc/self/fd/{stage_fd}/r"), pass_fds=(stage_fd,)
                )
                restored_fd = os.open("r", _DIR_FLAGS, dir_fd=stage_fd)
                try:
                    if parent_rel != anchor_rel:  # only now: a failed restore leaves no empty folders
                        root.mkdir(parent_rel, parents=True)  # through SafeRoot (no symlinks)
                    with root.dir_fd(parent_rel) as parent_fd:
                        _swap_in_restored(restored_fd, parent_fd, name)
                finally:
                    os.close(restored_fd)
            finally:
                _remove_own_dir(anchor_fd, stage_name, stage_fd)
                os.close(stage_fd)
        return {"restored": "/".join(parts)}

    op = registry.start("restore", [instance_id], _do)
    return {"operation": op.as_dict()}


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _deepest_existing_dir(root: SafeRoot, parts: list[str]) -> str:
    """The longest prefix of ``parts`` that exists as a directory (refusing files and links)."""
    found = 0
    for i in range(1, len(parts) + 1):
        rel = "/".join(parts[:i])
        try:
            entry = root.stat(rel)
        except PathError as exc:
            if exc.code == "not_found":
                break
            raise
        if entry.type != "directory":
            raise AgentError(f"'{rel}' is not a folder", "not_a_directory", 409)
        found = i
    return "/".join(parts[:found])


def _delete_entry(dir_fd: int, name: str) -> None:
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dir_fd)  # fd-based, never follows links
    else:
        os.unlink(name, dir_fd=dir_fd)


def _swap_in_restored(src_dir_fd: int, parent_fd: int, name: str) -> None:
    """Move ``name`` from ``src_dir_fd`` over ``name`` in ``parent_fd`` (same directory tree).

    The current item is renamed aside first and put back if the move fails; it is deleted
    only after the restored item is in place.
    """
    aside = None
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        aside = f".mcpanel-tmp-old-{secrets.token_hex(6)}"
        os.rename(name, aside, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    try:
        os.rename(name, name, src_dir_fd=src_dir_fd, dst_dir_fd=parent_fd)
    except OSError:
        if aside:
            os.rename(aside, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        raise
    if aside:
        try:
            _delete_entry(parent_fd, aside)
        except OSError as exc:
            log.warning("Could not delete the replaced copy %s: %s", aside, exc)


def _remove_own_dir(parent_fd: int, name: str, own_fd: int) -> None:
    """Delete ``name`` in ``parent_fd`` if it is still the directory ``own_fd`` refers to."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        own = os.fstat(own_fd)
        if (st.st_dev, st.st_ino) == (own.st_dev, own.st_ino):
            shutil.rmtree(name, dir_fd=parent_fd)
    except OSError as exc:
        log.warning("Could not remove restore staging folder %s: %s", name, exc)


@contextlib.contextmanager
def _staging_inside(volume_root: Path, what: str) -> Iterator[tuple[int, int, str]]:
    """A new hidden staging folder directly inside ``volume_root`` (a server's own ZFS dataset).

    Yields ``(root_fd, stage_fd, stage_name)``. The folder is reachable by the file manager, so
    writers go through ``/proc/self/fd/<stage_fd>/...`` (passing the descriptor on to child
    processes): renaming or replacing the folder meanwhile cannot redirect a write outside it.
    Removed on exit, failed or not, if it is still the folder that was created.
    """
    stage_name = f".mcpanel-tmp-{what}-{secrets.token_hex(6)}"  # .mcpanel-tmp-*: excluded from backups
    root_fd = os.open(volume_root, _DIR_FLAGS)
    try:
        os.mkdir(stage_name, 0o700, dir_fd=root_fd)
        stage_fd = os.open(stage_name, _DIR_FLAGS, dir_fd=root_fd)
        try:
            yield root_fd, stage_fd, stage_name
        finally:
            _remove_own_dir(root_fd, stage_name, stage_fd)
            os.close(stage_fd)
    finally:
        os.close(root_fd)


def _swap_in_staged(root_fd: int, stage_fd: int, name: str, stage_name: str) -> None:
    """Replace the entries of ``root_fd`` with those of ``name`` inside the staging folder."""
    from .snapshots import swap_entries

    staged_fd = os.open(name, _DIR_FLAGS, dir_fd=stage_fd)
    try:
        swap_entries(root_fd, staged_fd, keep=(stage_name,))
    finally:
        os.close(staged_fd)


SAVE_SETTLE_SECONDS = 1.0  # after save-all flush, before reading the world
SAVE_ON_ATTEMPTS = 5
SAVE_ON_RETRY_SECONDS = 2.0
SAVING_RECOVERY_SECONDS = 30.0  # how often paused servers left by an earlier run are retried


@dataclass
class _SavingState:
    running: bool = False
    paused: bool = False  # save-off and save-all flush both succeeded
    warnings: list[str] = field(default_factory=list)
    # how the copy was made consistent, for the panel's job log: "stopped" (not running: the files
    # were at rest), "paused" (saving paused, everything flushed), "saved" (the game saved right
    # before), "no_game" (no game loaded: nothing to save), "failed", "skipped" (consistency none)
    how: str = "stopped"

    @property
    def consistent(self) -> bool:
        return self.paused or not self.running


def _rcon(settings: Settings, instance_id: UUID, command: str, timeout: float) -> None:
    host, port, password = docker_ops.rcon_endpoint(settings, instance_id)
    asyncio.run(run_command(host, port, password, command, timeout=timeout))


# World saving that this agent switched off is recorded on disk (<state dir>/saving-paused/<id>)
# from just before save-off until save-on succeeded. The pause itself lives in the operation
# thread, which dies with the process (upgrade, restart, crash): the marker lets the shutdown
# hook, the next start and a periodic retry send save-on to a server that would otherwise keep
# running with autosave off until its next backup or restart.
_saving_guard = threading.Lock()
_saving_locks: dict[UUID, threading.Lock] = {}
_paused_here: set[UUID] = set()  # pauses of this process that are still in their block


def _saving_lock(instance_id: UUID) -> threading.Lock:
    with _saving_guard:
        return _saving_locks.setdefault(instance_id, threading.Lock())


def _saving_marker_dir(settings: Settings) -> Path:
    return settings.state_dir / "saving-paused"


def _saving_marker(settings: Settings, instance_id: UUID) -> Path:
    return _saving_marker_dir(settings) / str(instance_id)


def _mark_saving_paused(settings: Settings, instance_id: UUID) -> None:
    marker = _saving_marker(settings, instance_id)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(f".{marker.name}.tmp")
        tmp.write_text(f"{int(time.time())}\n")
        os.replace(tmp, marker)
    except OSError as exc:  # the pause goes on; only the recovery after a crash is lost
        log.warning("Could not record that world saving of %s is paused: %s", instance_id, exc)


def _clear_saving_marker(settings: Settings, instance_id: UUID) -> None:
    with contextlib.suppress(FileNotFoundError):
        _saving_marker(settings, instance_id).unlink()


def _log_waiter(instance_id: UUID) -> drivers.WaitLog:
    """``wait_log`` for a driver's save: waits for a line in the running container's log."""

    def wait(pattern: str, since: float, timeout: float) -> bool:
        return docker_ops.wait_for_log(docker_ops.running_container(instance_id), pattern, since, timeout)

    return wait


def _sender_with_timeout(settings: Settings, instance_id: UUID, timeout: float) -> drivers.Send:
    """Like :func:`_sender`, every command bounded by ``timeout`` instead of the driver's own
    (the recovery loop and the shutdown hook)."""
    return lambda command, _timeout: _rcon(settings, instance_id, command, timeout)


def _sender(settings: Settings, instance_id: UUID) -> drivers.Send:
    """The channel the saving pause sends its commands over (RCON drivers: :func:`_rcon`)."""
    return lambda command, timeout: _rcon(settings, instance_id, command, timeout)


@contextlib.contextmanager
def _world_saving_paused(
    settings: Settings, instance_id: UUID, consistency: str, op: Operation, what: str, progress: float
) -> Iterator[_SavingState]:
    """World saving paused on a running server for the duration of the block, the way its game
    does it (the container's control driver; Minecraft: ``save-off`` + ``save-all flush``, and
    ``save-on`` afterwards).

    Once the first pause command has been sent (even if its reply was lost), saving is resumed
    when the block ends, whatever happens: a failed flush, ``consistency=require`` refusing to go
    on, an exception or timeout in the block. With ``require`` a failed pause raises
    ``consistency_failed``; with ``auto`` it only adds a warning. If the process ends inside the
    block, the marker file makes the next agent (or this one's shutdown hook) resume saving.
    A driver that cannot pause but can save gets a save before the copy (no marker: nothing to
    resume); one that can do neither makes the copy inconsistent.
    """
    status = docker_ops.status(instance_id)
    state = _SavingState(running=bool(status.get("running")))
    driver = drivers.for_status(status)
    save_off_sent = False
    # an HTTP API driver saves over its own channel; RCON drivers over main._rcon
    send = driver.channel(settings, instance_id) or _sender(settings, instance_id)

    def send_pausing(command: str, timeout: float) -> None:
        nonlocal save_off_sent
        if not save_off_sent:
            with _saving_lock(instance_id):
                _paused_here.add(instance_id)
                _mark_saving_paused(settings, instance_id)
            save_off_sent = True
        send(command, timeout)

    def report(step: int, message: str) -> None:
        op.update(progress + 0.02 * step, message)

    try:
        if state.running and consistency == "none":
            state.how = "skipped"
        if state.running and consistency != "none":
            try:
                if driver.can_pause_saving:
                    driver.pause_saving(send_pausing, report)
                    state.how = "paused"
                elif driver.can_save:
                    outcome = driver.save(send, report, _log_waiter(instance_id))
                    # Satisfactory: no game loaded, so nothing was saved (and nothing changes)
                    state.how = "no_game" if outcome == "nothing_loaded" else "saved"
                else:
                    raise AgentError("this server's game cannot save on request", "save_unsupported", 409)
                time.sleep(SAVE_SETTLE_SECONDS)
                state.paused = True
            except Exception as exc:  # RCON, Docker (container gone meanwhile) or socket errors
                state.how = "failed"
                if consistency == "require":
                    raise AgentError(
                        f"Could not coordinate world saving: {exc}", "consistency_failed", 409
                    ) from exc
                state.warnings.append(f"Could not pause world saving ({exc}); {what} may be inconsistent")
        yield state
    finally:
        if save_off_sent:
            try:
                _resume_saving(settings, instance_id, state.warnings, driver)
            finally:
                with _saving_lock(instance_id):
                    _paused_here.discard(instance_id)


def _resume_saving(
    settings: Settings, instance_id: UUID, warnings: list[str], driver: drivers.ControlDriver | None = None
) -> None:
    """Resume saving (Minecraft: ``save-on``), retrying; never raises, so it cannot mask the
    block's own error.

    The marker is removed once it worked; after a failure it stays, and the periodic recovery
    (:func:`_recover_saving`) keeps trying."""
    if driver is None:
        try:
            driver = drivers.for_status(docker_ops.status(instance_id))
        except Exception:  # Docker not reachable: the container's labels cannot be read
            driver = drivers.for_labels({})
    for attempt in range(SAVE_ON_ATTEMPTS):
        try:
            driver.resume_saving(_sender(settings, instance_id))
            _clear_saving_marker(settings, instance_id)
            return
        except Exception as exc:
            if attempt == SAVE_ON_ATTEMPTS - 1:
                log.error("Could not re-enable world saving on %s: %s", instance_id, exc)
                warnings.append(f"FAILED to re-enable world saving ({driver.resume_label}): {exc}")
                return
            time.sleep(SAVE_ON_RETRY_SECONDS)


def _marked_instances(settings: Settings) -> list[UUID]:
    out = []
    try:
        names = sorted(os.listdir(_saving_marker_dir(settings)))
    except OSError:
        return []
    for name in names:
        try:
            out.append(UUID(name))
        except ValueError:
            continue  # a temp file of a write in progress, or not ours
    return out


def _recover_saving(settings: Settings, *, timeout: float = 30.0) -> int:
    """Send ``save-on`` to every server with a saving-paused marker; returns how many are left.

    Skipped (still left): a pause of this process that is still in its block, and servers that
    cannot be reached yet. A server without a running container needs nothing: saving is on when
    it starts again, so its marker is removed.
    """
    left = 0
    for instance_id in _marked_instances(settings):
        with _saving_lock(instance_id):
            if instance_id in _paused_here:
                continue
            try:
                status = docker_ops.status(instance_id)
            except Exception as exc:  # Docker not reachable (yet)
                log.debug("Saving recovery of %s: cannot read the container state: %s", instance_id, exc)
                left += 1
                continue
            driver = drivers.for_status(status)
            if not status.get("running") or not driver.can_pause_saving:
                # saving is on when it starts again; a container of a game that cannot pause
                # (replaced meanwhile) has nothing to resume
                _clear_saving_marker(settings, instance_id)
                continue
            try:
                driver.resume_saving(_sender_with_timeout(settings, instance_id, timeout))
            except Exception as exc:
                log.warning(
                    "World saving of %s is still paused; %s failed: %s", instance_id, driver.resume_label, exc
                )
                left += 1
                continue
            _clear_saving_marker(settings, instance_id)
            log.warning(
                "Re-enabled world saving (%s) on %s: a backup or snapshot was interrupted while it was paused",
                driver.resume_label,
                instance_id,
            )
    return left


def _saving_recovery_loop(settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            _recover_saving(settings)
        except Exception:
            log.exception("Saving recovery failed")
        stop.wait(SAVING_RECOVERY_SECONDS)


def _resume_all_saving(settings: Settings, timeout: float) -> None:
    """Shutdown: save-on for every marked server, this process's own pauses included, all at once."""
    threads = []
    for instance_id in _marked_instances(settings):

        def one(iid: UUID = instance_id) -> None:
            try:
                status = docker_ops.status(iid)
                driver = drivers.for_status(status)
                if status.get("running") and driver.can_pause_saving:
                    driver.resume_saving(_sender_with_timeout(settings, iid, timeout))
                    log.warning(
                        "Re-enabled world saving (%s) on %s before the agent exits", driver.resume_label, iid
                    )
                _clear_saving_marker(settings, iid)
            except Exception as exc:
                log.warning("Could not re-enable world saving on %s before the agent exits: %s", iid, exc)

        threads.append(_spawn(f"save-on-{instance_id.hex[:8]}", one))
    for t in threads:
        t.join(timeout + 1)


@app.get("/v1/instances/{instance_id}/snapshots", dependencies=[Depends(require_token)])
async def list_snapshots(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    from .snapshots import snapshot_provider

    snaps = snapshot_provider(settings, instance_id)
    cap = await run_in_threadpool(snaps.capability)
    items = await run_in_threadpool(snaps.list) if cap.get("instance_ready") else []
    return {"capability": cap, "snapshots": items}


@app.post("/v1/instances/{instance_id}/snapshots", dependencies=[Depends(require_token)])
async def create_snapshot(
    instance_id: UUID, body: SnapshotRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    from .snapshots import snapshot_provider

    snaps = snapshot_provider(settings, instance_id)
    await run_in_threadpool(snaps.require_ready)

    def _do(op: Operation) -> dict[str, Any]:
        with _world_saving_paused(settings, instance_id, body.consistency, op, "snapshot", 0.05) as saving:
            op.update(0.5, "Creating snapshot")
            info = snaps.create(body.label, {"consistent": saving.consistent, "kind": body.kind})
        info["warnings"] = saving.warnings
        return info

    op = registry.start("snapshot", [instance_id], _do)
    return {"operation": op.as_dict()}


@app.delete("/v1/instances/{instance_id}/snapshots/{name}", dependencies=[Depends(require_token)])
async def delete_snapshot(
    instance_id: UUID, name: str, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    from .snapshots import snapshot_provider

    await run_in_threadpool(snapshot_provider(settings, instance_id).delete, name)
    return {"deleted": name}


@app.post("/v1/instances/{instance_id}/snapshots/{name}/restore", dependencies=[Depends(require_token)])
async def restore_snapshot(
    instance_id: UUID, name: str, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    from .snapshots import snapshot_provider

    if (await run_in_threadpool(docker_ops.status, instance_id)).get("running"):
        raise AgentError("Stop the server before rolling back to a snapshot", "must_stop", 409)
    snaps = snapshot_provider(settings, instance_id)

    def _do(_op: Operation) -> Any:
        claimmarker.remove(settings, instance_id, "a snapshot rollback began")
        try:
            return snaps.restore(name)
        finally:
            claimmarker.remove(settings, instance_id, "the server folder was rolled back to a snapshot")

    op = registry.start("restore", [instance_id], _do)
    return {"operation": op.as_dict()}


@app.post("/v1/instances/{instance_id}/snapshots/convert", dependencies=[Depends(require_token)])
async def convert_for_snapshots(
    instance_id: UUID, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Make a plain server folder a btrfs subvolume or ZFS dataset (server stopped)."""
    from .snapshots import snapshot_provider

    if (await run_in_threadpool(docker_ops.status, instance_id)).get("running"):
        raise AgentError("Stop the server before enabling snapshots", "must_stop", 409)
    snaps = snapshot_provider(settings, instance_id)
    op = registry.start("import", [instance_id], lambda _op: snaps.convert())
    return {"operation": op.as_dict()}


# --- operations ----------------------------------------------------------------------
@app.get("/v1/operations/{op_id}", dependencies=[Depends(require_token)])
async def get_operation(op_id: str) -> dict[str, Any]:
    op = registry.get(op_id)
    if op is None:
        raise HTTPException(404, "Operation not found (the agent may have restarted)")
    return op.as_dict()


@app.get("/v1/operations", dependencies=[Depends(require_token)])
async def list_operations(instance_id: UUID | None = None) -> dict[str, Any]:
    return {"operations": [o.as_dict() for o in registry.list(instance_id)]}
