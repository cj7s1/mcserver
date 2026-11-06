"""HTTP/WebSocket API of the MCPanel node agent.

The agent is the only component with Docker socket access. It must only be
reachable on the internal Compose network; every request needs the shared bearer
token. The API is intentionally narrow: it manages containers created from a
validated spec and files inside instance directories — nothing else on the host.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from typing import Any
from uuid import UUID

import docker.errors
import requests.exceptions
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketState

from . import __version__, docker_ops, hashcache, storage, tls
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
    MkdirRequest,
    PrepareInstanceRequest,
    RenameRequest,
    RestorePathRequest,
    RestoreRequest,
    SnapshotRequest,
    StopRequest,
    WriteTextRequest,
)

logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("mcagent")

app = FastAPI(
    title="MCPanel Agent", version=__version__, docs_url=None, redoc_url=None, openapi_url="/v1/openapi.json"
)
TEXT_LIMIT = 5 * 1024 * 1024
UPLOAD_LIMIT = int(os.environ.get("AGENT_MAX_UPLOAD_BYTES", str(4 * 1024 * 1024 * 1024)))


_blocked_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def _load_blocked_networks(settings: Settings) -> None:
    """Subnets of the Minecraft instance network(s): requests from there are refused.

    Minecraft containers run third-party code and share a network with the agent (needed
    for RCON); they must never be able to talk to the agent API, token or not.
    """
    nets = []
    for name in {settings.instance_network, *settings.allowed_networks} - {"host"}:
        try:
            net = docker_ops.client().networks.get(name)
        except Exception as exc:  # network may not exist yet
            log.debug("cannot inspect network %s: %s", name, exc)
            continue
        for cfg in (net.attrs.get("IPAM") or {}).get("Config") or []:
            with contextlib.suppress(ValueError, TypeError):
                nets.append(ipaddress.ip_network(cfg.get("Subnet"), strict=False))
    _blocked_networks[:] = nets
    if nets:
        log.info("Refusing agent API requests from instance networks: %s", ", ".join(map(str, nets)))


def _source_blocked(client: Any) -> str | None:
    """The client address if it is on an instance network, else None."""
    client_ip = client[0] if client else None
    if not client_ip or not _blocked_networks:
        return None
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return None
    return client_ip if any(ip in net for net in _blocked_networks) else None


class _RefuseInstanceNetworks:
    """Refuse HTTP requests *and* WebSocket handshakes from instance networks (pure ASGI).

    A ``@app.middleware("http")`` function never sees WebSocket connections, so the
    log stream would otherwise be reachable from a Minecraft container.
    """

    def __init__(self, asgi_app: Any) -> None:
        self.app = asgi_app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            blocked = _source_blocked(scope.get("client"))
            if blocked:
                log.warning("Refused agent API %s from instance network address %s", scope["type"], blocked)
                if scope["type"] == "http":
                    response = JSONResponse(
                        {"error": {"code": "forbidden_source", "message": "Forbidden"}}, status_code=403
                    )
                    await response(scope, receive, send)
                else:  # closing before accept: the handshake is answered with HTTP 403
                    await WebSocket(scope, receive, send).close(code=4403)
                return
        await self.app(scope, receive, send)


app.add_middleware(_RefuseInstanceNetworks)


@app.exception_handler(AgentError)
async def _agent_error(_request: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)


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
    return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)


@app.on_event("startup")
async def _startup() -> None:
    settings = get_settings()
    for d in (
        settings.instances_dir,
        settings.backups_dir,
        settings.staging_dir,
        settings.trash_dir,
        settings.state_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    if not settings.resolve_token():
        log.error("AGENT_TOKEN is not configured; all requests will be rejected")
    try:
        await run_in_threadpool(docker_ops.ensure_network, settings)
        await run_in_threadpool(_load_blocked_networks, settings)
    except Exception as exc:  # docker not reachable yet; health endpoint will report it
        log.warning("Could not ensure instance network: %s", exc)


# At most one background pass per folder (two at a time) for progressive /files/hashes calls.
_hash_jobs = hashcache.HashJobs(workers=2)


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _hash_jobs
    _hash_jobs.shutdown()  # stop background hashing so the process can exit promptly
    _hash_jobs = hashcache.HashJobs(workers=2)


def _root(settings: Settings, instance_id: UUID) -> SafeRoot:
    return SafeRoot(
        docker_ops.instance_dir(settings, instance_id), settings.runtime_uid, settings.runtime_gid
    )


def _spec_hash(spec: Any) -> str:
    return hashlib.sha256(json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:16]


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
    }


@app.get("/v1/ports", dependencies=[Depends(require_token)])
async def ports() -> dict[str, Any]:
    return {"ports": await run_in_threadpool(docker_ops.published_ports)}


# --- instances: lifecycle ------------------------------------------------------------
@app.get("/v1/instances", dependencies=[Depends(require_token)])
async def list_instances() -> dict[str, Any]:
    return {"containers": await run_in_threadpool(docker_ops.list_managed)}


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


@app.put("/v1/instances/{instance_id}/container", dependencies=[Depends(require_token)])
async def apply_spec(
    instance_id: UUID, body: ApplySpecRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """(Re)create the container from a spec as an operation. World data is never touched."""
    image = docker_ops.validate_image(settings, body.spec.image)
    docker_ops.build_create_kwargs(settings, instance_id, body.spec, "validate")  # validate before queuing
    registry.assert_lifecycle_allowed(instance_id)

    def _apply(op: Operation) -> dict[str, Any]:
        if body.pull == "always" or (body.pull == "missing" and not docker_ops.image_present(image)):
            op.update(0.05, f"Pulling image {image}")
            docker_ops.pull_image(image, lambda p, m: op.update(0.05 + p * 0.8, m))
        elif body.pull == "never" and not docker_ops.image_present(image):
            raise AgentError(f"Image {image} is not present and pulling is disabled", "image_missing", 409)
        from .snapshots import ensure_instance_dir

        path = docker_ops.instance_dir(settings, instance_id)
        ensure_instance_dir(settings, path)
        with contextlib.suppress(PermissionError):
            os.chown(path, settings.runtime_uid, settings.runtime_gid)
        existing = docker_ops.get_container(instance_id)
        was_running = False
        if existing is not None:
            existing.reload()
            was_running = existing.status in ("running", "restarting")
            op.update(0.88, "Stopping and removing previous container (data is kept)")
            docker_ops.remove_container(instance_id, timeout=body.spec.stop_timeout)
        op.update(0.92, "Creating container")
        c = docker_ops.create_container(settings, instance_id, body.spec, _spec_hash(body.spec))
        if body.start or was_running:
            op.update(0.96, "Starting container")
            docker_ops.start(instance_id)
        c.reload()
        return {"container": docker_ops.describe(c), "recreated": existing is not None}

    op = registry.start("apply", [instance_id], _apply)
    return {"operation": op.as_dict()}


@app.delete("/v1/instances/{instance_id}/container", dependencies=[Depends(require_token)])
async def remove_container(instance_id: UUID, timeout: int = Query(60, ge=1, le=900)) -> dict[str, Any]:
    registry.assert_lifecycle_allowed(instance_id)
    removed = await run_in_threadpool(docker_ops.remove_container, instance_id, timeout=timeout)
    return {"removed": removed}


@app.get("/v1/instances/{instance_id}/status", dependencies=[Depends(require_token)])
async def instance_status(instance_id: UUID) -> dict[str, Any]:
    st = await run_in_threadpool(docker_ops.status, instance_id)
    active = registry.active_for(instance_id)
    st["operation"] = active.as_dict() if active else None
    return st


@app.post("/v1/instances/{instance_id}/start", dependencies=[Depends(require_token)])
async def start(instance_id: UUID) -> dict[str, Any]:
    registry.assert_lifecycle_allowed(instance_id)
    await run_in_threadpool(docker_ops.start, instance_id)
    return await run_in_threadpool(docker_ops.status, instance_id)


@app.post("/v1/instances/{instance_id}/stop", dependencies=[Depends(require_token)])
async def stop(instance_id: UUID, body: StopRequest | None = None) -> dict[str, Any]:
    registry.assert_lifecycle_allowed(instance_id)
    await run_in_threadpool(docker_ops.stop, instance_id, body.timeout if body else None)
    return await run_in_threadpool(docker_ops.status, instance_id)


@app.post("/v1/instances/{instance_id}/restart", dependencies=[Depends(require_token)])
async def restart(instance_id: UUID, body: StopRequest | None = None) -> dict[str, Any]:
    registry.assert_lifecycle_allowed(instance_id)
    await run_in_threadpool(docker_ops.stop, instance_id, body.timeout if body else None)
    await run_in_threadpool(docker_ops.start, instance_id)
    return await run_in_threadpool(docker_ops.status, instance_id)


@app.post("/v1/instances/{instance_id}/kill", dependencies=[Depends(require_token)])
async def kill(instance_id: UUID) -> dict[str, Any]:
    await run_in_threadpool(docker_ops.kill, instance_id)
    return await run_in_threadpool(docker_ops.status, instance_id)


@app.get("/v1/instances/{instance_id}/stats", dependencies=[Depends(require_token)])
async def stats(instance_id: UUID) -> dict[str, Any]:
    return await run_in_threadpool(docker_ops.stats, instance_id)


@app.get("/v1/instances/{instance_id}/disk", dependencies=[Depends(require_token)])
async def disk(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return await run_in_threadpool(storage.instance_usage, settings, instance_id)


@app.post("/v1/instances/{instance_id}/command", dependencies=[Depends(require_token)])
async def command(
    instance_id: UUID, body: CommandRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    host, port, password = await run_in_threadpool(docker_ops.rcon_endpoint, settings, instance_id)
    try:
        output = await run_command(host, port, password, body.command, timeout=body.timeout)
    except RconError as exc:
        raise AgentError(str(exc), "rcon_failed", 502) from exc
    return {"output": output}


@app.delete("/v1/instances/{instance_id}/data", dependencies=[Depends(require_token)])
async def delete_data(
    instance_id: UUID, body: DeleteDataRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Permanently delete an instance's container, files, snapshots and backup repository."""
    from .snapshots import BtrfsSnapshots

    if body.confirm != instance_id:
        raise AgentError("Confirmation id does not match", "confirmation_mismatch", 400)

    def _delete(op: Operation) -> dict[str, Any]:
        op.update(0.1, "Removing container")
        docker_ops.remove_container(instance_id, timeout=30)
        snapshots = BtrfsSnapshots(settings, instance_id)
        if snapshots.snap_dir.is_dir():
            # before the files: if btrfs refuses, the server is still intact and the delete can be retried
            op.update(0.2, "Deleting filesystem snapshots")
            snapshots.purge()
        path = docker_ops.instance_dir(settings, instance_id)
        if path.exists():
            op.update(0.3, "Deleting instance files")
            trash = settings.trash_dir / f"{instance_id}-{int(time.time())}"
            path.rename(trash)
            shutil.rmtree(trash, ignore_errors=True)
        hashcache.HashCache(settings.hash_cache_dir).forget(instance_id)
        repo = settings.backups_dir / "restic" / str(instance_id)
        if repo.exists():
            op.update(0.8, "Deleting backup repository")
            shutil.rmtree(repo, ignore_errors=True)
        return {"deleted": True}

    op = registry.start("delete_data", [instance_id], _delete)
    return {"operation": op.as_dict()}


