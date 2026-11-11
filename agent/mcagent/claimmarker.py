"""The claim marker and the start wrapper: a published Satisfactory server starts only with a
verified claim, whoever starts it (the panel, a hand ``docker start``, ``docker restart``, a
daemon restart).

The real-container gate of 2026-11-11 started a published container whose claim was lost (the
settings file removed) by hand: the game answered an InitialAdmin login on its published port for
about 1.6 s before the agent's check loop stopped it. The game writes
``saved/ServerSettings.7777.sav`` before anybody claimed it, so that file proves nothing. So:

* **The marker** ``.mcpanel/claim-verified`` (in the server folder; the panel's file API never
  writes ``.mcpanel/``, and the agent refuses writes onto the marker and the wrapper) records the
  size, ``mtime_ns`` and SHA-256 of the settings file at the moment the agent verified the claim
  through the game's API (an InitialAdmin login is refused, a login with the panel's admin password
  works). It is written only:

  - on a container **without published ports** (the panel's setup: claim, join password, game),
    after such a verification; or
  - on a published container right after an operation of the panel that changes the settings
    file (join password, new game, loading a save: the game writes the file then), when the marker
    matched the file right before that operation and the claim verifies again afterwards.

  It is removed whenever the claim may be lost: the panel's ``mark_unclaimed`` (restores, file
  changes under ``saved/``, the agent's refusals, a claim re-run), every file operation of the
  agent under ``saved/`` and every restore, clone or import, and when the check loop sees the
  settings file change outside such an operation.
* **The start wrapper** ``.mcpanel/start-guard.sh`` is the container's entrypoint
  (``/bin/sh /config/.mcpanel/start-guard.sh``): when the container publishes ports
  (``MCPANEL_PORTS_PUBLISHED`` is not ``0``; the agent sets it at creation, so it is fixed for the
  container's life) it exits with ``REFUSED_EXIT`` (78) unless the marker exists and the settings
  file still has the recorded size, mtime and SHA-256; otherwise it execs the image's own
  entrypoint (``/init.sh``). The game never runs, so its API never answers on a published port. The
  agent writes the wrapper before every create and start it makes (read-only mode); the image's
  ``chown -R /config`` gives it to the runtime uid like every file there, so a process inside the
  container could change it for a later start: the agent writes it again before each of its starts
  and its check loop still probes every published run (defence in depth).

Writes of the marker are serialised per server and carry a generation: a removal in between (a
file operation under ``saved/`` while the agent verified the claim) makes the pending write a no-op,
so a marker never describes a file nobody verified.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .safefs import PathError, SafeRoot

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

MARKER = ".mcpanel/claim-verified"
WRAPPER = ".mcpanel/start-guard.sh"
FOLDER = ".mcpanel"
REFUSED_EXIT = 78  # EX_CONFIG: the start wrapper refused to start the server
PUBLISHED_ENV = "MCPANEL_PORTS_PUBLISHED"
VERSION = 1
MAX_MARKER_BYTES = 4096
MAX_CLAIM_FILE_BYTES = 16 * 1024 * 1024
SETTLE_STABLE = 1.0  # the settings file must stay unchanged this long before it is recorded
SETTLE_MAX = 10.0
CHANGE_GRACE = 15.0  # seconds after an operation of the panel during which a change is its own

_lock = threading.RLock()
_generation: dict[UUID, int] = {}
_changing: dict[UUID, float] = {}  # instance -> monotonic time until which a change is the panel's


def _root(settings: Settings, instance_id: UUID) -> SafeRoot:
    from . import docker_ops

    return SafeRoot(
        docker_ops.instance_dir(settings, instance_id), settings.runtime_uid, settings.runtime_gid
    )


# --- fingerprints ---------------------------------------------------------------------------------------
def fingerprint(settings: Settings, instance_id: UUID, claim_file: str) -> dict[str, Any] | None:
    """Size, mtime_ns and SHA-256 of the settings file (a regular file reached without links), or
    None when there is none."""
    try:
        root = _root(settings, instance_id)
        entry = root.stat(claim_file)
        if entry.type != "file":
            return None
        with root.open_read(claim_file) as (fh, size):
            if size > MAX_CLAIM_FILE_BYTES:
                return None
            digest = hashlib.sha256(fh.read(MAX_CLAIM_FILE_BYTES + 1)).hexdigest()
        # stat again: the recorded size and time belong to the bytes that were hashed
        after = root.stat(claim_file)
    except (PathError, OSError) as exc:
        log.debug("settings file of %s: %s", instance_id, exc)
        return None
    if (after.size, after.mtime_ns) != (entry.size, entry.mtime_ns) or after.size != size:
        return None  # written while it was read: not a state anybody can record
    return {"size": int(after.size), "mtime_ns": int(after.mtime_ns or 0), "sha256": digest}


def _format(claim_file: str, fp: dict[str, Any]) -> bytes:
    lines = [
        "# MCPanel: the claim of this Satisfactory server was verified through its API (node agent).",
        "# The start wrapper refuses a published start unless the settings file still matches.",
        f"version={VERSION}",
        f"claim_file={claim_file}",
        f"size={int(fp['size'])}",
        f"mtime_ns={int(fp['mtime_ns'])}",
        f"sha256={fp['sha256']}",
        f"verified_at={datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
    ]
    return ("\n".join(lines) + "\n").encode("ascii")


def read(settings: Settings, instance_id: UUID) -> dict[str, Any] | None:
    """The recorded fingerprint (``size``, ``mtime_ns``, ``sha256``, ``claim_file``), or None."""
    try:
        raw = _root(settings, instance_id).read_bytes(MARKER, MAX_MARKER_BYTES)
    except (PathError, OSError):
        return None
    values: dict[str, str] = {}
    for line in raw.decode("ascii", "replace").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    try:
        if int(values.get("version", "0")) != VERSION:
            return None
        return {
            "claim_file": values["claim_file"],
            "size": int(values["size"]),
            "mtime_ns": int(values["mtime_ns"]),
            "sha256": values["sha256"].lower(),
        }
    except (KeyError, ValueError):
        return None


def matches(settings: Settings, instance_id: UUID, claim_file: str) -> bool:
    """Whether the marker exists and the settings file still is what it records."""
    recorded = read(settings, instance_id)
    if recorded is None or recorded["claim_file"] != claim_file:
        return False
    current = fingerprint(settings, instance_id, claim_file)
    return current is not None and all(current[k] == recorded[k] for k in ("size", "mtime_ns", "sha256"))


def generation(instance_id: UUID) -> int:
    with _lock:
        return _generation.get(instance_id, 0)


def remove(settings: Settings, instance_id: UUID, reason: str) -> bool:
    """Remove the marker (the claim must be verified again before a published start). Returns
    whether there was one. Never raises: a marker that cannot be removed is logged (the wrapper
    still compares it with the file)."""
    with _lock:
        _generation[instance_id] = _generation.get(instance_id, 0) + 1
        try:
            _root(settings, instance_id).delete(MARKER)
        except PathError as exc:
            if exc.code != "not_found":
                log.error("Could not remove the claim marker of %s (%s): %s", instance_id, reason, exc)
            return False
        except OSError as exc:
            log.error("Could not remove the claim marker of %s (%s): %s", instance_id, reason, exc)
            return False
    log.info("Claim marker of %s removed: %s", instance_id, reason)
    return True


def write(
    settings: Settings, instance_id: UUID, claim_file: str, fp: dict[str, Any], *, since_generation: int
) -> bool:
    """Record ``fp`` as verified, unless the marker was removed after ``since_generation`` (a
    change under ``saved/`` raced the verification) or the file changed again meanwhile."""
    with _lock:
        if _generation.get(instance_id, 0) != since_generation:
            log.warning(
                "Claim of %s verified, but its files changed meanwhile: no marker written", instance_id
            )
            return False
        if fingerprint(settings, instance_id, claim_file) != fp:
            log.warning(
                "The settings file of %s changed while its claim was verified: no marker", instance_id
            )
            return False
        root = _root(settings, instance_id)
        if not root.exists(FOLDER):
            root.mkdir(FOLDER, parents=True)
        root.write_bytes(MARKER, _format(claim_file, fp))
        _chmod(root, MARKER, 0o444)
    log.info("Claim marker of %s written (settings file %d bytes)", instance_id, fp["size"])
    return True


def _chmod(root: SafeRoot, rel: str, mode: int) -> None:
    import os

    parent, _, name = rel.rpartition("/")
    with contextlib.suppress(OSError, PathError), root.dir_fd(parent) as fd:
        os.chmod(name, mode, dir_fd=fd, follow_symlinks=False)


def settle(settings: Settings, instance_id: UUID, claim_file: str) -> dict[str, Any] | None:
    """The settings file's fingerprint once it stayed the same for SETTLE_STABLE seconds (the game
    writes it right after a claim, a new join password, a new game or a load), at most SETTLE_MAX."""
    deadline = time.monotonic() + SETTLE_MAX
    last = fingerprint(settings, instance_id, claim_file)
    since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.25)
        now = fingerprint(settings, instance_id, claim_file)
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= SETTLE_STABLE:
            return now
    return last


# --- operations of the panel that change the settings file ---------------------------------------------
@contextlib.contextmanager
def panel_change(instance_id: UUID) -> Iterator[None]:
    """Mark a change of the settings file as the panel's own (join password, new game, load) for its
    duration plus CHANGE_GRACE: the check loop does not take it for a change from outside."""
    with _lock:
        _changing[instance_id] = float("inf")
    try:
        yield
    finally:
        with _lock:
            _changing[instance_id] = time.monotonic() + CHANGE_GRACE


def in_panel_change(instance_id: UUID) -> bool:
    with _lock:
        until = _changing.get(instance_id)
        if until is None:
            return False
        if time.monotonic() > until:
            del _changing[instance_id]
            return False
        return True


def check_outside_change(settings: Settings, instance_id: UUID, claim_file: str) -> bool:
    """Remove the marker when the settings file no longer matches it and no operation of the panel
    explains the change (an admin in the game changed a setting, a hand edit on the host). Returns
    whether it was removed."""
    if in_panel_change(instance_id):
        return False
    recorded = read(settings, instance_id)
    if recorded is None:
        return False
    if matches(settings, instance_id, claim_file):
        return False
    if in_panel_change(instance_id):  # began while the file was read
        return False
    return remove(
        settings,
        instance_id,
        "the server settings file changed outside an operation of the panel; the claim is verified again "
        "before the next published start",
    )


# --- paths the agent's file operations touch -------------------------------------------------------------
def touches_claim(rel: str, saved_dir: str = "saved") -> bool:
    """``rel`` (relative to the server folder) is the folder itself, ``saved/`` or inside it: a
    change there can lose or replace the claim. (``.mcpanel/`` holds the panel's token file, written
    before every apply; writes onto the marker and the wrapper are refused, see :func:`is_managed`.)"""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return True
    return parts[0] == saved_dir


def is_managed(rel: str) -> bool:
    """The marker and the wrapper: only the agent writes them."""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts) in (MARKER, WRAPPER)


def files_changed(settings: Settings, instance_id: UUID, paths: list[str], why: str) -> None:
    """Remove the marker when one of ``paths`` touches the claim (see :func:`touches_claim`)."""
    if any(touches_claim(p) for p in paths):
        remove(settings, instance_id, f"{why} under saved/")


class GuardedRoot(SafeRoot):
    """The server folder as the agent's file API sees it: writes onto the claim marker or the start
    wrapper are refused (403 ``managed_file``: only the agent writes them), renames into or out of
    ``.mcpanel/`` too (a folder with a forged marker put in its place), and every change under
    ``saved/`` (or of the whole folder) removes the marker first (the claim may be lost)."""

    def __init__(
        self, root: Any, uid: int | None, gid: int | None, *, settings: Settings, instance_id: UUID
    ) -> None:
        super().__init__(root, uid, gid)
        self._settings = settings
        self._instance_id = instance_id

    def _refuse_managed(self, rel: str) -> None:
        if is_managed(rel):
            raise PathError(
                "This file belongs to the node agent (the claim check of the server); it cannot be written",
                "managed_file",
                403,
            )

    def _changed(self, *paths: str) -> None:
        files_changed(self._settings, self._instance_id, list(paths), "a file operation")

    def open_writer(self, rel: str, *, overwrite: bool = True, max_bytes: int | None = None) -> Any:
        self._refuse_managed(rel)
        self._changed(rel)
        return super().open_writer(rel, overwrite=overwrite, max_bytes=max_bytes)

    def mkdir(self, rel: str, *, parents: bool = False) -> None:
        self._refuse_managed(rel)
        self._changed(rel)
        super().mkdir(rel, parents=parents)

    def rename(self, src: str, dst: str, *, overwrite: bool = False) -> None:
        for rel in (src, dst):
            self._refuse_managed(rel)
            parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
            if parts and parts[0] == FOLDER:
                raise PathError(
                    "The .mcpanel folder belongs to the panel and the node agent", "managed_file", 403
                )
        self._changed(src, dst)
        super().rename(src, dst, overwrite=overwrite)

    def delete(self, rel: str, *, recursive: bool = False) -> None:
        # deleting the marker (or .mcpanel/) only removes a verification: the start is refused then
        self._changed(rel)
        super().delete(rel, recursive=recursive)


# --- the start wrapper ---------------------------------------------------------------------------------
def wrapper_script(*, mount: str, claim_file: str, original: tuple[str, ...]) -> bytes:
    """The container's entrypoint (POSIX sh; the image has GNU coreutils' stat and sha256sum)."""
    exec_line = " ".join(_sh_quote(a) for a in original)
    return f"""#!/bin/sh
