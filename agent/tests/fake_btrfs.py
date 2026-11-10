"""A stand-in ``btrfs`` command for the snapshot tests on machines without a btrfs filesystem.

Installed by ``tests/test_snapshots_btrfs_delete.py`` as an executable named ``btrfs`` first on
PATH. It emulates the few subcommands the agent uses, with real directories:

* a subvolume is a directory that holds the marker file ``.fake-btrfs-subvolume`` (its text is
  ``rw`` or ``ro``); the tests patch ``snapshots.is_subvolume`` to look for it. Like a real
  subvolume it keeps its identity when it is renamed and disappears when its directory is removed;
* a snapshot is a copy. A read-only one has the write bit taken off every directory in it, so that
  (for a test process that is not root) nothing in it can be removed until
  ``btrfs property set -ts <path> ro false`` puts the bits back, as on btrfs;
* ``btrfs subvolume delete`` removes the tree, or fails the way the kernel makes it fail for a
  process without CAP_SYS_ADMIN: always with ``FAKE_BTRFS_DELETE=eperm`` (a default mount), only
  for a read-only snapshot with ``FAKE_BTRFS_DELETE=rofs`` (a mount with
  ``user_subvol_rm_allowed``). The messages are the ones btrfs-progs 6.14 prints, from real runs;
* ``FAKE_BTRFS_PROPERTY=eperm`` makes ``property set`` fail the same way.

Every argv is appended to ``$FAKE_BTRFS_LOG`` (one JSON list per line) when that is set.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

MARKER = ".fake-btrfs-subvolume"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def is_subvolume(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (path / MARKER).is_file()


def set_mode(path: Path, *, read_only: bool) -> None:
    """Emulate the read-only flag with directory permissions (see the module docstring)."""
    for current, _dirs, _files in os.walk(path):
        os.chmod(current, 0o555 if read_only else 0o755)
    (path / MARKER).write_text("ro" if read_only else "rw")


def create(args: list[str]) -> None:
    path = Path(args[-1])
    if os.path.lexists(path):
        fail(f"target path already exists: {path}")
    path.mkdir()
    (path / MARKER).write_text("rw")
    print(f"Create subvolume '{path}'")


def snapshot(args: list[str]) -> None:
    read_only = "-r" in args
    src, dst = (Path(a) for a in [a for a in args if a != "-r"][-2:])
    if not is_subvolume(src):
        fail(f"Not a Btrfs subvolume: {src}")
    if os.path.lexists(dst):
        fail(f"target path already exists: {dst}")
    shutil.copytree(src, dst, symlinks=True)
    set_mode(dst, read_only=read_only)
    print(f"Create {'readonly ' if read_only else ''}snapshot of '{src}' in '{dst}'")


def delete(args: list[str]) -> None:
    path = Path(args[-1])
    if not is_subvolume(path):
        fail(f"Not a Btrfs subvolume: {path}")
    print(f"Delete subvolume 256 (no-commit): '{path}'")
    refusal = os.environ.get("FAKE_BTRFS_DELETE")
    if refusal == "eperm":
        print("ERROR: Could not destroy subvolume/snapshot: Operation not permitted", file=sys.stderr)
        print(
            "WARNING: deletion failed with EPERM, you don't have permissions or send may be in progress",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if refusal == "rofs" and (path / MARKER).read_text() == "ro":
        fail("Could not destroy subvolume/snapshot: Read-only file system")
    set_mode(path, read_only=False)
    shutil.rmtree(path)


def prop(args: list[str]) -> None:
    action, rest = args[0], [a for a in args[1:] if a != "-ts"]
    path = Path(rest[0])
    if not is_subvolume(path):
        fail(f"object is not a btrfs subvolume: {path}")
    if rest[1] != "ro":
        fail(f"unknown property: {rest[1]}")
    if action == "get":
        print(f"ro={'true' if (path / MARKER).read_text() == 'ro' else 'false'}")
        return
    if os.environ.get("FAKE_BTRFS_PROPERTY") == "eperm":
        fail("failed to set flags for subvolume: Operation not permitted")
    if rest[2] not in ("true", "false"):
        fail(f"invalid value for property: {rest[2]}")
    set_mode(path, read_only=rest[2] == "true")


def main(argv: list[str]) -> None:
    log = os.environ.get("FAKE_BTRFS_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(argv) + "\n")
    args = [a for a in argv if a != "-q"]
    if args[:1] == ["--version"]:
        print("btrfs-progs v0.0-fake")
    elif args[:2] == ["subvolume", "create"]:
        create(args[2:])
    elif args[:2] == ["subvolume", "snapshot"]:
        snapshot(args[2:])
    elif args[:2] == ["subvolume", "delete"]:
        delete(args[2:])
    elif args[:1] == ["property"] and args[1:2] in (["get"], ["set"]):
        prop(args[1:])
    else:
        fail(f"fake btrfs: unsupported command {argv}")


if __name__ == "__main__":
    main(sys.argv[1:])
