"""Worlds that are folders: the saves model of Palworld, Valheim, Project Zomboid and Rust (contract B2).

One class, configured per definition (``FolderSaves(definition, folder=..., markers=...)``); what is
game-specific is asked from the definition:

* ``world_paths(client, instance, purpose, params)``: the paths (relative to the server folder) that
  make up the current world. ``purpose`` is ``"reset"`` (what a new world replaces: Rust's map and
  saves, plus its blueprints when asked) or ``"import"`` / ``"export"`` (the whole world folder).
* ``world_target(client, instance, root_name, params)``: where an imported world folder goes, and
  ``after_import(client, instance, target, params)`` (Palworld: the settings name the world; Valheim
  with the old format: the world name follows the files).
* ``prepare_world_reset(client, instance, values, seed)``: what the new world is made from (Rust: a new
  seed; Palworld: the settings forget the old world's id); returns the seed (or "") for the log.
* ``save_now(client, instance)``: make a running server write its world (export, a running import or
  reset saves first through the safety backup anyway).

The jobs reuse ``saves_reset`` / ``saves_import`` (``game_handlers._replace_save``): stop, safety
backup, move the current world into ``saves-replaced/<UTC stamp>/`` (kept in the server folder, so it
is in backups and can be imported again), put the new world in place, start again when it was running.

An import is a zip of a world folder. The panel checks it before it is uploaded: the files that make a
world of the game (``markers``) in the zip's top folder or one folder below it, no links, no absolute
or ``..`` paths, at most ``max_bytes`` uncompressed and ``max_entries`` entries. The job extracts it
into a staging folder of the server (``.mcpanel-import-<hex>.d``, left out of backups), moves the
world folder into place and deletes the rest. An export is a zip of the world folder built by the agent
(a running server saves first when it can).
"""

from __future__ import annotations

import re
import secrets as _secrets
import stat
import zipfile
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError, ConflictError

from .base import ConfigFile

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.jobs.models import Job
    from apps.jobs.runner import JobContext
    from apps.nodes.client import AgentClient

SET_ASIDE_DIR = "saves-replaced"
MAX_WORLD_BYTES = 16 * 1024**3
MAX_ENTRIES = 200_000
MEASURE_LISTINGS = 64  # folder listings spent on measuring one world (sizes are a lower bound beyond)


def _entries(client: AgentClient, instance: Instance, path: str) -> list[dict[str, Any]]:
    try:
        answer = client.list_files(instance.id, path)
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return []
        raise
    return list(answer.get("entries") or [])


def _stat(client: AgentClient, instance: Instance, path: str) -> dict[str, Any] | None:
    try:
        return client.stat_file(instance.id, path)
    except ApiError as exc:
        if exc.error_code in ("not_found", "not_a_directory"):
            return None
        raise


