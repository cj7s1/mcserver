"""World import/export/reset: describe the world folders, replace them from an archive, delete them.

A Java Edition world is a folder with a ``level.dat`` (gzip-compressed NBT). Where the
nether and the end live depends on the server software:

* **vanilla layout** (Vanilla, Fabric, Forge, NeoForge, Quilt): everything is inside the
  world folder (``world/DIM-1`` = nether, ``world/DIM1`` = end);
* **bukkit layout** (Paper, Purpur, Folia, Spigot): separate sibling folders
  ``world_nether/DIM-1`` and ``world_the_end/DIM1``.

``replace`` extracts the archive into a private staging folder inside the instance (the
same filesystem, so every later step is an atomic rename), finds the world in it, moves
the current world folders aside, renames the new ones into place and only then deletes
the old ones. If any rename fails the new folders are taken out again and the old ones
are put back. Every path goes through :class:`SafeRoot` (no ``..``, no symlinks).

``reset`` deletes the world so the server generates a new one on its next start. It only
deletes folders that look like a world (see :func:`classify`), moves them aside first
(atomic renames that are undone on any failure) and deletes them afterwards. Every failure
is an :class:`AgentError` whose code says whether the world was touched.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import archive as archive_mod
from .docker_ops import AgentError
from .safefs import PathError, SafeRoot, split_path

GZIP_MAGIC = b"\x1f\x8b"
STAGING_PREFIX = ".mcpanel-world-"
# Bukkit-style dimension folders are siblings of the world folder with these suffixes.
DIMENSION_SUFFIXES = {"nether": "_nether", "end": "_the_end"}
# ... and inside the world folder in the vanilla layout.
VANILLA_DIMENSIONS = {"nether": "DIM-1", "end": "DIM1"}
# Top-level server folders that can never be a world (protects against level-name=plugins).
RESERVED_FOLDERS = frozenset(
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
FREE_SPACE_MARGIN = 128 * 1024 * 1024
MAX_SEARCH_DEPTH = 3
# Folders some archivers add next to the real content (macOS Finder: resource forks); never a world.
IGNORED_ARCHIVE_DIRS = frozenset({"__MACOSX"})
# What a world reset can carry over into the new world:
# keep option -> role -> ((entry name inside that world folder, required type), ...).
# Keep in sync with backend/apps/instances/world.py RESET_KEEP.
RESET_KEEP: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "datapacks": {"overworld": (("datapacks", "directory"),)},
    "config": {
        "overworld": (("serverconfig", "directory"), ("paper-world.yml", "file")),
        "nether": (("paper-world.yml", "file"),),
        "end": (("paper-world.yml", "file"),),
    },
}
# Every entry name RESET_KEEP can carry over: a folder with nothing else in it has no world (yet).
KEEPABLE_NAMES = frozenset({"datapacks", "serverconfig", "paper-world.yml"})
# A direct entry with one of these names makes a folder a world folder (Java Edition, every
# layout, including the "dimensions" folder of 26.1+ worlds).
WORLD_MARKERS = frozenset(
    {"level.dat", "level.dat_old", "session.lock", "uid.dat", "region", "DIM-1", "DIM1", "dimensions"}
)


def level_parts(level_name: str) -> list[str]:
    """Validate a world folder name (``level-name``) and split it into path components."""
    if not isinstance(level_name, str) or not level_name.strip():
        raise AgentError("The world folder name is empty", "invalid_level_name", 400)
    if "\\" in level_name or len(level_name) > 200:
        raise AgentError(f"Unsupported world folder name {level_name!r}", "invalid_level_name", 400)
    try:
        parts = split_path(level_name)
    except PathError as exc:
        raise AgentError(
            f"Invalid world folder name {level_name!r}: {exc}", "invalid_level_name", 400
        ) from exc
    if not parts or len(parts) > 4:
        raise AgentError(f"Invalid world folder name {level_name!r}", "invalid_level_name", 400)
    for part in parts:
        if part.startswith(".") or any(ord(ch) < 32 for ch in part):
            raise AgentError(f"Invalid world folder name {level_name!r}", "invalid_level_name", 400)
    if parts[0].lower() in RESERVED_FOLDERS:
        raise AgentError(
            f"'{level_name}' is a server folder, not a world folder; fix level-name in server.properties",
            "invalid_level_name",
            400,
        )
    return parts


def world_paths(parts: list[str], layout: str) -> list[tuple[str, str]]:
    """(role, relative path) of the folders that make up the world for this layout."""
    out = [("overworld", "/".join(parts))]
    if layout == "bukkit":
        for role, suffix in DIMENSION_SUFFIXES.items():
            out.append((role, "/".join([*parts[:-1], parts[-1] + suffix])))
    return out


def check_archive_location(archive: str, parts: list[str], layout: str) -> None:
    """Refuse an archive stored inside one of the world folders it is about to replace.

    The old world folders are deleted after the swap, so such an archive would be deleted
    with them (even when the caller wants to keep it).
    """
    try:
        arc = split_path(archive)
    except PathError as exc:
        raise AgentError(f"Invalid archive path: {exc}", "invalid_path", 400) from exc
    for _role, rel in world_paths(parts, layout):
        folder = rel.split("/")
        if arc[: len(folder)] == folder:
            raise AgentError(
                f"The archive is inside the world folder '{rel}', which is replaced (and deleted). "
                "Move it somewhere else in the server folder first.",
                "archive_in_world",
                400,
            )


def _kind(root: SafeRoot, rel: str) -> str | None:
    try:
        return root.stat(rel).type
    except PathError as exc:
        if exc.code in ("not_found", "not_a_directory", "symlink"):
            return None
        raise


def _state(root: SafeRoot, rel: str, kind: str | None) -> tuple[str, frozenset[str]]:
    """(state, names of the direct entries) of a would-be world folder of type ``kind``."""
    if kind is None:
        return "missing", frozenset()
    if kind != "directory":
        return "not_a_directory", frozenset()
    names = frozenset(e.name for e in root.list(rel))
    if not names:
        return "empty", names
    if names & WORLD_MARKERS:
        return "world", names
    return ("pending" if names <= KEEPABLE_NAMES else "unknown"), names


def classify(root: SafeRoot, rel: str) -> str:
    """What is at a world path. Links are never followed.

    * ``missing``: nothing there (or a parent is a file or a link);
    * ``not_a_directory``: a file, a link or something else;
    * ``empty``: a folder without entries;
    * ``world``: a folder with at least one :data:`WORLD_MARKERS` name among its direct entries;
    * ``pending``: a folder that only holds :data:`KEEPABLE_NAMES` entries, which is what a reset
      that carried something over leaves until the server generates the new world;
    * ``unknown``: any other folder. It is never deleted by :func:`reset`.
    """
    return _state(root, rel, _kind(root, rel))[0]


def keepable(root: SafeRoot, role: str, rel: str) -> dict[str, list[str]]:
    """Keep option -> entry names in the world folder ``rel`` that a reset would carry over.

    Only entries of the right type count, a folder only if it is not empty, a link never.
    """
    out: dict[str, list[str]] = {}
    for key, roles in RESET_KEEP.items():
        for name, kind in roles.get(role, ()):
            path = f"{rel}/{name}"
            if _kind(root, path) != kind:
                continue
            if kind == "directory" and not root.list(path):
                continue
            out.setdefault(key, []).append(name)
    return out


def info(root: SafeRoot, level_name: str, layout: str, *, sizes: bool = True) -> dict:
    """Which world folders exist, what they are (see :func:`classify`) and how big they are.

    ``sizes=False`` skips walking the folders: ``bytes`` and ``files`` are 0 and ``modified``
    is None then.
    """
    parts = level_parts(level_name)
    folders = []
    total = 0
    for role, rel in world_paths(parts, layout):
        entry: dict = {
            "role": role,
            "path": rel,
            "exists": False,
            "type": None,
            "has_level_dat": False,
            "bytes": 0,
            "files": 0,
            "modified": None,
            "state": "missing",
            "markers": [],
            "keepable": {},
        }
        kind = _kind(root, rel)
        entry["type"] = kind
        state, names = _state(root, rel, kind)
        entry.update(state=state, markers=sorted(names & WORLD_MARKERS))
        if state == "world":
            entry["keepable"] = keepable(root, role, rel)
        if kind == "directory":
            entry["exists"] = True
            entry["has_level_dat"] = _kind(root, f"{rel}/level.dat") == "file"
            if sizes:
                newest = root.stat(rel).modified
                size = count = 0
                for _path, st in root.walk_files(rel, max_depth=64):
                    size += st.st_size
                    count += 1
                    newest = max(newest, st.st_mtime)
                entry.update(bytes=size, files=count, modified=newest)
                total += size
            if role == "overworld":
                entry["dimensions"] = [
                    r for r, d in VANILLA_DIMENSIONS.items() if _kind(root, f"{rel}/{d}") == "directory"
                ]
        folders.append(entry)
    try:
        leftovers = sorted(e.name for e in root.list("") if e.name.startswith(STAGING_PREFIX))
    except PathError:
        leftovers = []  # the server folder does not exist yet (never installed)
    return {
        "level_name": "/".join(parts),
        "layout": layout,
        "folders": folders,
        "total_bytes": total,
        "leftovers": leftovers,
    }


@dataclass
class Located:
    overworld: str  # relative path of the world folder inside the staging area
    base: str  # the folder that contains it (or the world itself for a bare world)
    folder_name: str | None  # name of the world folder in the archive (None: archive root)
    siblings: dict[str, str] = field(default_factory=dict)  # role -> bukkit dimension folder
    others: list[str] = field(default_factory=list)  # unrelated entries next to the world


def _choose(worlds: list[str], preferred: str) -> str:
    dims = {
        w for w in worlds for s in DIMENSION_SUFFIXES.values() if w.endswith(s) and w[: -len(s)] in worlds
    }
    primary = sorted(w for w in worlds if w not in dims)
    if preferred in primary:
        return preferred
    if len(primary) == 1:
        return primary[0]
    if "world" in primary:
        return "world"
    shown = ", ".join(primary[:6]) + (", …" if len(primary) > 6 else "")
    raise AgentError(
        f"The archive contains several worlds ({shown}). Upload an archive with a single world.",
        "multiple_worlds",
        400,
    )


def locate(root: SafeRoot, staging: str, preferred: str) -> Located:
    """Find the world in an extracted archive: the archive root itself, or a folder in it."""
    base = staging
    for _depth in range(MAX_SEARCH_DEPTH):
        if _kind(root, f"{base}/level.dat") == "file":
            return Located(overworld=base, base=base, folder_name=None)
        entries = [e for e in root.list(base) if e.name not in IGNORED_ARCHIVE_DIRS]
        dirs = [e.name for e in entries if e.type == "directory"]
        worlds = [d for d in dirs if _kind(root, f"{base}/{d}/level.dat") == "file"]
        if worlds:
            main = _choose(worlds, preferred)
            siblings = {
                role: f"{base}/{main}{suffix}"
                for role, suffix in DIMENSION_SUFFIXES.items()
                if f"{main}{suffix}" in dirs
            }
            used = {main, *(s.rsplit("/", 1)[-1] for s in siblings.values())}
            others = sorted(e.name for e in entries if e.name not in used)
            return Located(
                overworld=f"{base}/{main}", base=base, folder_name=main, siblings=siblings, others=others
            )
        if len(dirs) != 1:
            break
        base = f"{base}/{dirs[0]}"
    raise AgentError(
        "No Minecraft world found in the archive. It must contain a level.dat file, either at the top "
        "level or inside a world folder (for example world/level.dat).",
        "no_level_dat",
        400,
    )


def check_level_dat(root: SafeRoot, world_dir: str) -> None:
    """Java Edition level.dat files are gzip-compressed NBT; refuse anything else."""
    with root.open_read(f"{world_dir}/level.dat") as (fh, _size):
        head = fh.read(2)
    if head == GZIP_MAGIC:
        return
    if _kind(root, f"{world_dir}/db") == "directory":
        raise AgentError(
            "This is a Bedrock Edition world (it has a LevelDB 'db' folder). Java Edition servers cannot load it; "
            "convert it to Java Edition first.",
            "bedrock_world",
            400,
        )
    raise AgentError(
        "level.dat is not a Java Edition level file (expected gzip-compressed NBT); the archive may be damaged",
        "invalid_world",
        400,
    )


def _fmt(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"  # pragma: no cover


def replace(
    root: SafeRoot,
    archive: str,
    level_name: str,
    layout: str,
    *,
    free_bytes: Callable[[], int] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Replace the world folders of ``level_name`` with the world found in ``archive``."""

    def report(value: float, message: str) -> None:
        if progress:
            progress(value, message)

    parts = level_parts(level_name)
    if layout not in ("vanilla", "bukkit"):
        raise AgentError("layout must be 'vanilla' or 'bukkit'", "invalid_layout", 400)
    if _kind(root, archive) != "file":
        raise AgentError("The world archive must be a regular file in the server folder", "not_a_file", 400)
    check_archive_location(archive, parts, layout)
    for _role, dest in world_paths(parts, layout):
        if _kind(root, dest) not in (None, "directory", "symlink"):
            raise AgentError(
                f"'{dest}' is a file, not a world folder; move it away or fix level-name in server.properties",
                "not_a_directory",
                409,
            )
    token = secrets.token_hex(6)
    staging = f"{STAGING_PREFIX}new-{token}"
    aside = f"{STAGING_PREFIX}old-{token}"
    warnings: list[str] = []

    def fits(_count: int, total: int) -> None:
        if free_bytes is None:
            return
        free = free_bytes()
        if total + FREE_SPACE_MARGIN > free:
            raise AgentError(
                f"Not enough free disk space: the world needs {_fmt(total)} but only {_fmt(free)} is free on the node",
                "insufficient_space",
                507,
            )

    root.mkdir(staging)
    try:
        report(0.02, "Extracting the world archive")
        stats = archive_mod.extract(
            root,
            archive,
            staging,
            strip="auto",
            overwrite=True,
            progress=lambda p, m: report(0.02 + 0.7 * p, m),
            check=fits,
        )
        report(0.74, "Looking for level.dat")
        found = locate(root, staging, parts[-1])
        check_level_dat(root, found.overworld)
        for junk in IGNORED_ARCHIVE_DIRS:  # a bare world zipped on macOS: do not import its resource forks
            if _kind(root, f"{found.overworld}/{junk}") == "directory":
                root.delete(f"{found.overworld}/{junk}", recursive=True)
        sources: dict[str, str] = {"overworld": found.overworld}
        converted: list[str] = []
        if layout == "bukkit":
            sources.update(found.siblings)
        else:
            # A Paper/Spigot world on a vanilla-layout server: move the dimension data into the world.
            for role, sibling in found.siblings.items():
                dim = VANILLA_DIMENSIONS[role]
                if (
                    _kind(root, f"{sibling}/{dim}") == "directory"
                    and _kind(root, f"{found.overworld}/{dim}") is None
                ):
                    root.rename(f"{sibling}/{dim}", f"{found.overworld}/{dim}")
                    converted.append(role)
                else:
                    warnings.append(f"'{sibling.rsplit('/', 1)[-1]}' in the archive was not used")
        if found.others:
            shown = ", ".join(found.others[:8]) + (", …" if len(found.others) > 8 else "")
            warnings.append(
                f"Only the world was imported; other entries in the archive were ignored: {shown}"
            )

        destinations = dict(world_paths(parts, layout))
        if len(parts) > 1:
            root.mkdir("/".join(parts[:-1]), parents=True)
        report(0.8, "Moving the current world aside")
        root.mkdir(aside)
        moved: list[tuple[str, str]] = []  # (world path, where it is kept meanwhile)
        installed: list[tuple[str, str]] = []  # (world path, where it came from)
        try:
            for role, dest in destinations.items():
                if _kind(root, dest) is None:
                    continue
                root.rename(dest, f"{aside}/{role}")
                moved.append((dest, f"{aside}/{role}"))
            report(0.85, "Putting the new world in place")
            for role, src in sources.items():
                root.rename(src, destinations[role])
                installed.append((destinations[role], src))
        except BaseException as exc:
            problems = []
            for dest, src in reversed(installed):
                try:
                    root.rename(dest, src)
                except (PathError, OSError) as undo_exc:
                    problems.append(f"{dest}: {undo_exc}")
            for dest, held in reversed(moved):
                try:
                    root.rename(held, dest)
                except (PathError, OSError) as undo_exc:
                    problems.append(f"{dest}: {undo_exc}")
            if problems:
                raise AgentError(
                    f"Replacing the world failed ({exc}) and the previous world could not be put back completely "
                    f"({'; '.join(problems)}). It is kept in the folder '{aside}'.",
                    "world_rollback_failed",
                    500,
                ) from exc
            try:
                root.delete(aside)
            except (PathError, OSError):
                pass
            raise
        report(0.9, "Deleting the previous world")
        try:
            root.delete(aside, recursive=True)
        except (PathError, OSError) as exc:
            warnings.append(f"The previous world could not be deleted ({exc}); it is kept in '{aside}'")
        return {
            "level_name": "/".join(parts),
            "layout": layout,
            "archive_folder": found.folder_name,
            "installed": [dest for dest, _src in installed],
            "replaced": [dest for dest, _held in moved],
            "converted": converted,
            "files": stats["files"],
            "bytes": stats["bytes"],
            "skipped_count": stats["skipped_count"],
            "warnings": warnings,
        }
    finally:
        if _kind(root, staging) is not None:
            try:
                root.delete(staging, recursive=True)
            except (PathError, OSError):
                pass