@app.post("/v1/instances/{instance_id}/clone", dependencies=[Depends(require_token)])
async def clone(
    instance_id: UUID, body: CloneRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    src = docker_ops.instance_dir(settings, instance_id)
    dst = docker_ops.instance_dir(settings, body.target_instance_id)
    if not src.exists():
        raise AgentError("Source instance has no data directory", "not_found", 404)
    if dst.exists() and any(dst.iterdir()):
        raise AgentError("Target instance directory is not empty", "target_not_empty", 409)
    status = await run_in_threadpool(docker_ops.status, instance_id)
    if status.get("running"):
        raise AgentError("Stop the source server before cloning it", "source_running", 409)

    def _clone(op: Operation) -> dict[str, Any]:
        op.update(0.05, "Copying files")
        staging = settings.staging_dir / f"clone-{body.target_instance_id}"
        if staging.exists():
            shutil.rmtree(staging)
        worlds = {"world", "world_nether", "world_the_end"} if body.exclude_worlds else set()

        def ignore(directory: str, names: list[str]) -> set[str]:
            skipped = worlds & set(names)
            for name in names:  # FIFOs, sockets and devices (a mod can mkfifo) are never copied
                try:
                    mode = os.lstat(os.path.join(directory, name)).st_mode
                except OSError:
                    continue
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                    skipped.add(name)
            return skipped

        shutil.copytree(src, staging, symlinks=True, ignore=ignore)
        if dst.exists():
            dst.rmdir()
        staging.rename(dst)
        return {"bytes": storage.dir_size(dst)}

    op = registry.start("clone_target", [instance_id, body.target_instance_id], _clone)
    return {"operation": op.as_dict()}


# --- logs ----------------------------------------------------------------------------
@app.get("/v1/instances/{instance_id}/logs", dependencies=[Depends(require_token)])
async def get_logs(
    instance_id: UUID, tail: int = Query(500, ge=1, le=20000), since: int | None = None
) -> dict[str, Any]:
    text = await run_in_threadpool(docker_ops.logs, instance_id, tail, since)
    return {"lines": text.splitlines()}


# Console output is produced by the Minecraft process (third-party code): every buffer
# between Docker and the WebSocket is bounded, and a client that cannot keep up loses the
# oldest lines (with a notice) instead of growing the agent's memory.
LOG_LINE_MAX_BYTES = 16 * 1024
LOG_BUFFER_MAX_LINES = 5000
LOG_BUFFER_MAX_BYTES = 4 * 1024 * 1024
LOG_BATCH_MAX_LINES = 500
LOG_BATCH_MAX_BYTES = 512 * 1024  # keeps one WebSocket message far below the panel's 8 MiB limit
LOG_TRUNCATED_SUFFIX = " [mcpanel: line truncated]"


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
        max_lines: int = LOG_BUFFER_MAX_LINES,
        max_bytes: int = LOG_BUFFER_MAX_BYTES,
    ) -> None:
        self._loop = loop
        self.max_lines = max_lines
        self.max_bytes = max_bytes
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