class FolderSaves:
    """A world that is a folder (or a few files) of the server folder; see the module docstring."""

    kind = "folder"
    extension = ".zip"
    content_type = "application/zip"
    export_endpoint = "archive"

    def __init__(
        self,
        definition: Any,
        *,
        folder: str,
        markers: tuple[str, ...],
        marker_help: str,
        max_bytes: int = MAX_WORLD_BYTES,
        max_entries: int = MAX_ENTRIES,
        export_exclude: tuple[str, ...] = (),
    ) -> None:
        self.definition = definition
        self.folder = folder  # the folder that holds the worlds (clones "without the worlds" leave it out)
        self.markers = tuple(re.compile(m) for m in markers)  # file names that make a world folder
        self.marker_help = marker_help
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.export_exclude = export_exclude  # names inside the world folder an export leaves out

    # -- description -------------------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "extension": self.extension,
            "folder": self.folder,
            "set_aside_folder": SET_ASIDE_DIR,
            "reset_fields": [dict(f) for f in self.definition.world_reset_fields()],
            "import_help": self.marker_help,
            "max_import_bytes": self.max_bytes,
        }

    def reset_config(self, instance: Instance) -> ConfigFile:
        return ConfigFile(id="new-world", path="", codec="json", label="New world", fields=self.definition.world_reset_fields())

    def current(self, instance: Instance) -> str:
        return str(self.definition.world_label(instance))

    def current_path(self, instance: Instance) -> str:
        return self.folder

    # -- listing -----------------------------------------------------------------------------------
    def _measure(self, client: AgentClient, instance: Instance, path: str) -> dict[str, Any]:
        """Size and newest change of a file or folder (a bounded walk: ``complete`` False beyond it)."""
        top = _stat(client, instance, path)
        if top is None:
            return {"exists": False, "size": 0, "modified": None, "complete": True, "type": None}
        if top.get("type") != "directory":
            return {"exists": True, "size": top.get("size"), "modified": top.get("modified"), "complete": True, "type": "file"}
        size, newest, complete = 0, top.get("modified") or 0, True
        pending, listings = [path], 0
        while pending:
            if listings >= MEASURE_LISTINGS:
                complete = False
                break
            current = pending.pop()
            listings += 1
            try:
                listing = client.list_files(instance.id, current)
            except ApiError as exc:
                if exc.error_code not in ("not_found", "not_a_directory"):
                    raise
                listing = {}
            if listing.get("truncated"):
                complete = False  # (B-6: a folder with more entries than one listing holds: a lower bound)
            for entry in listing.get("entries") or []:
                if entry.get("type") == "directory":
                    pending.append(f"{current}/{entry.get('name')}")
                elif entry.get("type") == "file":
                    size += int(entry.get("size") or 0)
                    newest = max(newest, entry.get("modified") or 0)
        return {"exists": True, "size": size, "modified": newest or None, "complete": complete, "type": "directory"}

    def list(self, client: AgentClient, instance: Instance) -> dict[str, Any]:
        current = self.definition.world_paths(client, instance, "export", {})
        saves = []
        for path in current:
            m = self._measure(client, instance, path)
            if not m["exists"]:
                continue
            saves.append(
                {
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "type": m["type"],
                    "size": m["size"],
                    "size_complete": m["complete"],
                    "modified": m["modified"],
                    "current": True,
                }
            )
        set_aside = []
        for folder in _entries(client, instance, SET_ASIDE_DIR):
            if folder.get("type") != "directory":
                continue
            names = [str(e.get("name")) for e in _entries(client, instance, f"{SET_ASIDE_DIR}/{folder.get('name')}")]
            set_aside.append({"folder": f"{SET_ASIDE_DIR}/{folder.get('name')}", "files": sorted(names)})
        set_aside.sort(key=lambda f: f["folder"], reverse=True)
        answer: dict[str, Any] = {
            "current": current[0].rsplit("/", 1)[-1] if current else self.current(instance),
            "saves": saves,
            "set_aside": set_aside,
            "options": {k: v for k, v in (instance.game_options or {}).items() if not isinstance(v, dict)},
            "reset_fields": self.describe()["reset_fields"],
            "import_help": self.marker_help,
        }
        if notes := self.definition.world_notes(instance):
            answer["notes"] = notes
        return answer

    # -- export ------------------------------------------------------------------------------------
    def export(self, client: AgentClient, instance: Instance, *, save_first: bool) -> tuple[str, str, bool]:
        """(the world folder, the download name, whether the server saved first)."""
        saved = False
        if save_first:
            try:
                saved = bool(self.definition.save_now(client, instance))
            except ApiError:
                saved = False  # not answering: the files as they are on disk
        paths = self.definition.world_paths(client, instance, "export", {})
        folder = next((p for p in paths if (_stat(client, instance, p) or {}).get("type") == "directory"), None)
        if folder is None:
            raise ApiError("The server has no world yet (it is created at the first start)", code="not_found", status_code=404)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{instance.name}-{folder.rsplit('/', 1)[-1]}").strip("_")[:120]
        return folder, f"{name or 'world'}.zip", saved

    def export_excludes(self, instance: Instance, path: str) -> list[str]:
        return [f"{path}/{name}" for name in self.export_exclude]

    # -- import: the upload ----------------------------------------------------------------------------
    def _inspect(self, upload: Any) -> tuple[str | None, dict[str, Any]]:
        """(why the zip is refused or None, {"root": the world folder inside the zip ("" = its top)})."""
        name = str(getattr(upload, "name", "") or "")
        if not name.lower().endswith(".zip"):
            return "Upload the world as a .zip file (a zip of the world folder)", {}
        try:
            upload.seek(0)
            with zipfile.ZipFile(upload) as zf:
                infos = zf.infolist()
        except (zipfile.BadZipFile, OSError, ValueError):
            return "The file is not a zip archive", {}
        finally:
            try:
                upload.seek(0)
            except (OSError, ValueError):
                pass
        if len(infos) > self.max_entries:
            return f"The zip has more than {self.max_entries} entries", {}
        total = 0
        roots: dict[str, int] = {}
        for info in infos:
            path = info.filename.replace("\\", "/")
            parts = [p for p in path.split("/") if p not in ("", ".")]
            if path.startswith("/") or ".." in parts or (parts and re.match(r"^[A-Za-z]:$", parts[0])):
                return f"The zip has an unsafe path: {path[:200]}", {}
            mode = (info.external_attr >> 16) & 0o170000
            if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                return f"The zip has a link or a special file ({path[:200]}); only plain files and folders are allowed", {}
            total += max(info.file_size, 0)
            if total > self.max_bytes:
                return f"The world is larger than {self.max_bytes // 1024**3} GB unpacked", {}
            if info.is_dir() or not parts:
                continue
            if any(m.fullmatch(parts[-1]) for m in self.markers):
                root = "/".join(parts[:-1])
                roots[root] = roots.get(root, 0) + 1
        # the world folder: the top of the zip or one folder below it (a zip of the folder itself)
        candidates = sorted((r for r in roots if r.count("/") < 1), key=lambda r: (r != "", r))
        if not candidates:
            return f"The zip does not look like a {self.definition.label} world: {self.marker_help}", {}
        if len(candidates) > 1 and "" not in candidates:
            return "The zip holds more than one world folder; upload one world at a time", {}
        # the game's own rules for the files of a world (Valheim: one old-format world with an allowed name),
        # checked before the upload is accepted (E-3: they used to be checked after the world was moved aside)
        problem = getattr(self.definition, "world_files_problem", None)
        if problem is not None:
            root = candidates[0]
            files = []
            for info in infos:
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p not in ("", ".")]
                if parts and not info.is_dir() and "/".join(parts[:-1]) == root:
                    files.append(parts[-1])
            if (why := problem(files)) is not None:
                return why, {}
        return None, {"root": candidates[0]}

    def validate_upload(self, upload: Any) -> str | None:
        return self._inspect(upload)[0]

    def upload_params(self, upload: Any) -> dict[str, Any]:
        """What the import job needs to know about the upload: the world folder inside the zip."""
        return {"root": self._inspect(upload)[1].get("root", "")}

    # -- the jobs -----------------------------------------------------------------------------------
    def set_aside_paths(self, client: AgentClient, instance: Instance, stamp: str, paths: list[str]) -> list[str]:
        """Move ``paths`` (those that exist) into saves-replaced/<stamp>/ under their own names."""
        existing = [p for p in paths if _stat(client, instance, p) is not None]
        if not existing:
            return []
        target = f"{SET_ASIDE_DIR}/{stamp}"
        client.mkdir(instance.id, target, parents=True)
        moved = []
        for path in existing:
            name = path.rsplit("/", 1)[-1]
            client.rename(instance.id, path, f"{target}/{name}", timeout=600)
            moved.append(name)
        return moved

    def set_aside(self, client: AgentClient, instance: Instance, stamp: str) -> list[str]:
        return self.set_aside_paths(client, instance, stamp, self.definition.world_paths(client, instance, "reset", {}))

    def run_reset(self, job: Job, ctx: JobContext) -> dict[str, Any]:
        from apps.instances.game_handlers import _replace_save

        params = job.params or {}
        values = dict(params.get("settings") or {})
        chosen: dict[str, Any] = {}

        def set_aside(client: AgentClient, instance: Instance, stamp: str) -> list[str]:
            paths = self.definition.world_paths(client, instance, "reset", values)
            return self.set_aside_paths(client, instance, stamp, paths)

        def check(client: AgentClient, instance: Instance) -> None:
            # the request's values are checked before anything is changed (E-3: Rust's seed bound, Valheim's name)
            game_check = getattr(self.definition, "check_world_reset", None)
            if game_check is not None:
                game_check(instance, values, params.get("seed"))

        def place(client: AgentClient, instance: Instance) -> None:
            seed = self.definition.prepare_world_reset(client, instance, values, params.get("seed"))
            chosen["seed"] = seed
            ctx.log(f"The server creates a new world at its next start{f' (seed {seed})' if seed else ''}")

        result = _replace_save(job, ctx, what="resetting the world", place=place, set_aside=set_aside, check=check)
        result["seed"] = chosen.get("seed")
        return result

    def run_import(self, job: Job, ctx: JobContext) -> dict[str, Any]:
        from apps.instances import secret_files
        from apps.instances.game_handlers import _replace_save
        from apps.instances.handlers import IMPORT_ARCHIVE_PREFIX, drop_import_archive
        from apps.instances.handlers import _instance as job_instance
        from apps.nodes.client import AgentClient as Client

        params = job.params or {}
        archive = str(params.get("path") or "")
        root = str(params.get("root") or "").strip("/")
        if root and (".." in root.split("/") or root.startswith("/")):
            raise ApiError("Invalid world folder in the upload", code="invalid_path")
        staging = f"{IMPORT_ARCHIVE_PREFIX}{_secrets.token_hex(6)}.d"
        placed: dict[str, Any] = {}

        def set_aside(client: AgentClient, instance: Instance, stamp: str) -> list[str]:
            paths = self.definition.world_paths(client, instance, "import", params)
            return self.set_aside_paths(client, instance, stamp, paths)

        def prepare(client: AgentClient, instance: Instance) -> None:
            # unpacked into the staging folder and checked while the current world is still in place (E-3: a
            # full disk or a world the game refuses used to fail after the world was moved aside); the agent
            # unpacks only into a stopped server, so this runs after the stop
            protected = secret_files.require_extract_support(instance, client)
            op = client.extract(
                instance.id,
                archive,
                staging,
                strip="none",
                overwrite=False,
                delete_archive=True,
                **({"protected": protected} if protected else {}),
            )
            done = client.wait_operation(op["id"], timeout=3 * 3600, on_progress=ctx.agent_progress(0.62, 0.03))
            result = done.get("result") or {}
            if result.get("skipped_count"):
                ctx.log(f"{result['skipped_count']} unsafe entries of the zip were skipped (links, special files)")
            source = f"{staging}/{root}" if root else staging
            if _stat(client, instance, source) is None:
                raise ConflictError("The uploaded world folder was not found after unpacking", code="invalid_world")
            problem = getattr(self.definition, "world_files_problem", None)
            if problem is not None:
                files = [str(e.get("name")) for e in _entries(client, instance, source) if e.get("type") == "file"]
                if (why := problem(files)) is not None:
                    raise ApiError(why, code="invalid_world")
            placed["source"] = source

        def place(client: AgentClient, instance: Instance) -> None:
            source = placed["source"]
            target = self.definition.world_target(client, instance, root.rsplit("/", 1)[-1] if root else "", params)
            parent = target.rsplit("/", 1)[0] if "/" in target else ""
            if parent:
                client.mkdir(instance.id, parent, parents=True)
            target = self.definition.place_world(client, instance, source, target) or target
            placed["target"] = target
            self.definition.after_import(client, instance, target, params)
            ctx.log(f"The uploaded world is now {target}")

        try:
            result = _replace_save(job, ctx, what="importing a world", place=place, set_aside=set_aside, prepare=prepare)
        except Exception:
            instance = job_instance(job)
            drop_import_archive(Client(instance.node), instance, archive)
            raise
        finally:
            instance = job_instance(job)
            _drop_staging(Client(instance.node), instance, staging)
        result["world"] = placed.get("target")
        return result


def _drop_staging(client: AgentClient, instance: Instance, path: str) -> None:
    """Delete the import's staging folder (only the panel's own, by its name)."""
    from apps.instances.handlers import IMPORT_ARCHIVE_PREFIX

    if not path.startswith(IMPORT_ARCHIVE_PREFIX) or "/" in path:
        return
    try:
        client.delete_files(instance.id, [path], True, timeout=600)
    except ApiError as exc:
        if exc.error_code != "not_found":
            import logging

            logging.getLogger(__name__).warning("could not delete the import folder %s of %s: %s", path, instance.pk, exc)


__all__ = ["SET_ASIDE_DIR", "FolderSaves"]
