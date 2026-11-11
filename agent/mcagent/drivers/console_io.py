"""Console input: one sanitised line into a game server's console (design 5.2, 9).

Some game images take commands only on their console. The runtime profile (never a request)
chooses how a line gets there:

* ``exec``: a fixed argv template with exactly one ``{line}`` element (PassiveLemon's Terraria
  image: ``["inject", "{line}"]``, a script that types the line into the server's tmux pane), run
  through Docker's exec API as the user the profile names. This is the **only** exec the agent
  runs: the argv comes from a reviewed profile, the line is one argument (never a shell), and
  nothing of a request chooses the program or its other arguments.
* ``stdin``: the line plus a newline written to the container's stdin through Docker's attach API
  (images whose server reads its stdin: TShock run directly). The container must have been created
  with ``stdin_open`` (the profile's ``stdin_open``).

Every line is sanitised first (:func:`sanitise_line`): surrounding blanks removed, not empty, at
most the profile's length, printable only (no control, format, surrogate or private-use
characters, no line or paragraph separators). With the ``tmux`` guard, lines tmux would not type
literally are refused as well: a single word that tmux reads as a key name (``C-c``, ``Escape``,
``0x03``, ``F1``, ...), a line that starts with ``-`` (an option of ``send-keys``) or ends with
``;`` (tmux's command separator).

Lines of one server are written one at a time (:func:`console_lock`, a per-server lock of its
own: the lifecycle lock is held while a restart stops the server, and the stop may itself write
to the console). The reply of a game console is read from the container log after the write
(:func:`read_output`, bounded), so a command answers with whatever the server printed meanwhile.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
import unicodedata
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from ..errors import AgentError

if TYPE_CHECKING:
    from docker.models.containers import Container

LINE = "{line}"
DEFAULT_MAX_LINE = 256
EXEC_TIMEOUT = 15.0
EXEC_OUTPUT_MAX = 256 * 1024
CONSOLE_LOCK_WAIT = 30.0
OUTPUT_POLL = 0.2
OUTPUT_QUIET = 0.5
OUTPUT_MAX_LINES = 200

# tmux key names (key-string.c), compared case-insensitively; a word that is one of them (with
# optional C-/M-/S-/^ modifiers) is sent by `tmux send-keys` as that key, not as text.
_TMUX_KEY_NAMES = frozenset(
    name.lower()
    for name in (
        "Any", "BSpace", "BTab", "DC", "Delete", "Down", "End", "Enter", "Escape", "Home", "IC",
        "Insert", "Left", "NPage", "PageDown", "PgDn", "PPage", "PageUp", "PgUp", "Right", "Space",
        "Tab", "Up", "None", "FocusIn", "FocusOut", "Paste", "PasteStart", "PasteEnd",
    )
)  # fmt: skip
_TMUX_MODIFIERS = re.compile(r"^(?:[CcMmSs]-|\^)+")
_TMUX_KEY_FAMILIES = re.compile(
    r"^(?:F\d{1,2}|KP.*|Mouse.*|WheelUp.*|WheelDown.*|DoubleClick.*|TripleClick.*|SecondClick.*|"
    r"User\d+|0x[0-9a-f]+)$",
    re.IGNORECASE,
)


# `screen -X stuff` reads ^X as a control key, \ooo and \r as escapes and $NAME / ${NAME} as the session's
# environment (the end-to-end run of 2026-11-11: `$HOME` arrived as /data): refused, never escaped (screen's
# own escaping is not reliable across its versions)
SCREEN_REFUSED = ("^", "\\", "$")
# what screen itself prints to the helper's stdout (not the server's reply): a dead session socket of the
# container's previous run (`docker restart` keeps /run/hkmp/screen)
SCREEN_NOISE = re.compile(r"^Remove dead screens with 'screen -wipe'\.$")
GUARDS = (None, "tmux", "screen")


def refused(reason: str) -> AgentError:
    return AgentError(f"The console line was refused: {reason}", "console_line_refused", 400)


def is_tmux_key(word: str) -> bool:
    """Whether ``tmux send-keys <word>`` would send a key instead of the text ``word``."""
    if not word or any(ch.isspace() for ch in word):
        return False
    rest = _TMUX_MODIFIERS.sub("", word)
    if rest != word:  # a modifier: C-c, M-x, ^C, S-Tab, ...
        return True
    return rest.lower() in _TMUX_KEY_NAMES or bool(_TMUX_KEY_FAMILIES.match(rest))


def sanitise_line(text: str, *, max_len: int = DEFAULT_MAX_LINE, guard: str | None = None) -> str:
    """The line as it is written, or 400 ``console_line_refused``."""
    if not isinstance(text, str):
        raise refused("not text")
    line = text.strip()
    if not line:
        raise refused("it is empty")
    if len(line) > max_len:
        raise refused(f"it is longer than {max_len} characters")
    for ch in line:
        category = unicodedata.category(ch)
        if category[0] == "C" or category in ("Zl", "Zp"):
            raise refused("it contains a control or invisible character")
    if guard == "tmux":
        if line.startswith("-"):
            raise refused("it starts with '-'")
        if line.endswith(";"):
            raise refused("it ends with ';'")
        if is_tmux_key(line):
            raise refused(f"'{line}' would be sent as a key, not as text")
    elif guard == "screen":
        # `screen -X stuff` reads ^X as a control key, \ooo / \r as escapes and $NAME as a variable: HKMP's
        # helper types the line with it, and 'announce x^Mlist' ran a second command (probe of 2026-11-10)
        for ch in SCREEN_REFUSED:
            if ch in line:
                raise refused(
                    f"it contains '{ch}', which the console would read as a key, an escape or a variable"
                )
    elif guard is not None:
        raise ValueError(f"Unknown console guard {guard!r}")
    return line


def validate_exec_template(argv: Sequence[str]) -> tuple[str, ...]:
    """A profile's exec argv: a fixed program, exactly one element that is ``{line}`` and no other
    element that mentions it. Raises ValueError (a profile error, found at import time)."""
    argv = tuple(str(a) for a in argv)
    if not argv or argv[0] in ("", LINE) or argv[0].startswith("-"):
        raise ValueError(f"Invalid console exec argv {argv!r}: it must start with a program")
    if argv.count(LINE) != 1:
        raise ValueError(f"Invalid console exec argv {argv!r}: exactly one element must be {LINE}")
    if any(a != LINE and ("{" in a or "}" in a) for a in argv):
        raise ValueError(f"Invalid console exec argv {argv!r}: no other element may be a template")
    return argv


def exec_argv(template: Sequence[str], line: str) -> list[str]:
    """The argv of one exec: the template with the sanitised line as its ``{line}`` element."""
    return [line if a == LINE else a for a in template]


def _docker_api() -> Any:
    from .. import docker_ops  # imported late: docker_ops imports the drivers

    return docker_ops.client().api


def exec_line(
    c: Container, template: Sequence[str], line: str, *, user: str | None, timeout: float | None = None
) -> str:
    """Run the profile's argv with ``line`` through Docker's exec API (no TTY, no stdin, not
    privileged) and wait for it, at most ``timeout`` seconds (default EXEC_TIMEOUT): 504
    ``console_timeout`` after that (the request ends, and the caller's console lock with it; the
    program may still finish inside the container), 502 ``console_failed`` when it exits non-zero.
    Returns what the program printed (stdout and stderr, at most EXEC_OUTPUT_MAX bytes)."""
    api = _docker_api()
    argv = exec_argv(template, line)
    limit = EXEC_TIMEOUT if timeout is None else timeout
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            created = api.exec_create(
                c.id,
                argv,
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                privileged=False,
                user=user or "",
            )
            box["output"] = api.exec_start(created["Id"], detach=False, tty=False, stream=False)
            box["code"] = (api.exec_inspect(created["Id"]) or {}).get("ExitCode")
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, name="console-exec", daemon=True)
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        raise AgentError(
            f"The server's console did not take the line within {limit:g} s", "console_timeout", 504
        )
    if "error" in box:
        exc = box["error"]
        raise AgentError(f"Could not write to the server's console: {exc}", "console_failed", 502) from exc
    output, code = box.get("output"), box.get("code")
    if isinstance(output, bytes):
        output = output[-EXEC_OUTPUT_MAX:]
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
    if code != 0:
        brief = " ".join(text.split())[:200]
        raise AgentError(
            f"The server's console did not take the line (exit code {code}){': ' + brief if brief else ''}",
            "console_failed",
            502,
        )
    return text


def write_stdin(c: Container, line: str) -> None:
    """Write ``line`` and a newline to the container's stdin through Docker's attach API. 409
    ``console_unavailable`` when the container's stdin is not open."""
    if not ((c.attrs.get("Config") or {}).get("OpenStdin")):
        raise AgentError("This server's console does not take input", "console_unavailable", 409)
    api = _docker_api()
    try:
        sock = api.attach_socket(c.id, params={"stdin": 1, "stdout": 0, "stderr": 0, "stream": 1})
    except Exception as exc:
        raise AgentError(f"Could not attach to the server's console: {exc}", "console_failed", 502) from exc
    raw = getattr(sock, "_sock", sock)
    try:
        raw.sendall((line + "\n").encode("utf-8"))
    except OSError as exc:
        raise AgentError(f"Could not write to the server's console: {exc}", "console_failed", 502) from exc
    finally:
        for closing in (sock, raw):
            with contextlib.suppress(Exception):
                closing.close()


