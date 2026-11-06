"""Shared fixtures for the MCPanel backend test suite.

* ``MCPANEL_TESTING=1`` (set here and in ``tests/settings.py``, which pytest-django
  loads first) switches settings to a locmem cache, in-memory channel layer, MD5
  hasher and eager Celery.
* ``fake_agent`` replaces every network method of ``apps.nodes.client.AgentClient``
  with an in-memory simulation of the node agent (containers, instance files,
  restic-like snapshots, operations). Operations complete synchronously.
* Jobs are dispatched from ``transaction.on_commit``; inside the per-test
  transaction wrap the request in ``django_capture_on_commit_callbacks(execute=True)``
  to actually run the handler.
"""

from __future__ import annotations

import os

os.environ.setdefault("MCPANEL_TESTING", "1")

import copy
import hashlib
import itertools
import json
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

PASSWORD = "Correct-Horse-Battery-42"
ALLOWED_FETCH_HOSTS = {
    "cdn.modrinth.com",
    "cdn-raw.modrinth.com",
    "edge.forgecdn.net",
    "mediafilez.forgecdn.net",
    "hangarcdn.papermc.io",
}


# --------------------------------------------------------------------------------------
# Fake node agent
# --------------------------------------------------------------------------------------
class FakeAgent:
    """In-memory node agent. Mirrors the real agent's semantics and error codes closely
    enough that handlers and views behave as they would against the real thing."""

    METHODS = (
        "health",
        "info",
        "published_ports",
        "list_containers",
        "prepare",
        "apply",
        "remove_container",
        "status",
        "start",
        "stop",
        "restart",
        "kill",
        "stats",
        "disk",
        "logs",
        "command",
        "delete_data",
        "clone",
        "list_files",
        "stat_file",
        "read_text",
        "write_text",
        "download",
        "upload",
        "mkdir",
        "rename",
        "delete_files",
        "search_files",
        "hash_files",
        "fetch_file",
        "list_backups",
        "create_backup",
        "restore_backup",
        "forget_backups",
        "check_backups",
        "snapshots",
        "create_snapshot",
        "delete_snapshot",
        "restore_snapshot",
        "convert_snapshots",
        "operation",
    )

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.fs: dict[str, dict[str, Any]] = {}
        self.repos: dict[str, list[dict[str, Any]]] = {}
        self.snaps: dict[str, list[dict[str, Any]]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.foreign_ports: list[dict[str, Any]] = []
        self.remote_files: dict[str, bytes] = {}
        self.log_lines: dict[str, list[str]] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.commands: list[tuple[str, str]] = []
        # method name -> exception raised by that client method
        self.failures: dict[str, Exception] = {}
        # operation kind -> (message, code): the agent operation is reported as failed
        self.failed_operations: dict[str, tuple[str, str]] = {}
        # optional callable(instance_id, command) -> output | None (None = default output)
        self.command_handler: Callable[[str, str], str | None] | None = None
        self.start_healthy = True

    # -- wiring ------------------------------------------------------------------------
    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apps.nodes.client import AgentClient

        for name in self.METHODS:
            monkeypatch.setattr(AgentClient, name, self._proxy(name))

        def unsimulated(_client: Any, method: str, path: str, **_kwargs: Any) -> Any:
            raise RuntimeError(f"FakeAgent does not simulate {method} {path}; add it to tests/conftest.py")

        # any client method without a simulation fails loudly instead of touching the network
        monkeypatch.setattr(AgentClient, "_request", unsimulated)

    def _proxy(self, name: str) -> Callable[..., Any]:
        impl = getattr(self, name)

        def method(_client: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            exc = self.failures.get(name)
            if exc is not None:
                raise exc
            return impl(*args, **kwargs)

        method.__name__ = name
        return method

    def called(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    # -- helpers for tests -------------------------------------------------------------
    def add_foreign_port(self, port: int, container: str = "other-app", protocol: str = "tcp") -> None:
        self.foreign_ports.append(
            {
                "host_ip": "0.0.0.0",
                "host_port": port,
                "protocol": protocol,
                "container": container,
                "managed": False,
                "instance_id": None,
            }
        )

    def put_file(self, instance_id: Any, path: str, data: bytes | str) -> None:
        root = self._root(str(instance_id), create=True)
        parts = self._parts(path)
        for i in range(1, len(parts)):
            root["dirs"].add("/".join(parts[:i]))
        rel = "/".join(parts)
        root["files"][rel] = data.encode() if isinstance(data, str) else bytes(data)
        root["mtime"][rel] = time.time()

    def get_file(self, instance_id: Any, path: str) -> bytes | None:
        root = self.fs.get(str(instance_id))
        if root is None:
            return None
        return root["files"].get("/".join(self._parts(path)))

    def text(self, instance_id: Any, path: str) -> str | None:
        data = self.get_file(instance_id, path)
        return None if data is None else data.decode()

    def serve(self, url: str, data: bytes) -> dict[str, str]:
        """Make ``url`` downloadable through fetch_file; returns its hashes."""
        self.remote_files[url] = data
        return {
            "sha1": hashlib.sha1(data).hexdigest(),
            "sha512": hashlib.sha512(data).hexdigest(),
        }

    def set_container_state(self, instance_id: Any, **state: Any) -> None:
        self.containers[str(instance_id)].update(state)

    # -- internals ---------------------------------------------------------------------
    @staticmethod
    def _err(message: str, code: str, status: int) -> Exception:
        from apps.nodes.client import AgentError

        return AgentError(message, code, status)

    @staticmethod
    def _now() -> str:
        return timezone.now().isoformat().replace("+00:00", "Z")

    def _op(self, kind: str, iid: str | None, result: dict[str, Any] | None = None, *, error: Any = None) -> dict[str, Any]:
        now = time.time()
        op = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "instance_id": iid,
            "status": "failed" if error else "succeeded",
            "progress": 0.5 if error else 1.0,
            "message": "",
            "result": {} if error else (result or {}),
            "error": error[0] if error else None,
            "error_code": error[1] if error else None,
            "created_at": now,
            "finished_at": now,
            "log": [],
        }
        self.operations[op["id"]] = op
        return copy.deepcopy(op)

    def _injected(self, kind: str, iid: str) -> dict[str, Any] | None:
        failure = self.failed_operations.get(kind)
        return self._op(kind, iid, error=failure) if failure else None

    def _container(self, iid: str) -> dict[str, Any]:
        c = self.containers.get(iid)
        if c is None:
            raise self._err("The instance has no container; apply its configuration first", "no_container", 404)
        return c

    def _public(self, c: dict[str, Any]) -> dict[str, Any]:
        out = {k: copy.deepcopy(v) for k, v in c.items() if k != "spec"}
        if not c["running"]:
            out["ports"] = []
        return out

    def _set_running(self, c: dict[str, Any]) -> None:
        c.update(
            status="running",
            running=True,
            restarting=False,
            exit_code=0,
            oom_killed=False,
            started_at=self._now(),
            finished_at=None,
            health="healthy" if self.start_healthy else "starting",
        )

    def _set_exited(self, c: dict[str, Any], exit_code: int) -> None:
        c.update(status="exited", running=False, exit_code=exit_code, finished_at=self._now())

    @staticmethod
    def _parts(path: str | None) -> list[str]:
        from apps.nodes.client import AgentError

        path = path or ""
        if "\x00" in path:
            raise AgentError("Path contains a NUL byte", "invalid_path", 400)
        parts = []
        for part in path.replace("\\", "/").split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise AgentError("Parent directory references ('..') are not allowed", "invalid_path", 400)
            parts.append(part)
        return parts

    def _root(self, iid: str, *, create: bool = False) -> dict[str, Any]:
        root = self.fs.get(iid)
        if root is None:
            if not create:
                raise self._err("Instance directory does not exist", "not_found", 404)
            root = self.fs[iid] = {"files": {}, "dirs": set(), "mtime": {}}
        return root

    @staticmethod
    def _kind(root: dict[str, Any], rel: str) -> str | None:
        if rel == "":
            return "directory"
        if rel in root["files"]:
            return "file"
        if rel in root["dirs"]:
            return "directory"
        return None

    def _require_parent(self, root: dict[str, Any], parts: list[str]) -> None:
        for i in range(1, len(parts)):
            kind = self._kind(root, "/".join(parts[:i]))
            if kind is None:
                raise self._err(f"'{parts[i - 1]}' does not exist", "not_found", 404)
            if kind != "directory":
                raise self._err(f"'{parts[i - 1]}' is not a directory", "not_a_directory", 400)

    def _entry(self, root: dict[str, Any], rel: str) -> dict[str, Any]:
        kind = self._kind(root, rel)
        return {
            "name": rel.rsplit("/", 1)[-1] if rel else "",
            "path": rel,
            "type": kind,
            "size": len(root["files"][rel]) if kind == "file" else 4096,
            "modified": root["mtime"].get(rel, 0.0),
            "mode": "-rw-r--r--" if kind == "file" else "drwxr-xr-x",
        }

    def _children(self, root: dict[str, Any], rel: str) -> list[str]:
        prefix = f"{rel}/" if rel else ""
        names = set()
        for p in itertools.chain(root["files"], root["dirs"]):
            if p.startswith(prefix) and "/" not in p[len(prefix) :]:
                names.add(p)
        return sorted(names, key=lambda p: (self._kind(root, p) != "directory", p.rsplit("/", 1)[-1].lower()))

    def _write(self, iid: str, path: str, data: bytes, *, overwrite: bool) -> int:
        root = self._root(iid)
        parts = self._parts(path)
        if not parts:
            raise self._err("Cannot write to the root directory", "invalid_path", 400)
        self._require_parent(root, parts)
        rel = "/".join(parts)
        kind = self._kind(root, rel)
        if kind == "directory":
            raise self._err("A directory with that name exists", "is_directory", 409)
        if kind == "file" and not overwrite:
            raise self._err("File already exists", "exists", 409)
        root["files"][rel] = data
        root["mtime"][rel] = time.time()
        return len(data)

    def _read(self, iid: str, path: str) -> bytes:
        root = self._root(iid)
        parts = self._parts(path)
        if not parts:
            raise self._err("Not a regular file", "not_a_file", 400)
        self._require_parent(root, parts)
        rel = "/".join(parts)
        kind = self._kind(root, rel)
        if kind is None:
            raise self._err("File does not exist", "not_found", 404)
        if kind != "file":
            raise self._err("Not a regular file", "not_a_file", 400)
        return root["files"][rel]

    # -- host --------------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return {"status": "ok", "docker": True, "version": "fake"}

    def info(self) -> dict[str, Any]:
        return {
            "version": "fake",
            "docker": {"server_version": "fake", "cpus": 4, "memory_bytes": 16 * 1024**3},
            "restic": "restic 0.19.1 (fake)",
            "storage": {},
            "policy": {"allowed_images": ["itzg/minecraft-server"], "port_min": 1024, "port_max": 65535},
        }

    def published_ports(self) -> list[dict[str, Any]]:
        used = [dict(p) for p in self.foreign_ports]
        for iid, c in self.containers.items():
            if not c["running"]:
                continue
            for p in c["spec"]["ports"]:
                used.append(
                    {
                        "host_ip": p.get("host_ip") or "0.0.0.0",
                        "host_port": int(p["host_port"]),
                        "protocol": p.get("protocol", "tcp"),
                        "container": c["name"],
                        "managed": True,
                        "instance_id": iid,
                    }
                )
        return used

    # -- containers --------------------------------------------------------------------
    def list_containers(self) -> list[dict[str, Any]]:
        return [self._public(c) for c in self.containers.values()]

    def prepare(self, instance_id: Any, files: dict[str, str] | None = None) -> dict[str, Any]:
        iid = str(instance_id)
        created = iid not in self.fs
        root = self._root(iid, create=True)
        written = []
        for rel, content in (files or {}).items():
            if self._kind(root, "/".join(self._parts(rel))) is None:
                self.put_file(iid, rel, content)
                written.append(rel)
        return {"created": created, "written": written}

    def apply(self, instance_id: Any, spec: dict[str, Any], *, start: bool = False, pull: str = "missing") -> dict[str, Any]:
        iid = str(instance_id)
        repo = str(spec.get("image", "")).rpartition(":")[0]
        if repo not in ("itzg/minecraft-server", "docker.io/itzg/minecraft-server"):
            raise self._err(f"Image repository '{repo}' is not in the agent allow-list", "image_not_allowed", 403)
        if failed := self._injected("apply", iid):
            return failed
        existing = self.containers.get(iid)
        was_running = bool(existing and existing["running"])
        self._root(iid, create=True)
        spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
        c = {
            "exists": True,
            "id": uuid.uuid4().hex,
            "name": f"mcpanel-{uuid.UUID(iid).hex[:12]}",
            "image": spec["image"],
            "status": "created",
            "running": False,
            "restarting": False,
            "exit_code": 0,
            "oom_killed": False,
            "error": None,
            "started_at": None,
            "finished_at": None,
            "health": None,
            "health_output": None,
            "restart_count": 0,
            "spec_hash": spec_hash,
            "ip_address": "172.30.0.2",
            "ports": [
                {
                    "container_port": int(p["container_port"]),
                    "protocol": p.get("protocol", "tcp"),
                    "host_ip": p.get("host_ip") or "0.0.0.0",
                    "host_port": int(p["host_port"]),
                }
                for p in spec.get("ports", [])
            ],
            "created_at": self._now(),
            "instance_id": iid,
            "spec": copy.deepcopy(spec),
        }
        self.containers[iid] = c
        if start or was_running:
            self._set_running(c)
        return self._op("apply", iid, {"container": self._public(c), "recreated": existing is not None})

    def remove_container(self, instance_id: Any, timeout: int = 60) -> bool:
        return self.containers.pop(str(instance_id), None) is not None

    def status(self, instance_id: Any) -> dict[str, Any]:
        c = self.containers.get(str(instance_id))
        if c is None:
            return {"exists": False, "operation": None}
        return {**self._public(c), "operation": None}

    def start(self, instance_id: Any) -> dict[str, Any]:
        c = self._container(str(instance_id))
        if not c["running"]:
            self._set_running(c)
        return self.status(instance_id)

    def stop(self, instance_id: Any, timeout: int | None = None) -> dict[str, Any]:
        c = self._container(str(instance_id))
        if c["running"]:
            self._set_exited(c, 0)
        return self.status(instance_id)

    def restart(self, instance_id: Any, timeout: int | None = None) -> dict[str, Any]:
        self._set_running(self._container(str(instance_id)))
        return self.status(instance_id)

    def kill(self, instance_id: Any) -> dict[str, Any]:
        c = self._container(str(instance_id))
        if c["running"]:
            self._set_exited(c, 137)
        return self.status(instance_id)

    def stats(self, instance_id: Any) -> dict[str, Any]:
        c = self._container(str(instance_id))
        if not c["running"]:
            return {"running": False}
        return {
            "running": True,
            "timestamp": self._now(),
            "cpu_percent": 12.5,
            "cpu_count": 4,
            "memory_bytes": 512 * 1024 * 1024,
            "memory_limit_bytes": c["spec"]["memory_limit_mb"] * 1024 * 1024,
            "network_rx_bytes": 0,
            "network_tx_bytes": 0,
            "block_read_bytes": 0,
            "block_write_bytes": 0,
            "pids": 42,
        }

    def disk(self, instance_id: Any) -> dict[str, Any]:
        root = self.fs.get(str(instance_id)) or {"files": {}}
        return {
            "bytes": sum(len(v) for v in root["files"].values()),
            "backup_bytes": 0,
            "measured_at": time.time(),
            "cached": False,
        }

    def logs(self, instance_id: Any, tail: int = 500) -> list[str]:
        self._container(str(instance_id))
        return self.log_lines.get(str(instance_id), [])[-tail:]

    def command(self, instance_id: Any, command: str, timeout: float = 30) -> str:
        iid = str(instance_id)
        if not self._container(iid)["running"]:
            raise self._err("The server is not running", "not_running", 409)
        self.commands.append((iid, command))
        if self.command_handler is not None:
            out = self.command_handler(iid, command)
            if out is not None:
                return out
        if command == "list":
            return "There are 0 of a max of 20 players online: "
        return ""

    def delete_data(self, instance_id: Any) -> dict[str, Any]:
        iid = str(instance_id)
        if failed := self._injected("delete_data", iid):
            return failed
        self.containers.pop(iid, None)
        self.fs.pop(iid, None)
        self.repos.pop(iid, None)
        return self._op("delete_data", iid, {"deleted": True})

    def clone(self, instance_id: Any, target_id: Any, exclude_worlds: bool = False) -> dict[str, Any]:
        src, dst = str(instance_id), str(target_id)
        if src not in self.fs:
            raise self._err("Source instance has no data directory", "not_found", 404)
        target = self.fs.get(dst)
        if target and (target["files"] or target["dirs"]):
            raise self._err("Target instance directory is not empty", "target_not_empty", 409)
        if self.containers.get(src, {}).get("running"):
            raise self._err("Stop the source server before cloning it", "source_running", 409)
        if failed := self._injected("clone_target", src):
            return failed
        tree = copy.deepcopy(self.fs[src])
        if exclude_worlds:
            worlds = ("world", "world_nether", "world_the_end")
            for key in ("files", "dirs"):
                items = tree[key]
                for p in list(items):
                    if p.split("/", 1)[0] in worlds:
                        if isinstance(items, dict):
                            del items[p]
                        else:
                            items.discard(p)
        self.fs[dst] = tree
        return self._op("clone_target", src, {"bytes": sum(len(v) for v in tree["files"].values())})

    # -- files -------------------------------------------------------------------------
    def list_files(self, instance_id: Any, path: str) -> dict[str, Any]:
        root = self._root(str(instance_id))
        parts = self._parts(path)
        rel = "/".join(parts)
        self._require_parent(root, [*parts, "_"])
        kind = self._kind(root, rel)
        if kind is None:
            raise self._err(f"'{parts[-1]}' does not exist", "not_found", 404)
        if kind != "directory":
            raise self._err(f"'{parts[-1]}' is not a directory", "not_a_directory", 400)
        return {"path": rel, "entries": [self._entry(root, p) for p in self._children(root, rel)]}

    def stat_file(self, instance_id: Any, path: str) -> dict[str, Any]:
        root = self._root(str(instance_id))
        parts = self._parts(path)
        self._require_parent(root, parts)
        rel = "/".join(parts)
        if self._kind(root, rel) is None:
            raise self._err("Does not exist", "not_found", 404)
        return self._entry(root, rel)

    def read_text(self, instance_id: Any, path: str) -> dict[str, Any]:
        iid = str(instance_id)
        data = self._read(iid, path)
        if b"\x00" in data[:8192]:
            raise self._err("This looks like a binary file and cannot be edited as text", "binary_file", 415)
        entry = self.stat_file(iid, path)
        return {
            "path": entry["path"],
            "content": data.decode("utf-8", errors="replace"),
            "size": len(data),
            "modified": entry["modified"],
        }

    def write_text(self, instance_id: Any, path: str, content: str, create_only: bool = False) -> dict[str, Any]:
        size = self._write(str(instance_id), path, content.encode(), overwrite=not create_only)
        return {"path": path, "size": size}

    def download(self, instance_id: Any, path: str) -> tuple[Iterator[bytes], int | None, Any]:
        data = self._read(str(instance_id), path)
        return iter([data]), len(data), None

    def upload(
        self,
        instance_id: Any,
        path: str,
        chunks: Iterable[bytes] | bytes,
        overwrite: bool = False,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        data = chunks if isinstance(chunks, bytes) else b"".join(chunks)
        digest = hashlib.sha256(data).hexdigest()
        if sha256 and digest != sha256.lower():
            raise self._err("Uploaded file failed SHA-256 verification", "hash_mismatch", 422)
        size = self._write(str(instance_id), path, data, overwrite=overwrite)
        return {"path": path, "size": size, "sha256": digest}

    def mkdir(self, instance_id: Any, path: str, parents: bool = False) -> dict[str, Any]:
        root = self._root(str(instance_id))
        parts = self._parts(path)
        if not parts:
            raise self._err("Cannot create the root directory", "invalid_path", 400)
        if parents:
            for i in range(1, len(parts) + 1):
                rel = "/".join(parts[:i])
                kind = self._kind(root, rel)
                if kind == "file":
                    raise self._err(f"'{parts[i - 1]}' is not a directory", "not_a_directory", 400)
                root["dirs"].add(rel)
            return {"path": path}
        self._require_parent(root, parts)
        rel = "/".join(parts)
        if self._kind(root, rel) is not None:
            raise self._err("Already exists", "exists", 409)
        root["dirs"].add(rel)
        return {"path": path}

    def rename(self, instance_id: Any, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        root = self._root(str(instance_id))
        sp, dp = self._parts(source), self._parts(destination)
        if not sp or not dp:
            raise self._err("Cannot rename the root directory", "invalid_path", 400)
        if dp[: len(sp)] == sp:
            raise self._err("Cannot move a directory into itself", "invalid_path", 400)
        self._require_parent(root, sp)
        self._require_parent(root, dp)
        src, dst = "/".join(sp), "/".join(dp)
        kind = self._kind(root, src)
        if kind is None:
            raise self._err("Source does not exist", "not_found", 404)
        if self._kind(root, dst) is not None and not overwrite:
            raise self._err("Destination already exists", "exists", 409)
        if kind == "file":
            root["files"][dst] = root["files"].pop(src)
            root["mtime"][dst] = root["mtime"].pop(src, time.time())
        else:
            for p in [p for p in root["files"] if p == src or p.startswith(src + "/")]:
                root["files"][dst + p[len(src) :]] = root["files"].pop(p)
            for p in [p for p in root["dirs"] if p == src or p.startswith(src + "/")]:
                root["dirs"].discard(p)
                root["dirs"].add(dst + p[len(src) :])
        return {"source": source, "destination": destination}

    def delete_files(self, instance_id: Any, paths: list[str], recursive: bool = False) -> dict[str, Any]:
        root = self._root(str(instance_id))
        deleted = []
        for path in paths:
            parts = self._parts(path)
            if not parts:
                raise self._err("This operation is not allowed on the root directory", "invalid_path", 400)
            self._require_parent(root, parts)
            rel = "/".join(parts)
            kind = self._kind(root, rel)
            if kind is None:
                raise self._err("Does not exist", "not_found", 404)
            if kind == "file":
                del root["files"][rel]
                root["mtime"].pop(rel, None)
            else:
                inside = [p for p in itertools.chain(root["files"], root["dirs"]) if p.startswith(rel + "/")]
                if inside and not recursive:
                    raise self._err("Directory is not empty", "not_empty", 409)
                for p in inside:
                    root["files"].pop(p, None)
                    root["dirs"].discard(p)
                root["dirs"].discard(rel)
            deleted.append(path)
        return {"deleted": deleted}

    def search_files(self, instance_id: Any, query: str, path: str = "") -> list[dict[str, Any]]:
        root = self._root(str(instance_id))
        prefix = "/".join(self._parts(path))
        needle = query.lower()
        results: list[dict[str, Any]] = []
        for rel, data in sorted(root["files"].items()):
            if prefix and not rel.startswith(prefix + "/"):
                continue
            if needle in rel.lower():
                results.append({"path": rel, "line": None, "text": None})
            for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if needle in line.lower():
                    results.append({"path": rel, "line": lineno, "text": line.strip()[:300]})
        return results

    def hash_files(self, instance_id: Any, path: str, algorithm: str = "sha1") -> list[dict[str, Any]]:
        try:
            listing = self.list_files(instance_id, path)
        except Exception as exc:
            if getattr(exc, "error_code", None) == "not_found":
                return []
            raise
        root = self._root(str(instance_id))
        out = []
        for entry in listing["entries"]:
            if entry["type"] == "file":
                digest = hashlib.new(algorithm, root["files"][entry["path"]]).hexdigest()
                out.append({**entry, "hash": digest})
        return out

    def fetch_file(
        self,
        instance_id: Any,
        *,
        url: str,
        path: str,
        sha1: str | None = None,
        sha512: str | None = None,
        sha256: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_FETCH_HOSTS:
            raise self._err("Download URL is not on an allowed provider host", "url_not_allowed", 403)
        if not (sha1 or sha512 or sha256):
            raise self._err("A sha1, sha256 or sha512 hash is required to verify downloads", "hash_required", 400)
        if url not in self.remote_files:
            raise self._err("Download failed: HTTP Error 404: Not Found", "download_failed", 502)
        data = self.remote_files[url]
        h1 = hashlib.sha1(data).hexdigest()
        h512 = hashlib.sha512(data).hexdigest()
        if sha1 and h1 != sha1.lower():
            raise self._err("Downloaded file failed SHA-1 verification", "hash_mismatch", 502)
        if sha512 and h512 != sha512.lower():
            raise self._err("Downloaded file failed SHA-512 verification", "hash_mismatch", 502)
        if sha256 and hashlib.sha256(data).hexdigest() != sha256.lower():
            raise self._err("Downloaded file failed SHA-256 verification", "hash_mismatch", 502)
        size = self._write(str(instance_id), path, data, overwrite=overwrite)
        return {"path": path, "size": size, "sha1": h1, "sha512": h512}

    # -- snapshots (btrfs semantics) -----------------------------------------------------
    def snapshots(self, instance_id: Any) -> dict[str, Any]:
        iid = str(instance_id)
        return {
            "capability": {
                "provider": "btrfs",
                "available": True,
                "fstype": "btrfs",
                "reason": None,
                "instance_is_subvolume": iid in self.fs,
            },
            "snapshots": [{k: v for k, v in s.items() if k != "tree"} for s in reversed(self.snaps.get(iid, []))],
        }

    def create_snapshot(
        self, instance_id: Any, label: str = "", consistency: str = "auto", kind: str = "manual"
    ) -> dict[str, Any]:
        iid = str(instance_id)
        if iid not in self.fs:
            raise self._err("This server's folder is a plain directory", "not_subvolume", 409)
        n = len(self.snaps.get(iid, [])) + 1
        slug = "-".join("".join(ch if ch.isalnum() else " " for ch in label.lower()).split())[:40]
        name = f"20261103T{n:06d}Z" + (f"-{slug}" if slug else "")
        running = bool(self.containers.get(iid, {}).get("running"))
        info = {
            "name": name,
            "label": label,
            "created_at": self._now(),
            "kind": kind,
            "consistent": (consistency != "none") or not running,
        }
        self.snaps.setdefault(iid, []).append({**info, "tree": copy.deepcopy(self.fs[iid])})
        return self._op("snapshot", iid, {**info, "warnings": []})

    def delete_snapshot(self, instance_id: Any, name: str) -> dict[str, Any]:
        iid = str(instance_id)
        before = len(self.snaps.get(iid, []))
        self.snaps[iid] = [s for s in self.snaps.get(iid, []) if s["name"] != name]
        if len(self.snaps[iid]) == before:
            raise self._err("Snapshot not found", "snapshot_not_found", 404)
        return {"deleted": name}

    def restore_snapshot(self, instance_id: Any, name: str) -> dict[str, Any]:
        iid = str(instance_id)
        if self.containers.get(iid, {}).get("running"):
            raise self._err("Stop the server before rolling back to a snapshot", "must_stop", 409)
        snap = next((s for s in self.snaps.get(iid, []) if s["name"] == name), None)
        if snap is None:
            raise self._err("Snapshot not found", "snapshot_not_found", 404)
        self.fs[iid] = copy.deepcopy(snap["tree"])
        return self._op("restore", iid, {"restored": name})

    def convert_snapshots(self, instance_id: Any) -> dict[str, Any]:
        return self._op("import", str(instance_id), {"converted": True})

    # -- backups -----------------------------------------------------------------------
    def list_backups(self, instance_id: Any) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.items() if k != "tree"} for s in self.repos.get(str(instance_id), [])]

    def create_backup(
        self, instance_id: Any, backup_id: str, *, tags: list[str], consistency: str = "auto", excludes: list[str] | None = None
    ) -> dict[str, Any]:
        iid = str(instance_id)
        if iid not in self.fs:
            raise self._err("Instance has no data directory", "not_found", 404)
        if failed := self._injected("backup", iid):
            return failed
        running = bool(self.containers.get(iid, {}).get("running"))
        paused = False
        if running and consistency != "none":
            self.commands.extend([(iid, "save-off"), (iid, "save-all flush")])
            paused = True
        snap_id = hashlib.sha256(f"{iid}:{backup_id}:{time.time_ns()}".encode()).hexdigest()
        tree = copy.deepcopy(self.fs[iid])
        self.repos.setdefault(iid, []).append(
            {
                "id": snap_id,
                "short_id": snap_id[:8],
                "time": self._now(),
                "tags": [f"instance:{iid}", f"backup:{backup_id}", *tags],
                "paths": [f"/srv/mcpanel/instances/{iid}"],
                "tree": tree,
            }
        )
        if paused:
            self.commands.append((iid, "save-on"))
        total = sum(len(v) for v in tree["files"].values())
        return self._op(
            "backup",
            iid,
            {
                "message_type": "summary",
                "snapshot_id": snap_id,
                "files_new": len(tree["files"]),
                "files_changed": 0,
                "files_unmodified": 0,
                "data_added": total,
                "total_bytes_processed": total,
                "total_duration": 0.01,
                "warnings": [],
                "consistent": paused or not running,
            },
        )

    def restore_backup(self, instance_id: Any, snapshot_id: str, target_instance_id: Any = None) -> dict[str, Any]:
        src = str(instance_id)
        target = str(target_instance_id) if target_instance_id else src
        if self.containers.get(target, {}).get("running"):
            raise self._err("Stop the target server before restoring", "target_running", 409)
        existing = self.fs.get(target)
        if target_instance_id and existing and (existing["files"] or existing["dirs"]):
            raise self._err("Target instance directory is not empty", "target_not_empty", 409)
        if failed := self._injected("restore", src):
            return failed
        snap = next((s for s in self.repos.get(src, []) if s["id"].startswith(snapshot_id)), None)
        if snap is None:
            return self._op("restore", src, error=("Backup snapshot not found", "snapshot_not_found"))
        self.fs[target] = copy.deepcopy(snap["tree"])
        return self._op("restore", src, {"snapshot": snap["id"], "files_restored": len(snap["tree"]["files"])})

    def forget_backups(self, instance_id: Any, snapshot_ids: list[str], prune: bool = True) -> dict[str, Any]:
        iid = str(instance_id)
        if failed := self._injected("forget", iid):
            return failed
        self.repos[iid] = [s for s in self.repos.get(iid, []) if s["id"] not in snapshot_ids]
        return self._op("forget", iid, {"removed": list(snapshot_ids)})

    def check_backups(self, instance_id: Any, read_data_subset: str | None = None) -> dict[str, Any]:
        iid = str(instance_id)
        if iid not in self.repos:
            return self._op("check", iid, error=("No backup repository exists for this instance yet", "repo_missing"))
        if failed := self._injected("check", iid):
            return failed
        return self._op("check", iid, {"ok": True, "output": ["no errors were found"]})

    # -- operations --------------------------------------------------------------------
    def operation(self, op_id: str) -> dict[str, Any]:
        op = self.operations.get(op_id)
        if op is None:
            raise self._err("Operation not found (the agent may have restarted)", "agent_error", 404)
        return copy.deepcopy(op)


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_cache() -> Iterator[None]:
    """Locmem cache is process-wide: clear it so throttles and catalog caches don't leak."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    agent = FakeAgent()
    agent.install(monkeypatch)
    return agent


@pytest.fixture
def roles(db: Any) -> dict[str, Any]:
    from apps.accounts.models import Role
    from apps.accounts.views import ensure_system_roles

    ensure_system_roles()
    return {r.key: r for r in Role.objects.filter(is_system=True)}


@pytest.fixture
def make_user(db: Any, roles: dict[str, Any]) -> Callable[..., Any]:
    from apps.accounts.models import Role, User

    def _make(
        username: str,
        *,
        role: str | Role | None = None,
        owner: bool = False,
        active: bool = True,
        password: str = PASSWORD,
        **extra: Any,
    ) -> User:
        user = User(
            username=username,
            is_owner=owner,
            is_active=active,
            role=roles[role] if isinstance(role, str) else role,
            is_staff=owner,
            is_superuser=owner,
            **extra,
        )
        user.set_password(password)
        user.save()
        return user

    return _make


@pytest.fixture
def users(make_user: Callable[..., Any]) -> dict[str, Any]:
    return {
        "owner": make_user("owner", owner=True),
        "admin": make_user("admin", role="administrator"),
        "manager": make_user("manager", role="server_manager"),
        "operator": make_user("operator", role="operator"),
        "viewer": make_user("viewer", role="viewer"),
        "norole": make_user("norole"),
    }


@pytest.fixture
def client_for() -> Callable[..., APIClient]:
    def _client(user: Any = None, *, csrf: bool = False) -> APIClient:
        client = APIClient(enforce_csrf_checks=csrf)
        if user is not None:
            client.force_login(user)
        return client

    return _client


@pytest.fixture
def clients(users: dict[str, Any], client_for: Callable[..., APIClient]) -> dict[str, APIClient]:
    out = {name: client_for(user) for name, user in users.items()}
    out["anon"] = client_for(None)
    return out


@pytest.fixture
def node(db: Any) -> Any:
    from apps.nodes.models import Node

    # ".invalid" never resolves: anything that bypasses the fake agent fails fast.
    return Node.objects.create(name="test-node", agent_url="http://agent.invalid:8700", is_default=True, use_settings_token=True)


_ports = itertools.count(40000)


@pytest.fixture
def make_instance(db: Any, node: Any) -> Callable[..., Any]:
    from apps.instances.models import Instance

    counter = itertools.count(1)

    def _make(**fields: Any) -> Instance:
        n = next(counter)
        values: dict[str, Any] = {
            "name": f"Server {n}",
            "node": node,
            "server_type": "VANILLA",
            "minecraft_version": "1.20.4",
            "java_version": 17,
            "image": "itzg/minecraft-server:stable-java17",
            "memory_mb": 2048,
            "game_port": next(_ports),
        }
        values.update(fields)
        return Instance.objects.create(**values)

    return _make


@pytest.fixture
def mojang_java(db: Any) -> Callable[[str, int], None]:
    """Prime the catalog cache with Mojang's Java requirement (no network in API tests)."""
    from django.core.cache import cache

    def _prime(version: str, major: int) -> None:
        cache.set(f"catalog:mojang:java:{version}", major, 3600)

    _prime("1.20.4", 17)
    return _prime