# MCPanel start guard, written by the node agent before every start it makes (do not edit; it is
# written again). A container with published ports runs the game only when the agent verified its
# claim: {MARKER} must match {claim_file} (size, mtime, SHA-256).
if [ "${{{PUBLISHED_ENV}:-1}}" != "0" ]; then
  marker={mount}/{MARKER}
  claim={mount}/{claim_file}
  refuse() {{
    echo "MCPanel start guard: $1. This server has published ports, so it is not started (exit {REFUSED_EXIT}): whoever reaches an unclaimed Satisfactory server first owns it. Start it from the panel, which verifies the claim on an unpublished container first."
    exit {REFUSED_EXIT}
  }}
  if [ ! -f "$marker" ] || [ -L "$marker" ]; then refuse "the claim was not verified by the panel (no claim marker)"; fi
  if [ ! -f "$claim" ] || [ -L "$claim" ]; then refuse "the server settings file with the claim is missing"; fi
  want_size=$(sed -n 's/^size=//p' "$marker")
  want_mtime=$(sed -n 's/^mtime_ns=//p' "$marker")
  want_sha=$(sed -n 's/^sha256=//p' "$marker")
  got_size=$(stat -c %s "$claim") || refuse "the server settings file cannot be read"
  got_mtime=$(stat -c %.9Y "$claim" | tr -d .) || refuse "the server settings file cannot be read"
  got_sha=$(sha256sum < "$claim" | cut -d' ' -f1) || refuse "the server settings file cannot be read"
  if [ -z "$want_sha" ] || [ "$want_size" != "$got_size" ] || [ "$want_mtime" != "$got_mtime" ] || [ "$want_sha" != "$got_sha" ]; then
    refuse "the server settings file changed since the claim was verified"
  fi
  echo "MCPanel start guard: claim verified by the panel; starting"
fi
exec {exec_line} "$@"
""".encode()


def _sh_quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "/._-" for c in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def write_wrapper(settings: Settings, instance_id: UUID, content: bytes) -> None:
    """Put the start wrapper in place (unchanged content is left alone), read-only."""
    root = _root(settings, instance_id)
    try:
        if root.read_bytes(WRAPPER, len(content) + 1) == content:
            _chmod(root, WRAPPER, 0o444)
            return
    except (PathError, OSError):
        pass
    if not root.exists(FOLDER):
        root.mkdir(FOLDER, parents=True)
    root.write_bytes(WRAPPER, content)
    _chmod(root, WRAPPER, 0o444)


__all__ = [
    "MARKER",
    "PUBLISHED_ENV",
    "REFUSED_EXIT",
    "WRAPPER",
    "GuardedRoot",
    "check_outside_change",
    "files_changed",
    "fingerprint",
    "generation",
    "in_panel_change",
    "is_managed",
    "matches",
    "panel_change",
    "read",
    "remove",
    "settle",
    "touches_claim",
    "wrapper_script",
    "write",
    "write_wrapper",
]
