"""SpigotMC (via Spiget) as a plugin source for Paper, Purpur and Folia servers.

What can be installed, and how (see ``apps/modpacks/spiget.py`` for the verified API behaviour):

* Only free resources whose file is a ``.jar`` hosted on SpigotMC. Premium (paid) and external
  resources, and archives (``.zip``), are listed with a link to SpigotMC instead of an install button.
* Only **Spiget's mirror of the latest file** (``cdn.spiget.org``) can be fetched automatically.
  SpigotMC itself sits behind a Cloudflare browser challenge, and older versions are only on SpigotMC.
  The mirror may lag behind SpigotMC; that is detected from the mirror's storage timestamp and must be
  confirmed by the user, and the version actually installed is read from the jar's ``plugin.yml``.

Why the panel downloads, and not the agent: Spiget publishes no hash of the file, and the agent's
``/files/fetch`` deliberately refuses downloads without a provider-published hash. A hash the panel
computed itself and handed to the agent would only prove that two downloads matched, while widening the
agent's download allow-list. Instead the panel downloads the jar once over HTTPS from ``cdn.spiget.org``
only (no redirects followed), checks it against the SHA-1 the mirror's storage reports, refuses
anything that is not a Bukkit/Paper plugin jar, and streams exactly those bytes to the agent's upload
endpoint together with their SHA-256, which the agent verifies before committing the file. The agent's
download allow-list is unchanged. The trade-off is recorded in ``docs/SECURITY.md``.

Installs are recorded on the instance (``modpack_meta["spigot"][filename]``) because SpigotMC has no
hash lookup; the inventory uses that record to label the file and to offer updates.
"""

from __future__ import annotations

import logging
import re
import zipfile
import zlib
from datetime import UTC, datetime
from typing import IO, Any

from apps.core.errors import ApiError, ConflictError
from apps.instances.models import PLUGIN_TYPES, Instance
from apps.modpacks import spiget
from apps.nodes.client import AgentClient, AgentError

from . import service
from .service import ContentContext

log = logging.getLogger(__name__)

META_KEY = "spigot"
MAX_JAR_BYTES = service.MAX_UPLOAD_BYTES
CONFIRMABLE = ("untested_minecraft_version", "mirror_outdated", "mirror_unknown")
NOTES = [
    "Restart the server to load the plugin.",
    "Plugins are third-party code that runs with the server's full permissions.",
]
# plugin.yml is read line by line (no regular expression over the whole file: a crafted file with a
# long run of blanks made one backtrack for minutes while holding the GIL, freezing the panel).
DESCRIPTOR_MAX_LINES = 2000
DESCRIPTOR_MAX_LINE = 1000
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._+-]+")


# =====================================================================================
# Context
# =====================================================================================
def _context(instance: Instance) -> ContentContext:
    if instance.server_type not in PLUGIN_TYPES:
        raise ApiError(
            "SpigotMC plugins are only available for Paper, Purpur and Folia servers",
            code="content_unsupported",
            status_code=409,
        )
    return service.context_for(instance)  # also refuses servers that follow LATEST


def recorded(instance: Instance) -> dict[str, dict[str, Any]]:
    return dict((instance.modpack_meta or {}).get(META_KEY) or {})


def _save_records(instance: Instance, records: dict[str, dict[str, Any]]) -> None:
    meta = dict(instance.modpack_meta or {})
    meta[META_KEY] = records
    instance.modpack_meta = meta
    instance.save(update_fields=["modpack_meta"])


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _tested_warning(ctx: ContentContext, res: dict[str, Any]) -> dict[str, str] | None:
    tested = res.get("tested_versions") or []
    ok = spiget.tested_for(ctx.minecraft_version, tested)
    if ok:
        return None
    if not tested:
        return _warning(
            "untested_minecraft_version",
            f"The author lists no tested Minecraft versions, so it is unknown whether it works on {ctx.minecraft_version}.",
        )
    shown = ", ".join(tested[-4:])
    return _warning(
        "untested_minecraft_version",
        f"The author has not listed Minecraft {ctx.minecraft_version} as tested (tested: {shown}). It may not work.",
    )


