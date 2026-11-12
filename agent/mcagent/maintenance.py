"""Housekeeping when the agent starts: folder permissions and what interrupted operations left.

Operations live in memory (``operations.py``): when the agent process ends during one (an
upgrade recreates its container, a crash, a host shutdown) nothing removes its temporary files.
:func:`sweep_leftovers` does that once at start-up. It only touches what is older than the
process (anything newer may belong to an operation of this process) and never what may be the
only copy of a server's files: a previous server folder whose replacement is not in place is
kept and reported.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from .config import Settings

log = logging.getLogger(__name__)

DATA_ROOT_MODE = 0o750
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Inside a server folder (any depth): upload temp files (safefs), restore and swap staging.
_IN_SERVER_TMP = re.compile(r"^\.mcpanel-tmp-(?:[a-z]+-)?[0-9a-f]{12}$")
_IN_SERVER_OLD = re.compile(r"^\.mcpanel-tmp-old-[0-9a-f]{12}$")
# Directly inside a server folder: a world import being extracted (world.replace).
_WORLD_NEW = re.compile(r"^\.mcpanel-world-new-[0-9a-f]+$")
_WORLD_OLD = re.compile(r"^\.mcpanel-world-old-[0-9a-f]+$")
# Next to the server folders: <id>.mcpanel-<kind>[-<suffix>]
_SIBLING = re.compile(
    rf"^({_UUID})\.mcpanel-(clone|restore|old|rollback|convert|preconvert|discard)(?:-([0-9a-f]+))?$"
)
# Kinds that hold a complete earlier copy of the server: removed only while the live folder exists.
_PREVIOUS_COPY = {"restore", "old", "rollback", "convert", "preconvert"}
# staging/ and trash/
_STAGING_CLONE = re.compile(rf"^clone-({_UUID})$")
_STAGING_RESTORE = re.compile(rf"^restore-({_UUID})-\d+$")
_TRASH_PREVIOUS = re.compile(rf"^(?:pre-restore-({_UUID})-\d+|({_UUID})-preconvert-\d+)$")
_TRASH_PRE_RESTORE = re.compile(rf"^pre-restore-({_UUID})-(\d+)$")
_TRASH_PRECONVERT = re.compile(rf"^({_UUID})-preconvert-\d+$")
# What a sweep renamed before deleting it (see _discard): removed by the next sweep if cut off.
_DISCARDED = re.compile(rf"^(?:{_UUID}\.mcpanel-discard-[0-9a-f]{{12}}|(?:{_UUID}-)?discard-[0-9a-f]{{12}})$")
MAX_DEPTH = 16


def restrict_data_root(settings: Settings) -> list[str]:
    """Make the data root and the instances folder ``0750``, owned by root when the agent runs as root.

    The server folders keep their owner and modes: a Minecraft container mounts its own folder and
    never needs to traverse the ones above it. Nothing else reads them (the panel goes through the
    agent API), so this only shuts out other host users and guest-readable shares (the RCON
    password is in server.properties). Never raises; returns what could not be changed.
    """
    problems: list[str] = []
    for path in (settings.data_root, settings.instances_dir):
        try:
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                problems.append(f"{path} is not a directory")
                continue
            if os.geteuid() == 0 and (st.st_uid, st.st_gid) != (0, 0):
                os.chown(path, 0, 0, follow_symlinks=False)
            if stat.S_IMODE(st.st_mode) != DATA_ROOT_MODE:
                os.chmod(path, DATA_ROOT_MODE)
                log.info("Restricted %s to mode %o", path, DATA_ROOT_MODE)
        except OSError as exc:
            problems.append(f"{path}: {exc}")
    for problem in problems:
        log.warning("Could not restrict the permissions of the data folder: %s", problem)
    return problems


def _older(st: os.stat_result, started_at: float) -> bool:
    return max(st.st_mtime, st.st_ctime) < started_at


def _remove_at(dir_fd: int, name: str) -> None:
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dir_fd)  # fd-based, never follows links
    else:
        os.unlink(name, dir_fd=dir_fd)


class _Report:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.kept: list[str] = []
        self.failed: list[str] = []

    def as_dict(self) -> dict[str, list[str]]:
        return {"removed": self.removed, "kept": self.kept, "failed": self.failed}


def _sweep_server_folder(path: Path, started_at: float, report: _Report) -> None:
    """Agent temp entries inside one server folder (fd-based walk, links never followed)."""

    def walk(fd: int, rel: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        try:
            with os.scandir(fd) as it:
                entries = list(it)
        except OSError:
            return
        # An entry-by-entry swap (ZFS restore or clone, single-path restore) moves the current
        # entries into .mcpanel-tmp-old-* before it moves the new ones in from the staging folder:
        # interrupted in between, those two hold the only copies. Then nothing of the kind is
        # removed in that folder, only reported.
        swap_halted = any(_IN_SERVER_OLD.match(de.name) for de in entries)
        for de in entries:
            name = de.name
            try:
                st = de.stat(follow_symlinks=False)
            except OSError:
                continue
            where = f"{path}/{rel}{name}"
            top = depth == 0
            if _IN_SERVER_TMP.match(name) or (top and _WORLD_NEW.match(name)):
                if not _older(st, started_at):
                    continue
                if swap_halted and _IN_SERVER_TMP.match(name):
                    report.kept.append(where)
                    continue
                try:
                    _remove_at(fd, name)
                    report.removed.append(where)
                except OSError as exc:
                    report.failed.append(f"{where}: {exc}")
                continue
            if top and _WORLD_OLD.match(name):
                # the world a failed reset or replace moved aside: it can be the only copy
                report.kept.append(where)
                continue
            if stat.S_ISDIR(st.st_mode) and name != ".zfs":
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError:
                    continue
                try:
                    walk(child, f"{rel}{name}/", depth + 1)
                finally:
                    os.close(child)

    try:
        fd = os.open(path, _DIR_FLAGS)
    except OSError:
        return
    try:
        walk(fd, "", 0)
    finally:
        os.close(fd)


def _discard_name(path: Path, uid: UUID | None) -> Path:
    """A name of its own for something about to be deleted, which a later sweep still removes if
    the delete is cut off: ``<id>.mcpanel-discard-<hex>`` next to the server folders,
    ``<id>-discard-<hex>`` (or ``discard-<hex>``) in staging/ and trash/."""
    token = secrets.token_hex(6)
    if _SIBLING.match(path.name):
        return path.with_name(f"{uid}.mcpanel-discard-{token}")
    return path.with_name(f"{uid}-discard-{token}" if uid is not None else f"discard-{token}")


def _discard(
    path: Path, report: _Report, started_at: float, uid: UUID | None, busy: Callable[[UUID], bool]
) -> None:
    from .snapshots import _discard as discard
    from .snapshots import is_mount_point

    # checked again right before: an operation may have started since the folder was listed
    try:
        st = path.lstat()
        if not _older(st, started_at) or (uid is not None and busy(uid)):
            return
    except OSError:
        return
    if is_mount_point(path):  # a ZFS dataset mounted there: never removed by a sweep
        report.kept.append(str(path))
        return
    # Moved out of the way first (one rename), then deleted under its new name: an operation that
    # starts meanwhile and creates the fixed name again (a clone stages into staging/clone-<id>)
    # gets a folder of its own that this delete never reaches.
    target = path
    if not _DISCARDED.match(path.name):
        target = _discard_name(path, uid)
        try:
            os.rename(path, target)
            moved = target.lstat()
        except OSError as exc:
            report.failed.append(f"{path}: {exc}")
            return
        if (moved.st_dev, moved.st_ino) != (st.st_dev, st.st_ino):  # replaced since it was checked
            with contextlib.suppress(OSError):
                if not os.path.lexists(path):
                    os.rename(target, path)
            report.kept.append(str(path))
            return
    problem = discard(target)
    if problem:
        report.failed.append(
            f"{path} (moved to {target}): {problem}" if target != path else f"{path}: {problem}"
        )
    else:
        report.removed.append(str(path))


def sweep_leftovers(
    settings: Settings, started_at: float, busy: Callable[[UUID], bool] = lambda _iid: False
) -> dict[str, list[str]]:
    """Remove what interrupted operations left behind; see the module docstring.

    * in every server folder: ``.mcpanel-tmp-*`` (any depth) and ``.mcpanel-world-new-*``;
      ``.mcpanel-world-old-*`` is kept and reported (it can hold the only copy of a world), and so
      is every ``.mcpanel-tmp-*`` of a folder that holds a ``.mcpanel-tmp-old-*`` (a swap that
      was interrupted half-way: the old entries are there, the new ones in the staging folder);
    * next to the server folders: ``<id>.mcpanel-clone-*`` always; ``-restore-*``, ``-old-*``,
      ``-rollback*``, ``-convert`` and ``-preconvert-*`` only while ``<id>`` itself exists and is
      not empty;
    * ``staging/`` and ``trash/``: everything, except copies of a server whose folder is missing
      or empty;
    * never both halves of an interrupted swap (:func:`_swap_halves`), whatever the server folder
      holds;
    * what a sweep renamed to delete (``*discard-*``, see :func:`_discard`) always.

    Only entries older than ``started_at`` and of servers ``busy`` says are idle. Never raises.
    """
    report = _Report()
    try:
        _sweep(settings, started_at, busy, report)
    except Exception:
        log.exception("Sweeping leftovers of interrupted operations failed")
    # %r: the names of what is swept may come from the game (one line, quoted; B-9)
    for path in report.removed:
        log.info("Removed leftover of an interrupted operation: %r", path)
    for path in report.kept:
        log.warning("Kept leftover of an interrupted operation (it may be the only copy of data): %r", path)
    for problem in report.failed:
        log.warning("Could not remove leftover of an interrupted operation: %r", problem)
    return report.as_dict()


def _live(settings: Settings, iid: str) -> bool:
    """The server folder is there and holds something. An empty one may have been made by a
    start after an interruption between the two renames of a swap (the old copy is then the
    only copy): it does not count."""
    path = settings.instances_dir / iid
    try:
        with os.scandir(path) as it:
            return any(True for _ in it)
    except OSError:
        return False


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _entries(folder: Path) -> list[tuple[Path, os.stat_result]]:
    out = []
    try:
        children = sorted(folder.iterdir())
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            log.warning("Cannot read %s: %s", folder, exc)
        return []
    for child in children:
        try:
            out.append((child, child.lstat()))
        except OSError:
            continue
    return out


def _swap_halves(siblings: list[Path], staging: list[Path], trash: list[Path]) -> set[str]:
    """Both halves of a swap that was interrupted between its two renames: the server's previous
    files in one folder, the new ones in the other. Whatever the server folder holds by now (a
    start after the interruption makes a new world in it), these may be the only copies of the
    server's files: never removed by a sweep, only reported.

    * enabling snapshots: ``<id>.mcpanel-convert`` + ``<id>.mcpanel-preconvert-*`` (ZFS) or
      ``trash/<id>-preconvert-*`` (btrfs);
    * a restore: ``<id>.mcpanel-restore-<t>`` + ``<id>.mcpanel-old-<t>`` (subvolume), or
      ``staging/restore-<id>-<t>`` + ``trash/pre-restore-<id>-<t>``;
    * a btrfs rollback: ``<id>.mcpanel-rollback-*`` + ``<id>.mcpanel-old-*``.
    """
    by_server: dict[str, dict[str, list[tuple[str, Path]]]] = {}

    def add(iid: str, kind: str, suffix: str, path: Path) -> None:
        by_server.setdefault(iid, {}).setdefault(kind, []).append((suffix, path))

    for path in siblings:
        m = _SIBLING.match(path.name)
        if m:
            add(m.group(1), m.group(2), m.group(3) or "", path)
    for path in staging:
        m = _STAGING_RESTORE.match(path.name)
        if m:
            add(m.group(1), "staged-restore", path.name.rsplit("-", 1)[1], path)
    for path in trash:
        m = _TRASH_PRE_RESTORE.match(path.name)
        if m:
            add(m.group(1), "pre-restore", m.group(2), path)
        m = _TRASH_PRECONVERT.match(path.name)
        if m:
            add(m.group(1), "trash-preconvert", "", path)

    halves: set[str] = set()
    for kinds in by_server.values():

        def paths(kind: str, suffix: str | None = None, kinds: dict = kinds) -> list[Path]:
            return [p for s, p in kinds.get(kind, []) if suffix is None or s == suffix]

        preconvert = paths("preconvert") + paths("trash-preconvert")
        if paths("convert") and preconvert:
            halves.update(str(p) for p in paths("convert") + preconvert)
        for suffix, restored in kinds.get("restore", []):
            if paths("old", suffix):
                halves.update(str(p) for p in [restored, *paths("old", suffix)])
        for suffix, staged in kinds.get("staged-restore", []):
            if paths("pre-restore", suffix):
                halves.update(str(p) for p in [staged, *paths("pre-restore", suffix)])
        if paths("rollback") and paths("old"):
            halves.update(str(p) for p in paths("rollback") + paths("old"))
    return halves


def _sweep(settings: Settings, started_at: float, busy: Callable[[UUID], bool], report: _Report) -> None:
    siblings = _entries(settings.instances_dir)
    staged = {folder: _entries(folder) for folder in (settings.staging_dir, settings.trash_dir)}
    halves = _swap_halves(
        [p for p, _ in siblings],
        [p for p, _ in staged[settings.staging_dir]],
        [p for p, _ in staged[settings.trash_dir]],
    )
    for child, st in siblings:
        if not _older(st, started_at):
            continue
        sibling = _SIBLING.match(child.name)
        if sibling:
            iid, kind = sibling.group(1), sibling.group(2)
            uid = _uuid(iid)
            if uid is None or busy(uid):
                continue
            if str(child) in halves or (kind in _PREVIOUS_COPY and not _live(settings, iid)):
                report.kept.append(str(child))
                continue
            _discard(child, report, started_at, uid, busy)
            continue
        uid = _uuid(child.name)
        if uid is not None and stat.S_ISDIR(st.st_mode) and not busy(uid):
            _sweep_server_folder(child, started_at, report)
    for folder in (settings.staging_dir, settings.trash_dir):
        for child, st in staged[folder]:
            if not _older(st, started_at):
                continue
            m = _STAGING_CLONE.match(child.name) or _STAGING_RESTORE.match(child.name)
            previous = _TRASH_PREVIOUS.match(child.name)
            iid = (m.group(1) if m else None) or (previous and (previous.group(1) or previous.group(2)))
            if iid is None:
                head = child.name[:36]
                iid = head if _uuid(head) else None
            uid = _uuid(iid) if iid else None
            if uid is not None and busy(uid):
                continue
            restore_copy = bool(_STAGING_RESTORE.match(child.name)) or bool(previous)
            if str(child) in halves or (restore_copy and iid and not _live(settings, iid)):
                report.kept.append(str(child))
                continue
            _discard(child, report, started_at, uid, busy)


def summary(report: dict[str, Any]) -> str:
    return f"{len(report['removed'])} removed, {len(report['kept'])} kept, {len(report['failed'])} failed"
