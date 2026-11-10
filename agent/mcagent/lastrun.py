"""The last-run record of a server: what is kept of a container when it is removed.

A stopped server has no container (the panel asks the agent to *retire* it, see
``docker_ops.retire``). Everything the panel used to read from the exited container is saved
here first: exit code, out-of-memory flag, restart count, start and finish time, the last health
output, the image id and the end of the console output. The status, logs and console-stream
endpoints answer from this record while there is no container, and show its console lines in
front of the next run's output until the next record replaces it.

Where it lives: ``<data root>/agent/last-run/<instance id>.json``, in the agent's own state
folder. That is outside every server folder (a server cannot read or change it, and it is never
part of a backup or a clone) and outside the panel database. The folder is 0700 and the files
0600, owned by the user the agent runs as (root in the agent container). Console output can
contain player addresses, chat and passwords typed into the console, so the record is only
served through the endpoints that already serve the container log.

The console lines come from the Minecraft process (third-party code). Nothing here may fail
because of their content: bytes are decoded with replacement, control bytes are removed (colour
sequences are kept, like the web console does), every line and the whole tail are bounded, and
the file is written as ASCII-only JSON.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import stat
import threading
from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from .config import Settings

log = logging.getLogger(__name__)

RECORD_VERSION = 1
# The console tail: at most this many lines, each cut at MAX_LINE_BYTES (the limit of the live
# console stream), MAX_LOG_BYTES of text in total (the oldest lines go first). 2,000 lines is
# what "Load history" asks for and more than the 1,500 the diagnostics read; ordinary server
# output of that length is a few hundred kilobytes.
MAX_LINES = 2000
MAX_LINE_BYTES = 16 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_FIELD_CHARS = 500
# A file larger than anything write() produces is not one of ours: it is ignored, not parsed.
# ASCII-only JSON makes the text at most about three times as long (\uXXXX for non-ASCII
# characters and for the ESC of a colour sequence), so every record stays far below this.
MAX_RECORD_BYTES = 16 * 1024 * 1024
TRUNCATED_SUFFIX = " [mcpanel: line truncated]"

_NAME_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$")
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC
_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")  # every control character but TAB

# Summaries by file identity, so listing every server's record (each reconcile pass of the panel
# asks for them) does not parse the console tails again and again.
_summary_lock = threading.Lock()
_summary_cache: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}


class LastRunError(Exception):
    """The record could not be written."""


# --- paths --------------------------------------------------------------------------------------
def directory(settings: Settings) -> Path:
    return settings.state_dir / "last-run"


def path(settings: Settings, instance_id: UUID) -> Path:
    return directory(settings) / f"{instance_id}.json"


# --- untrusted text -----------------------------------------------------------------------------
def clean_text(text: str) -> str:
    """``text`` without control characters. Colour sequences (``ESC [ <digits;> m``) and TAB stay.

    The same rule as the web console (frontend console-ansi.ts): cursor movement, screen clears,
    OSC/DCS strings (titles, hyperlinks, clipboard), NUL, other C0 and C1 controls and DEL are
    dropped. Never raises.
    """
    if not _CONTROL_RE.search(text):  # nothing to remove: almost every line
        return text
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        code = ord(ch)
        if code == 0x1B:
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "[":
                j = i + 2
                while j < n and 0x30 <= ord(text[j]) <= 0x3F:
                    j += 1
                params_end = j
                while j < n and 0x20 <= ord(text[j]) <= 0x2F:
                    j += 1
                if j < n and 0x40 <= ord(text[j]) <= 0x7E:
                    params = text[i + 2 : params_end]
                    if text[j] == "m" and params_end == j and all(c.isdigit() or c == ";" for c in params):
                        out.append(text[i : j + 1])
                    i = j + 1
                else:
                    i = j  # incomplete sequence: dropped
                continue
            if nxt in ("]", "P", "_", "^"):
                j = i + 2
                while j < n and text[j] != "\x07" and not (text[j] == "\x1b" and text[j + 1 : j + 2] == "\\"):
                    j += 1
                i = n if j >= n else (j + 1 if text[j] == "\x07" else j + 2)
                continue
            i += 2
            continue
        if code == 0x09 or (code >= 0x20 and code != 0x7F and not 0x80 <= code <= 0x9F):
            out.append(ch)
        i += 1
    return "".join(out)


def _field(value: Any) -> str | None:
    """A short, clean string for a record field (None stays None)."""
    if value is None:
        return None
    return clean_text(str(value))[:MAX_FIELD_CHARS]


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def collect_tail(
    chunks: Iterable[bytes],
    *,
    max_lines: int = MAX_LINES,
    max_line_bytes: int = MAX_LINE_BYTES,
    max_bytes: int = MAX_LOG_BYTES,
) -> tuple[list[str], bool]:
    """The last ``max_lines`` lines of a byte stream, cleaned and bounded.

    Returns ``(lines, truncated)``; ``truncated`` says that a line was cut or that older lines
    were dropped to stay inside ``max_bytes``. Memory use is bounded whatever the stream holds:
    a line is never buffered beyond ``max_line_bytes``.
    """
    lines: deque[tuple[str, int]] = deque()
    total = 0
    truncated = False
    buf = bytearray()
    overflow = False  # the current line is longer than max_line_bytes: the rest is skipped

    def finish_line() -> None:
        nonlocal total, truncated, overflow
        text = clean_text(bytes(buf).decode("utf-8", errors="replace").rstrip("\r"))
        if overflow:
            text += TRUNCATED_SUFFIX
            truncated = True
        size = len(text.encode("utf-8", errors="replace"))
        lines.append((text, size))
        total += size
        while lines and (len(lines) > max_lines or total > max_bytes):
            if total > max_bytes:
                truncated = True
            total -= lines.popleft()[1]
        buf.clear()
        overflow = False

    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray)):
            chunk = str(chunk).encode("utf-8", errors="replace")
        pos = 0
        while pos < len(chunk):
            nl = chunk.find(b"\n", pos)
            part = chunk[pos : len(chunk) if nl == -1 else nl]
            if not overflow:
                room = max_line_bytes - len(buf)
                if len(part) > room:
                    buf += part[:room]
                    overflow = True
                else:
                    buf += part
            if nl == -1:
                break
            finish_line()
            pos = nl + 1
    if buf or overflow:
        finish_line()
    return [text for text, _size in lines], truncated


# --- building a record --------------------------------------------------------------------------
def has_run(described: dict[str, Any]) -> bool:
    """Whether the container was ever started (a container that was only created has no run to keep)."""
    return bool(described.get("started_at"))


def build(instance_id: UUID, described: dict[str, Any], lines: list[str], truncated: bool) -> dict[str, Any]:
    """The record for a container, from ``docker_ops.describe`` and its console tail."""
    return {
        "version": RECORD_VERSION,
        "instance_id": str(instance_id),
        "retired_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "container_id": _field(described.get("id")),
        "container_name": _field(described.get("name")),
        "image": _field(described.get("image")),
        "image_id": _field(described.get("image_id")),
        "status": _field(described.get("status")),
        "exit_code": _int(described.get("exit_code")),
        "oom_killed": bool(described.get("oom_killed")),
        "error": _field(described.get("error")),
        "restart_count": _int(described.get("restart_count")) or 0,
        "created_at": _field(described.get("created_at")),
        "started_at": _field(described.get("started_at")),
        "finished_at": _field(described.get("finished_at")),
        "health": _field(described.get("health")),
        "health_output": _field(described.get("health_output")),
        "spec_hash": _field(described.get("spec_hash")),
        "log": {"lines": list(lines), "truncated": bool(truncated)},
    }


def summarize(record: dict[str, Any]) -> dict[str, Any]:
    """The record without its console lines: what the status answer carries."""
    out = {k: v for k, v in record.items() if k not in ("log", "version", "instance_id")}
    console = record.get("log") if isinstance(record.get("log"), dict) else {}
    out["log_lines"] = len(console.get("lines") or [])
    out["log_truncated"] = bool(console.get("truncated"))
    return out


def _when(value: Any) -> str:
    """Docker's ``2026-11-10T03:12:45.123456789Z`` in this agent's time zone (TZ, the same one the
    servers get), like the times in the console lines above it: ``2026-11-09 19:12:45 PST``.
    With TZ unset that is ``2026-11-10 03:12:45 UTC``. Empty when unknown."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", str(value or ""))
    if not m:
        return ""
    try:
        local = (
            datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=UTC)
            .astimezone()
        )
        return f"{local:%Y-%m-%d %H:%M:%S} {local.tzname() or 'UTC'}"
    except (ValueError, OverflowError, OSError):
        return f"{m.group(1)} {m.group(2)} UTC"