def _mirror_warning(state: str, info: spiget.MirrorInfo | None, latest: dict[str, Any] | None) -> dict[str, str] | None:
    if state == "current":
        return None
    if state == "outdated" and info and info.uploaded_at and latest:
        return _warning(
            "mirror_outdated",
            f"Spiget's copy was stored on {info.uploaded_at:%Y-%m-%d}, before SpigotMC's latest version "
            f"{latest['name']} ({(latest.get('released') or '')[:10]}). An older version will be installed; "
            "newer ones are only available on SpigotMC.",
        )
    return _warning("mirror_unknown", "Could not tell which version Spiget's copy holds; it may be older than the latest.")


def _latest(rid: int) -> dict[str, Any] | None:
    found = spiget.versions(rid, size=1)
    return found[0] if found else None


def _require_installable(res: dict[str, Any]) -> None:
    code, message = spiget.availability(res)
    if code:
        raise ApiError(f"{res['title']}: {message}", code="not_downloadable", details={"reason": code, "url": res["url"]})


def _require_confirmed(warnings: list[dict[str, str]], confirmed: set[str]) -> None:
    missing = [w for w in warnings if w["code"] not in confirmed]
    if missing:
        raise ConflictError(
            " ".join(w["message"] for w in missing) + " Confirm to install anyway.",
            code="confirmation_required",
            details={"warnings": missing},
        )


def normalize_confirm(value: Any) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ApiError("confirm must be a list of warning codes", code="invalid_parameter")
    unknown = sorted(set(value) - set(CONFIRMABLE))
    if unknown:
        raise ApiError(f"Unknown warning codes: {', '.join(unknown)}", code="invalid_parameter")
    return set(value)


# =====================================================================================
# Reads
# =====================================================================================
def _annotate(ctx: ContentContext, res: dict[str, Any], installed_ids: set[int]) -> dict[str, Any]:
    code, message = spiget.availability(res)
    return {
        **res,
        "installable": code is None,
        "reason": code,
        "reason_message": message,
        "tested": spiget.tested_for(ctx.minecraft_version, res.get("tested_versions") or []),
        "installed": res["id"] in installed_ids,
    }


def _present_resource_ids(ctx: ContentContext) -> set[int]:
    """Resources whose recorded jar (or its .disabled copy) is still in the plugins folder."""
    records = recorded(ctx.instance)
    if not records:
        return set()
    try:
        present = {f.filename for f in service.scan(ctx, AgentClient(ctx.instance.node))}
    except ApiError as exc:
        log.info("Could not list plugins of %s: %s", ctx.instance.pk, exc)
        return {int(r["resource_id"]) for r in records.values() if r.get("resource_id")}
    return {int(r["resource_id"]) for n, r in records.items() if r.get("resource_id") and service._name_taken(n, present)}


def search(instance: Instance, query: str, page: int = 1) -> dict[str, Any]:
    ctx = _context(instance)
    result = spiget.search(query, page=page)
    installed = _present_resource_ids(ctx)
    return {
        **result,
        "results": [_annotate(ctx, r, installed) for r in result["results"]],
        "minecraft_version": ctx.minecraft_version,
    }


def _installed_files(ctx: ContentContext, client: AgentClient, rid: int) -> list[dict[str, Any]]:
    files = {f.filename: f for f in service.scan(ctx, client)}
    out = []
    for name, rec in recorded(ctx.instance).items():
        if rec.get("resource_id") != rid:
            continue
        for candidate in (name, f"{name}.disabled"):
            local = files.get(candidate)
            if local is not None:
                out.append({"filename": candidate, "enabled": local.enabled, "sha1": local.sha1, "version": rec.get("version")})
    return out