@app.websocket("/v1/instances/{instance_id}/logs/stream")
async def stream_logs(websocket: WebSocket, instance_id: UUID, tail: int = 200) -> None:
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
        while not stop_event.is_set():
            try:
                c = docker_ops.get_container(instance_id)
            except Exception as exc:
                emit({"type": "error", "message": str(exc)})
                return
            if c is None:
                if last_state != "missing":
                    emit({"type": "state", "state": "missing"})
                    last_state = "missing"
                stop_event.wait(2)
                continue
            c.reload()
            state = c.status
            if state != last_state:
                emit({"type": "state", "state": state})
                last_state = state
            if state != "running" and not first:
                stop_event.wait(2)
                continue
            kwargs: dict[str, Any] = {"stream": True, "follow": True, "stdout": True, "stderr": True}
            if first:
                kwargs["tail"] = tail if tail > 0 else 0
            else:
                kwargs["since"] = since or int(time.time())
                kwargs["tail"] = "all"
            first = False
            try:
                stream = c.logs(**kwargs)
            except Exception as exc:
                emit({"type": "error", "message": f"Cannot attach to logs: {exc}"})
                stop_event.wait(2)
                continue
            current_stream["s"] = stream
            splitter = _LineSplitter()
            try:
                for chunk in stream:
                    if stop_event.is_set():
                        break
                    for line in splitter.feed(chunk):
                        emit({"type": "log", "line": line})
            except Exception as exc:  # stream broke (container stopped / daemon restarted)
                log.debug("log stream for %s ended: %s", instance_id, exc)
            finally:
                for line in splitter.flush():
                    emit({"type": "log", "line": line})
                with contextlib.suppress(Exception):
                    stream.close()
                current_stream.pop("s", None)
            since = int(time.time())
            # the container stopped (stream ended); report and wait for it to come back
            stop_event.wait(1)
        emit({"type": "closed"})

    thread = threading.Thread(target=pump, daemon=True, name=f"logs-{instance_id.hex[:8]}")
    thread.start()

    async def receiver() -> None:
        try:
            while True:
                await websocket.receive_text()  # we ignore client messages; detect disconnect
        except WebSocketDisconnect:
            pass

    recv_task = asyncio.create_task(receiver())
    try:
        while not recv_task.done():
            batch = await buffer.get_batch(timeout=15)
            if batch is None:
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


