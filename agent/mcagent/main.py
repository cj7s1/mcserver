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
import shutil
import threading
import time
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketState

from . import __version__, docker_ops, storage
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


@app.middleware("http")
async def _refuse_instance_networks(request: Request, call_next):  # type: ignore[no-untyped-def]
    client_ip = request.client.host if request.client else None
    if client_ip and _blocked_networks:
        try:
            ip = ipaddress.ip_address(client_ip)
        except ValueError:
            ip = None
        if ip is not None and any(ip in net for net in _blocked_networks):
            log.warning("Refused agent API request from instance network address %s", client_ip)
            return JSONResponse(
                {"error": {"code": "forbidden_source", "message": "Forbidden"}}, status_code=403
            )
    return await call_next(request)


@app.exception_handler(AgentError)
async def _agent_error(_request: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)


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


@app.get("/v1/info", dependencies=[Depends(require_token)])
async def info(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    docker_info = await run_in_threadpool(docker_ops.docker_info)
    caps = await run_in_threadpool(storage.capabilities, settings)
    return {
        "version": __version__,
        "docker": docker_info,
        "restic": await run_in_threadpool(restic_available, settings),
        "storage": caps,
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
    """Permanently delete an instance's container, files and backup repository."""
    if body.confirm != instance_id:
        raise AgentError("Confirmation id does not match", "confirmation_mismatch", 400)

    def _delete(op: Operation) -> dict[str, Any]:
        op.update(0.1, "Removing container")
        docker_ops.remove_container(instance_id, timeout=30)
        path = docker_ops.instance_dir(settings, instance_id)
        if path.exists():
            op.update(0.3, "Deleting instance files")
            trash = settings.trash_dir / f"{instance_id}-{int(time.time())}"
            path.rename(trash)
            shutil.rmtree(trash, ignore_errors=True)
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
        ignore = None
        if body.exclude_worlds:
            ignore = shutil.ignore_patterns("world", "world_nether", "world_the_end")
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


@app.websocket("/v1/instances/{instance_id}/logs/stream")
async def stream_logs(websocket: WebSocket, instance_id: UUID, tail: int = 200) -> None:
    settings = get_settings()
    if not await websocket_authorized(websocket, settings):
        return
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=10000)
    stop_event = threading.Event()
    current_stream: dict[str, Any] = {}
    tail = max(0, min(tail, 5000))

    def emit(msg: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            loop.call_soon_threadsafe(queue.put_nowait, msg)

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
            buf = b""
            try:
                for chunk in stream:
                    if stop_event.is_set():
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        emit({"type": "log", "line": line.decode("utf-8", errors="replace").rstrip("\r")})
            except Exception as exc:  # stream broke (container stopped / daemon restarted)
                log.debug("log stream for %s ended: %s", instance_id, exc)
            finally:
                if buf:
                    emit({"type": "log", "line": buf.decode("utf-8", errors="replace")})
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
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            if msg is None:
                break
            batch = [msg]
            while not queue.empty() and len(batch) < 500:
                batch.append(queue.get_nowait())
            logs = [m["line"] for m in batch if m and m.get("type") == "log"]
            others = [m for m in batch if m and m.get("type") != "log"]
            if logs:
                await websocket.send_json({"type": "logs", "lines": logs})
            for m in others:
                await websocket.send_json(m)
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
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    root = _root(settings, instance_id)
    writer = await run_in_threadpool(root.open_writer, path, overwrite=overwrite, max_bytes=UPLOAD_LIMIT)
    try:
        async for chunk in request.stream():
            if chunk:
                await run_in_threadpool(writer.write, chunk)
        size = await run_in_threadpool(writer.commit)
    except BaseException:
        await run_in_threadpool(writer.abort)
        raise
    return {"path": path, "size": size}


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
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Hash every regular file directly inside ``path`` (used to identify mods/plugins)."""
    root = _root(settings, instance_id)

    def _do() -> list[dict[str, Any]]:
        out = []
        for entry in root.list(path):
            if entry.type != "file":
                continue
            h = hashlib.new(algorithm)
            try:
                with root.open_read(entry.path) as (fh, _size):
                    while chunk := fh.read(1024 * 1024):
                        h.update(chunk)
            except PathError as exc:
                # removed or replaced (e.g. by a symlink) after listing: skip just this entry,
                # otherwise the outer not_found handler would report the whole folder as empty
                if exc.code in ("not_found", "symlink", "not_a_file"):
                    continue
                raise
            out.append({**entry.as_dict(), "hash": h.hexdigest()})
        return out

    try:
        files = await run_in_threadpool(_do)
    except PathError as exc:
        if exc.code == "not_found":
            files = []
        else:
            raise
    return {"algorithm": algorithm, "files": files}


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
        paused = False
        warnings: list[str] = []
        status = docker_ops.status(instance_id)
        if status.get("running") and body.consistency != "none":
            try:
                host, port, password = docker_ops.rcon_endpoint(settings, instance_id)
                op.update(0.02, "Pausing world saving (save-off)")
                asyncio.run(run_command(host, port, password, "save-off", timeout=30))
                paused = True
                op.update(0.04, "Flushing world to disk (save-all flush)")
                asyncio.run(run_command(host, port, password, "save-all flush", timeout=300))
                time.sleep(1)
            except (RconError, AgentError) as exc:
                if body.consistency == "require":
                    raise AgentError(
                        f"Could not coordinate world saving: {exc}", "consistency_failed", 409
                    ) from exc
                warnings.append(f"Could not pause world saving ({exc}); backup may be inconsistent")
        try:
            op.update(0.06, "Running restic backup")
            summary = restic.backup(
                source,
                [f"instance:{instance_id}", f"backup:{body.backup_id}", *body.tags],
                body.excludes,
                lambda p, m: op.update(0.06 + p * 0.9, m),
            )
        finally:
            if paused:
                for attempt in range(5):
                    try:
                        host, port, password = docker_ops.rcon_endpoint(settings, instance_id)
                        asyncio.run(run_command(host, port, password, "save-on", timeout=30))
                        break
                    except (RconError, AgentError) as exc:
                        if attempt == 4:
                            warnings.append(f"FAILED to re-enable world saving (save-on): {exc}")
                        time.sleep(2)
        summary["warnings"] = warnings + summary.get("warnings", [])
        summary["consistent"] = paused or not status.get("running")
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


def _coordinated_save(
    settings: Settings, instance_id: UUID, consistency: str, op: Operation
) -> tuple[bool, list[str]]:
    """save-off + save-all flush on a running server. Returns (paused, warnings)."""
    warnings: list[str] = []
    status = docker_ops.status(instance_id)
    if not status.get("running") or consistency == "none":
        return False, warnings
    try:
        host, port, password = docker_ops.rcon_endpoint(settings, instance_id)
        op.update(0.05, "Pausing world saving (save-off)")
        asyncio.run(run_command(host, port, password, "save-off", timeout=30))
        op.update(0.1, "Flushing world to disk (save-all flush)")
        asyncio.run(run_command(host, port, password, "save-all flush", timeout=300))
        time.sleep(1)
        return True, warnings
    except (RconError, AgentError) as exc:
        if consistency == "require":
            raise AgentError(f"Could not coordinate world saving: {exc}", "consistency_failed", 409) from exc
        warnings.append(f"Could not pause world saving ({exc}); snapshot may be inconsistent")
        return False, warnings


def _resume_saving(settings: Settings, instance_id: UUID, warnings: list[str]) -> None:
    for attempt in range(5):
        try:
            host, port, password = docker_ops.rcon_endpoint(settings, instance_id)
            asyncio.run(run_command(host, port, password, "save-on", timeout=30))
            return
        except (RconError, AgentError) as exc:
            if attempt == 4:
                warnings.append(f"FAILED to re-enable world saving (save-on): {exc}")
            time.sleep(2)


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
        paused, warnings = _coordinated_save(settings, instance_id, body.consistency, op)
        try:
            op.update(0.5, "Creating snapshot")
            info = snaps.create(
                body.label,
                {
                    "consistent": paused or not docker_ops.status(instance_id).get("running"),
                    "kind": body.kind,
                },
            )
        finally:
            if paused:
                _resume_saving(settings, instance_id, warnings)
        info["warnings"] = warnings
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
