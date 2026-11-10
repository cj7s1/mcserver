"""Files of a CurseForge / Modrinth modpack that the user disabled, deleted or replaced.

The itzg runtime installs AUTO_CURSEFORGE and MODRINTH packs on every container start. It keeps a
manifest of the files it installed (``/data/.curseforge-manifest.json`` or
``/data/.modrinth-modpack-manifest.json``, ``files[]``: paths relative to /data) and re-installs
the whole pack when one of them is missing ("Re-installing due to missing files from modpack").
Without help, a pack mod the user disabled (renamed to ``.jar.disabled``), deleted or updated to
another file comes back at the next start, and the pack's override files are written over edited
copies.

The panel therefore records the manifest paths it removed (``Instance.modpack_meta
["removed_pack_files"]``, together with the pack release the manifest belongs to) and
:func:`ignore_missing_env` hands them to the runtime as ``CF_IGNORE_MISSING_FILES`` /
``MODRINTH_IGNORE_MISSING_FILES`` (called from ``apps.instances.spec.build_env``). The variable is
part of the container configuration, so the change applies when the container is recreated: the
panel's restart does that for a changed configuration. A record only counts for the pack release
it was made for: after a modpack update the new release's files are installed as the pack ships
them (to keep a mod out across pack updates, use CF_EXCLUDE_MODS).

Only a single jar directly in ``mods/`` or ``plugins/`` is ever recorded (:func:`recordable`):
disabled, deleted or replaced on the Mods tab, or one jar deleted or renamed in the file manager.
A deleted or renamed folder (``mods/`` itself, ``config/``) and config files are never recorded:
the pack must be able to put them back. The Modpack tab lists the record and
:func:`clear` ("Let the pack reinstall them") drops it again.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from apps.core.errors import ApiError
from apps.instances.models import Instance, ServerType

log = logging.getLogger(__name__)

META_KEY = "removed_pack_files"
# (manifest file, its key of the installed release, the runtime variable)
MANIFESTS: dict[str, tuple[str, str, str]] = {
    ServerType.AUTO_CURSEFORGE: (".curseforge-manifest.json", "fileId", "CF_IGNORE_MISSING_FILES"),
    ServerType.MODRINTH: (".modrinth-modpack-manifest.json", "versionId", "MODRINTH_IGNORE_MISSING_FILES"),
}
MAX_RECORDED = 2000
MAX_ENV_CHARS = 30_000  # the agent accepts environment values up to 32 KiB


@dataclass(frozen=True)
class PackManifest:
    release: str  # CF file id / Modrinth version id of the installed pack ("" if the manifest has none)
    files: frozenset[str]


def applies(instance: Instance) -> bool:
    return instance.server_type in MANIFESTS


def read_manifest(instance: Instance, client: Any) -> PackManifest | None:
    """The runtime's install manifest of this pack server; None when there is none (yet) or it is unreadable."""
    if not applies(instance):
        return None
    path, release_key, _var = MANIFESTS[instance.server_type]
    try:
        data = json.loads(client.read_text(instance.id, path)["content"])
    except ApiError as exc:
        if exc.error_code != "not_found":
            log.info("could not read the pack manifest of %s: %s", instance.pk, exc)
        return None
    except ValueError:
        log.info("the pack manifest of %s is not valid JSON", instance.pk)
        return None
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    paths = frozenset(_norm(p) for p in files if isinstance(p, str) and _norm(p)) if isinstance(files, list) else frozenset()
    return PackManifest(release=str(data.get(release_key) or ""), files=paths)


def _norm(path: str) -> str:
    if not path:
        return ""
    path = path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


# the folders whose single jars may be recorded (a pack's server-side jars live directly in them)
JAR_FOLDERS = ("mods", "plugins")


def recordable(path: str) -> bool:
    """A single ``.jar`` directly in ``mods/`` or ``plugins/``: the only kind of path ever recorded."""
    parts = _norm(path).split("/")
    return len(parts) == 2 and parts[0] in JAR_FOLDERS and parts[1].lower().endswith(".jar") and parts[1] != ".jar"


def managed_paths(manifest: PackManifest | None, paths: Iterable[str]) -> list[str]:
    """The recordable manifest paths among ``paths`` (never the files below a folder)."""
    if manifest is None or not manifest.files:
        return []
    return sorted({p for p in (_norm(p) for p in paths) if p and recordable(p) and p in manifest.files})