@app.get("/v1/instances/{instance_id}/files/text", dependencies=[Depends(require_token)])
async def files_read_text(
    instance_id: UUID, path: str, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    root = _root(settings, instance_id)
    data = await run_in_threadpool(root.read_bytes, path, TEXT_LIMIT)
    if b"\x00" in data[:8192]:
        raise AgentError("This looks like a binary file and cannot be edited as text", "binary_file", 415)
    entry = await run_in_threadpool(root.stat, path)
    return {
        "path": entry.path,
        "content": data.decode("utf-8", errors="replace"),
        "size": entry.size,
        "modified": entry.modified,
    }


@app.put("/v1/instances/{instance_id}/files/text", dependencies=[Depends(require_token)])
async def files_write_text(
    instance_id: UUID, body: WriteTextRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    root = _root(settings, instance_id)
    size = await run_in_threadpool(
        root.write_bytes, body.path, body.content.encode("utf-8"), overwrite=not body.create_only
    )
    return {"path": body.path, "size": size}


@app.get("/v1/instances/{instance_id}/files/download", dependencies=[Depends(require_token)])
async def files_download(
    instance_id: UUID, path: str, settings: Settings = Depends(get_settings)
) -> StreamingResponse:
    root = _root(settings, instance_id)
    entry = await run_in_threadpool(root.stat, path)
    if entry.type != "file":
        raise AgentError("Only regular files can be downloaded", "not_a_file", 400)

    def _iter() -> Any:
        with root.open_read(path) as (fh, _size):
            while chunk := fh.read(256 * 1024):
                yield chunk

    return StreamingResponse(
        _iter(), media_type="application/octet-stream", headers={"Content-Length": str(entry.size)}
    )


@app.get("/v1/instances/{instance_id}/files/archive", dependencies=[Depends(require_token)])
async def files_archive(
    instance_id: UUID, path: str = "", settings: Settings = Depends(get_settings)
) -> StreamingResponse:
    """Download a directory (e.g. a world) as a zip streamed on the fly."""
    from .archive import stream_zip

    root = _root(settings, instance_id)
    entry = await run_in_threadpool(root.stat, path)
    if entry.type != "directory":
        raise AgentError("Only folders can be downloaded as a zip", "not_a_directory", 400)
    gen = await run_in_threadpool(lambda: stream_zip(root, path))
    return StreamingResponse(gen, media_type="application/zip")


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
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Hash every regular file directly inside ``path`` (used to identify mods/plugins).

    Digests are cached on disk per file (name, size, mtime, ctime, inode), so only new or changed
    files are read. Without ``wait`` the call returns once every file is hashed. With
    ``wait`` it returns after at most about that many seconds; files not read yet then
    have ``hash: null``, ``complete`` is false, and hashing continues in the background.
    """
    root = _root(settings, instance_id)
    cache = hashcache.HashCache(settings.hash_cache_dir)
    try:
        result = await run_in_threadpool(
            hashcache.hash_directory, root, cache, _hash_jobs, instance_id, path, algorithm, wait=wait
        )
    except PathError as exc:
        if exc.code != "not_found":
            raise
        result = {"files": [], "complete": True, "pending": 0}
    except hashcache.HashingCancelled as exc:
        raise AgentError("The agent is shutting down", "shutting_down", 503) from exc
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
    """Safely extract an archive that is already inside the instance (import server/world/server pack)."""
    from .archive import extract

    status = await run_in_threadpool(docker_ops.status, instance_id)
    if status.get("running"):
        raise AgentError("Stop the server before extracting archives into it", "must_stop", 409)
    root = _root(settings, instance_id)
    entry = await run_in_threadpool(root.stat, body.path)
    if entry.type != "file":
        raise AgentError("The archive must be a regular file", "not_a_file", 400)

    def _do(op: Operation) -> dict[str, Any]:
        op.update(0.01, f"Extracting {body.path}")
        result = extract(
            root,
            body.path,
            body.destination,
            strip=body.strip,
            overwrite=body.overwrite,
            progress=lambda p, m: op.update(p * 0.98, m),
        )
        if body.delete_archive:
            root.delete(body.path)
        return result

    op = registry.start("import", [instance_id], _do)
    return {"operation": op.as_dict()}


# --- backups -------------------------------------------------------------------------
@app.get("/v1/instances/{instance_id}/backups", dependencies=[Depends(require_token)])
async def list_backups(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    r = Restic(settings, instance_id)
    return {"snapshots": await run_in_threadpool(r.snapshots)}


@app.post("/v1/instances/{instance_id}/backups", dependencies=[Depends(require_token)])
async def create_backup(
    instance_id: UUID, body: BackupRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    source = docker_ops.instance_dir(settings, instance_id)
    if not source.exists():
        raise AgentError("Instance has no data directory", "not_found", 404)
    restic = Restic(settings, instance_id)

    def _backup(op: Operation) -> dict[str, Any]:
        with _world_saving_paused(settings, instance_id, body.consistency, op, "backup", 0.02) as saving:
            op.update(0.06, "Running restic backup")
            summary = restic.backup(
                source,
                [f"instance:{instance_id}", f"backup:{body.backup_id}", *body.tags],
                body.excludes,
                lambda p, m: op.update(0.06 + p * 0.9, m),
            )
        summary["warnings"] = saving.warnings + summary.get("warnings", [])
        summary["consistent"] = saving.consistent
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
        staging = settings.staging_dir / f"restore-{target_id}-{int(time.time())}"
        op.update(0.02, "Restoring snapshot into staging area")
        try:
            result = restic.restore(body.snapshot_id, staging, lambda p, m: op.update(0.02 + p * 0.9, m))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        op.update(0.94, "Swapping restored files into place")
        old = None
        if target_dir.exists():
            old = settings.trash_dir / f"pre-restore-{target_id}-{int(time.time())}"
            target_dir.rename(old)
        try:
            staging.rename(target_dir)
        except BaseException:
            if old is not None:
                old.rename(target_dir)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return result

    kinds = [instance_id] if target_id == instance_id else [instance_id, target_id]
    op = registry.start("restore", kinds, _restore)
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
    proc = await run_in_threadpool(Restic(settings, instance_id).dump, snapshot_id, parts)

    def _iter() -> Any:
        assert proc.stdout is not None
        try:
            while chunk := proc.stdout.read(256 * 1024):
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait(timeout=30)

    return StreamingResponse(_iter(), media_type="application/octet-stream")


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
    inst_dir = docker_ops.instance_dir(settings, instance_id)

    def _do(op: Operation) -> dict[str, Any]:
        staging = settings.staging_dir / f"restore-path-{instance_id}-{int(time.time())}"
        try:
            op.update(0.1, f"Restoring {body.path} from backup")
            restored = restic.restore_path(snapshot_id, parts, staging)
            # Ensure the parent exists inside the instance, using the confined API.
            root = _root(settings, instance_id)
            if len(parts) > 1:
                root.mkdir("/".join(parts[:-1]), parents=True)
            # The parent directory was created/validated through SafeRoot (no symlinks); move into it.
            parent_fd = os.open(inst_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for comp in parts[:-1]:
                    nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    os.close(parent_fd)
                    parent_fd = nfd
                name = parts[-1]
                trash_name = None
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    trash_name = f".mcpanel-old-{int(time.time())}-{name}"[:255]
                    os.rename(name, trash_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                staging_fd = os.open(restored.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.rename(restored.name, name, src_dir_fd=staging_fd, dst_dir_fd=parent_fd)
                except OSError:
                    if trash_name:
                        os.rename(trash_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    raise
                finally:
                    os.close(staging_fd)
                if trash_name:
                    old = SafeRoot(inst_dir)
                    old.delete("/".join([*parts[:-1], trash_name]), recursive=True)
            finally:
                os.close(parent_fd)
            return {"restored": "/".join(parts)}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    op = registry.start("restore", [instance_id], _do)
    return {"operation": op.as_dict()}


SAVE_SETTLE_SECONDS = 1.0  # after save-all flush, before reading the world
SAVE_ON_ATTEMPTS = 5
SAVE_ON_RETRY_SECONDS = 2.0


@dataclass
class _SavingState:
    running: bool = False
    paused: bool = False  # save-off and save-all flush both succeeded
    warnings: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return self.paused or not self.running


def _rcon(settings: Settings, instance_id: UUID, command: str, timeout: float) -> None:
    host, port, password = docker_ops.rcon_endpoint(settings, instance_id)
    asyncio.run(run_command(host, port, password, command, timeout=timeout))


@contextlib.contextmanager
def _world_saving_paused(
    settings: Settings, instance_id: UUID, consistency: str, op: Operation, what: str, progress: float
) -> Iterator[_SavingState]:
    """``save-off`` + ``save-all flush`` on a running server for the duration of the block.

    Once ``save-off`` has been sent (even if its reply was lost), ``save-on`` is sent when the
    block ends, whatever happens: a failed flush, ``consistency=require`` refusing to go on,
    an exception or timeout in the block. With ``require`` a failed pause raises
    ``consistency_failed``; with ``auto`` it only adds a warning.
    """
    state = _SavingState(running=bool(docker_ops.status(instance_id).get("running")))
    save_off_sent = False
    try:
        if state.running and consistency != "none":
            try:
                op.update(progress, "Pausing world saving (save-off)")
                save_off_sent = True
                _rcon(settings, instance_id, "save-off", 30)
                op.update(progress + 0.02, "Flushing world to disk (save-all flush)")
                _rcon(settings, instance_id, "save-all flush", 300)
                time.sleep(SAVE_SETTLE_SECONDS)
                state.paused = True
            except (RconError, AgentError, OSError) as exc:
                if consistency == "require":
                    raise AgentError(
                        f"Could not coordinate world saving: {exc}", "consistency_failed", 409
                    ) from exc
                state.warnings.append(f"Could not pause world saving ({exc}); {what} may be inconsistent")
        yield state
    finally:
        if save_off_sent:
            _resume_saving(settings, instance_id, state.warnings)


def _resume_saving(settings: Settings, instance_id: UUID, warnings: list[str]) -> None:
    """Send save-on (retrying); never raises, so it cannot mask the block's own error."""
    for attempt in range(SAVE_ON_ATTEMPTS):
        try:
            _rcon(settings, instance_id, "save-on", 30)
            return
        except Exception as exc:
            if attempt == SAVE_ON_ATTEMPTS - 1:
                log.error("Could not re-enable world saving on %s: %s", instance_id, exc)
                warnings.append(f"FAILED to re-enable world saving (save-on): {exc}")
                return
            time.sleep(SAVE_ON_RETRY_SECONDS)


@app.get("/v1/instances/{instance_id}/snapshots", dependencies=[Depends(require_token)])
async def list_snapshots(instance_id: UUID, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    from .snapshots import BtrfsSnapshots

    snaps = BtrfsSnapshots(settings, instance_id)
    cap = await run_in_threadpool(snaps.capability)
    items = await run_in_threadpool(snaps.list) if cap.get("instance_is_subvolume") else []
    return {"capability": cap, "snapshots": items}


@app.post("/v1/instances/{instance_id}/snapshots", dependencies=[Depends(require_token)])
async def create_snapshot(
    instance_id: UUID, body: SnapshotRequest, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    from .snapshots import BtrfsSnapshots

    snaps = BtrfsSnapshots(settings, instance_id)
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
    from .snapshots import BtrfsSnapshots

    await run_in_threadpool(BtrfsSnapshots(settings, instance_id).delete, name)
    return {"deleted": name}


@app.post("/v1/instances/{instance_id}/snapshots/{name}/restore", dependencies=[Depends(require_token)])
async def restore_snapshot(
    instance_id: UUID, name: str, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    from .snapshots import BtrfsSnapshots

    if (await run_in_threadpool(docker_ops.status, instance_id)).get("running"):
        raise AgentError("Stop the server before rolling back to a snapshot", "must_stop", 409)
    snaps = BtrfsSnapshots(settings, instance_id)
    op = registry.start("restore", [instance_id], lambda _op: snaps.restore(name))
    return {"operation": op.as_dict()}


@app.post("/v1/instances/{instance_id}/snapshots/convert", dependencies=[Depends(require_token)])
async def convert_to_subvolume(
    instance_id: UUID, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    from .snapshots import BtrfsSnapshots

    if (await run_in_threadpool(docker_ops.status, instance_id)).get("running"):
        raise AgentError("Stop the server before enabling snapshots", "must_stop", 409)
    snaps = BtrfsSnapshots(settings, instance_id)
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