@dataclass
class _ResetPlan:
    level_name: str
    present: list[tuple[str, str]] = field(default_factory=list)  # (role, path) of the folders with a world
    carry: dict[str, list[str]] = field(default_factory=dict)  # role -> entries carried over
    skipped: list[str] = field(default_factory=list)  # folders without a world: left as they are


def _reset_plan(
    root: SafeRoot, level_name: str, layout: str, expect: Iterable[str], keep: Iterable[str]
) -> _ResetPlan:
    """The checks of :func:`reset` (they only read): what is moved aside, carried over and skipped."""
    parts = level_parts(level_name)
    if layout not in ("vanilla", "bukkit"):
        raise AgentError("layout must be 'vanilla' or 'bukkit'", "invalid_layout", 400)
    keys: list[str] = []
    for key in keep:
        if not isinstance(key, str) or key not in RESET_KEEP:
            raise AgentError(f"Unknown keep option {key!r}", "invalid_keep", 400)
        if key not in keys:
            keys.append(key)
    plan = _ResetPlan(level_name="/".join(parts))
    for role, dest in world_paths(parts, layout):
        state = classify(root, dest)
        if state == "missing":
            continue
        if state == "not_a_directory":
            raise AgentError(
                f"'{dest}' is not a folder (a file or a link); move it away or fix level-name in "
                "server.properties. Nothing was changed.",
                "not_a_directory",
                409,
            )
        if state == "unknown":
            raise AgentError(
                f"'{dest}' does not look like a Minecraft world (no level.dat, session.lock or region folder "
                "in it), so it was not deleted. Check level-name in server.properties, or delete the folder on "
                "the Files page. Nothing was changed.",
                "not_a_world",
                409,
            )
        if state != "world":  # empty, or only what an earlier reset carried over: no world in it yet
            plan.skipped.append(dest)
            continue
        plan.present.append((role, dest))
        available = keepable(root, role, dest)
        names = [name for key in keys for name in available.get(key, [])]
        if names:
            plan.carry[role] = names
    found = [dest for _role, dest in plan.present]
    if sorted(expect) != sorted(found):
        now = ", ".join(f"'{dest}'" for dest in found) or "none"
        raise AgentError(
            "The world folders are not the ones that were confirmed for this reset "
            f"(the world folders are now: {now}). Nothing was changed.",
            "world_changed",
            409,
        )
    return plan


