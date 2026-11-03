"""Versioned, deduplicated, encrypted backups using restic.

One restic repository per instance (``<backup_root>/restic/<instance-id>``) keeps
locking independent between servers and makes deleting an instance's backups a
simple directory removal. All arguments passed to restic are constructed by the
agent from validated values; user input never reaches a shell.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from .config import Settings
from .docker_ops import AgentError

log = logging.getLogger(__name__)

# Restic exit codes (restic >= 0.17)
EXIT_REPO_MISSING = 10
EXIT_LOCKED = 11
EXIT_WRONG_PASSWORD = 12


def restic_available(settings: Settings) -> str | None:
    binary = shutil.which(settings.restic_binary)
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=10, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Restic:
    def __init__(self, settings: Settings, instance_id: UUID) -> None:
        self.settings = settings
        self.instance_id = instance_id
        self.repo = settings.backups_dir / "restic" / str(instance_id)
        binary = shutil.which(settings.restic_binary)
        if not binary:
            raise AgentError("restic is not installed in the agent image", "restic_missing", 503)
        self.binary = binary

    # -- plumbing -------------------------------------------------------------------
    def _password_file(self) -> Path:
        if self.settings.restic_password_file and self.settings.restic_password_file.exists():
            return self.settings.restic_password_file
        pw = self.settings.resolve_restic_password()
        path = self.settings.state_dir / "restic-password"
        if not path.exists() or path.read_text().strip() != pw:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pw)
            path.chmod(0o600)
        return path

    def _env(self) -> dict[str, str]:
        cache = self.settings.state_dir / "restic-cache"
        cache.mkdir(parents=True, exist_ok=True)
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.settings.state_dir),
            "RESTIC_REPOSITORY": str(self.repo),
            "RESTIC_PASSWORD_FILE": str(self._password_file()),
            "RESTIC_CACHE_DIR": str(cache),
            "RESTIC_PROGRESS_FPS": "0.5",
        }

    def _raise_for(self, proc: subprocess.CompletedProcess[str] | int, stderr: str, action: str) -> None:
        code = proc if isinstance(proc, int) else proc.returncode
        msg = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit code {code}"
        if code == EXIT_LOCKED:
            raise AgentError(f"Backup repository is locked by another process ({action})", "repo_locked", 409)
        if code == EXIT_WRONG_PASSWORD:
            raise AgentError(
                "Backup repository password is wrong (was the agent restic password changed?)",
                "repo_password",
                500,
            )
        if code == EXIT_REPO_MISSING:
            raise AgentError("No backup repository exists for this instance yet", "repo_missing", 404)
        raise AgentError(f"restic {action} failed: {msg}", "restic_failed", 500)

    def run(
        self, args: list[str], *, timeout: float | None = 3600, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [self.binary, *args],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
        if proc.returncode not in (0, 3):  # 3 = backup completed but some files unreadable
            self._raise_for(proc, proc.stderr, args[0])
        return proc

    def initialized(self) -> bool:
        return (self.repo / "config").exists()

    def ensure_init(self) -> None:
        if self.initialized():
            return
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        self.run(["init"], timeout=120)

    # -- operations ------------------------------------------------------------------
    def backup(
        self,
        source: Path,
        tags: list[str],
        excludes: list[str],
        progress: Callable[[float, str], None] | None = None,
    ) -> dict[str, Any]:
        self.ensure_init()
        args = [self.binary, "backup", "--json", "--host", "mcpanel", "--one-file-system"]
        for t in tags:
            args += ["--tag", t]
        for ex in excludes:
            args += ["--exclude", ex]
        args += ["--exclude", ".mcpanel-tmp-*", str(source)]
        proc = subprocess.Popen(
            args, env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        summary: dict[str, Any] = {}
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("message_type") == "status" and progress:
                pct = float(msg.get("percent_done") or 0.0)
                files = msg.get("files_done", 0)
                total = msg.get("total_files", 0)
                progress(pct, f"Backing up files ({files}/{total})")
            elif msg.get("message_type") == "summary":
                summary = msg
        stderr = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        if code not in (0, 3):
            self._raise_for(code, stderr, "backup")
        if not summary.get("snapshot_id"):
            raise AgentError(
                f"restic backup produced no snapshot: {stderr.strip()[-300:]}", "restic_failed", 500
            )
        summary["warnings"] = stderr.strip().splitlines()[-20:] if code == 3 else []
        return summary

    def snapshots(self, tag: str | None = None) -> list[dict[str, Any]]:
        if not self.initialized():
            return []
        args = ["snapshots", "--json"]
        if tag:
            args += ["--tag", tag]
        proc = self.run(args, timeout=300)
        return json.loads(proc.stdout or "[]") or []

    def snapshot(self, snapshot_id: str) -> dict[str, Any]:
        for snap in self.snapshots():
            if snap.get("id", "").startswith(snapshot_id) or snap.get("short_id") == snapshot_id:
                return snap
        raise AgentError("Backup snapshot not found", "snapshot_not_found", 404)

    def restore(
        self, snapshot_id: str, target: Path, progress: Callable[[float, str], None] | None = None
    ) -> dict[str, Any]:
        snap = self.snapshot(snapshot_id)
        paths = snap.get("paths") or []
        if len(paths) != 1:
            raise AgentError("Snapshot has an unexpected layout", "snapshot_layout", 500)
        target.mkdir(parents=True, exist_ok=False)
        args = [self.binary, "restore", f"{snap['id']}:{paths[0]}", "--target", str(target), "--json"]
        proc = subprocess.Popen(
            args, env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        summary: dict[str, Any] = {}
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("message_type") == "status" and progress:
                progress(float(msg.get("percent_done") or 0.0), "Restoring files")
            elif msg.get("message_type") == "summary":
                summary = msg
        stderr = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        if code != 0:
            self._raise_for(code, stderr, "restore")
        return {"snapshot": snap["id"], **summary}

    def forget(self, snapshot_ids: list[str], prune: bool = True) -> dict[str, Any]:
        if not snapshot_ids:
            return {"removed": []}
        args = ["forget", *snapshot_ids]
        if prune:
            args.append("--prune")
        self.run(args, timeout=3600)
        return {"removed": snapshot_ids}

    def check(self, read_data_subset: str | None = None) -> dict[str, Any]:
        if not self.initialized():
            raise AgentError("No backup repository exists for this instance yet", "repo_missing", 404)
        args = ["check"]
        if read_data_subset:
            args += ["--read-data-subset", read_data_subset]
        proc = subprocess.run(
            [self.binary, *args], env=self._env(), capture_output=True, text=True, timeout=7200, check=False
        )
        ok = proc.returncode == 0
        output = (proc.stdout + proc.stderr).strip().splitlines()[-40:]
        if proc.returncode == EXIT_LOCKED:
            raise AgentError("Backup repository is locked", "repo_locked", 409)
        return {"ok": ok, "output": output}

    def stats(self) -> dict[str, Any]:
        if not self.initialized():
            return {"total_size": 0, "snapshots_count": 0}
        proc = self.run(["stats", "--mode", "raw-data", "--json"], timeout=600)
        return json.loads(proc.stdout or "{}")

    # -- browsing ----------------------------------------------------------------------
    def _root_path(self, snap: dict[str, Any]) -> str:
        paths = snap.get("paths") or []
        if len(paths) != 1:
            raise AgentError("Snapshot has an unexpected layout", "snapshot_layout", 500)
        return paths[0].rstrip("/")

    def ls(self, snapshot_id: str, rel_parts: list[str]) -> list[dict[str, Any]]:
        """Direct children of a directory inside a snapshot (paths relative to the server folder)."""
        snap = self.snapshot(snapshot_id)
        base = self._root_path(snap)
        target = "/".join([base, *rel_parts])
        proc = self.run(["ls", "--json", snap["id"], target], timeout=600)
        out = []
        for line in proc.stdout.splitlines():
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            if node.get("struct_type") != "node" and "path" not in node:
                continue
            path = node.get("path", "")
            if path == target or not path.startswith(base + "/"):
                continue
            rel = path[len(base) + 1 :]
            if rel.count("/") != len(rel_parts):  # only direct children
                continue
            out.append(
                {
                    "name": node.get("name"),
                    "path": rel,
                    "type": {"dir": "directory"}.get(node.get("type"), node.get("type")),
                    "size": node.get("size", 0),
                    "modified": node.get("mtime"),
                }
            )
        out.sort(key=lambda e: (e["type"] != "directory", (e["name"] or "").lower()))
        return out

    def dump(self, snapshot_id: str, rel_parts: list[str]) -> subprocess.Popen[bytes]:
        """Stream a single file from a snapshot (caller reads stdout)."""
        snap = self.snapshot(snapshot_id)
        target = "/".join([self._root_path(snap), *rel_parts])
        return subprocess.Popen(
            [self.binary, "dump", snap["id"], target],
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def restore_path(self, snapshot_id: str, rel_parts: list[str], staging: Path) -> Path:
        """Restore one file/directory into ``staging``; returns the restored item path."""
        if not rel_parts:
            raise AgentError("Choose a file or folder to restore", "invalid_path", 400)
        snap = self.snapshot(snapshot_id)
        parent = "/".join([self._root_path(snap), *rel_parts[:-1]])
        staging.mkdir(parents=True, exist_ok=False)
        self.run(
            ["restore", f"{snap['id']}:{parent}", "--target", str(staging), "--include", f"/{rel_parts[-1]}"],
            timeout=6 * 3600,
        )
        restored = staging / rel_parts[-1]
        if not restored.exists() and not restored.is_symlink():
            raise AgentError("That path does not exist in the backup", "not_found", 404)
        return restored

    def unlock(self) -> None:
        if self.initialized():
            self.run(["unlock"], timeout=60)

    def destroy(self) -> None:
        if self.repo.exists():
            shutil.rmtree(self.repo)