def details(instance: Instance, resource_id: int) -> dict[str, Any]:
    ctx = _context(instance)
    rid = spiget.validate_resource_id(resource_id)
    res = spiget.resource(rid)
    out = _annotate(ctx, res, set())
    out["author"] = spiget.author_name(res.get("author_id"))
    version_list = spiget.versions(rid)
    latest = version_list[0] if version_list else None
    warnings: list[dict[str, str]] = []
    mirror: dict[str, Any] | None = None
    if out["installable"]:
        tested = _tested_warning(ctx, res)
        if tested:
            warnings.append(tested)
        try:
            info = spiget.mirror_head(rid)
        except ApiError as exc:
            info = None
            mirror = {"available": False, "error": str(exc.detail)}
        if info is None and mirror is None:
            mirror = {"available": False, "error": "Spiget does not hold a copy of this file yet."}
        if info is not None:
            state = spiget.freshness(info, latest)
            mirror = {"available": True, **info.as_dict(), "freshness": state}
            mirror_warning = _mirror_warning(state, info, latest)
            if mirror_warning:
                warnings.append(mirror_warning)
    try:
        installed = _installed_files(ctx, AgentClient(instance.node), rid)
    except ApiError as exc:
        log.info("Could not list plugins of %s: %s", instance.pk, exc)
        installed = []
    out["installed"] = bool(installed)
    mirror_sha1 = (mirror or {}).get("sha1")
    return {
        "resource": out,
        "versions": version_list,
        "latest_version": latest,
        "mirror": mirror,
        "warnings": warnings,
        "installed_files": [{**f, "update_available": bool(mirror_sha1 and f["sha1"] != mirror_sha1)} for f in installed],
        "minecraft_version": ctx.minecraft_version,
    }


# =====================================================================================
# Jar inspection
# =====================================================================================
def _descriptor(fileobj: IO[bytes]) -> dict[str, Any]:
    """Plugin name/version from plugin.yml (top-level keys only; displayed and used for the file name)."""
    fileobj.seek(0)
    try:
        with zipfile.ZipFile(fileobj) as zf:
            names = set(zf.namelist())
            proxy = bool(names & {"bungee.yml", "velocity-plugin.json"}) and not names & {"plugin.yml", "paper-plugin.yml"}
            for candidate in ("paper-plugin.yml", "plugin.yml"):
                if candidate not in names:
                    continue
                info = zf.getinfo(candidate)
                if info.file_size > 256 * 1024:
                    continue
                text = zf.read(info).decode("utf-8", errors="replace").lstrip("﻿")
                keys = _top_level_scalars(text, ("name", "version"))
                return {"name": _yaml_scalar(keys.get("name")), "version": _yaml_scalar(keys.get("version")), "proxy": False}
            return {"name": None, "version": None, "proxy": proxy}
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        KeyError,
        ValueError,
        NotImplementedError,
        RuntimeError,
        zlib.error,
    ):
        return {"name": None, "version": None, "proxy": False}
    finally:
        fileobj.seek(0)


