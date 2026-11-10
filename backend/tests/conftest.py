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
import fnmatch
import hashlib
import io
import itertools
import json
import re
import time
import uuid
import zipfile
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
# agent/mcagent/main.py FEATURES of the agent released with this panel (tests/test_contracts_2026_11_10.py
# checks that the two agree).
CURRENT_AGENT_FEATURES = ("retire", "pull_newer", "ping", "clone_level_name")


class FakeAgent:
    """In-memory node agent. Mirrors the real agent's semantics and error codes closely
    enough that handlers and views behave as they would against the real thing."""

    METHODS = (
        "health",
        "info",
        "published_ports",
        "list_containers",
        "list_state",
        "prepare",
        "apply",
        "remove_container",
        "retire",
        "forget_last_run",
        "status",
        "start",
        "stop",
        "restart",
        "kill",
        "stats",
        "disk",
        "logs",
        "command",
        "ping",
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
        "hash_files_partial",
        "fetch_file",
        "world_info",
        "world_replace",
        "world_reset",
        "list_backups",
        "create_backup",
        "restore_backup",
        "forget_backups",
        "check_backups",
        "backup_files",
        "backup_restore_path",
        "snapshots",
        "create_snapshot",
        "delete_snapshot",
        "restore_snapshot",
        "convert_snapshots",
        "operation",
    )

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        # instance id -> the record the agent keeps of the last run of a server whose container was
        # removed (agent/mcagent/lastrun.py): the container's fields plus "log" with console lines
        self.last_runs: dict[str, dict[str, Any]] = {}
        # What the agent offers beyond the first API version (GET /v1/info); [] = an older agent,
        # which keeps the containers of stopped servers.
        # The default is the current agent's list (agent/mcagent/main.py FEATURES); tests of older
        # agents set fewer.
        self.features: list[str] = list(CURRENT_AGENT_FEATURES)
        # Total RAM of the node as GET /v1/info reports it (docker.memory_bytes): memory admission
        self.memory_bytes: int | None = 16 * 1024**3
        # pull="newer" (feature "pull_newer"): image name -> the id the registry has for its tag
        # now; a different id than the local one is "pulled". Without an entry the local image is current.
        self.registry_image_ids: dict[str, str] = {}
        self.apply_warnings: list[str] = []
        # GET /v1/instances/{id}/ping (feature "ping"): fields replacing the default answer
        self.ping_answer: dict[str, Any] = {}
        # image name -> image id of containers created from it (default: derived from the name)
        self.image_ids: dict[str, str] = {}
        self.fs: dict[str, dict[str, Any]] = {}
        # files/text: True = the current agent (strict decoding, ``encoding``, 415 not_utf8);
        # False = an older agent that decodes bytes that are not UTF-8 as U+FFFD
        self.strict_text = True
        # files/hashes: paths of files larger than the agent hashes (hash None, too_large True)
        self.too_large_files: set[str] = set()
        self.repos: dict[str, list[dict[str, Any]]] = {}
        self.snaps: dict[str, list[dict[str, Any]]] = {}
        self.snapshot_provider = "btrfs"  # tests may switch to "zfs"
        self.snapshot_delete_error: Exception | None = None
        self.snapshot_dependents: set[str] = set()  # ZFS: names other datasets are cloned from
        self.delete_data_warnings: list[str] = []
        self.delete_data_leftovers: list[str] = []  # paths the agent could not remove
        # What the agent reports as its container format with every apply (docker_ops.CONTAINER_FORMAT);
        # None = an agent from before the number existed (the key is missing from the result).
        self.container_format: int | None = self._current_container_format()
        # Exit code of a server that is stopped (0 = it shut down by itself; 137 = killed at the timeout).
        self.stop_exit_code = 0
        # Extra fields of the "stop" object in the answers to stop and restart (flushed, warnings, ...).
        self.stop_info: dict[str, Any] = {}
        # False = an agent from before the "stop" object existed.
        self.reports_stop = True
        self.operations: dict[str, dict[str, Any]] = {}
        # instance id -> the operation the status of that server reports as still running
        self.active_operations: dict[str, dict[str, Any]] = {}
        self.foreign_ports: list[dict[str, Any]] = []
        self.remote_files: dict[str, bytes] = {}
        self.log_lines: dict[str, list[str]] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.commands: list[tuple[str, str]] = []
        # method name -> exception raised by that client method
        self.failures: dict[str, Exception] = {}
        # method name -> callable(*args, **kwargs) run before the simulated method (and before an
        # injected failure): lets a test change state in the middle of a job
        self.hooks: dict[str, Callable[..., Any]] = {}
        # operation kind -> (message, code): the agent operation is reported as failed
        self.failed_operations: dict[str, tuple[str, str]] = {}
        # optional callable(instance_id, command) -> output | None (None = default output)
        self.command_handler: Callable[[str, str], str | None] | None = None
        self.start_healthy = True
        # restic exit code 3 ("some files could not be read"): the backup succeeds with these warnings
        self.backup_warnings: list[str] = []
        # world reset: deleting the old world fails after it was moved aside (a warning plus "leftover")
        self.world_reset_delete_error: str | None = None

    @staticmethod
    def _current_container_format() -> int:
        """An agent of the same release as the panel."""
        from apps.instances.spec import CONTAINER_FORMAT

        return CONTAINER_FORMAT

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
            hook = self.hooks.get(name)
            if hook is not None:
                hook(*args, **kwargs)
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

    def age_containers(self, instance_id: Any = None, seconds: float = 3600) -> None:
        """Make containers look as if they have been in their state for ``seconds``: the retire
        rule leaves a container alone that stopped (or was created) a moment ago."""
        from datetime import timedelta

        then = (timezone.now() - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        for iid, c in self.containers.items():
            if instance_id is not None and iid != str(instance_id):
                continue
            c["created_at"] = then
            if c.get("finished_at"):
                c["finished_at"] = then

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
        c.update(status="exited", running=False, restarting=False, exit_code=exit_code, finished_at=self._now())

    @staticmethod
    def _live(c: dict[str, Any]) -> bool:
        """Running, or in Docker's restart loop (docker_ops.stop / kill and the world reset route)."""
        return bool(c.get("running") or c.get("restarting") or c.get("status") == "restarting")

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
            "features": list(self.features),
            "docker": {"server_version": "fake", "cpus": 4, "memory_bytes": self.memory_bytes},
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

    def list_state(self) -> dict[str, Any]:
        # the same request as list_containers on the real agent (GET /v1/instances): tests that
        # make that one fail mean "the agent cannot be asked"
        failure = self.failures.get("list_containers")
        if failure is not None:
            raise failure
        return {
            "containers": self.list_containers(),
            "last_runs": {iid: self._last_run_summary(iid) for iid in self.last_runs},
        }

    # -- last-run record (agent/mcagent/lastrun.py) ------------------------------------
    LAST_RUN_MAX_LINES = 2000

    def _last_run_summary(self, iid: str) -> dict[str, Any] | None:
        record = self.last_runs.get(iid)
        if record is None:
            return None
        out = {k: copy.deepcopy(v) for k, v in record.items() if k != "log"}
        out["log_lines"] = len(record["log"]["lines"])
        out["log_truncated"] = record["log"]["truncated"]
        return out

    def _save_last_run(self, iid: str, c: dict[str, Any]) -> bool:
        """What the agent does before it removes a container that has run; False when it never ran."""
        if not c.get("started_at"):
            return False
        lines = self.log_lines.pop(iid, [])  # the container's log goes with the container
        self.last_runs[iid] = {
            "retired_at": self._now(),
            "container_id": c["id"],
            "container_name": c["name"],
            "image": c["image"],
            "image_id": c.get("image_id"),
            "status": c["status"],
            "exit_code": c["exit_code"],
            "oom_killed": bool(c["oom_killed"]),
            "error": c.get("error"),
            "restart_count": int(c.get("restart_count") or 0),
            "created_at": c.get("created_at"),
            "started_at": c.get("started_at"),
            "finished_at": c.get("finished_at"),
            "health": c.get("health"),
            "health_output": c.get("health_output"),
            "spec_hash": c.get("spec_hash"),
            "log": {"lines": list(lines[-self.LAST_RUN_MAX_LINES :]), "truncated": False},
        }
        return True

    def _last_run_console(self, iid: str) -> list[str]:
        record = self.last_runs.get(iid)
        if record is None or not record["log"]["lines"]:
            return []
        return [
            *record["log"]["lines"],
            f"[mcpanel] ---- end of the run that stopped (exited with code {record['exit_code']}) ----",
        ]

    def retire(self, instance_id: Any, *, settle_seconds: float = 10.0) -> dict[str, Any]:
        """docker_ops.retire: save the last-run record, then remove the container of a stopped server."""
        from django.utils.dateparse import parse_datetime

        iid = str(instance_id)
        c = self.containers.get(iid)
        if c is None:
            return {"retired": False, "reason": "no_container", "recorded": False, "last_run": self._last_run_summary(iid)}
        if self._live(c) or c["status"] not in ("created", "exited", "dead"):
            raise self._err(
                "The server's container is running; only the container of a stopped server is retired", "container_running", 409
            )
        since = parse_datetime(str((c.get("finished_at") if c.get("started_at") else None) or c.get("created_at")))
        if since is not None and (timezone.now() - since).total_seconds() < settle_seconds:
            raise self._err("The container stopped a moment ago", "not_settled", 409)
        recorded = self._save_last_run(iid, c)
        del self.containers[iid]
        return {"retired": True, "reason": "", "recorded": recorded, "last_run": self._last_run_summary(iid)}

    def forget_last_run(self, instance_id: Any) -> bool:
        return self.last_runs.pop(str(instance_id), None) is not None

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
        if pull not in ("missing", "always", "never") and not (pull == "newer" and "pull_newer" in self.features):
            # an agent's request schema: "newer" exists only with the feature
            raise self._err("Request validation failed", "agent_validation_error", 422)
        if failed := self._injected("apply", iid):
            return failed
        existing = self.containers.get(iid)
        local_id = self.image_ids.get(spec["image"]) or "sha256:" + hashlib.sha256(spec["image"].encode()).hexdigest()
        image_before = (existing or {}).get("image_id") or (self.last_runs.get(iid) or {}).get("image_id")
        pulled = False
        if pull in ("newer", "always") and self.registry_image_ids.get(spec["image"], local_id) != local_id:
            local_id = self.image_ids[spec["image"]] = self.registry_image_ids[spec["image"]]
            pulled = True
        was_running = bool(existing and existing["running"])
        if existing is not None and "retire" in self.features:
            # docker_ops.remove_container inside the agent's apply: the old container is stopped,
            # the record of its run is saved (when it ran) and its log goes with it
            if self._live(existing):
                self._set_exited(existing, self.stop_exit_code)
            self._save_last_run(iid, existing)
        self._root(iid, create=True)
        spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
        c = {
            "exists": True,
            "id": uuid.uuid4().hex,
            "name": f"mcpanel-{uuid.UUID(iid).hex[:12]}",
            "image": spec["image"],
            "image_id": local_id,
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
        result = {"container": self._public(c), "recreated": existing is not None}
        if "pull_newer" in self.features:
            result.update(image_id_before=image_before, image_id=local_id, pulled=pulled, warnings=list(self.apply_warnings))
        if self.container_format is not None:
            result["container_format"] = self.container_format
        return self._op("apply", iid, result)

    def remove_container(self, instance_id: Any, timeout: int = 60) -> bool:
        iid = str(instance_id)
        c = self.containers.pop(iid, None)
        if c is None:
            return False
        if self._live(c):
            self._set_exited(c, self.stop_exit_code)
        if "retire" in self.features:  # agents with the feature keep the last run of a removed container
            self._save_last_run(iid, c)
        return True

    def status(self, instance_id: Any) -> dict[str, Any]:
        c = self.containers.get(str(instance_id))
        # an operation the agent is still working on for this server (tests set it; default none)
        operation = copy.deepcopy(self.active_operations.get(str(instance_id)))
        if c is None:
            answer: dict[str, Any] = {"exists": False, "operation": operation}
            last_run = self._last_run_summary(str(instance_id))
            if last_run is not None:
                answer["last_run"] = last_run
            return answer
        return {**self._public(c), "operation": operation}

    def start(self, instance_id: Any) -> dict[str, Any]:
        c = self._container(str(instance_id))
        if not c["running"]:
            self._set_running(c)
        return self.status(instance_id)

    def _graceful_stop(self, c: dict[str, Any]) -> dict[str, Any]:
        """docker_ops.graceful_stop: what the stop did, with the exit code of the run it ended."""
        info: dict[str, Any] = {"flushed": False, "stop_sent_via_rcon": False, "warnings": [], "exit_code": None}
        if self._live(c):
            info["flushed"] = bool(c.get("running")) and c.get("health") == "healthy"
            self._set_exited(c, self.stop_exit_code)
            info["exit_code"] = c["exit_code"]
            info.update(self.stop_info)
        return info

    def _with_stop(self, instance_id: Any, info: dict[str, Any]) -> dict[str, Any]:
        answer = self.status(instance_id)
        if self.reports_stop:
            answer["stop"] = info
        return answer

    def stop(self, instance_id: Any, timeout: int | None = None) -> dict[str, Any]:
        info = self._graceful_stop(self._container(str(instance_id)))
        return self._with_stop(instance_id, info)

    def restart(self, instance_id: Any, timeout: int | None = None) -> dict[str, Any]:
        c = self._container(str(instance_id))
        info = self._graceful_stop(c)
        self._set_running(c)  # the status in the answer describes the new run (exit code 0 again)
        return self._with_stop(instance_id, info)

    def _container_or_not_running(self, iid: str) -> dict[str, Any] | None:
        """A server without a container is "not running" for kill, stats and commands. Agents
        from before the retire feature answered 404 no_container instead."""
        if "retire" in self.features:
            return self.containers.get(iid)
        return self._container(iid)

    def kill(self, instance_id: Any) -> dict[str, Any]:
        c = self._container_or_not_running(str(instance_id))
        if c is not None and self._live(c):
            self._set_exited(c, 137)
        return self.status(instance_id)

    def stats(self, instance_id: Any) -> dict[str, Any]:
        c = self._container_or_not_running(str(instance_id))
        if c is None or not c["running"]:
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

    def logs(self, instance_id: Any, tail: int = 500, *, previous: bool = True) -> list[str]:
        """docker_ops.console_history: the container log with the end of the previous run in front
        (not with ``previous=False``); without a container the end of the last run; without
        either 404 no_container."""
        iid = str(instance_id)
        c = self.containers.get(iid)
        if c is None:
            if iid not in self.last_runs:
                self._container(iid)  # raises no_container
            return self._last_run_console(iid)[-tail:]
        lines = self.log_lines.get(iid, [])[-tail:]
        record = self._last_run_console(iid)
        if previous and record and self.last_runs[iid]["container_id"] != c["id"] and len(lines) < tail:
            lines = record[-(tail - len(lines)) :] + lines
        return lines

    def ping(self, instance_id: Any) -> dict[str, Any]:
        """GET /v1/instances/{id}/ping: a Minecraft status ping (agents with the feature "ping")."""
        if "ping" not in self.features:
            raise self._err("Not Found", "agent_error", 404)
        c = self.containers.get(str(instance_id))
        if c is None or not c["running"]:
            raise self._err("The server is not running", "not_running", 409)
        answer = {"online": 0, "max": 20, "sample": [], "version": "1.20.4", "motd": "A Minecraft Server", "latency_ms": 3}
        return {**answer, **copy.deepcopy(self.ping_answer)}

    def command(self, instance_id: Any, command: str, timeout: float = 30) -> str:
        iid = str(instance_id)
        c = self._container_or_not_running(iid)
        if c is None or not c["running"]:
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
        self.last_runs.pop(iid, None)
        self.fs.pop(iid, None)
        self.repos.pop(iid, None)
        return self._op(
            "delete_data",
            iid,
            {
                "deleted": True,
                "snapshots_deleted": 0,
                "warnings": list(self.delete_data_warnings),
                "leftovers": list(self.delete_data_leftovers),
            },
        )

    def clone(
        self,
        instance_id: Any,
        target_id: Any,
        exclude_worlds: bool = False,
        *,
        level_name: str | None = None,
        layout: str | None = None,
    ) -> dict[str, Any]:
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
            # the folders of the world the panel names (agents before level_name: the default names)
            level = level_name or "world"
            worlds = (level, f"{level}_nether", f"{level}_the_end")
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
        """files/text. The current agent decodes strictly: UTF-8, or ISO-8859-1 for a ``*.properties``
        file, with the ``encoding`` it read; other files that are not UTF-8 are refused with 415
        ``not_utf8``. ``strict_text = False`` = an older agent (lossy U+FFFD decoding, no ``encoding``)."""
        iid = str(instance_id)
        data = self._read(iid, path)
        if b"\x00" in data[:8192]:
            raise self._err("This looks like a binary file and cannot be edited as text", "binary_file", 415)
        entry = self.stat_file(iid, path)
        answer = {"path": entry["path"], "size": len(data), "modified": entry["modified"]}
        if not self.strict_text:
            return {**answer, "content": data.decode("utf-8", errors="replace")}
        try:
            return {**answer, "content": data.decode("utf-8"), "encoding": "utf-8"}
        except UnicodeDecodeError:
            if path.lower().endswith(".properties"):
                return {**answer, "content": data.decode("iso-8859-1"), "encoding": "iso-8859-1"}
            raise self._err("This file is not UTF-8 text; editing it here would damage it", "not_utf8", 415) from None

    def write_text(
        self, instance_id: Any, path: str, content: str, create_only: bool = False, encoding: str | None = None
    ) -> dict[str, Any]:
        """files/text PUT: the requested encoding, else the one the agent reads the file with
        (ISO-8859-1 only for an existing *.properties file that is not UTF-8)."""
        iid = str(instance_id)
        if not self.strict_text:
            size = self._write(iid, path, content.encode(), overwrite=not create_only)
            return {"path": path, "size": size}
        chosen = encoding
        if chosen is None:
            chosen = "utf-8"
            current = self.get_file(iid, path)
            if current is not None and path.lower().endswith(".properties"):
                try:
                    current.decode("utf-8")
                except UnicodeDecodeError:
                    chosen = "iso-8859-1"
        try:
            data = content.encode(chosen)
        except UnicodeEncodeError:
            if encoding is not None:
                raise self._err(f"The text contains characters that {chosen} cannot store", "not_encodable", 400) from None
            chosen, data = "utf-8", content.encode("utf-8")
        size = self._write(iid, path, data, overwrite=not create_only)
        return {"path": path, "size": size, "encoding": chosen}

    def download(
        self, instance_id: Any, path: str | list[str], *, endpoint: str = "download"
    ) -> tuple[Iterator[bytes], int | None, Any]:
        iid = str(instance_id)
        if endpoint != "archive":
            data = self._read(iid, path)  # type: ignore[arg-type]
            return iter([data]), len(data), None
        root = self._root(iid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for item in [path] if isinstance(path, str) else list(path):
                parts = self._parts(item)
                rel = "/".join(parts)
                kind = self._kind(root, rel)
                if kind is None:
                    raise self._err("Does not exist", "not_found", 404)
                if kind != "directory":
                    raise self._err("Only folders can be downloaded as a zip", "not_a_directory", 400)
                prefix = parts[-1] if parts else "server"
                for f, data in sorted(root["files"].items()):
                    if not rel or f.startswith(rel + "/"):
                        zf.writestr(f"{prefix}/{f[len(rel) + 1 :] if rel else f}", data)
        return iter([buf.getvalue()]), None, None

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

    def rename(
        self, instance_id: Any, source: str, destination: str, overwrite: bool = False, *, timeout: float | None = None
    ) -> dict[str, Any]:
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

    def delete_files(
        self, instance_id: Any, paths: list[str], recursive: bool = False, *, timeout: float | None = None
    ) -> dict[str, Any]:
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

    def hash_files(
        self, instance_id: Any, path: str, algorithm: str = "sha1", *, suffixes: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """files/hashes: files in ``too_large_files`` (paths) get ``hash: None, too_large: True``;
        ``suffixes`` filters the names (the current agent's ``suffix``)."""
        try:
            listing = self.list_files(instance_id, path)
        except Exception as exc:
            if getattr(exc, "error_code", None) == "not_found":
                return []
            raise
        root = self._root(str(instance_id))
        out = []
        for entry in listing["entries"]:
            if entry["type"] != "file" or (suffixes and not entry["name"].endswith(tuple(suffixes))):
                continue
            if entry["path"] in self.too_large_files:
                out.append({**entry, "hash": None, "too_large": True})
                continue
            digest = hashlib.new(algorithm, root["files"][entry["path"]]).hexdigest()
            out.append({**entry, "hash": digest})
        return out

    def hash_files_partial(
        self, instance_id: Any, path: str, algorithm: str = "sha1", *, wait: float, suffixes: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        files = self.hash_files(instance_id, path, algorithm, suffixes=suffixes)
        return {"files": files, "complete": True, "pending": 0}

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

    # -- worlds (agent/mcagent/world.py semantics, zip archives only) --------------------
    WORLD_DIMENSIONS = {"nether": ("_nether", "DIM-1"), "end": ("_the_end", "DIM1")}
    WORLD_RESERVED_FOLDERS = frozenset(
        {
            "mods",
            "plugins",
            "config",
            "defaultconfigs",
            "libraries",
            "logs",
            "crash-reports",
            "versions",
            "cache",
            "kubejs",
            "scripts",
            "resourcepacks",
            "downloads",
            "backups",
            "server.properties",
        }
    )
    # world reset: keep option -> role -> ((entry name in that world folder, required type), ...)
    WORLD_RESET_KEEP: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
        "datapacks": {"overworld": (("datapacks", "directory"),)},
        "config": {
            "overworld": (("serverconfig", "directory"), ("paper-world.yml", "file")),
            "nether": (("paper-world.yml", "file"),),
            "end": (("paper-world.yml", "file"),),
        },
    }
    WORLD_KEEPABLE_NAMES = frozenset({"datapacks", "serverconfig", "paper-world.yml"})
    WORLD_MARKERS = frozenset({"level.dat", "level.dat_old", "session.lock", "uid.dat", "region", "DIM-1", "DIM1", "dimensions"})

    def _world_paths(self, level_name: str, layout: str) -> list[tuple[str, str]]:
        parts = self._parts(level_name)
        out = [("overworld", "/".join(parts))]
        if layout == "bukkit":
            for role, (suffix, _dim) in self.WORLD_DIMENSIONS.items():
                out.append((role, "/".join([*parts[:-1], parts[-1] + suffix])))
        return out

    def _level_parts(self, level_name: str) -> list[str]:
        """agent/mcagent/world.py level_parts: validate ``level-name`` and split it."""
        from apps.nodes.client import AgentError

        if not isinstance(level_name, str) or not level_name.strip():
            raise self._err("The world folder name is empty", "invalid_level_name", 400)
        if "\\" in level_name or len(level_name) > 200:
            raise self._err(f"Unsupported world folder name {level_name!r}", "invalid_level_name", 400)
        try:
            parts = self._parts(level_name)
        except AgentError as exc:
            raise self._err(f"Invalid world folder name {level_name!r}: {exc.detail}", "invalid_level_name", 400) from None
        if not parts or len(parts) > 4 or any(p.startswith(".") or any(ord(ch) < 32 for ch in p) for p in parts):
            raise self._err(f"Invalid world folder name {level_name!r}", "invalid_level_name", 400)
        if parts[0].lower() in self.WORLD_RESERVED_FOLDERS:
            raise self._err(
                f"'{level_name}' is a server folder, not a world folder; fix level-name in server.properties",
                "invalid_level_name",
                400,
            )
        return parts

    def _world_state(self, root: dict[str, Any], rel: str) -> tuple[str, set[str]]:
        """agent/mcagent/world.py classify: (state, names of the direct entries)."""
        kind = self._kind(root, rel)
        if kind is None:
            return "missing", set()
        if kind != "directory":
            return "not_a_directory", set()
        names = {p.rsplit("/", 1)[-1] for p in self._children(root, rel)}
        if not names:
            return "empty", names
        if names & self.WORLD_MARKERS:
            return "world", names
        return ("pending" if names <= self.WORLD_KEEPABLE_NAMES else "unknown"), names

    def _world_keepable(self, root: dict[str, Any], role: str, rel: str) -> dict[str, list[str]]:
        """agent/mcagent/world.py keepable: keep option -> entries a reset would carry over."""
        out: dict[str, list[str]] = {}
        for key, roles in self.WORLD_RESET_KEEP.items():
            for name, kind in roles.get(role, ()):
                path = f"{rel}/{name}"
                if self._kind(root, path) != kind:
                    continue
                if kind == "directory" and not self._children(root, path):
                    continue
                out.setdefault(key, []).append(name)
        return out

    def world_info(self, instance_id: Any, level_name: str, layout: str, *, sizes: bool = True) -> dict[str, Any]:
        root = self.fs.get(str(instance_id)) or {"files": {}, "dirs": set(), "mtime": {}}
        level = "/".join(self._level_parts(level_name))
        folders, total = [], 0
        for role, rel in self._world_paths(level, layout):
            kind = self._kind(root, rel)
            exists = kind == "directory"
            state, names = self._world_state(root, rel)
            entry: dict[str, Any] = {
                "role": role,
                "path": rel,
                "exists": exists,
                "type": kind,
                "has_level_dat": exists and f"{rel}/level.dat" in root["files"],
                "bytes": 0,
                "files": 0,
                "modified": None,
                "state": state,
                "markers": sorted(names & self.WORLD_MARKERS),
                "keepable": self._world_keepable(root, role, rel) if state == "world" else {},
            }
            if exists and sizes:  # sizes=False skips the walk over every file of the world
                files = {p: v for p, v in root["files"].items() if p.startswith(rel + "/")}
                size = sum(len(v) for v in files.values())
                entry.update(bytes=size, files=len(files), modified=max((root["mtime"].get(p, 0.0) for p in files), default=None))
                total += size
            if role == "overworld" and exists:
                entry["dimensions"] = [
                    r for r, (_s, dim) in self.WORLD_DIMENSIONS.items() if self._kind(root, f"{rel}/{dim}") == "directory"
                ]
            folders.append(entry)
        leftovers = sorted(d for d in root["dirs"] if "/" not in d and d.startswith(".mcpanel-world-"))
        return {"level_name": level, "layout": layout, "folders": folders, "total_bytes": total, "leftovers": leftovers}

    def world_reset(
        self, instance_id: Any, *, level_name: str, layout: str, expect: list[str], keep: list[str]
    ) -> dict[str, Any]:
        """POST /world/reset: a refusal before the operation raises; what happens after it is the
        operation's result (``failed_operations["world_reset"]`` injects a failed one)."""
        iid = str(instance_id)
        if (  # WorldResetRequest: FastAPI answers 422
            layout not in ("vanilla", "bukkit")
            or not 1 <= len(level_name) <= 200
            or not 1 <= len(expect) <= 3
            or len(keep) > 2
            or any(k not in self.WORLD_RESET_KEEP for k in keep)
        ):
            raise self._err("Request validation failed", "agent_validation_error", 422)
        parts = self._level_parts(level_name)
        if self._live(self.containers.get(iid, {})):
            raise self._err("Stop the server before resetting its world", "must_stop", 409)
        if failed := self._injected("world_reset", iid):
            return failed
        return self._world_reset(iid, parts, layout, list(expect), list(dict.fromkeys(keep)))

    def _world_reset(self, iid: str, parts: list[str], layout: str, expect: list[str], keep: list[str]) -> dict[str, Any]:
        """agent/mcagent/world.py reset: only folders that look like a world, and only the expected ones."""

        def fail(message: str, code: str) -> dict[str, Any]:
            return self._op("world_reset", iid, error=(message, code))

        root = self.fs.get(iid) or {"files": {}, "dirs": set(), "mtime": {}}
        level = "/".join(parts)
        present: list[tuple[str, str]] = []
        carry: dict[str, list[str]] = {}
        skipped: list[str] = []
        for role, dest in self._world_paths(level, layout):
            state, _names = self._world_state(root, dest)
            if state == "missing":
                continue
            if state == "not_a_directory":
                return fail(
                    f"'{dest}' is not a folder (a file or a link); move it away or fix level-name in "
                    "server.properties. Nothing was changed.",
                    "not_a_directory",
                )
            if state == "unknown":
                return fail(
                    f"'{dest}' does not look like a Minecraft world (no level.dat, session.lock or region folder "
                    "in it), so it was not deleted. Check level-name in server.properties, or delete the folder on "
                    "the Files page. Nothing was changed.",
                    "not_a_world",
                )
            if state != "world":  # empty, or only what an earlier reset carried over
                skipped.append(dest)
                continue
            present.append((role, dest))
            available = self._world_keepable(root, role, dest)
            names = [name for key in keep for name in available.get(key, [])]
            if names:
                carry[role] = names
        found = [dest for _role, dest in present]
        if sorted(expect) != sorted(found):
            now = ", ".join(f"'{dest}'" for dest in found) or "none"
            return fail(
                "The world folders are not the ones that were confirmed for this reset "
                f"(the world folders are now: {now}). Nothing was changed.",
                "world_changed",
            )
        result: dict[str, Any] = {
            "level_name": level,
            "layout": layout,
            "deleted": [],
            "kept": [],
            "skipped": skipped,
            "leftover": None,
            "warnings": [],
        }
        if not present:
            return self._op("world_reset", iid, result)
        aside = f".mcpanel-world-old-{uuid.uuid4().hex[:12]}"
        self.mkdir(iid, aside)
        for role, dest in present:
            self.rename(iid, dest, f"{aside}/{role}")
        for role, dest in present:
            if role not in carry:
                continue
            self.mkdir(iid, dest)
            for name in carry[role]:
                self.rename(iid, f"{aside}/{role}/{name}", f"{dest}/{name}")
                result["kept"].append(f"{dest}/{name}")
        result["deleted"] = found
        if self.world_reset_delete_error:  # past the point of no return: a warning, never an error
            result["leftover"] = aside
            result["warnings"].append(
                f"The old world could not be deleted completely ({self.world_reset_delete_error}); "
                f"what is left of it is in '{aside}'"
            )
        else:
            self.delete_files(iid, [aside], recursive=True)
        return self._op("world_reset", iid, result)

    def world_replace(
        self, instance_id: Any, *, archive: str, level_name: str, layout: str, delete_archive: bool = False
    ) -> dict[str, Any]:
        iid = str(instance_id)
        if self.containers.get(iid, {}).get("running"):
            raise self._err("Stop the server before replacing its world", "must_stop", 409)
        parts = self._parts(level_name)
        if not parts or parts[0].startswith(".") or parts[0].lower() in ("mods", "plugins", "config", "logs"):
            raise self._err(f"Invalid world folder name {level_name!r}", "invalid_level_name", 400)
        arc = self._parts(archive)
        for _role, rel in self._world_paths(level_name, layout):
            if arc[: len(rel.split("/"))] == rel.split("/"):
                raise self._err(f"The archive is inside the world folder '{rel}'", "archive_in_world", 400)
        data = self._read(iid, archive)
        try:
            if failed := self._injected("world_replace", iid):
                return failed
            return self._world_swap(iid, data, parts, layout)
        finally:
            if delete_archive:
                self._root(iid)["files"].pop("/".join(self._parts(archive)), None)

    def _world_swap(self, iid: str, data: bytes, parts: list[str], layout: str) -> dict[str, Any]:
        def fail(message: str, code: str) -> dict[str, Any]:
            return self._op("world_replace", iid, error=(message, code))

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}
        except zipfile.BadZipFile:
            return fail("The archive is corrupt or truncated", "archive_corrupt")
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) == 1 and all("/" in n for n in names):  # strip=auto
            names = {n.split("/", 1)[1]: v for n, v in names.items()}
        base, main, siblings = "", None, {}
        for _depth in range(3):
            if f"{base}level.dat" in names:
                main = ""
                break
            dirs = sorted({n[len(base) :].split("/", 1)[0] for n in names if n.startswith(base) and "/" in n[len(base) :]})
            worlds = [d for d in dirs if f"{base}{d}/level.dat" in names]
            if worlds:
                dims = {w for w in worlds for s, _d in self.WORLD_DIMENSIONS.values() if w.endswith(s) and w[: -len(s)] in worlds}
                primary = [w for w in worlds if w not in dims]
                if parts[-1] in primary:
                    main = parts[-1]
                elif len(primary) == 1:
                    main = primary[0]
                elif "world" in primary:
                    main = "world"
                else:
                    return fail(f"The archive contains several worlds ({', '.join(primary)})", "multiple_worlds")
                siblings = {r: f"{base}{main}{s}/" for r, (s, _d) in self.WORLD_DIMENSIONS.items() if f"{main}{s}" in dirs}
                main = f"{main}/"
                break
            if len(dirs) != 1:
                break
            base = f"{base}{dirs[0]}/"
        if main is None:
            return fail("No Minecraft world found in the archive", "no_level_dat")
        prefix = f"{base}{main}"
        if not names[f"{prefix}level.dat"].startswith(b"\x1f\x8b"):
            return fail("level.dat is not a Java Edition level file", "invalid_world")

        def subtree(pre: str) -> dict[str, bytes]:
            return {n[len(pre) :]: v for n, v in names.items() if n.startswith(pre)}

        trees = {"overworld": subtree(prefix)}
        converted = []
        if layout == "bukkit":
            trees.update({role: subtree(pre) for role, pre in siblings.items()})
        else:
            for role, pre in siblings.items():
                dim = self.WORLD_DIMENSIONS[role][1]
                moved = {f"{dim}/{k[len(dim) + 1 :]}": v for k, v in subtree(pre).items() if k.startswith(f"{dim}/")}
                if moved and not any(k.startswith(f"{dim}/") for k in trees["overworld"]):
                    trees["overworld"].update(moved)
                    converted.append(role)
        root = self._root(iid)
        replaced, installed = [], []
        for role, dest in self._world_paths("/".join(parts), layout):
            if self._kind(root, dest) is not None:
                self.delete_files(iid, [dest], recursive=True)
                replaced.append(dest)
            if role in trees:
                root["dirs"].add(dest)
                for rel, content in trees[role].items():
                    self.put_file(iid, f"{dest}/{rel}", content)
                installed.append(dest)
        return self._op(
            "world_replace",
            iid,
            {
                "level_name": "/".join(parts),
                "layout": layout,
                "installed": installed,
                "replaced": replaced,
                "converted": converted,
                "files": sum(len(t) for t in trees.values()),
                "bytes": sum(len(v) for t in trees.values() for v in t.values()),
                "skipped_count": 0,
                "warnings": [],
            },
        )

    # -- snapshots (btrfs / zfs semantics; the agent's provider decides) --------------------
    def snapshots(self, instance_id: Any) -> dict[str, Any]:
        iid = str(instance_id)
        return {
            "capability": {
                "provider": self.snapshot_provider,
                "available": True,
                "fstype": self.snapshot_provider,
                "reason": None,
                "instance_is_subvolume": iid in self.fs,
                "instance_ready": iid in self.fs,
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
        if self.snapshot_delete_error is not None:
            raise self.snapshot_delete_error
        if name in self.snapshot_dependents:
            raise self._err(f"Snapshot {name} cannot be deleted yet", "snapshot_has_dependents", 409)
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
    @staticmethod
    def _excluded(rel: str, patterns: list[str]) -> bool:
        """restic ``--exclude`` for the patterns the panel sends: plain names and ``*`` globs.

        A pattern is matched against whole path components and may match at any depth; a match
        on a folder leaves out everything inside it. A pattern with a leading ``/`` is anchored
        at the filesystem root and so never matches inside the server folder. ``**`` is not simulated.
        """
        comps = rel.split("/")
        for pattern in patterns:
            pat = [part for part in pattern.split("/") if part]
            if not pat or pattern.startswith("/") or "**" in pat:
                continue
            for end in range(len(pat), len(comps) + 1):
                if all(fnmatch.fnmatchcase(c, part) for c, part in zip(comps[end - len(pat) : end], pat, strict=True)):
                    return True
        return False

    def list_backups(self, instance_id: Any, timeout: float = 300) -> list[dict[str, Any]]:
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
        patterns = [*(excludes or []), ".mcpanel-tmp-*"]  # the agent always adds its own temporary files
        for key in ("files", "mtime"):
            for path in [path for path in tree[key] if self._excluded(path, patterns)]:
                del tree[key][path]
        tree["dirs"] = {path for path in tree["dirs"] if not self._excluded(path, patterns)}
        total = sum(len(v) for v in tree["files"].values())
        self.repos.setdefault(iid, []).append(
            {
                "id": snap_id,
                "short_id": snap_id[:8],
                "time": self._now(),
                "tags": [f"instance:{iid}", f"backup:{backup_id}", *tags],
                "paths": [f"/srv/mcpanel/instances/{iid}"],
                # what restic keeps with every snapshot (restic 0.17 and later)
                "summary": {
                    "backup_start": self._now(),
                    "backup_end": self._now(),
                    "files_new": len(tree["files"]),
                    "files_changed": 0,
                    "files_unmodified": 0,
                    "data_added": total,
                    "total_files_processed": len(tree["files"]),
                    "total_bytes_processed": total,
                },
                "tree": tree,
            }
        )
        if paused:
            self.commands.append((iid, "save-on"))
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
                "warnings": list(self.backup_warnings),
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

    def backup_files(self, instance_id: Any, snapshot_id: str, path: str = "") -> dict[str, Any]:
        """Restic.ls: the direct children of a folder in a snapshot (a path that is not in it lists nothing)."""
        if not re.fullmatch(r"[a-f0-9]{8,64}", snapshot_id):
            raise self._err("Request validation failed", "agent_validation_error", 422)
        snap = next((s for s in self.repos.get(str(instance_id), []) if s["id"].startswith(snapshot_id)), None)
        if snap is None:
            raise self._err("Backup snapshot not found", "snapshot_not_found", 404)
        tree = snap["tree"]
        rel = "/".join(self._parts(path))
        entries = []
        if self._kind(tree, rel) == "directory":
            for child in self._children(tree, rel):
                kind = self._kind(tree, child)
                entries.append(
                    {
                        "name": child.rsplit("/", 1)[-1],
                        "path": child,
                        "type": kind,
                        "size": len(tree["files"][child]) if kind == "file" else 0,
                        "modified": None,
                    }
                )
        return {"path": rel, "entries": entries}

    def backup_restore_path(self, instance_id: Any, snapshot_id: str, path: str) -> dict[str, Any]:
        """Restore one file or folder of a snapshot over the server folder (restore-path)."""
        iid = str(instance_id)
        rel = "/".join(self._parts(path))
        if not rel:
            raise self._err("Choose a file or folder (use a full restore for everything)", "invalid_path", 400)
        if self.containers.get(iid, {}).get("running"):
            raise self._err("Stop the server before restoring", "target_running", 409)
        if failed := self._injected("restore_path", iid):
            return failed
        snap = next((s for s in self.repos.get(iid, []) if s["id"].startswith(snapshot_id)), None)
        if snap is None:
            return self._op("restore_path", iid, error=("Backup snapshot not found", "snapshot_not_found"))
        tree = snap["tree"]
        files = {p: d for p, d in tree["files"].items() if p == rel or p.startswith(rel + "/")}
        if not files and rel not in tree["dirs"]:
            return self._op("restore_path", iid, error=(f"'{rel}' is not in this backup", "not_found"))
        for p, data in files.items():
            self.put_file(iid, p, data)
        return self._op("restore_path", iid, {"restored": rel, "files": len(files)})

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
@pytest.fixture(scope="session", autouse=True)
def _upload_dir() -> None:
    """The web process makes the upload spool folder at start (mcpanel/asgi.py); importing the
    settings creates nothing, so the tests make it the same way."""
    from apps.core.uploads import ensure_upload_dir

    ensure_upload_dir()


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