def console_lines(record: dict[str, Any]) -> list[str]:
    """The saved console lines, followed by one line that marks where that run ended.

    The marker deliberately does not read "exit code N": the diagnostics look for that phrase in
    what the server itself printed.
    """
    console = record.get("log") if isinstance(record.get("log"), dict) else {}
    lines = [str(line) for line in console.get("lines") or []]
    if not lines:
        return []
    when = _when(record.get("finished_at"))
    how = []
    if record.get("exit_code") is not None:
        how.append(f"exited with code {record['exit_code']}")
    if record.get("oom_killed"):
        how.append("out of memory")
    detail = f" ({', '.join(how)})" if how else ""
    lines.append(f"[mcpanel] ---- end of the run that stopped{' ' + when if when else ''}{detail} ----")
    return lines


# --- storage ------------------------------------------------------------------------------------
def _ensure_directory(settings: Settings) -> Path:
    folder = directory(settings)
    folder.mkdir(parents=True, exist_ok=True)
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode):
        raise LastRunError(f"{folder} is not a directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(folder, 0o700)
    return folder


def write(settings: Settings, instance_id: UUID, record: dict[str, Any]) -> Path:
    """Store the record (replacing the previous one) atomically; raises LastRunError on any failure."""
    try:
        folder = _ensure_directory(settings)
        # ASCII-only JSON: whatever the console printed, the file is plain text
        data = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        final = folder / f"{instance_id}.json"
        tmp = folder / f".{instance_id}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)  # whatever the umask is
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, final)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):  # make the rename durable; not every filesystem allows it
            dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except LastRunError:
        raise
    except Exception as exc:
        raise LastRunError(f"{type(exc).__name__}: {exc}") from exc
    with _summary_lock:
        _summary_cache.pop(str(final), None)
    return final