_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


@contextlib.contextmanager
def console_lock(key: str, wait: float = CONSOLE_LOCK_WAIT) -> Iterator[None]:
    """One writer per server console at a time (``key``: the instance id); 409 ``console_busy``."""
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(timeout=wait):
        raise AgentError("Another console command is in progress for this server", "console_busy", 409)
    try:
        yield
    finally:
        lock.release()


_PROGRESS = re.compile(r"^.*:\s*\d{1,3}%$")


def output_lines(text: str) -> list[str]:
    """Console text as lines: split on CR and LF (progress output uses CR), blank lines and
    percentage-progress lines dropped."""
    lines = [line.rstrip() for line in re.split(r"[\r\n]+", text or "")]
    return [line for line in lines if line.strip() and not _PROGRESS.match(line.strip())]


def read_output(
    c: Container, since: float, wait: float, *, redact: Any = None, quiet: float = OUTPUT_QUIET
) -> str:
    """What the container printed after ``since`` (epoch seconds): read until nothing new came for
    ``quiet`` seconds once something came, or ``wait`` seconds in all. Never raises."""
    deadline = time.monotonic() + max(0.0, wait)
    seen = ""
    changed_at: float | None = None
    while True:
        try:
            text = c.logs(stdout=True, stderr=True, timestamps=False, since=since, tail=OUTPUT_MAX_LINES * 4)
            text = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else str(text)
        except Exception:
            break
        now = time.monotonic()
        if text != seen:
            seen, changed_at = text, now
        elif changed_at is not None and now - changed_at >= quiet:
            break
        if now >= deadline:
            break
        time.sleep(OUTPUT_POLL)
    lines = output_lines(seen)[-OUTPUT_MAX_LINES:]
    out = "\n".join(lines)
    return redact(out) if redact is not None and out else out


__all__ = [
    "LINE",
    "console_lock",
    "exec_argv",
    "exec_line",
    "is_tmux_key",
    "output_lines",
    "read_output",
    "refused",
    "sanitise_line",
    "validate_exec_template",
    "write_stdin",
]
