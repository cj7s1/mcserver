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
  it in with renames and deletes the old subvolume only after the swap succeeded.

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


def _btrfs() -> str:
    binary = shutil.which("btrfs")
    if not binary:
        raise AgentError("btrfs-progs is not installed in the agent image", "snapshots_unavailable", 503)
    return binary


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        raise AgentError(f"btrfs failed: {msg[-1] if msg else proc.returncode}", "btrfs_failed", 500)
    return proc


def is_subvolume(path: Path) -> bool:
    # On btrfs, subvolume roots always have inode number 256.
    try:
        return path.stat().st_ino == 256 and (storage.mount_for(path) or {}).get("fstype") == "btrfs"
    except FileNotFoundError:
        return False


def slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]


class BtrfsSnapshots:
    provider = "btrfs"

    def __init__(self, settings: Settings, instance_id: UUID) -> None:
        self.settings = settings
        self.instance_id = instance_id
        self.instance_dir = settings.instances_dir / str(instance_id)
        self.snap_dir = settings.data_root / "snapshots" / str(instance_id)

    # -- capability ----------------------------------------------------------------
    def capability(self) -> dict[str, Any]:
        mount = storage.mount_for(self.settings.instances_dir) or {}
        fstype = mount.get("fstype")
        info: dict[str, Any] = {
            "provider": "btrfs",
            "available": False,
            "fstype": fstype,
            "reason": None,
            "instance_is_subvolume": False,
            "instance_ready": False,
        }
        if fstype != "btrfs":
            info["reason"] = (
                f"The data directory is on '{fstype}', which has no native snapshots (btrfs or ZFS needed)"
            )
            return info
        if not shutil.which("btrfs"):
            info["reason"] = "btrfs-progs is not installed in the agent image"
            return info
        info["instance_is_subvolume"] = info["instance_ready"] = is_subvolume(self.instance_dir)
        info["available"] = True
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
        tmp = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-convert")
        if tmp.exists():
            raise AgentError(
                "A previous conversion was interrupted; remove the .mcpanel-convert folder",
                "convert_conflict",
                409,
            )
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
            if tmp.exists():
                _run([_btrfs(), "subvolume", "delete", str(tmp)])
            raise
        shutil.rmtree(old, ignore_errors=True)
        return {"converted": True}

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
        path = self._validate(name)
        _run([_btrfs(), "subvolume", "delete", str(path)])
        self._meta_path(name).unlink(missing_ok=True)

    def restore(self, name: str) -> dict[str, Any]:
        self.require_ready()
        snap = self._validate(name)
        tmp = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-rollback")
        if tmp.exists():
            _run([_btrfs(), "subvolume", "delete", str(tmp)])
        _run([_btrfs(), "subvolume", "snapshot", str(snap), str(tmp)])  # writable copy
        old = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-old-{int(time.time())}")
        self.instance_dir.rename(old)
        try:
            tmp.rename(self.instance_dir)
        except BaseException:
            old.rename(self.instance_dir)
            raise
        _run([_btrfs(), "subvolume", "delete", str(old)])
        return {"restored": name}

    def destroy_all(self) -> dict[str, Any]:
        """Delete every snapshot of the instance and its snapshot folder (the server is being deleted).

        Snapshots are read-only subvolumes, which a plain recursive delete cannot remove. Raises
        if btrfs refuses: the caller runs this *before* deleting the server's files, so a failed
        delete leaves the server intact and can be retried.
        """
        destroyed: list[str] = []
        if self.snap_dir.is_dir():
            for entry in sorted(self.snap_dir.iterdir()):
                if entry.is_dir() and not entry.is_symlink():
                    if is_subvolume(entry):
                        _run([_btrfs(), "subvolume", "delete", str(entry)])
                    else:
                        shutil.rmtree(entry)
                    destroyed.append(entry.name)
                else:
                    entry.unlink()  # label sidecars
            self.snap_dir.rmdir()
        return {"destroyed": destroyed, "snapshots": len(destroyed), "warnings": []}


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
    """Best-effort removal of a directory tree that may be a btrfs subvolume."""
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        return
    if path.is_dir() and is_subvolume(path):
        try:
            _run([_btrfs(), "subvolume", "delete", str(path)])
            return
        except AgentError:
            pass  # e.g. it contains a nested subvolume: fall back to a recursive delete
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