def _read(file: Path, instance_id: str) -> tuple[dict[str, Any], tuple[int, int, int]] | None:
    """The record in ``file`` and the identity of the file it was read from; None when unusable."""
    try:
        fd = os.open(file, os.O_RDONLY | _FILE_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Cannot open the last-run record %s: %s", file, exc)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECORD_BYTES:
            log.warning("Ignoring the last-run record %s: not a regular file of a plausible size", file)
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            record = json.loads(fh.read(MAX_RECORD_BYTES + 1))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring the unreadable last-run record %s: %s", file, exc)
        return None
    finally:
        os.close(fd)
    if (
        not isinstance(record, dict)
        or record.get("version") != RECORD_VERSION
        or record.get("instance_id") != instance_id
    ):
        log.warning("Ignoring the last-run record %s: not a record of this server", file)
        return None
    console = record.get("log")
    lines = console.get("lines") if isinstance(console, dict) else None
    record["log"] = {
        "lines": [str(line) for line in lines] if isinstance(lines, list) else [],
        "truncated": bool(console.get("truncated")) if isinstance(console, dict) else False,
    }
    return record, (st.st_mtime_ns, st.st_size, st.st_ino)


def load(settings: Settings, instance_id: UUID) -> dict[str, Any] | None:
    """The stored record, or None (no record, or one that cannot be used). Never raises."""
    try:
        found = _read(path(settings, instance_id), str(instance_id))
    except Exception:
        log.exception("Reading the last-run record of %s failed", instance_id)
        return None
    return found[0] if found else None


def _summary_of(file: Path, instance_id: str) -> dict[str, Any] | None:
    try:
        st = os.lstat(file)
    except OSError:
        with _summary_lock:
            _summary_cache.pop(str(file), None)
        return None
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _summary_lock:
        cached = _summary_cache.get(str(file))
        if cached and cached[0] == key:
            return dict(cached[1])
    found = _read(file, instance_id)
    if found is None:
        return None
    summary = summarize(found[0])
    with _summary_lock:
        _summary_cache[str(file)] = (found[1], summary)
    return dict(summary)


def summary(settings: Settings, instance_id: UUID) -> dict[str, Any] | None:
    """The record without its console lines, or None. Never raises."""
    try:
        return _summary_of(path(settings, instance_id), str(instance_id))
    except Exception:
        log.exception("Reading the last-run record of %s failed", instance_id)
        return None


def summaries(settings: Settings) -> dict[str, dict[str, Any]]:
    """Summaries of every stored record by instance id. Never raises."""
    out: dict[str, dict[str, Any]] = {}
    try:
        names = [n for n in os.listdir(directory(settings)) if _NAME_RE.match(n)]
    except OSError:
        return out
    for name in names:
        instance_id = name[: -len(".json")]
        try:
            found = _summary_of(directory(settings) / name, instance_id)
        except Exception:
            log.exception("Reading the last-run record %s failed", name)
            continue
        if found is not None:
            out[instance_id] = found
    return out


def delete(settings: Settings, instance_id: UUID) -> bool:
    """Remove the record (and what an interrupted write left). Returns whether there was one."""
    file = path(settings, instance_id)
    existed = False
    try:
        os.unlink(file)
        existed = True
    except FileNotFoundError:
        pass
    with _summary_lock:
        _summary_cache.pop(str(file), None)
    with contextlib.suppress(OSError):
        for name in os.listdir(directory(settings)):
            if name.startswith(f".{instance_id}.") and name.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    os.unlink(directory(settings) / name)
    return existed
