"""Native filesystem snapshots for instance directories: btrfs here, ZFS in ``zfs.py``.

``snapshot_provider`` picks the provider from the filesystem of the instances directory; both
have the same interface (capability, require_ready, convert, create, list, delete, restore,
destroy_all). btrfs design:

* On btrfs, each instance directory is a **subvolume** (new instances are created as
  subvolumes; existing plain directories can be converted while the server is stopped,
  using reflink copies so no extra space is used up front).
* Snapshots are read-only subvolumes under ``<data root>/snapshots/<instance-id>/<name>``
  (same filesystem, required by btrfs), with a small JSON sidecar for labels.
* Restore creates a writable snapshot of the chosen snapshot next to the instance, swaps
  it in with renames and deletes the old subvolume only after the swap succeeded. Once the
  swap is done the rollback is committed: what cannot be tidied up afterwards is a warning.
* Deleting a subvolume or snapshot goes through :func:`delete_subvolume`: ``btrfs subvolume
  delete`` where the kernel allows it (CAP_SYS_ADMIN; on a pool mounted with
  ``user_subvol_rm_allowed`` once a snapshot's read-only flag is cleared), otherwise file by
  file (clear the read-only flag, remove the contents, ``rmdir`` the empty subvolume), which
  root in a default container may do.
  Whether either works is measured once per process by :func:`delete_support`; snapshots are
  only offered where the agent can delete them again.

Restic restores and clones replace a whole instance folder too; ``prepare_replacement`` decides
how, so that a subvolume stays a subvolume and a dataset stays a dataset (see there).

Every command is an argv list built from validated values (UUIDs and generated names).
Snapshots are not backups: they live on the same disk. Use restic backups for disaster
recovery.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from . import storage
from .config import Settings
from .docker_ops import AgentError

NAME_RE = re.compile(r"^\d{8}T\d{6}Z(-[a-z0-9-]{1,40})?$")
log = logging.getLogger(__name__)

# How a subvolume or snapshot gets deleted (see delete_subvolume).
DELETE_NATIVE = "native"  # `btrfs subvolume delete`
DELETE_FALLBACK = "fallback"  # made writable, emptied file by file, rmdir
DOCS = "see docs/UNRAID.md, section 6"
FALLBACK_NOTE = (
    "The agent has no CAP_SYS_ADMIN, so it removes snapshots and replaced server folders file by file "
    "instead of with 'btrfs subvolume delete'. Everything works; deleting is slower for large servers "
    f"({DOCS})."
)
# A snapshot that is being removed file by file is renamed to this prefix first, so that a
# half-emptied snapshot is never listed or offered for a rollback.
DELETING_PREFIX = ".deleting-"
PROBE_DIR = ".mcpanel-probe"  # in the data root, only while delete_support() measures
PROBE_TIMEOUT = 120
PROBE_RETRY_SECONDS = 60.0  # a failed probe is repeated after this long: its cause may be gone


def _btrfs() -> str:
    binary = shutil.which("btrfs")
    if not binary:
        raise AgentError("btrfs-progs is not installed in the agent image", "snapshots_unavailable", 503)
    return binary


def _try(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run a command and return it whatever its exit status; a timeout counts as a failure."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"no answer after {timeout} s")


def _message(proc: subprocess.CompletedProcess[str]) -> str:
    """The line of a failed btrfs command that says why (it prints hints after its ERROR line)."""
    lines = [line.strip() for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR")]
    return (errors or lines or [f"exit status {proc.returncode}"])[-1]


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    proc = _try(args, timeout)
    if proc.returncode != 0:
        raise AgentError(f"btrfs failed: {_message(proc)}", "btrfs_failed", 500)
    return proc


def is_subvolume(path: Path) -> bool:
    # On btrfs, subvolume roots always have inode number 256.
    try:
        return path.stat().st_ino == 256 and (storage.mount_for(path) or {}).get("fstype") == "btrfs"
    except FileNotFoundError:
        return False


def slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]


# -- deleting subvolumes and snapshots ----------------------------------------------------
def _native_delete(path: Path) -> str | None:
    """``btrfs subvolume delete``. Returns None if it worked, else why it did not.

    The kernel refuses it (EPERM) to a process without CAP_SYS_ADMIN: the default agent container.
    On a filesystem mounted with ``user_subvol_rm_allowed`` the owner may use it too, but only on a
    writable subvolume (a read-only snapshot gets EROFS until its read-only flag is cleared).
    """
    try:
        proc = _try([_btrfs(), "subvolume", "delete", str(path)])
    except AgentError as exc:  # no btrfs binary
        return str(exc)
    return None if proc.returncode == 0 else _message(proc)


def _make_writable(path: Path) -> None:
    """Clear the read-only flag of a snapshot. Its owner (or CAP_FOWNER) may; no CAP_SYS_ADMIN needed."""
    _run([_btrfs(), "property", "set", "-ts", str(path), "ro", "false"], timeout=120)


def _remove_files(path: Path) -> None:
    """Remove a directory tree file by file, a writable subvolume included.

    The last step is an ``rmdir`` of the emptied subvolume, which needs no CAP_SYS_ADMIN (Linux
    4.18+). Never follows links (``shutil.rmtree`` works on directory descriptors). Goes on after an
    entry that cannot be removed, so as much space as possible is freed, and tolerates another
    thread removing the same tree: it is done when the path is gone.
    """
    errors: list[OSError] = []

    def note(_func: Any, _path: Any, exc: BaseException) -> None:
        if isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError):
            errors.append(exc)

    shutil.rmtree(path, onexc=note)
    if os.path.lexists(path):
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise OSError(f"{errors[0]}{more}" if errors else f"{path} is still there")


def delete_subvolume(path: Path) -> str:
    """Delete the btrfs subvolume or snapshot at ``path``; returns how (DELETE_NATIVE or DELETE_FALLBACK).

    Every delete of a subvolume goes through here. ``btrfs subvolume delete`` comes first (one
    ioctl, however large the subvolume is). Where the kernel refuses that, the subvolume is made
    writable and the ioctl tried once more (enough on a ``user_subvol_rm_allowed`` mount); then it
    is emptied file by file and removed with ``rmdir``: slower, but it needs no privilege beyond
    owning the files, so snapshots can be deleted on the default stack. Raises AgentError
    (``btrfs_failed``) with both reasons if the subvolume is still there afterwards.
    """
    refused = _native_delete(path)
    if refused is None:
        return DELETE_NATIVE
    not_writable: Exception | None = None
    try:
        _make_writable(path)
    except (AgentError, OSError) as exc:
        not_writable = exc  # a writable subvolume needs no flag change, so still try
    else:
        if _native_delete(path) is None:
            return DELETE_NATIVE
    try:
        _remove_files(path)
    except OSError as exc:
        raise AgentError(
            f"Could not delete the btrfs subvolume {path}: 'btrfs subvolume delete' was refused ({refused}) "
            f"and removing it file by file failed too ({not_writable or exc})",
            "btrfs_failed",
            500,
        ) from exc
    return DELETE_FALLBACK


def _discard(path: Path) -> str | None:
    """Remove something that is no longer needed: a subvolume, a snapshot, a plain tree or a file.

    Never raises (it runs after swaps that are already committed). Returns None when ``path`` is
    gone, else why it is still there.
    """
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        elif is_subvolume(path):
            delete_subvolume(path)
        else:
            _remove_files(path)
    except Exception as exc:
        return str(exc) or type(exc).__name__
    return "it is still there" if os.path.lexists(path) else None


# -- can this agent delete what it creates? -------------------------------------------------
_probe_lock = threading.Lock()
_probe_cache: dict[str, dict[str, Any]] = {}


def _clear_probe(work: Path) -> str | None:
    """Remove the probe folder with whatever is in it; None when it is gone, else why not."""
    if not os.path.lexists(work):
        return None
    problems = []
    try:
        children = sorted(work.iterdir())
    except OSError as exc:
        children, problems = [], [str(exc)]
    for child in children:
        problem = _discard(child)
        if problem:
            problems.append(problem)
    try:
        work.rmdir()
    except OSError as exc:
        problems.append(str(exc))
    if not os.path.lexists(work):
        return None
    return problems[0]


def _probe(root: Path) -> tuple[str | None, str | None]:
    """(mode, reason): create a small subvolume and a read-only snapshot of it under ``root`` and
    delete both the way real ones are deleted. mode is None if that is not possible here."""
    work = root / PROBE_DIR
    if not root.is_dir():
        return None, f"The data directory {root} does not exist"
    # Left over from a probe that could not delete (or a killed agent). If it still cannot be
    # removed the answer is the same as last time, and nothing new is created.
    problem = _clear_probe(work)
    modes: list[str] = []
    if problem is None:
        src, snap = work / "src", work / "snap"
        try:
            work.mkdir(mode=0o700)
            _run([_btrfs(), "subvolume", "create", str(src)], timeout=PROBE_TIMEOUT)
            (src / "file").write_text("mcpanel\n")
            _run([_btrfs(), "subvolume", "snapshot", "-r", str(src), str(snap)], timeout=PROBE_TIMEOUT)
            modes = [delete_subvolume(snap), delete_subvolume(src)]
        except (AgentError, OSError) as exc:
            problem = str(exc)
        left = _clear_probe(work)
        if left and problem is None:
            problem = left
    if problem is None:
        return (DELETE_FALLBACK if DELETE_FALLBACK in modes else DELETE_NATIVE), None
    reason = (
        f"btrfs snapshots are unavailable: the agent's self-test in {root} failed ({problem}). The agent must "
        "be able to create and delete a subvolume and a read-only snapshot there; give it CAP_SYS_ADMIN "
        f"(cap_add: [SYS_ADMIN]), {DOCS}"
    )
    if os.path.lexists(work):
        reason += f". The test folder {work} could not be removed; delete it on the host"
    return None, reason


def delete_support(root: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Whether and how this process can delete btrfs subvolumes and read-only snapshots under ``root``.

    ``{"mode": "native" | "fallback" | None, "reason": None | str, "checked_at": <unix time>}``.
    Measured with a real subvolume and snapshot in ``<root>/.mcpanel-probe`` (removed again), not
    guessed from capabilities or mount options: what counts is whether the deletes work. The answer
    is kept for the life of the process; ``refresh=True`` measures again, and a failed probe is
    repeated after PROBE_RETRY_SECONDS because its cause (a full disk, a remount) may be gone.
    """
    key = os.path.realpath(root)
    with _probe_lock:
        cached = _probe_cache.get(key)
        fresh = cached is not None and (
            cached["mode"] is not None or time.monotonic() - cached["at"] < PROBE_RETRY_SECONDS
        )
        if cached is None or refresh or not fresh:
            mode, reason = _probe(Path(key))
            cached = {"mode": mode, "reason": reason, "checked_at": time.time(), "at": time.monotonic()}
            _probe_cache[key] = cached
            if mode is None:
                log.warning("%s", reason)
            else:
                log.info("btrfs snapshots under %s: subvolumes are deleted in %s mode", key, mode)
        return {k: cached[k] for k in ("mode", "reason", "checked_at")}