def _top_level_scalars(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """The first value of each of ``keys`` written at the start of a line (``key: value``).

    Linear in the size of the text: at most DESCRIPTOR_MAX_LINES lines are looked at and lines
    longer than DESCRIPTOR_MAX_LINE are skipped.
    """
    found: dict[str, str] = {}
    for count, line in enumerate(text.splitlines()):
        if count >= DESCRIPTOR_MAX_LINES or len(found) == len(keys):
            break
        if len(line) > DESCRIPTOR_MAX_LINE or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip(" \t")
        if key in keys and key not in found and value:
            found[key] = value
    return found


def _yaml_scalar(value: str | None) -> str | None:
    if not value:
        return None
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end > 0 else value[1:]
    else:
        value = value.split(" #", 1)[0]
    value = spiget.clean_text(value, 64)
    return value or None


def _inspect(ctx: ContentContext, download: spiget.MirrorDownload) -> dict[str, Any]:
    platforms, folia = service._inspect_jar(download.file)  # raises invalid_jar for anything that is not a zip
    meta = _descriptor(download.file)
    problems = service._platform_warnings(ctx, platforms, folia)
    if problems:
        hint = " It looks like a BungeeCord/Velocity proxy plugin." if meta["proxy"] else ""
        raise ApiError(problems[0] + hint, code="incompatible_platform", details={"platforms": sorted(platforms)})
    return meta


def _filename(meta: dict[str, Any], res: dict[str, Any]) -> str:
    def part(value: str | None, limit: int) -> str:
        return _UNSAFE_FILENAME.sub("-", value or "").strip("-._+")[:limit].strip("-._+")

    base = part(meta.get("name"), 64) or part(res.get("title"), 64) or f"spigot-{res['id']}"
    version = part(meta.get("version"), 40)
    return service.validate_filename(f"{base}-{version}.jar" if version else f"{base}.jar", allow_disabled=False)


def _record(
    res: dict[str, Any], meta: dict[str, Any], download: spiget.MirrorDownload, latest: dict[str, Any] | None, state: str
) -> dict[str, Any]:
    return {
        "resource_id": res["id"],
        "title": res["title"],
        "version": meta.get("version"),
        "spigot_version": latest.get("name") if latest and state == "current" else None,
        "spigot_version_id": latest.get("id") if latest and state == "current" else None,
        "sha1": download.sha1,
        "sha256": download.sha256,
        "mirror_uploaded_at": download.info.as_dict()["uploaded_at"],
        "installed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _upload(
    client: AgentClient, ctx: ContentContext, download: spiget.MirrorDownload, filename: str, *, overwrite: bool
) -> dict[str, Any]:
    try:
        result = client.upload(
            ctx.instance.id, ctx.path(filename), download.chunks(), overwrite=overwrite, sha256=download.sha256
        )
    except AgentError as exc:
        if exc.agent_status == 409:
            raise ConflictError(f"{filename} already exists in {ctx.directory}/", code="file_exists") from exc
        raise
    if result.get("sha256") is None:
        download.warnings.append("The node agent is older and did not re-check the file's SHA-256; update the agent.")
    elif result["sha256"] != download.sha256:  # an agent that verifies would have refused already
        raise ApiError("The node agent stored different bytes than were sent", code="hash_mismatch", status_code=502)
    return result


# =====================================================================================
# Mutations
# =====================================================================================
def _modrinth_duplicate(ctx: ContentContext, client: AgentClient, sha1: str) -> str | None:
    """Title of an already installed Modrinth project that this exact file belongs to."""
    try:
        release = service.identify([sha1]).get(sha1)
        if not release or release.get("project_id") not in service.installed_project_ids(ctx, client):
            return None
        summary = service.project_summaries([release["project_id"]]).get(release["project_id"]) or {}
        return summary.get("title") or release["project_id"]
    except ApiError as exc:
        log.info("Could not check %s on Modrinth: %s", sha1, exc)
        return None


def install(instance: Instance, resource_id: Any, confirm: set[str] | None = None) -> dict[str, Any]:
    ctx = _context(instance)
    service.ensure_mutable(instance)
    rid = spiget.validate_resource_id(resource_id)
    res = spiget.resource(rid)
    _require_installable(res)
    client = AgentClient(instance.node)
    files = service.scan(ctx, client)
    present = {f.filename for f in files}
    mine = [n for n, r in recorded(instance).items() if r.get("resource_id") == rid and service._name_taken(n, present)]
    if mine:
        raise ConflictError(
            f"{res['title']} is already installed from SpigotMC ({', '.join(mine)}). Use Update to replace it.",
            code="already_installed",
            details={"filenames": mine},
        )
    tested = _tested_warning(ctx, res)
    latest = _latest(rid)
    with spiget.open_mirror(rid) as stream:
        state = spiget.freshness(stream.info, latest)
        warnings = [w for w in (tested, _mirror_warning(state, stream.info, latest)) if w]
        _require_confirmed(warnings, confirm or set())  # refused before the body is downloaded
        download = stream.download(MAX_JAR_BYTES)
    try:
        meta = _inspect(ctx, download)
        same = next((f.filename for f in files if f.sha1 == download.sha1), None)
        if same:
            raise ConflictError(
                f"This exact file is already installed as {same}", code="already_installed", details={"filenames": [same]}
            )
        duplicate_of = _modrinth_duplicate(ctx, client, download.sha1)
        if duplicate_of:
            raise ConflictError(
                f"This file is {duplicate_of} from Modrinth, which is already installed. Use update in the Installed tab.",
                code="already_installed",
            )
        filename = _filename(meta, res)
        if service._name_taken(filename, present):
            raise ConflictError(
                f"A file named {filename} already exists in {ctx.directory}/",
                code="file_exists",
                details={"filenames": [filename]},
            )
        client.mkdir(instance.id, ctx.directory, parents=True)
        result = _upload(client, ctx, download, filename, overwrite=False)
    finally:
        download.close()
        service.invalidate(instance)
    records = recorded(instance)
    records[filename] = _record(res, meta, download, latest, state)
    _save_records(instance, records)
    return {
        "installed": {
            "filename": filename,
            "resource_id": rid,
            "title": res["title"],
            "version": meta.get("version"),
            "latest_version": latest.get("name") if latest else None,
            "mirror": state,
            "size": result.get("size", download.size),
            "sha256": download.sha256,
        },
        "warnings": [w["message"] for w in warnings] + download.warnings,
        "notes": NOTES,
        "restart_required": ctx.restart_required,
    }


def update(instance: Instance, filename: Any, confirm: set[str] | None = None) -> dict[str, Any]:
    """Replace a SpigotMC-installed jar with Spiget's current copy when that copy differs."""
    ctx = _context(instance)
    service.ensure_mutable(instance)
    name = service.validate_filename(filename)
    base = name.removesuffix(".disabled")
    records = recorded(instance)
    rec = records.get(base)
    if not rec or not rec.get("resource_id"):
        raise ApiError(f"{name} was not installed from SpigotMC by the panel", code="not_from_spigot")
    client = AgentClient(instance.node)
    files = {f.filename: f for f in service.scan(ctx, client)}
    current = files.get(name)
    if current is None:
        raise ApiError(f"{name} was not found in {ctx.directory}/", code="file_not_found", status_code=404)
    rid = int(rec["resource_id"])
    res = spiget.resource(rid)
    _require_installable(res)
    latest = _latest(rid)
    tested = _tested_warning(ctx, res)
    up_to_date = {
        "status": "up_to_date",
        "filename": name,
        "version": rec.get("version"),
        "warnings": [],
        "restart_required": False,
    }
    with spiget.open_mirror(rid) as stream:
        state = spiget.freshness(stream.info, latest)
        if stream.info.sha1 and stream.info.sha1 == current.sha1:
            return up_to_date  # decided from the response headers; the body is never downloaded
        warnings = [w for w in (tested, _mirror_warning(state, stream.info, latest)) if w]
        _require_confirmed(warnings, confirm or set())
        download = stream.download(MAX_JAR_BYTES)
    try:
        if download.sha1 == current.sha1:
            return up_to_date
        meta = _inspect(ctx, download)
        new_base = _filename(meta, res)
        new_name = new_base if current.enabled else f"{new_base}.disabled"
        same = new_name == name
        sibling = new_base if new_name != new_base else f"{new_base}.disabled"
        clash = next((n for n in (new_name, sibling) if n in files and n != name), None)
        if clash:
            raise ConflictError(f"{clash} already exists in {ctx.directory}/", code="file_exists", details={"filenames": [clash]})
        result = _upload(client, ctx, download, new_name, overwrite=same)
        if not same:
            try:
                client.delete_files(instance.id, [ctx.path(name)])
            except ApiError as exc:
                download.warnings.append(f"The new version was installed but the old file could not be removed: {exc.detail}")
    finally:
        download.close()
        service.invalidate(instance)
    records.pop(base, None)
    records[new_base] = _record(res, meta, download, latest, state)
    _save_records(instance, records)
    return {
        "status": "updated",
        "filename": name,
        "new_filename": new_name,
        "from_version": rec.get("version"),
        "to_version": meta.get("version"),
        "latest_version": latest.get("name") if latest else None,
        "mirror": state,
        "size": result.get("size", download.size),
        "sha256": download.sha256,
        "warnings": [w["message"] for w in warnings] + download.warnings,
        "restart_required": ctx.restart_required and current.enabled,
    }


def annotate_inventory(instance: Instance, inventory: dict[str, Any]) -> dict[str, Any]:
    rec = recorded(instance)
    if not rec:
        return inventory
    for item in inventory.get("files", []):
        base = item["filename"].removesuffix(".disabled")
        entry = rec.get(base)
        # a file Modrinth identifies keeps that source: Modrinth then also handles its updates
        if entry and item.get("source") != "modrinth" and item.get("sha1") and item.get("sha1") == entry.get("sha1"):
            item["source"] = "spigot"
            item["spigot"] = {
                "resource_id": entry.get("resource_id"),
                "title": entry.get("title"),
                "version": entry.get("version"),
                "url": spiget.resource_url(int(entry["resource_id"])) if entry.get("resource_id") else None,
            }
    summary = inventory.get("summary") or {}
    summary["manual"] = sum(1 for i in inventory.get("files", []) if i.get("source") == "manual")
    summary["spigot"] = sum(1 for i in inventory.get("files", []) if i.get("source") == "spigot")
    return inventory