def _undo_reset(root: SafeRoot, journal: list[tuple[str, str, str]], aside: str) -> list[str]:
    """Put back what :func:`reset` moved, last step first. Returns what went wrong (empty: all is back)."""
    problems: list[str] = []
    for action, a, b in reversed(journal):
        try:
            if action == "rename":
                root.rename(b, a)
            else:
                root.delete(a)  # rmdir: only ever the empty folder made for the carry-overs
        except Exception as exc:
            problems.append(f"{a}: {exc}")
    if problems:
        return problems
    # Every recorded step is undone, so the aside folder must be empty. If it is not, a step
    # that failed had moved something after all: that part of the world is still in there.
    try:
        left = sorted(e.name for e in root.list(aside))
    except PathError as exc:
        return [] if exc.code == "not_found" else [f"{aside}: {exc}"]
    except OSError as exc:
        return [f"{aside}: {exc}"]
    if left:
        return [f"{aside}: still holds {', '.join(left)}"]
    with contextlib.suppress(PathError, OSError):
        root.delete(aside)
    return []


def reset(
    root: SafeRoot,
    level_name: str,
    layout: str,
    *,
    expect: Iterable[str],
    keep: Iterable[str] = (),
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Delete the world folders of ``level_name``; the server generates a new world on its next start.

    Only folders that :func:`classify` calls a ``world`` are deleted, and only if they are exactly
    the folders in ``expect`` (what the user confirmed). ``keep`` names :data:`RESET_KEEP` options
    that are carried over into a new, otherwise empty world folder. The result lists the folders
    that were ``deleted``, the entries that were ``kept`` and the folders that were ``skipped``
    because they exist but hold no world (``empty`` or ``pending``).

    Every failure is an :class:`AgentError`. The codes ``invalid_level_name``, ``invalid_layout``,
    ``invalid_keep``, ``not_a_directory``, ``not_a_world``, ``world_changed`` and
    ``world_reset_failed`` mean that nothing was changed; ``world_rollback_failed`` means that the
    old world (or a part of it) is in the aside folder named in the message. Once the folders are
    moved aside the reset has happened: a failure to delete them is a warning with ``leftover``.
    (An interrupt such as ``KeyboardInterrupt`` is passed on after everything is put back.)
    """

    def report(value: float, message: str) -> None:
        if progress:
            progress(value, message)

    aside = f"{STAGING_PREFIX}old-{secrets.token_hex(6)}"
    # --- checks: nothing is changed here, and every failure says so ---------------------
    try:
        plan = _reset_plan(root, level_name, layout, expect, keep)
        result: dict = {
            "level_name": plan.level_name,
            "layout": layout,
            "deleted": [],
            "kept": [],
            "skipped": plan.skipped,
            "leftover": None,
            "warnings": [],
        }
        if not plan.present:
            return result
        report(0.05, "Moving the world aside")
        root.mkdir(aside)
    except AgentError:
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            root.delete(aside)  # rmdir: mkdir can fail after it made the (empty) folder
        raise AgentError(
            f"The world could not be checked ({exc}). Nothing was changed.", "world_reset_failed", 500
        ) from exc
    # --- move aside and carry over: any failure is undone -------------------------------
    journal: list[tuple[str, str, str]] = []  # ("rename", original, moved to) | ("mkdir", path, "")
    kept: list[str] = []
    try:
        for role, dest in plan.present:
            root.rename(dest, f"{aside}/{role}")
            journal.append(("rename", dest, f"{aside}/{role}"))
        for role, dest in plan.present:
            if role not in plan.carry:
                continue
            report(0.15, "Carrying over what is kept")
            root.mkdir(dest)
            journal.append(("mkdir", dest, ""))
            for name in plan.carry[role]:
                root.rename(f"{aside}/{role}/{name}", f"{dest}/{name}")
                journal.append(("rename", f"{aside}/{role}/{name}", f"{dest}/{name}"))
                kept.append(f"{dest}/{name}")
    except BaseException as exc:
        problems = _undo_reset(root, journal, aside)
        if problems:
            raise AgentError(
                f"Resetting the world failed ({exc}) and the world could not be put back completely "
                f"({'; '.join(problems)}). It is kept in the folder '{aside}'.",
                "world_rollback_failed",
                500,
            ) from exc
        if not isinstance(exc, Exception):
            raise  # KeyboardInterrupt / SystemExit: everything is back, let it through
        raise AgentError(
            f"The world could not be moved aside ({exc}). Nothing was changed.", "world_reset_failed", 500
        ) from exc
    # --- point of no return: the server folder has no world (or only what was kept) -----
    result.update(deleted=[dest for _role, dest in plan.present], kept=kept)
    report(0.3, "Deleting the old world")
    try:
        root.delete(aside, recursive=True)
    except Exception as exc:  # whatever it is, the reset itself has happened
        result["leftover"] = aside
        result["warnings"].append(
            f"The old world could not be deleted completely ({exc}); what is left of it is in '{aside}'"
        )
    return result