def update_meta(instance: Instance, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read-modify-write ``Instance.modpack_meta`` under a row lock (other keys written meanwhile
    by another request stay intact); ``instance`` gets the new value."""
    with transaction.atomic():
        current = Instance.objects.select_for_update().filter(pk=instance.pk).values_list("modpack_meta", flat=True).first()
        meta = dict(current or {})
        change(meta)
        Instance.objects.filter(pk=instance.pk).update(modpack_meta=meta)
    instance.modpack_meta = meta
    return meta


def recorded(instance: Instance) -> dict[str, Any]:
    rec = (instance.modpack_meta or {}).get(META_KEY)
    return rec if isinstance(rec, dict) else {}


def _bump(instance: Instance) -> None:
    from apps.instances.services import bump_revision

    bump_revision(instance)


def record_removed(instance: Instance, client: Any, paths: Iterable[str], *, manifest: PackManifest | None = None) -> list[str]:
    """Remember the pack jars among ``paths`` that are gone now (disabled, deleted, replaced, moved).

    Returns the manifest paths that were recorded (empty for files that are not part of the pack,
    for anything but a single jar directly in mods/ or plugins/, and for servers that do not run a
    CurseForge/Modrinth pack).
    """
    paths = [p for p in paths if recordable(p)]
    if not applies(instance) or not paths:
        return []
    manifest = manifest or read_manifest(instance, client)
    found = managed_paths(manifest, paths)
    if not found or manifest is None:
        return []
    added: list[str] = []

    def change(meta: dict[str, Any]) -> None:
        rec = meta.get(META_KEY) if isinstance(meta.get(META_KEY), dict) else {}
        same_release = str(rec.get("release") or "") == manifest.release
        known = list(rec.get("paths") or []) if same_release else []
        for p in found:
            if p not in known:
                known.append(p)
                added.append(p)
        meta[META_KEY] = {"release": manifest.release, "paths": known[-MAX_RECORDED:]}

    update_meta(instance, change)
    if added:
        _bump(instance)
        log.info("recorded %d removed pack file(s) of %s", len(added), instance.pk)
    return found


def forget(instance: Instance, paths: Iterable[str]) -> list[str]:
    """A recorded pack file is back (enabled again, uploaded again): stop ignoring it."""
    wanted = {p for p in (_norm(p) for p in paths) if p}
    if not wanted or not applies(instance) or not wanted & set(recorded(instance).get("paths") or []):
        return []
    removed: list[str] = []

    def change(meta: dict[str, Any]) -> None:
        rec = meta.get(META_KEY) if isinstance(meta.get(META_KEY), dict) else {}
        keep = []
        for p in rec.get("paths") or []:
            (removed if p in wanted else keep).append(p)
        meta[META_KEY] = {**rec, "paths": keep}

    update_meta(instance, change)
    if removed:
        _bump(instance)
    return removed


def clear(instance: Instance) -> dict[str, Any]:
    """Drop the whole record ("Let the pack reinstall them"). Returns the record that was dropped.

    The configuration revision changes with the runtime variable, so the next restart recreates
    the container and the runtime installs the missing pack files again.
    """
    dropped: dict[str, Any] = {}

    def change(meta: dict[str, Any]) -> None:
        rec = meta.pop(META_KEY, None)
        if isinstance(rec, dict):
            dropped.update(rec)

    update_meta(instance, change)
    if dropped.get("paths"):
        _bump(instance)
    return dropped


def ignore_missing_env(instance: Instance, env: dict[str, str]) -> dict[str, str]:
    """``CF_IGNORE_MISSING_FILES`` / ``MODRINTH_IGNORE_MISSING_FILES`` for ``build_env``.

    Merged with a value the user set in the advanced environment (``env``). Empty when nothing
    was recorded, or when the record belongs to another pack release than the pinned one.
    """
    if not applies(instance):
        return {}
    rec = recorded(instance)
    paths = [p for p in rec.get("paths") or [] if isinstance(p, str) and p]
    pinned = str(instance.modpack_version_id or "")
    if not paths or (pinned and rec.get("release") and str(rec["release"]) != pinned):
        return {}
    var = MANIFESTS[instance.server_type][2]
    entries = [line.strip() for line in str(env.get(var) or "").replace(",", "\n").splitlines() if line.strip()]
    for p in paths:
        if p not in entries:
            entries.append(p)
    value = "\n".join(entries)
    while len(value) > MAX_ENV_CHARS and entries:
        entries.pop()
        value = "\n".join(entries)
    return {var: value} if value else {}
