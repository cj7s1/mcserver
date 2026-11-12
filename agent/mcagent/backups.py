"""Versioned, deduplicated, encrypted backups using restic.

One restic repository per instance (``<backup_root>/restic/<instance-id>``) keeps
locking independent between servers and makes deleting an instance's backups a
simple directory removal. All arguments passed to restic are constructed by the
agent from validated values; user input never reaches a shell.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

from .config import Settings, write_private
from .docker_ops import AgentError

log = logging.getLogger(__name__)

# Restic exit codes (restic >= 0.17)
EXIT_REPO_MISSING = 10
EXIT_LOCKED = 11
EXIT_WRONG_PASSWORD = 12

STDERR_TAIL_LINES = 200  # kept for error messages; the rest of restic's stderr is discarded
MAX_OUTPUT_LINE = 1024 * 1024  # longest line read from restic at once
MAX_STORED_LINE = 2000

# Locks. restic never removes a stale lock by itself: one restic that dies without cleaning up
# (SIGKILL, the agent container stopped mid-backup) blocks that server's repository until
# `restic unlock` runs. So: every repository is unlocked with --remove-all when the agent starts
# (no restic of this agent can be alive then, see startup_unlock), a command that finds the
# repository locked (exit 11) runs `restic unlock` (stale locks only) and is tried once more, the
# long commands wait for a lock instead of failing at once, and restic is always stopped with
# SIGINT first (it then removes its own lock) and only killed when it does not react.
RETRY_LOCK = "2m"  # --retry-lock for backup, forget and check
UNLOCK_TIMEOUT = 120.0
SIGINT_GRACE_SECONDS = 15.0  # after SIGINT, before SIGTERM
SIGTERM_GRACE_SECONDS = 5.0  # after SIGTERM, before SIGKILL
# restic's include/exclude patterns are globs per path component (Go filepath.Match)
_GLOB_SPECIAL = re.compile(r"([\\\[\]*?])")


def glob_escape(name: str) -> str:
    """``name`` as a restic pattern that matches exactly that name."""
    return _GLOB_SPECIAL.sub(r"\\\1", name)


# Every restic process this agent started and has not reaped yet (stopped at agent shutdown).
_children_lock = threading.Lock()
_children: set[subprocess.Popen[Any]] = set()


def _register(proc: subprocess.Popen[Any]) -> None:
    with _children_lock:
        _children.add(proc)


def _unregister(proc: subprocess.Popen[Any]) -> None:
    with _children_lock:
        _children.discard(proc)


def running_children() -> int:
    with _children_lock:
        return sum(1 for p in _children if p.poll() is None)


def terminate(
    proc: subprocess.Popen[Any],
    int_grace: float = SIGINT_GRACE_SECONDS,
    term_grace: float = SIGTERM_GRACE_SECONDS,
) -> int:
    """Stop a restic process the way that lets it remove its lock: SIGINT, then SIGTERM, then SIGKILL."""
    for sig, grace in ((signal.SIGINT, int_grace), (signal.SIGTERM, term_grace)):
        if proc.poll() is not None:
            return proc.returncode
        with contextlib.suppress(OSError):
            proc.send_signal(sig)
        try:
            return proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
    with contextlib.suppress(OSError):
        proc.kill()
    return proc.wait()


def stop_children(grace: float = 6.0) -> int:
    """Agent shutdown: stop every running restic child (all at once, within about ``grace`` seconds).

    SIGINT first, so restic removes its lock; SIGTERM for what is still running after most of
    ``grace``; SIGKILL for the rest. Returns how many processes were running.
    """
    with _children_lock:
        procs = [p for p in _children if p.poll() is None]
    if not procs:
        return 0
    log.warning("Stopping %d running restic process(es) before the agent exits", len(procs))
    stages = ((signal.SIGINT, grace * 0.7), (signal.SIGTERM, grace * 0.3))
    for sig, wait in stages:
        alive = [p for p in procs if p.poll() is None]
        for p in alive:
            with contextlib.suppress(OSError):
                p.send_signal(sig)
        deadline = time.monotonic() + wait
        for p in alive:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
    for p in procs:
        if p.poll() is None:
            with contextlib.suppress(OSError):
                p.kill()
    return len(procs)


# Repositories whose start-up unlock has not run yet: commands on them wait for it (so the unlock
# with --remove-all can never remove the lock of a command this agent has just started).
_startup_lock = threading.Condition()
_startup_pending: set[str] = set()


def repositories(settings: Settings) -> list[Path]:
    """Every initialised per-instance repository under the backup root."""
    base = settings.backups_dir / "restic"
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    return [p for p in children if p.is_dir() and not p.is_symlink() and (p / "config").exists()]


def prepare_startup_unlock(settings: Settings) -> list[Path]:
    """Mark every repository as waiting for its start-up unlock (call before serving requests)."""
    repos = repositories(settings) if shutil.which(settings.restic_binary) else []
    with _startup_lock:
        _startup_pending.update(str(p) for p in repos)
    return repos


def _startup_unlock_done(repo: Path) -> None:
    with _startup_lock:
        _startup_pending.discard(str(repo))
        _startup_lock.notify_all()


def startup_unlock(settings: Settings, repos: Iterable[Path], workers: int = 4) -> dict[str, str]:
    """``restic unlock --remove-all`` on each repository (the agent just started, so no restic of
    it is running: every lock in there is left over). Bounded in time, logged; never raises.
    Returns {repository: "ok" | why not}."""
    results: dict[str, str] = {}

    def one(repo: Path) -> None:
        try:
            r = Restic(settings, UUID(repo.name))
            proc = r._exec(["unlock", "--remove-all"], timeout=UNLOCK_TIMEOUT)
            if proc.returncode == 0:
                results[str(repo)] = "ok"
                removed = (proc.stdout + proc.stderr).strip()
                log.info(
                    "restic unlock --remove-all %s: %s", repo, removed.splitlines()[-1] if removed else "ok"
                )
            else:
                tail = (proc.stderr.strip().splitlines() or [f"exit code {proc.returncode}"])[-1]
                results[str(repo)] = tail
                log.warning("restic unlock --remove-all %s failed: %s", repo, tail)
        except Exception as exc:  # a bad folder name, restic missing, a timeout
            results[str(repo)] = str(exc)
            log.warning("restic unlock --remove-all %s failed: %s", repo, exc)
        finally:
            _startup_unlock_done(repo)

    repos = list(repos)
    if repos:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="restic-unlock") as pool:
            list(pool.map(one, repos))
    return results


# Always left out of backups: the agent's temporary files and folders inside a server folder
# (uploads in progress, staging folders of world imports and restores, imported archives).
FIXED_EXCLUDES = (".mcpanel-tmp-*", ".mcpanel-import-*", ".mcpanel-world-*")


# entries one backup-browser listing shows (B-6); restic's output is read line by line
LS_MAX_ENTRIES = 20_000


class DumpStream:
    """A running ``restic dump``: stdout is read by the caller, stderr drained into a bounded tail."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        self.proc = proc
        self.stdout = proc.stdout
        self._tail: deque[bytes] = deque(maxlen=STDERR_TAIL_LINES)
        self.code: int | None = None  # restic's exit code once finished
        _register(proc)
        err = proc.stderr

        def drain() -> None:
            with contextlib.suppress(OSError, ValueError):
                while line := err.readline(MAX_OUTPUT_LINE):
                    self._tail.append(line[:MAX_STORED_LINE])

        self._drainer = threading.Thread(target=drain, name="restic-dump-stderr", daemon=True)
        self._drainer.start()

    @property
    def stderr(self) -> str:
        return b"".join(self._tail).decode("utf-8", errors="replace")

    def read(self, size: int) -> bytes:
        if self.code is not None:  # finished (and its pipes closed): nothing more to read
            return b""
        return self.stdout.read(size)

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)

    def _close(self) -> None:
        self._drainer.join(timeout=5)
        for pipe in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(OSError):
                pipe.close()  # type: ignore[union-attr]
        _unregister(self.proc)

    def finish(self, timeout: float = 60) -> int:
        """restic's exit code once everything was read (stopped if it does not end in ``timeout``)."""
        if self.code is None:
            try:
                code = self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                code = terminate(self.proc)
            self._close()
            self.code = code
        return self.code

    def abort(self) -> None:
        """The client went away or reading failed: stop restic so that it removes its lock."""
        if self.code is not None:
            return
        with contextlib.suppress(OSError):
            self.proc.stdout.close()  # type: ignore[union-attr]
        self.code = terminate(self.proc)
        self._close()


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
    # Backstops for a hung restic (a backup may run while world saving is paused).
    BACKUP_TIMEOUT = 12 * 3600
    RESTORE_TIMEOUT = 12 * 3600

    def __init__(self, settings: Settings, instance_id: UUID) -> None:
        self.settings = settings
        self.instance_id = instance_id
        self.repo = settings.backups_dir / "restic" / str(instance_id)
        binary = shutil.which(settings.restic_binary)
        if not binary:
            raise AgentError("restic is not installed in the agent image", "restic_missing", 503)
        self.binary = binary

    # -- plumbing -------------------------------------------------------------------
    def _wait_for_startup_unlock(self) -> None:
        key = str(self.repo)
        with _startup_lock:
            if key in _startup_pending:
                _startup_lock.wait_for(lambda: key not in _startup_pending, timeout=UNLOCK_TIMEOUT + 30)

    def _password_file(self) -> Path:
        if self.settings.restic_password_file and self.settings.restic_password_file.exists():
            return self.settings.restic_password_file
        pw = self.settings.resolve_restic_password()
        path = self.settings.state_dir / "restic-password"
        if not path.exists() or path.read_text().strip() != pw:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_private(path, pw)
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

    def _exec(
        self,
        args: list[str],
        *,
        timeout: float | None = 3600,
        cwd: Path | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        """Run restic and collect its output. After ``timeout`` it is stopped (SIGINT first, so
        it removes its lock) and the answer is 504 ``restic_timeout``."""
        proc = subprocess.Popen(
            [self.binary, *args],
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            cwd=cwd,
            pass_fds=pass_fds,
        )
        _register(proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                stopper = threading.Thread(target=terminate, args=(proc,), daemon=True)
                stopper.start()
                proc.communicate()  # drains the pipes while restic shuts down
                stopper.join()
                raise AgentError(
                    f"restic {args[0]} did not finish within {int(timeout or 0)} seconds and was stopped",
                    "restic_timeout",
                    504,
                ) from None
            except BaseException:
                terminate(proc)
                raise
        finally:
            _unregister(proc)
        return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)

    def unlock_stale(self) -> None:
        """``restic unlock``: removes stale locks only (never raises)."""
        try:
            proc = self._exec(["unlock"], timeout=UNLOCK_TIMEOUT)
        except Exception as exc:
            log.warning("restic unlock of %s failed: %s", self.repo, exc)
            return
        log.warning(
            "Repository %s was locked; restic unlock (stale locks only) exited with %s",
            self.repo,
            proc.returncode,
        )

    def run(
        self,
        args: list[str],
        *,
        timeout: float | None = 3600,
        cwd: Path | None = None,
        pass_fds: tuple[int, ...] = (),
        ok_codes: tuple[int, ...] = (0, 3),  # 3 = backup completed but some files unreadable
    ) -> subprocess.CompletedProcess[str]:
        """Run a restic command; a locked repository (exit 11) is unlocked (stale locks) and the
        command tried once more."""
        self._wait_for_startup_unlock()
        proc = self._exec(args, timeout=timeout, cwd=cwd, pass_fds=pass_fds)
        if proc.returncode == EXIT_LOCKED:
            self.unlock_stale()
            proc = self._exec(args, timeout=timeout, cwd=cwd, pass_fds=pass_fds)
        if proc.returncode not in ok_codes:
            self._raise_for(proc, proc.stderr, args[0])
        return proc

    def initialized(self) -> bool:
        return (self.repo / "config").exists()

    def ensure_init(self) -> None:
        if self.initialized():
            return
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        self.run(["init"], timeout=120)

    def _stream_json(
        self,
        args: list[str],
        on_message: Callable[[dict[str, Any]], None],
        *,
        timeout: float,
        action: str,
        pass_fds: tuple[int, ...] = (),
    ) -> tuple[int, str]:
        """Run restic with ``--json`` output, handing each JSON line of stdout to ``on_message``.

        stderr is drained concurrently into a bounded tail (restic blocks once a pipe it
        writes to is full, so reading one pipe to EOF and then the other can deadlock; a
        mod can make restic print an error per file). The process is stopped (SIGINT, then
        SIGTERM, then SIGKILL: see :func:`terminate`) after ``timeout`` seconds or if
        ``on_message`` raises. Returns (exit code, stderr tail).
        """
        self._wait_for_startup_unlock()
        proc = subprocess.Popen(
            args,
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            pass_fds=pass_fds,
        )
        assert proc.stdout is not None and proc.stderr is not None
        _register(proc)
        stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        stderr_pipe = proc.stderr

        def drain_stderr() -> None:
            with contextlib.suppress(OSError, ValueError):
                while line := stderr_pipe.readline(MAX_OUTPUT_LINE):
                    stderr_tail.append(line[:MAX_STORED_LINE])

        timed_out = threading.Event()

        def kill_on_timeout() -> None:
            timed_out.set()
            terminate(proc)

        drainer = threading.Thread(target=drain_stderr, name="restic-stderr", daemon=True)
        drainer.start()
        timer = threading.Timer(timeout, kill_on_timeout)
        timer.daemon = True
        timer.start()
        try:
            while line := proc.stdout.readline(MAX_OUTPUT_LINE):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict):
                    on_message(msg)
        except BaseException:
            terminate(proc)
            raise
        finally:
            timer.cancel()
            try:
                code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                code = terminate(proc)
            drainer.join(timeout=10)
            proc.stdout.close()
            stderr_pipe.close()
            _unregister(proc)
        if timed_out.is_set():
            raise AgentError(
                f"restic {action} did not finish within {int(timeout)} seconds and was stopped",
                "restic_timeout",
                504,
            )
        return code, "".join(stderr_tail)

    def _stream_json_retrying(
        self, args: list[str], on_message: Callable[[dict[str, Any]], None], **kwargs: Any
    ) -> tuple[int, str]:
        """:meth:`_stream_json`; a locked repository (exit 11) is unlocked and the command tried once more."""
        code, stderr = self._stream_json(args, on_message, **kwargs)
        if code == EXIT_LOCKED:
            self.unlock_stale()
            code, stderr = self._stream_json(args, on_message, **kwargs)
        return code, stderr

    # -- operations ------------------------------------------------------------------
    def backup(
        self,
        source: Path,
        tags: list[str],
        excludes: list[str],
        progress: Callable[[float, str], None] | None = None,
    ) -> dict[str, Any]:
        self.ensure_init()
        args = [
            self.binary,
            "backup",
            "--json",
            "--retry-lock",
            RETRY_LOCK,
            "--host",
            "mcpanel",
            "--one-file-system",
        ]
        for t in tags:
            args += ["--tag", t]
        for ex in excludes:
            args += ["--exclude", ex]
        for ex in FIXED_EXCLUDES:
            args += ["--exclude", ex]
        args.append(str(source))
        summary: dict[str, Any] = {}

        def on_message(msg: dict[str, Any]) -> None:
            nonlocal summary
            if msg.get("message_type") == "status" and progress:
                pct = float(msg.get("percent_done") or 0.0)
                files = msg.get("files_done", 0)
                total = msg.get("total_files", 0)
                progress(pct, f"Backing up files ({files}/{total})")
            elif msg.get("message_type") == "summary":
                summary = msg

        code, stderr = self._stream_json_retrying(
            args, on_message, timeout=self.BACKUP_TIMEOUT, action="backup"
        )
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
        self,
        snapshot_id: str,
        target: Path,
        progress: Callable[[float, str], None] | None = None,
        *,
        pass_fds: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """Restore a whole snapshot into ``target`` (an empty or missing directory).

        Like ``restore_path``, ``target`` may be ``/proc/self/fd/<n>/...`` with ``n`` in ``pass_fds``.
        """
        snap = self.snapshot(snapshot_id)
        paths = snap.get("paths") or []
        if len(paths) != 1:
            raise AgentError("Snapshot has an unexpected layout", "snapshot_layout", 500)
        # ``target`` may already exist (e.g. a freshly created btrfs subvolume) but must be empty
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir() or target.is_symlink() or any(target.iterdir()):
            raise AgentError("The restore target is not an empty directory", "target_not_empty", 409)
        args = [self.binary, "restore", f"{snap['id']}:{paths[0]}", "--target", str(target), "--json"]
        summary: dict[str, Any] = {}

        def on_message(msg: dict[str, Any]) -> None:
            nonlocal summary
            if msg.get("message_type") == "status" and progress:
                progress(float(msg.get("percent_done") or 0.0), "Restoring files")
            elif msg.get("message_type") == "summary":
                summary = msg

        code, stderr = self._stream_json_retrying(
            args, on_message, timeout=self.RESTORE_TIMEOUT, action="restore", pass_fds=pass_fds
        )
        if code != 0:
            self._raise_for(code, stderr, "restore")
        return {"snapshot": snap["id"], **summary}

    def forget(self, snapshot_ids: list[str], prune: bool = True) -> dict[str, Any]:
        if not snapshot_ids:
            return {"removed": []}
        args = ["forget", "--retry-lock", RETRY_LOCK, *snapshot_ids]
        if prune:
            args.append("--prune")
        self.run(args, timeout=3600)
        return {"removed": snapshot_ids}

    def check(self, read_data_subset: str | None = None) -> dict[str, Any]:
        if not self.initialized():
            raise AgentError("No backup repository exists for this instance yet", "repo_missing", 404)
        args = ["check", "--retry-lock", RETRY_LOCK]
        if read_data_subset:
            args += ["--read-data-subset", read_data_subset]
        # Any exit code is a result ("ok" false) except a lock that stays after the unlock and
        # retry (409 repo_locked) and a wrong password.
        proc = self.run(args, timeout=7200, ok_codes=tuple(c for c in range(256) if c not in (11, 12)))
        ok = proc.returncode == 0
        output = (proc.stdout + proc.stderr).strip().splitlines()[-40:]
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

    def _ls_lines(self, args: list[str], *, timeout: float = 600) -> Iterator[str]:
        """The output lines of ``restic <args>`` as restic prints them, never all held at once (B-6). A
        caller that stops early ends restic. Errors as :meth:`run` (a locked repository: unlocked and
        tried once more)."""
        for attempt in range(2):
            self._wait_for_startup_unlock()
            proc = subprocess.Popen(
                [self.binary, *args],
                env=self._env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )
            _register(proc)
            err: deque[str] = deque(maxlen=200)
            drain = threading.Thread(target=err.extend, args=(proc.stderr,), daemon=True)
            drain.start()
            timer = threading.Timer(timeout, terminate, args=(proc,))
            timer.daemon = True
            timer.start()
            finished = False
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    yield line.rstrip("\n")
                proc.wait()
                finished = True
            finally:
                timer.cancel()
                if not finished:
                    terminate(proc)  # the caller stopped early (or failed): restic ends
                drain.join(5)
                _unregister(proc)
            if proc.returncode == EXIT_LOCKED and attempt == 0:
                self.unlock_stale()
                continue
            if proc.returncode not in (0, 3):
                self._raise_for(proc, "".join(err), args[0])
            return

    def ls(self, snapshot_id: str, rel_parts: list[str]) -> list[dict[str, Any]]:
        return self.ls_bounded(snapshot_id, rel_parts)[0]

    def ls_bounded(
        self, snapshot_id: str, rel_parts: list[str], limit: int = LS_MAX_ENTRIES
    ) -> tuple[list[dict[str, Any]], bool]:
        """Direct children of a directory inside a snapshot (paths relative to the server folder), at most
        ``limit`` (B-6), and whether there were more."""
        snap = self.snapshot(snapshot_id)
        base = self._root_path(snap)
        target = "/".join([base, *rel_parts])
        out = []
        truncated = False
        for line in self._ls_lines(["ls", "--json", snap["id"], target]):
            if len(out) >= limit:
                truncated = True
                break
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
        return out, truncated

    def node_type(self, snapshot_id: str, rel_parts: list[str]) -> str:
        """``file``, ``directory``, ``symlink``, ... of one path in a snapshot (404 when it is not there)."""
        snap = self.snapshot(snapshot_id)
        target = "/".join([self._root_path(snap), *rel_parts])
        for line in self._ls_lines(["ls", "--json", snap["id"], target]):  # stops at the node (B-6)
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(node, dict)
                and node.get("path") == target
                and node.get("struct_type") != "snapshot"
            ):
                kind = node.get("type")
                return {"dir": "directory"}.get(kind, kind or "other")
        raise AgentError("That path does not exist in the backup", "not_found", 404)

    def dump(self, snapshot_id: str, rel_parts: list[str]) -> DumpStream:
        """Start streaming a single file from a snapshot (the caller reads it, see :class:`DumpStream`)."""
        snap = self.snapshot(snapshot_id)
        target = "/".join([self._root_path(snap), *rel_parts])
        self._wait_for_startup_unlock()
        proc = subprocess.Popen(
            [self.binary, "dump", snap["id"], target],
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # drained into a bounded tail by DumpStream
        )
        return DumpStream(proc)

    def open_dump(self, snapshot_id: str, rel_parts: list[str], chunk: int) -> tuple[DumpStream, bytes]:
        """A dump of one regular file whose start is known to have worked: (stream, first chunk).

        A folder is refused (restic would send a tar under the folder's name). When restic fails
        before sending anything (a lock, a missing path, a read error) the error is raised here,
        so the caller can answer with a proper status instead of an empty download. A locked
        repository is unlocked (stale locks) and the dump tried once more.
        """
        kind = self.node_type(snapshot_id, rel_parts)
        if kind != "file":
            raise AgentError("Only files can be downloaded from a backup", "not_a_file", 400)
        for attempt in range(2):
            stream = self.dump(snapshot_id, rel_parts)
            try:
                first = stream.read(chunk)
            except BaseException:
                stream.abort()
                raise
            if first:
                return stream, first
            code = stream.finish()
            if code == 0:
                return stream, b""  # an empty file
            if code == EXIT_LOCKED and attempt == 0:
                self.unlock_stale()
                continue
            self._raise_for(code, stream.stderr, "dump")
        raise AssertionError("unreachable")  # pragma: no cover

    def restore_path(
        self, snapshot_id: str, rel_parts: list[str], staging: Path, *, pass_fds: tuple[int, ...] = ()
    ) -> Path:
        """Restore one file/directory into ``staging``; returns the restored item path.

        ``staging`` may be ``/proc/self/fd/<n>/...`` with ``n`` in ``pass_fds``: restic then
        writes into the directory that descriptor refers to, whatever its name is meanwhile.
        """
        if not rel_parts:
            raise AgentError("Choose a file or folder to restore", "invalid_path", 400)
        snap = self.snapshot(snapshot_id)
        parent = "/".join([self._root_path(snap), *rel_parts[:-1]])
        staging.mkdir(parents=True, exist_ok=False)
        # the name is a pattern for restic: [ ] * ? and \\ are escaped so it matches only itself
        include = f"/{glob_escape(rel_parts[-1])}"
        self.run(
            ["restore", f"{snap['id']}:{parent}", "--target", str(staging), "--include", include],
            timeout=6 * 3600,
            pass_fds=pass_fds,
        )
        restored = staging / rel_parts[-1]
        if not restored.exists() and not restored.is_symlink():
            raise AgentError("That path does not exist in the backup", "not_found", 404)
        return restored

    def unlock(self) -> None:
        if self.initialized():
            self.run(["unlock"], timeout=UNLOCK_TIMEOUT)

    def destroy(self) -> None:
        if self.repo.exists():
            shutil.rmtree(self.repo)
