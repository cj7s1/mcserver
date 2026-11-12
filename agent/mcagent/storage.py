"""Storage introspection: filesystem type, free space, snapshot capability, usage."""

from __future__ import annotations

import os
import shutil
import stat as statmod
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from .config import Settings

_usage_cache: dict[str, tuple[float, int]] = {}
_usage_lock = threading.Lock()
USAGE_TTL = 60.0


def _unescape(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def mounts() -> list[dict[str, str]]:
    """All mounts of this mount namespace, from /proc/self/mountinfo, in mount order.

    ``propagation`` holds the optional fields (``shared:N``, ``master:N``, ...), which tell
    whether mounts made below a mount propagate to other mount namespaces (ZFS needs that).
    ``root`` is the directory of the filesystem that is mounted: ``/`` for a whole filesystem
    (a ZFS dataset), a subdirectory for a bind mount of one.
    """
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        pre, _, post = line.partition(" - ")
        fields = pre.split()
        post_fields = post.split()
        if len(fields) < 5 or len(post_fields) < 2:
            continue
        out.append(
            {
                "mount_point": _unescape(fields[4]),
                "fstype": post_fields[0],
                "source": post_fields[1],
                "root": _unescape(fields[3]),
                "propagation": " ".join(fields[6:]),
            }
        )
    return out


def mount_for(path: Path) -> dict[str, str] | None:
    """Find the mount (from /proc/self/mountinfo) that contains ``path`` (see ``mounts``)."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    best: dict[str, str] | None = None
    for mount in mounts():
        mount_point = mount["mount_point"]
        if real == mount_point or real.startswith(mount_point.rstrip("/") + "/") or mount_point == "/":
            if best is None or len(mount_point) >= len(best["mount_point"]):
                best = mount
    return best


def describe_path(
    path: Path, *, same_host_path: bool = True, snapshot_root: Path | None = None
) -> dict[str, Any]:
    """Filesystem, space and snapshot support of ``path``; ``same_host_path``: the agent sees it at
    the same path as the host (ZFS snapshots need that). ``snapshot_root``: the directory on the
    same btrfs filesystem in which the agent may test that it can delete snapshots (the data root);
    without it btrfs snapshot support is reported as not checked, never guessed."""
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    mount = mount_for(path) or {}
    fstype = mount.get("fstype", "unknown")
    snapshot: dict[str, Any] = {"provider": None, "available": False, "reason": None}
    if fstype == "btrfs":
        snapshot["provider"] = "btrfs"
        if not shutil.which("btrfs"):
            snapshot["reason"] = "btrfs-progs is not installed in the agent image"
        elif snapshot_root is None:
            snapshot["reason"] = "Not checked: MCPanel only takes snapshots of the server folders"
        else:
            from .snapshots import DELETE_FALLBACK, FALLBACK_NOTE, delete_support

            support = delete_support(snapshot_root)
            snapshot["available"] = support["mode"] is not None
            snapshot["reason"] = support["reason"]
            snapshot["delete_mode"] = support["mode"]
            snapshot["note"] = FALLBACK_NOTE if support["mode"] == DELETE_FALLBACK else None
    elif fstype == "zfs":
        from .zfs import environment_problem

        snapshot["provider"] = "zfs"
        snapshot["reason"] = environment_problem(mount, same_host_path=same_host_path)
        snapshot["available"] = snapshot["reason"] is None
    elif fstype in ("fuse.shfs", "shfs"):
        snapshot["reason"] = (
            "Unraid user shares (/mnt/user) cannot be snapshotted; point the data directory at a pool path such as /mnt/cache/..."
        )
    else:
        snapshot["reason"] = f"Filesystem '{fstype}' has no native snapshot support"
    return {
        "path": str(path),
        "fstype": fstype,
        "mount_point": mount.get("mount_point"),
        "source": mount.get("source"),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "snapshot": snapshot,
    }


def capabilities(settings: Settings) -> dict[str, Any]:
    # Snapshots are taken from the server folders into <data root>/snapshots, so whether they can
    # be deleted again is measured in the data root; the answer holds for the backups directory
    # only if that is on the same filesystem.
    data_source = (mount_for(settings.data_root) or {}).get("source")
    backups_source = (mount_for(settings.backups_dir) or {}).get("source")
    return {
        "instances": describe_path(
            settings.instances_dir,
            same_host_path=settings.host_instances_dir == settings.instances_dir,
            snapshot_root=settings.data_root,
        ),
        "backups": describe_path(
            settings.backups_dir,
            snapshot_root=settings.data_root if data_source and backups_source == data_source else None,
        ),
    }


def dir_size(path: Path) -> int:
    """Bytes the folder takes (blocks, else the size), walked through descriptors: a folder replaced by a
    link during the walk is not followed (B-4)."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def walk(fd: int, depth: int) -> int:
        total = 0
        try:
            with os.scandir(fd) as it:
                entries = list(it)
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            return 0
        for de in entries:
            try:
                st = de.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if statmod.S_ISDIR(st.st_mode):
                if depth >= MAX_SIZE_DEPTH:
                    continue
                try:
                    child = os.open(de.name, flags, dir_fd=fd)
                except OSError:
                    continue  # replaced by a link or removed meanwhile
                try:
                    total += walk(child, depth + 1)
                finally:
                    os.close(child)
            else:
                total += getattr(st, "st_blocks", 0) * 512 or st.st_size
        return total

    try:
        root = os.open(path, flags)
    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError):
        return 0
    try:
        return walk(root, 0)
    finally:
        os.close(root)


MAX_SIZE_DEPTH = 256


def instance_usage(settings: Settings, instance_id: UUID, *, max_age: float = USAGE_TTL) -> dict[str, Any]:
    path = settings.instances_dir / str(instance_id)
    key = str(path)
    now = time.time()
    with _usage_lock:
        cached = _usage_cache.get(key)
    if cached and now - cached[0] < max_age:
        return {"bytes": cached[1], "measured_at": cached[0], "cached": True}
    size = dir_size(path) if path.exists() else 0
    backups = settings.backups_dir / "restic" / str(instance_id)
    backup_size = dir_size(backups) if backups.exists() else 0
    with _usage_lock:
        _usage_cache[key] = (now, size)
    return {"bytes": size, "backup_bytes": backup_size, "measured_at": now, "cached": False}
