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


def describe_path(path: Path, *, same_host_path: bool = True) -> dict[str, Any]:
    """Filesystem, space and snapshot support of ``path``; ``same_host_path``: the agent sees it at
    the same path as the host (ZFS snapshots need that)."""
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    mount = mount_for(path) or {}
    fstype = mount.get("fstype", "unknown")
    snapshot: dict[str, Any] = {"provider": None, "available": False, "reason": None}
    if fstype == "btrfs":
        snapshot["provider"] = "btrfs"
        if not shutil.which("btrfs"):
            snapshot["reason"] = "btrfs-progs is not installed in the agent image"
        else:
            snapshot["available"] = True
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
    return {
        "instances": describe_path(
            settings.instances_dir, same_host_path=settings.host_instances_dir == settings.instances_dir
        ),
        "backups": describe_path(settings.backups_dir),
    }


def dir_size(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if statmod.S_ISDIR(st.st_mode):
                        stack.append(de.path)
                    else:
                        total += getattr(st, "st_blocks", 0) * 512 or st.st_size
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            continue
    return total


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
