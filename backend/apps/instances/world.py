"""World folder helpers for world import/export (``GET /world``, ``POST /world/replace``) and for
resetting the world (``GET`` / ``POST /world/reset``).

The world folder is ``level-name`` from server.properties (default ``world``), unless the
user set itzg's ``LEVEL`` variable, which itzg writes into server.properties on every
start and therefore wins. Bukkit-based servers (Paper, Purpur, Folia, Spigot) keep the
nether and the end in sibling folders (``world_nether``, ``world_the_end``); every other
server keeps them inside the world folder (``world/DIM-1``, ``world/DIM1``).

The agent re-validates the level name and does the actual work (``agent/mcagent/world.py``);
the checks here only turn mistakes into clear errors before anything is uploaded.

A reset deletes the world folders so the server generates a new world on its next start.
``reset_plan`` works out which folders those are and refuses every setup in which the
folder Minecraft really uses cannot be told for certain (see ``RESET_BLOCKER_STATUS``); the
view, the ``world_reset`` job and the agent each check the folders again.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlencode

from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.nodes.client import AgentClient

from . import properties
from .models import PLUGIN_TYPES, Instance

DEFAULT_LEVEL = "world"
# keep in sync with agent/mcagent/world.py RESERVED_FOLDERS
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

# --- world reset ---------------------------------------------------------------------------
SEED_MODES = ("keep", "random", "custom")
# What a reset can carry over into the new world (keep in sync with agent/mcagent/world.py RESET_KEEP).
RESET_KEEP = ("datapacks", "config")
# Error codes of a failed agent reset after which the world is exactly as it was. Every other
# failure (``world_rollback_failed``, ``internal_error``, a lost or timed-out operation, an
# unreachable agent) leaves the outcome unknown, and the server is then left stopped.
WORLD_RESET_UNTOUCHED = frozenset(
    {
        "must_stop",
        "invalid_level_name",
        "invalid_layout",
        "invalid_keep",
        "not_a_directory",
        "not_a_world",
        "world_changed",
        "world_reset_failed",
    }
)
# HTTP status of the reasons a reset is refused (``reset_plan()["blockers"]``); 409 unless listed.
RESET_BLOCKER_STATUS = {"invalid_level_name": 400, "agent_outdated": 502}
SEED_MAX_LENGTH = 200
# ``settings.world-container`` of bukkit.yml, read line by line (the panel has no YAML parser).
# Blanks only, not ``\s``: an empty value must not swallow the next line as its value.
_WORLD_CONTAINER_RE = re.compile(r"^[ \t]+world-container[ \t]*:[ \t]*(.+)$", re.MULTILINE)
# Server arguments that move the world away from ``level-name`` in the server folder (vanilla:
# --universe / --world; Bukkit: -w / --world / --level-name, -W / --universe / --world-container / --world-dir).
_WORLD_ARG_PREFIXES = ("--universe", "--level-name", "--world")


def validate_level_name(name: str) -> str:
    """Normalise ``level-name``; raise a clear ApiError for names the agent would refuse."""
    raw = name if isinstance(name, str) else ""
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    bad = (
        not raw.strip()
        or "\\" in raw
        or "\x00" in raw
        or len(raw) > 200
        or not parts
        or len(parts) > 4
        or any(p == ".." or p.startswith(".") or any(ord(c) < 32 for c in p) or len(p) > 255 for p in parts)
    )
    if bad:
        raise ApiError(
            f"The world folder name {raw!r} (level-name) is not supported; use a plain folder name such as 'world'",
            code="invalid_level_name",
        )
    if parts[0].lower() in RESERVED_FOLDERS:
        raise ApiError(
            f"level-name is '{raw}', which is a server folder, not a world folder. Fix level-name in server.properties first.",
            code="invalid_level_name",
        )
    return "/".join(parts)


# Sending ``save-all flush`` before a download is a console command: only for users who could run it
# themselves (``instance.command``) or trigger the same flush through a backup (``instance.backup``).
FLUSH_PERMISSIONS = frozenset({"instance.command", "instance.backup"})


def may_flush(user: Any, instance: Instance) -> bool:
    return bool(FLUSH_PERMISSIONS & user.instance_permissions(instance.pk))


def world_folders(level: str, world_layout: str) -> list[str]:
    """The folders a replacement swaps (keep in sync with agent/mcagent/world.py world_paths)."""
    parts = level.split("/")
    out = [level]
    if world_layout == "bukkit":
        out += ["/".join([*parts[:-1], parts[-1] + suffix]) for suffix in ("_nether", "_the_end")]
    return out


def check_archive_location(archive: str, level: str, world_layout: str) -> None:
    """An archive inside a world folder would be deleted with the old world; refuse it early.

    The agent re-checks this; doing it here avoids stopping the server and taking a safety backup first.
    """
    arc = [p for p in archive.replace("\\", "/").split("/") if p not in ("", ".")]
    for folder in world_folders(level, world_layout):
        parts = folder.split("/")
        if arc[: len(parts)] == parts:
            raise ApiError(
                f"The archive is inside the world folder '{folder}', which is replaced (and deleted). "
                "Move it somewhere else in the server folder first.",
                code="archive_in_world",
            )


def level_name(instance: Instance, client: AgentClient) -> tuple[str, str]:
    """(world folder, where it came from: ``env`` | ``server.properties`` | ``default``)."""
    env_level = str((instance.env or {}).get("LEVEL") or "").strip()
    if env_level:
        return env_level, "env"
    try:
        raw = client.read_text(instance.id, "server.properties")["content"]
    except ApiError as exc:
        if exc.error_code not in ("not_found", "not_a_directory"):
            raise
        raw = ""
    value = properties.parse(raw).get("level-name", "").strip()
    return (value, "server.properties") if value else (DEFAULT_LEVEL, "default")


def layout(instance: Instance, client: AgentClient, *, strict: bool = False) -> str:
    """``bukkit`` for Paper-family servers (or any server folder with a bukkit.yml), else ``vanilla``.

    With ``strict`` only the agent's ``not_found`` means "no bukkit.yml"; any other agent error is
    raised instead of being read as "vanilla" (a reset must not guess which folders hold the world).
    """
    if instance.server_type in PLUGIN_TYPES:
        return "bukkit"
    try:
        entry = client.stat_file(instance.id, "bukkit.yml")
    except ApiError as exc:
        if strict and exc.error_code != "not_found":
            raise
        return "vanilla"
    return "bukkit" if entry.get("type") == "file" else "vanilla"


def warnings_for(instance: Instance) -> list[str]:
    env = instance.env or {}
    out = []
    if env.get("WORLD"):
        out.append(
            "The WORLD environment variable is set: the runtime copies that world in on start when the world folder "
            "is missing (or always, with FORCE_WORLD_COPY), which can undo a replaced world."
        )
    return out


def download_url(instance: Instance, paths: list[str], level: str, *, flush: bool = True) -> str:
    """Link to the zip export (reuses ``GET /files/archive``; several folders for bukkit layouts)."""
    name = f"{instance.name}-{level.rsplit('/', 1)[-1]}"
    query = urlencode([*(("path", p) for p in paths), ("name", name), *([("flush", "true")] if flush else [])])
    return f"/api/instances/{instance.id}/files/archive?{query}"


def describe(instance: Instance, client: AgentClient, *, flush: bool = True) -> dict[str, Any]:
    level, source = level_name(instance, client)
    world_layout = layout(instance, client)
    data: dict[str, Any] = {
        "level_name": level,
        "level_name_source": source,
        "layout": world_layout,
        "folders": [],
        "total_bytes": 0,
        "exists": False,
        "leftovers": [],
        "download_url": None,
        "warnings": warnings_for(instance),
        "error": None,
    }
    try:
        validate_level_name(level)
    except ApiError as exc:
        data["error"] = {"code": exc.error_code, "message": str(exc.detail)}
        return data
    try:
        info = client.world_info(instance.id, level, world_layout)
    except ApiError as exc:
        if exc.error_code != "not_found":
            raise
        info = {"folders": [], "total_bytes": 0, "leftovers": []}
    data.update(folders=info["folders"], total_bytes=info["total_bytes"], leftovers=info.get("leftovers", []))
    present = [f["path"] for f in info["folders"] if f.get("exists")]
    data["exists"] = any(f.get("role") == "overworld" and f.get("has_level_dat") for f in info["folders"])
    if present:
        data["download_url"] = download_url(instance, present, level, flush=flush)
    return data


# --- world reset ---------------------------------------------------------------------------
def is_live(status: dict[str, Any]) -> bool:
    """Whether the container runs, or will run again by itself (Docker's restart loop).

    A crash-looping server is "restarting" between two attempts while ``instance.status`` says
    ``failed``, and Docker starts it again at any moment: for a reset that counts as running.
    Always fed with the agent's container status, never with ``instance.status``. Keep in sync
    with the agent's check in its ``world/reset`` route.
    """
    return bool(status.get("running") or status.get("restarting") or status.get("status") == "restarting")


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _read_properties(instance: Instance, client: AgentClient) -> str:
    """The text of server.properties; empty when the file does not exist (yet)."""
    try:
        return client.read_text(instance.id, "server.properties")["content"]
    except ApiError as exc:
        if exc.error_code != "not_found":
            raise
        return ""


def _reset_level(env: dict[str, Any], props: dict[str, str]) -> tuple[str, str, list[dict[str, str]]]:
    """(world folder, where it came from, why it cannot be used) for a reset.

    Stricter than ``level_name``: the value is taken as Java reads it, so spaces around it are
    refused instead of stripped (Minecraft keeps them in the folder name), and a ``level-name``
    key that is present but empty is refused instead of read as "world" (Minecraft then keeps
    the world in the server folder itself). Only a missing key means the default. A ``LEVEL``
    variable that is set but empty is refused for the same reason: the runtime is handed the
    empty value, and what it writes into server.properties on the next start is not known here.
    """
    env_set = env.get("LEVEL") is not None
    env_level = str(env["LEVEL"]) if env_set else ""
    file_level = props.get("level-name")
    if env_set:
        raw, source, where = env_level, "env", "The LEVEL variable"
    elif file_level is not None:
        raw, source, where = file_level, "server.properties", "level-name in server.properties"
    else:
        return DEFAULT_LEVEL, "default", []
    level = raw
    blockers: list[dict[str, str]] = []
    if not raw and source == "env":
        blockers.append(
            _blocker(
                "invalid_level_name",
                "The LEVEL variable is set but empty, so the world folder cannot be told for certain. Remove the "
                "variable, or set it to a folder name such as 'world', first.",
            )
        )
    elif not raw:
        blockers.append(
            _blocker(
                "invalid_level_name",
                "level-name in server.properties is empty. Minecraft then keeps the world in the server folder "
                "itself, which cannot be reset here. Set level-name to a folder name such as 'world' first.",
            )
        )
    elif raw != raw.strip():
        blockers.append(
            _blocker(
                "invalid_level_name",
                f"{where} has a space at its start or end ({raw!r}). Minecraft keeps such spaces in the folder "
                "name, so the world folder cannot be told for certain. Remove them first.",
            )
        )
    else:
        try:
            level = validate_level_name(raw)
        except ApiError as exc:
            blockers.append(_blocker(exc.error_code, str(exc.detail)))
    if env_level and file_level and file_level != env_level:
        blockers.append(
            _blocker(
                "level_name_conflict",
                f"The LEVEL variable says the world folder is '{env_level}', but server.properties still says "
                f"'{file_level}': the server was not started since LEVEL was changed, so its world can be in "
                "either folder. Start the server once so both agree, or fix one of the two.",
            )
        )
    return level, source, blockers


def _yaml_scalar(text: str) -> str:
    """The value of a one-line YAML scalar: quotes and a trailing comment removed."""
    value = text.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()


def _world_location_blockers(instance: Instance, client: AgentClient, world_layout: str) -> list[dict[str, str]]:
    """Settings that move the world away from ``level-name`` in the server folder."""
    out: list[dict[str, str]] = []
    if world_layout == "bukkit":
        try:
            text = client.read_text(instance.id, "bukkit.yml")["content"]
        except ApiError as exc:
            if exc.error_code != "not_found":
                raise
            text = ""
        for match in _WORLD_CONTAINER_RE.finditer(text):
            container = _yaml_scalar(match.group(1))
            if container not in ("", "."):
                out.append(
                    _blocker(
                        "world_location_overridden",
                        f"bukkit.yml sets world-container to '{container}', so the worlds are kept in that folder "
                        "and not where level-name points. Resetting the world is not supported for this setup: "
                        "stop the server and delete the world folders on the Files page instead.",
                    )
                )
                break
    for token in str((instance.env or {}).get("EXTRA_ARGS") or "").split():
        arg = token.strip("\"'")
        if arg.startswith(_WORLD_ARG_PREFIXES) or arg[:2] in ("-w", "-W"):
            out.append(
                _blocker(
                    "world_location_overridden",
                    f"The EXTRA_ARGS variable passes '{arg}' to the server, which changes where the world is kept, "
                    "so the world folder cannot be told for certain. Remove that argument from EXTRA_ARGS, or "
                    "stop the server and delete the world folders on the Files page instead.",
                )
            )
            break
    return out


def _reset_folders(
    instance: Instance, client: AgentClient, level: str, world_layout: str, *, sizes: bool
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    """(world folders with their state, leftover staging folders, why they cannot be reset)."""
    try:
        info = client.world_info(instance.id, level, world_layout, sizes=sizes)
    except ApiError as exc:
        if exc.error_code != "not_found":
            raise
        # no server folder yet (never installed)
        roles = ("overworld", "nether", "end")
        missing = [
            {"role": r, "path": p, "state": "missing"} for r, p in zip(roles, world_folders(level, world_layout), strict=False)
        ]
        info = {"folders": missing, "leftovers": []}
    folders = [
        {
            "role": f.get("role"),
            "path": f.get("path"),
            "state": f.get("state"),
            "markers": list(f.get("markers") or []),
            "keepable": dict(f.get("keepable") or {}),
            "bytes": f.get("bytes") or 0,
            "files": f.get("files") or 0,
            "modified": f.get("modified"),
        }
        for f in info.get("folders") or []
    ]
    blockers: list[dict[str, str]] = []
    if any(f["state"] is None for f in folders):
        # an agent from before this feature answers without "state" (and has no reset route)
        blockers.append(
            _blocker(
                "agent_outdated",
                f"The node agent on '{instance.node.name}' is older than the panel and cannot reset worlds. "
                "Update the agent to the panel's version.",
            )
        )
        return folders, list(info.get("leftovers") or []), blockers
    for f in folders:
        if f["state"] == "not_a_directory":
            blockers.append(
                _blocker(
                    "not_a_directory",
                    f"'{f['path']}' is not a folder (it is a file or a link). Move it away, or fix level-name in "
                    "server.properties.",
                )
            )
        elif f["state"] == "unknown":
            blockers.append(
                _blocker(
                    "not_a_world",
                    f"'{f['path']}' does not look like a Minecraft world (no level.dat, session.lock or region "
                    "folder in it), so it is not deleted. Check level-name in server.properties, or delete the "
                    "folder on the Files page.",
                )
            )
    if not blockers and not any(f["state"] == "world" for f in folders):
        blockers.append(
            _blocker(
                "no_world",
                f"There is no world to reset: '{level}' does not exist or holds no world yet. The server generates "
                "a new world the next time it starts.",
            )
        )
    return folders, list(info.get("leftovers") or []), blockers


def reset_warnings(instance: Instance) -> list[str]:
    """Settings that can bring a world back after a reset instead of a newly generated one."""
    env = instance.env or {}
    out = []
    if env.get("WORLD"):
        out.append(
            "The WORLD environment variable is set: the runtime copies that world in on start when the world folder "
            "is missing, so the server comes back with that world instead of a newly generated one."
        )
    if env.get("CF_SET_LEVEL_FROM"):
        out.append(
            "The CF_SET_LEVEL_FROM environment variable is set: on the next start the modpack installer may put "
            "the world that comes with the modpack back instead of letting the server generate a new one."
        )
    return out


def reset_plan(instance: Instance, client: AgentClient, *, sizes: bool = True) -> dict[str, Any]:
    """What a world reset would delete, and whether it is possible (``GET /world/reset``).

    ``delete`` lists the folders in state ``world``; the request, the job and the agent must all
    agree on that list. ``blockers`` are the reasons a reset is refused (``can_reset`` is false
    then); ``raise_reset_blocker`` turns the first one into an error. ``sizes=False`` skips
    measuring the folders. Agent errors other than "does not exist" are raised, never read as
    "nothing there".
    """
    from apps.backups.models import RetentionPolicy

    env = instance.env or {}
    props = properties.parse(_read_properties(instance, client))
    level, source, blockers = _reset_level(env, props)
    world_layout = layout(instance, client, strict=True)
    blockers += _world_location_blockers(instance, client, world_layout)
    folders: list[dict[str, Any]] = []
    leftovers: list[str] = []
    if not any(b["code"] == "invalid_level_name" for b in blockers):
        folders, leftovers, folder_blockers = _reset_folders(instance, client, level, world_layout, sizes=sizes)
        blockers += folder_blockers
    if "SEED" in env:
        seed = {"value": str(env["SEED"]), "source": "env"}
    elif props.get("level-seed"):
        seed = {"value": props["level-seed"], "source": "server.properties"}
    else:
        seed = {"value": "", "source": "unset"}
    # read, not policy_for(): looking at the plan must not create the retention row
    policy = RetentionPolicy.objects.filter(instance=instance).first()
    keep_days = policy.keep_safety_days if policy else RetentionPolicy._meta.get_field("keep_safety_days").default
    return {
        "level_name": level,
        "level_name_source": source,
        "layout": world_layout,
        "folders": folders,
        "delete": [f["path"] for f in folders if f["state"] == "world"],
        "can_reset": not blockers,
        "blockers": blockers,
        "seed": seed,
        "level_type": str(env.get("LEVEL_TYPE") or props.get("level-type") or ""),
        "running": is_live(client.status(instance.id)),
        "pre_restore_backup_default": bool(SystemSetting.get("pre_restore_backup")),
        "safety_backup_keep_days": keep_days,
        # the safety backup of a reset ignores the server's own exclusions (they could leave the world out)
        "backup_exclusions": len(policy.excludes or []) if policy else 0,
        "leftovers": leftovers,
        "warnings": reset_warnings(instance),
    }


def raise_reset_blocker(plan: dict[str, Any]) -> None:
    """Raise the first reason ``plan`` cannot be carried out (see ``RESET_BLOCKER_STATUS``)."""
    if plan["blockers"]:
        first = plan["blockers"][0]
        raise ApiError(first["message"], code=first["code"], status_code=RESET_BLOCKER_STATUS.get(first["code"], 409))


def validate_seed(value: str) -> str:
    """A custom ``level-seed``: trimmed, 1-200 characters, no line breaks or control characters."""
    seed = value.strip() if isinstance(value, str) else ""
    if not seed:
        raise ApiError("Enter a seed", code="invalid_seed")
    if len(seed) > SEED_MAX_LENGTH:
        raise ApiError(f"A seed can have at most {SEED_MAX_LENGTH} characters", code="invalid_seed")
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in seed):
        raise ApiError("A seed cannot contain line breaks or control characters", code="invalid_seed")
    return seed


def check_seed_env(instance: Instance, seed_mode: str) -> None:
    """409 ``seed_env_set`` when a reset is asked to change the seed although ``SEED`` is set.

    itzg writes ``SEED`` into server.properties on every start, so a seed written by the reset
    would be gone before the new world is generated.
    """
    if seed_mode != "keep" and "SEED" in (instance.env or {}):
        raise ConflictError(
            "The SEED environment variable is set, and the runtime writes it into server.properties on every start, "
            "so a seed chosen here would not be used. Change or remove SEED in the server's environment settings, or "
            "keep the current seed setting.",
            code="seed_env_set",
        )


def write_seed(instance: Instance, client: AgentClient, seed: str) -> str | None:
    """Set ``level-seed`` in server.properties (empty = a random seed), keeping comments and order.

    Returns the previous value ("" if the key was not set), or None when the file already says
    ``seed`` and was left alone.
    """
    raw = _read_properties(instance, client)
    previous = properties.parse(raw).get("level-seed", "")
    if previous == seed:
        return None
    client.write_text(instance.id, "server.properties", properties.update(raw, {"level-seed": seed}))
    return previous


def restore_seed(instance: Instance, client: AgentClient, previous: str) -> None:
    """Put ``level-seed`` back to ``previous`` after a reset that did not happen.

    The file is read again and only this one key is set, so anything else that was edited in
    the meantime stays as it is.
    """
    raw = _read_properties(instance, client)
    if properties.parse(raw).get("level-seed", "") == previous:
        return
    client.write_text(instance.id, "server.properties", properties.update(raw, {"level-seed": previous}))


def verify_world_in_backup(instance: Instance, client: AgentClient, snapshot_id: str, folders: list[dict[str, Any]]) -> None:
    """409 ``safety_backup_incomplete`` unless the snapshot holds every world folder in ``folders``.

    ``folders`` are the ``reset_plan`` entries that are about to be deleted. For each one the
    snapshot must list, directly inside it, every marker (level.dat, region, ...) the agent saw
    in the folder on disk: a backup taken across a mount point, or one that left the world out
    for any other reason, is not a safety backup of the world.
    """
    for folder in folders:
        markers = list(folder.get("markers") or [])
        listed: set[str] = set()
        if snapshot_id:
            entries = client.backup_files(instance.id, snapshot_id, folder["path"]).get("entries") or []
            listed = {str(e.get("name")) for e in entries}
        missing = [m for m in markers if m not in listed]
        if missing or not markers:
            what = f" ({', '.join(missing)} missing from the snapshot)" if missing else ""
            raise ConflictError(
                f"The safety backup does not hold the world folder '{folder['path']}'{what}, so nothing was deleted. "
                "Check the backup's log and where the world folder is stored (for example a separate mount).",
                code="safety_backup_incomplete",
            )