class BtrfsSnapshots:
    provider = "btrfs"

    def __init__(self, settings: Settings, instance_id: UUID) -> None:
        self.settings = settings
        self.instance_id = instance_id
        self.instance_dir = settings.instances_dir / str(instance_id)
        self.snap_dir = settings.data_root / "snapshots" / str(instance_id)

    # -- capability ----------------------------------------------------------------
    def capability(self, *, refresh: bool = False) -> dict[str, Any]:
        """What the panel shows. ``available`` is only true if the agent can also delete snapshots
        (see :func:`delete_support`; ``refresh`` measures that again). ``delete_mode`` says how
        they are deleted, ``note`` what that means for the user."""
        mount = storage.mount_for(self.settings.instances_dir) or {}
        fstype = mount.get("fstype")
        info: dict[str, Any] = {
            "provider": "btrfs",
            "available": False,
            "fstype": fstype,
            "reason": None,
            "instance_is_subvolume": False,
            "instance_ready": False,
            "delete_mode": None,
            "note": None,
        }
        if fstype != "btrfs":
            info["reason"] = (
                f"The data directory is on '{fstype}', which has no native snapshots (btrfs or ZFS needed)"
            )
            return info
        if not shutil.which("btrfs"):
            info["reason"] = "btrfs-progs is not installed in the agent image"
            return info
        info["instance_is_subvolume"] = is_subvolume(self.instance_dir)
        support = delete_support(self.settings.data_root, refresh=refresh)
        if support["mode"] is None:
            # Creating snapshots would work, but they could never be pruned or deleted and a
            # server that has one could not be deleted: not offered.
            info["reason"] = support["reason"]
            return info
        info["available"] = True
        info["delete_mode"] = support["mode"]
        if support["mode"] == DELETE_FALLBACK:
            info["note"] = FALLBACK_NOTE
        info["instance_ready"] = info["instance_is_subvolume"]
        if not info["instance_is_subvolume"]:
            info["reason"] = (
                "This server's folder is a plain directory; convert it (server stopped) to enable snapshots"
            )
        return info

    def require_ready(self) -> None:
        cap = self.capability()
        if not cap["available"]:
            raise AgentError(cap["reason"] or "Snapshots unavailable", "snapshots_unavailable", 409)
        if not cap["instance_is_subvolume"]:
            raise AgentError(cap["reason"], "not_subvolume", 409)

    # -- conversion / creation --------------------------------------------------------
    @staticmethod
    def create_subvolume(path: Path) -> None:
        _run([_btrfs(), "subvolume", "create", str(path)])

    def convert(self) -> dict[str, Any]:
        """Turn the existing instance directory into a subvolume (reflink copy + swap)."""
        if is_subvolume(self.instance_dir):
            return {"converted": False, "reason": "already a subvolume"}
        if not self.instance_dir.exists():
            raise AgentError("Instance directory does not exist", "not_found", 404)
        cap = self.capability()
        if not cap["available"]:
            raise AgentError(cap["reason"] or "Snapshots unavailable", "snapshots_unavailable", 409)
        tmp = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-convert")
        if os.path.lexists(tmp):
            # Left by a conversion that was interrupted (the agent stopped during the copy). The
            # server folder is still here, so it is only a partial copy of it, and no other
            # operation of this server can be using it (the caller holds the server's lock).
            problem = _discard(tmp)
            if problem:
                raise AgentError(
                    f"A previous conversion was interrupted and its copy {tmp} could not be removed "
                    f"({problem}); delete it on the host",
                    "convert_conflict",
                    409,
                )
            log.warning("Removed %s, left by an interrupted conversion", tmp)
        self.create_subvolume(tmp)
        old = self.settings.trash_dir / f"{self.instance_id}-preconvert-{int(time.time())}"
        moved = False
        try:
            # cp -a preserves ownership/permissions/symlinks (never dereferences); --reflink shares extents.
            proc = subprocess.run(
                ["cp", "-a", "--reflink=auto", f"{self.instance_dir}/.", str(tmp)],
                capture_output=True,
                text=True,
                timeout=6 * 3600,
                check=False,
            )
            if proc.returncode != 0:
                raise AgentError(f"Copy failed: {proc.stderr.strip()[-300:]}", "convert_failed", 500)
            old.parent.mkdir(parents=True, exist_ok=True)
            self.instance_dir.rename(old)
            moved = True
            tmp.rename(self.instance_dir)
        except BaseException:
            if moved and not self.instance_dir.exists():
                old.rename(self.instance_dir)  # put the original back
            remove_tree(tmp)  # the half-made copy; never hides the error that got us here
            raise
        result: dict[str, Any] = {"converted": True}
        problem = _discard(old)
        if problem:
            log.warning("The folder before the conversion was not removed: %s (%s)", old, problem)
            result["warnings"] = [
                f"The server folder as it was before the conversion could not be removed completely "
                f"({problem}); the rest is in {old}. It still uses disk space; delete it on the host"
            ]
            result["leftovers"] = [str(old)]
        return result

    # -- snapshots ---------------------------------------------------------------------
    def _meta_path(self, name: str) -> Path:
        return self.snap_dir / f"{name}.json"

    def _validate(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise AgentError("Invalid snapshot name", "invalid_snapshot", 400)
        path = self.snap_dir / name
        if not path.exists():
            raise AgentError("Snapshot not found", "snapshot_not_found", 404)
        return path

    def create(self, label: str = "", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require_ready()
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        slug = slugify(label)
        name = f"{stamp}-{slug}" if slug else stamp
        target = self.snap_dir / name
        if target.exists():
            raise AgentError(
                "A snapshot with this timestamp already exists; retry in a second", "snapshot_exists", 409
            )
        _run([_btrfs(), "subvolume", "snapshot", "-r", str(self.instance_dir), str(target)])
        info = {
            "name": name,
            "label": label[:120],
            "created_at": datetime.now(UTC).isoformat(),
            **(meta or {}),
        }
        self._meta_path(name).write_text(json.dumps(info))
        return info

    def list(self) -> list[dict[str, Any]]:
        if not self.snap_dir.exists():
            return []
        out = []
        for entry in sorted(self.snap_dir.iterdir(), reverse=True):
            if entry.is_dir() and NAME_RE.match(entry.name):
                meta: dict[str, Any] = {"name": entry.name, "label": "", "created_at": None}
                try:
                    meta.update(json.loads(self._meta_path(entry.name).read_text()))
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                out.append(meta)
        return out

    def delete(self, name: str) -> None:
        """Delete one snapshot (also what the panel's scheduled pruning calls)."""
        self._delete_snapshot(name, self._validate(name))
        # What an interrupted file-by-file delete left behind (e.g. the agent was restarted).
        for leftover in sorted(self.snap_dir.glob(f"{DELETING_PREFIX}*")):
            problem = _discard(leftover)
            if problem:
                log.warning("Could not remove the rest of a deleted snapshot, %s: %s", leftover, problem)

    def _delete_snapshot(self, name: str, path: Path) -> None:
        refused = _native_delete(path)
        if refused is not None:
            self._delete_unprivileged(name, path, refused)
        self._meta_path(name).unlink(missing_ok=True)

    def _delete_unprivileged(self, name: str, path: Path, refused: str) -> None:
        """Delete a snapshot the kernel would not delete as it is (see :func:`delete_subvolume`).

        It leaves the list first: emptying takes a while for a large server, and a snapshot that
        is writable or half-emptied must never be offered for a rollback. Until it has been made
        writable nothing is lost, so a refusal up to there puts the snapshot back.
        """
        doomed = self.snap_dir / f"{DELETING_PREFIX}{name}"
        try:
            path.rename(doomed)
            try:
                _make_writable(doomed)
            except BaseException:
                doomed.rename(path)  # untouched and still read-only: keep it listed
                raise
        except (AgentError, OSError) as exc:
            raise AgentError(
                f"Could not delete snapshot {name}: 'btrfs subvolume delete' was refused ({refused}) and the "
                f"snapshot could not be made writable to remove it file by file ({exc}); {DOCS}",
                "btrfs_failed",
                500,
            ) from exc
        self._meta_path(name).unlink(missing_ok=True)
        if _native_delete(doomed) is None:  # a user_subvol_rm_allowed mount: writable was all it took
            return
        try:
            _remove_files(doomed)
        except OSError as exc:
            raise AgentError(
                f"Snapshot {name} is deleted, but some of its files could not be removed ({exc}). They are in "
                f"{doomed}; the agent tries again with the next snapshot it deletes and when the server is deleted",
                "btrfs_failed",
                500,
            ) from exc

    def _stale_siblings(self) -> list[Path]:
        """Folders next to the server folder that a rollback or a restore left behind: the previous
        server folder (``.mcpanel-old-<time>``, kept only until the swap is done) and the copy an
        interrupted rollback was preparing (``.mcpanel-rollback*``). Never the live files."""
        parent = self.instance_dir.parent
        found: list[Path] = []
        for pattern in (f"{self.instance_id}.mcpanel-old-*", f"{self.instance_id}.mcpanel-rollback*"):
            try:
                found += parent.glob(pattern)
            except OSError:
                continue
        return sorted(found)

    def _tidy_siblings(self) -> tuple[list[str], list[str]]:
        """Remove :meth:`_stale_siblings`. Never raises; returns (warnings, paths that are still there)."""
        warnings: list[str] = []
        leftovers: list[str] = []
        for path in self._stale_siblings():
            problem = _discard(path)
            if problem:
                what = (
                    "the previous server folder"
                    if ".mcpanel-old-" in path.name
                    else "a temporary rollback copy"
                )
                warnings.append(
                    f"Could not remove {what} {path}: {problem}. It still uses disk space; the agent tries "
                    "again at the next rollback and when the server is deleted"
                )
                leftovers.append(str(path))
        return warnings, leftovers

    def restore(self, name: str) -> dict[str, Any]:
        self.require_ready()
        snap = self._validate(name)
        # Its own name every time: the copy an interrupted rollback left behind cannot block this one.
        tmp = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-rollback-{secrets.token_hex(4)}")
        _run([_btrfs(), "subvolume", "snapshot", str(snap), str(tmp)])  # writable copy
        stamp = int(time.time())
        old = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-old-{stamp}")
        while os.path.lexists(old):  # left by an earlier rollback in the same second
            stamp += 1
            old = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-old-{stamp}")
        try:
            self.instance_dir.rename(old)
            try:
                tmp.rename(self.instance_dir)
            except BaseException as exc:
                try:
                    old.rename(self.instance_dir)  # put the current files back
                except OSError as undo:
                    raise AgentError(
                        f"Rollback failed ({exc}) and the server folder could not be put back ({undo}). "
                        f"Its files are in {old}; rename that folder back on the host before starting the server",
                        "rollback_failed",
                        500,
                    ) from exc
                raise
        except BaseException:
            remove_tree(tmp)
            raise
        # The rollback is done: the server folder holds the snapshot's files. The rest only tidies
        # up (the previous folder, what earlier rollbacks left); it never undoes it and never raises.
        warnings, leftovers = self._tidy_siblings()
        for warning in warnings:
            log.warning("Rollback of %s to %s: %s", self.instance_id, name, warning)
        return {"restored": name, "warnings": warnings, "leftovers": leftovers}

    def destroy_all(self) -> dict[str, Any]:
        """Delete every snapshot of the instance, its snapshot folder and what rollbacks left next to
        the server folder (the server is being deleted).

        Snapshots are read-only subvolumes, which a plain recursive delete cannot remove; they go
        through :func:`delete_subvolume`. Never raises for something that cannot be removed: the
        caller has already removed the container and deletes the server's files next, so a refusal
        here must not leave a server that can neither run nor be deleted. What is left is named in
        ``warnings`` and ``leftovers``.
        """
        destroyed: list[str] = []
        warnings: list[str] = []
        leftovers: list[str] = []
        if self.snap_dir.is_dir() and not self.snap_dir.is_symlink():
            try:
                entries = sorted(self.snap_dir.iterdir())
            except OSError as exc:
                entries = []
                warnings.append(f"Could not read the snapshot folder {self.snap_dir}: {exc}")
            for entry in entries:
                is_snapshot = entry.is_dir() and not entry.is_symlink() and bool(NAME_RE.match(entry.name))
                left = entry
                if is_snapshot and is_subvolume(entry):
                    # Like a single delete: if this stops half-way and the server is not deleted
                    # after all, a half-emptied snapshot must not be listed.
                    try:
                        self._delete_snapshot(entry.name, entry)
                        problem = None
                    except Exception as exc:
                        problem = str(exc)
                        hidden = self.snap_dir / f"{DELETING_PREFIX}{entry.name}"
                        left = hidden if os.path.lexists(hidden) else entry
                elif entry.suffix == ".json" and os.path.lexists(entry.with_suffix("")):
                    continue  # the label of a snapshot that could not be deleted (it sorts after it)
                else:
                    problem = _discard(entry)  # half-deleted snapshots, label sidecars
                if problem:
                    leftovers.append(str(left))
                    warnings.append(
                        f"Could not delete {'snapshot' if is_snapshot else 'snapshot data'} {left}: {problem}. "
                        "It still uses disk space; delete it on the host (btrfs subvolume delete)"
                    )
                elif is_snapshot:
                    destroyed.append(entry.name)
            try:
                self.snap_dir.rmdir()
            except OSError as exc:
                if not leftovers:
                    leftovers.append(str(self.snap_dir))
                    warnings.append(f"Could not remove the snapshot folder {self.snap_dir}: {exc}")
        more_warnings, more_leftovers = self._tidy_siblings()
        return {
            "destroyed": destroyed,
            "snapshots": len(destroyed),
            "warnings": warnings + more_warnings,
            "leftovers": leftovers + more_leftovers,
        }


def snapshot_provider(settings: Settings, instance_id: UUID) -> Any:
    """BtrfsSnapshots or ZfsSnapshots, by the filesystem of the instances directory."""
    if (storage.mount_for(settings.instances_dir) or {}).get("fstype") == "zfs":
        from .zfs import ZfsSnapshots

        return ZfsSnapshots(settings, instance_id)
    return BtrfsSnapshots(settings, instance_id)


def is_mount_point(path: Path) -> bool:
    """True if something is mounted exactly at ``path`` (e.g. the server's own ZFS dataset)."""
    if not path.is_dir() or path.is_symlink():
        return False
    return (storage.mount_for(path) or {}).get("mount_point") == os.path.realpath(path)


def is_volume_root(path: Path) -> bool:
    """True if ``path`` is a btrfs subvolume or a mount point (a ZFS dataset).

    Such a directory must stay what it is: renaming a mount point fails (EBUSY), and swapping a
    plain directory in for a subvolume would silently turn snapshots off for that server.
    """
    if not path.is_dir() or path.is_symlink():
        return False
    return is_subvolume(path) or is_mount_point(path)


def _subvolumes_possible(parent: Path) -> bool:
    return (storage.mount_for(parent) or {}).get("fstype") == "btrfs" and shutil.which("btrfs") is not None


def _datasets_possible(parent: Path) -> bool:
    return (storage.mount_for(parent) or {}).get("fstype") == "zfs"


def ensure_instance_dir(settings: Settings, path: Path) -> bool:
    """Create an instance directory: a subvolume on btrfs, its own dataset on ZFS (when the
    tools and privileges are there), a plain directory otherwise.

    Returns True if a subvolume or dataset was created.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if _subvolumes_possible(path.parent):
        try:
            BtrfsSnapshots.create_subvolume(path)
            return True
        except AgentError:
            pass  # fall back to a plain directory (e.g. missing privileges)
    elif _datasets_possible(path.parent):
        from .zfs import create_instance_dataset

        if create_instance_dataset(settings, path):
            return True
    path.mkdir(parents=True, exist_ok=True)
    return False


def replacement_wants_subvolume(path: Path) -> bool:
    """Whether a directory that will replace ``path`` (e.g. a restore) should be a subvolume.

    True if ``path`` is a subvolume (replacing it with a plain directory would silently
    disable snapshots), or if it does not exist and a new instance directory there would be
    created as one (see :func:`ensure_instance_dir`).
    """
    if path.exists() or path.is_symlink():
        return is_subvolume(path)
    return _subvolumes_possible(path.parent)


def prepare_replacement(settings: Settings, path: Path) -> str:
    """How a restic restore or a clone puts a new tree at the instance folder ``path``.

    * ``"in_place"``: ``path`` is a mount point (the server's own ZFS dataset). A mount point
      cannot be renamed, so the tree is built in a hidden folder inside it and swapped in entry
      by entry (:func:`swap_entries`); the dataset and its snapshots stay.
    * ``"subvolume"``: ``path`` is a btrfs subvolume, or would be created as one. The tree is
      built in a new subvolume next to it (outside the server's folder, so out of reach of the
      file manager) and renamed into place; the old one is deleted after the swap.
    * ``"plain"``: built in ``<data root>/staging`` and renamed into place.

    A missing ``path`` on ZFS is created first as the server's own dataset (where the agent can),
    so it is never filled as a plain folder that snapshots cannot cover.
    """
    if not path.exists() and not path.is_symlink() and _datasets_possible(path.parent):
        ensure_instance_dir(settings, path)
    if is_mount_point(path):
        return "in_place"
    if replacement_wants_subvolume(path):
        return "subvolume"
    return "plain"


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
ZFS_CONTROL_DIR = ".zfs"  # visible in a dataset's root with snapdir=visible; never moved


def swap_entries(target_fd: int, source_fd: int, *, keep: tuple[str, ...] = ()) -> None:
    """Replace the entries of directory ``target_fd`` with those of ``source_fd``.

    Used for a volume root that cannot be replaced by renaming the folder itself. Both are open
    directories on the same filesystem (``source_fd`` normally lies inside the target): every
    rename is between two directory descriptors, so no path is resolved and a folder renamed or
    replaced meanwhile cannot redirect it. ``keep`` (the staging folder), the folder holding the
    old entries and ZFS's ``.zfs`` stay where they are. If anything fails, everything moved so far
    is moved back. The old entries are deleted once the new ones are in place.
    """
    old = f".mcpanel-tmp-old-{secrets.token_hex(6)}"  # .mcpanel-tmp-*: excluded from backups
    os.mkdir(old, 0o700, dir_fd=target_fd)
    old_fd = os.open(old, _DIR_FLAGS, dir_fd=target_fd)
    moved_old: list[str] = []
    moved_new: list[str] = []
    try:
        try:
            for name in sorted(os.listdir(target_fd)):
                if name in (*keep, old, ZFS_CONTROL_DIR):
                    continue
                os.rename(name, name, src_dir_fd=target_fd, dst_dir_fd=old_fd)
                moved_old.append(name)
            for name in sorted(os.listdir(source_fd)):
                os.rename(name, name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
                moved_new.append(name)
        except BaseException:
            for name in reversed(moved_new):
                os.rename(name, name, src_dir_fd=target_fd, dst_dir_fd=source_fd)
            for name in reversed(moved_old):
                os.rename(name, name, src_dir_fd=old_fd, dst_dir_fd=target_fd)
            os.rmdir(old, dir_fd=target_fd)
            raise
    finally:
        os.close(old_fd)
    try:
        shutil.rmtree(old, dir_fd=target_fd)  # fd-based, never follows links
    except OSError as exc:
        log.warning("Could not delete the replaced files in %s: %s", old, exc)


def swap_contents(target: Path, staging: Path) -> None:
    """Replace the entries of ``target`` with those of ``staging`` (a directory inside it).

    Path front end of :func:`swap_entries`; ``staging`` is removed afterwards.
    """
    if staging.parent != target:
        raise AgentError("Staging directory must be inside the target", "internal", 500)
    target_fd = os.open(target, _DIR_FLAGS)
    try:
        staging_fd = os.open(staging.name, _DIR_FLAGS, dir_fd=target_fd)
        try:
            swap_entries(target_fd, staging_fd, keep=(staging.name,))
        finally:
            os.close(staging_fd)
        os.rmdir(staging.name, dir_fd=target_fd)
    finally:
        os.close(target_fd)


def create_empty_dir(path: Path, *, subvolume: bool) -> bool:
    """Create the empty directory ``path`` (as a subvolume if asked and possible).

    Returns True if it is a subvolume; falls back to a plain directory if btrfs refuses.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if subvolume:
        try:
            BtrfsSnapshots.create_subvolume(path)
            return True
        except AgentError:
            pass
    path.mkdir()
    return False


def remove_tree(path: Path) -> None:
    """Best-effort removal of a directory tree that may be a btrfs subvolume or snapshot.

    Never raises; what could not be removed is logged with its path.
    """
    problem = _discard(path)
    if problem:
        log.warning("Could not remove %s: %s", path, problem)
