"""``console``: a control driver for games that take commands on their console only.

Terraria in PassiveLemon's image (vanilla and tModLoader) runs the server in a tmux pane; its
stdin is a bash script's, so lines go in through the image's ``inject`` script (``exec``
transport, argv fixed by the profile). TShock run directly reads its stdin (``stdin``
transport). The primitives, their sanitising and the per-server console lock are in
:mod:`mcagent.drivers.console_io`.

Data a profile gives (``control_options``):

* ``transport``: ``exec`` (with ``exec_argv``, e.g. ``("inject", "{line}")``, and
  ``exec_user_env``, the variables naming the uid and gid the server runs as, e.g.
  ``("PUID", "PGID")``) or ``stdin``;
* ``guard``: ``tmux`` for consoles typed through ``tmux send-keys`` (see console_io);
* ``max_line_len``;
* ``save_command`` and how the end of a save shows: ``save_done_log`` (a regular expression of
  the log line, vanilla: ``Backing up world file``) or ``save_done_file`` (a glob below the server
  folder whose file must be written after the command, tModLoader: ``Worlds/*.twld``, whose save
  prints no line at its end); ``save_timeout``;
* ``broadcast``: the command template of an in-game message (``say {text}``);
* ``output_wait``: how long a command waits for the server's reply in the log.

A command answers with what the server printed after the line was written (bounded, so it may
include unrelated lines, or nothing yet). The stop is the image's own signal handling (a plain
``docker stop``): PassiveLemon's script saves on SIGTERM (exit 143), TShock on SIGINT.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from ..errors import AgentError
from . import console_io
from .base import ControlDriver, Report, Send, WaitLog

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings

log = logging.getLogger(__name__)

SAVE_LOG_SLACK = 0.2
SAVE_FILE_SETTLE = 0.5  # a save file must keep its mtime this long before it counts as written
SAVE_FILE_POLL = 0.25


def _ops() -> Any:
    from .. import docker_ops  # imported late: docker_ops imports the drivers

    return docker_ops


class ConsoleChannel:
    """The ``send`` of the saving pause for a console driver (``ControlDriver.channel``): writes one
    command to the running container's console. It also knows the server folder, so a save can
    wait for its file (:meth:`wait_file`)."""

    def __init__(self, driver: ConsoleDriver, settings: Settings, instance_id: UUID) -> None:
        self.driver = driver
        self.settings = settings
        self.instance_id = instance_id

    def container(self) -> Container:
        return _ops().running_container(self.instance_id)

    def __call__(self, command: str, timeout: float) -> None:
        self.driver.write(self.container(), command)

    def wait_file(self, pattern: str, since: float, timeout: float) -> bool:
        return self.driver.wait_file(self.settings, self.instance_id, pattern, since, timeout)

    def wait_log(self, pattern: str, since: float, timeout: float) -> bool:
        return bool(_ops().wait_for_log(self.container(), pattern, since, timeout))


class ConsoleDriver(ControlDriver):
    id: ClassVar[str] = "console"

    def __init__(
        self,
        *,
        transport: str,
        exec_argv: Sequence[str] | None = None,
        exec_user_env: Sequence[str] | None = None,
        guard: str | None = None,
        max_line_len: int = console_io.DEFAULT_MAX_LINE,
        save_command: str | None = "save",
        save_done_log: str | None = None,
        save_done_file: str | None = None,
        save_timeout: float = 120.0,
        broadcast: str | None = "say {text}",
        output_wait: float = 1.5,
        exec_reply: str | None = None,
        refused_lines: Sequence[str] = (),
    ) -> None:
        if transport not in ("exec", "stdin"):
            raise ValueError(f"Unknown console transport {transport!r}")
        if transport == "exec":
            if exec_argv is None:
                raise ValueError("An exec console needs exec_argv")
            self.exec_argv: tuple[str, ...] | None = console_io.validate_exec_template(exec_argv)
            if exec_user_env is not None and len(tuple(exec_user_env)) != 2:
                raise ValueError("exec_user_env names the uid and the gid variable")
        else:
            self.exec_argv = None
        if guard not in console_io.GUARDS:
            raise ValueError(f"Unknown console guard {guard!r}")
        if exec_reply not in (None, "stdout") or (exec_reply is not None and transport != "exec"):
            raise ValueError(
                "exec_reply is 'stdout' (an exec console whose program prints the reply) or None"
            )
        if not 1 <= int(max_line_len) <= 4096:
            raise ValueError("max_line_len must be between 1 and 4096")
        if save_done_file is not None and (
            save_done_file.startswith("/") or ".." in save_done_file.split("/")
        ):
            raise ValueError(f"Invalid save_done_file {save_done_file!r}")
        if broadcast is not None and broadcast.count("{text}") != 1:
            raise ValueError("The broadcast template needs exactly one {text}")
        self.transport = transport
        self.exec_user_env = tuple(exec_user_env) if exec_user_env else None
        self.guard = guard
        self.max_line_len = int(max_line_len)
        self.save_command = save_command
        self.save_done_log = save_done_log
        self.save_done_file = save_done_file
        self.save_timeout = float(save_timeout)
        self.broadcast_template = broadcast
        self.output_wait = float(output_wait)
        # "stdout": the exec program prints the server's reply itself (HKMP's helper returns what
        # the server appended to its log), so that is the reply; None: read the container log
        self.exec_reply = exec_reply
        # whole lines that are refused (HKMP's helper runs its own subcommands, e.g. ``check``
        # asks GitHub, instead of typing them into the console)
        self.refused_lines = frozenset(refused_lines)

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> ConsoleDriver:
        return cls(**dict(options))

    # --- writing a line ----------------------------------------------------------------------------
    def sanitise(self, text: str) -> str:
        line = console_io.sanitise_line(text, max_len=self.max_line_len, guard=self.guard)
        if line in self.refused_lines:
            raise console_io.refused(f"'{line}' is not a console command of this server")
        return line

    def exec_user(self, c: Container) -> str | None:
        """``uid:gid`` the exec runs as: the values of ``exec_user_env`` in the container's own
        environment (the user the image switched to); None without them (the image's user)."""
        if not self.exec_user_env:
            return None
        env = {}
        for item in (c.attrs.get("Config") or {}).get("Env") or []:
            key, _, value = str(item).partition("=")
            env[key] = value
        uid, gid = (env.get(k, "") for k in self.exec_user_env)
        if not (uid.isdigit() and gid.isdigit()):
            raise AgentError(
                "The server's container does not say which user runs its console", "console_unavailable", 409
            )
        return f"{uid}:{gid}"

    def write(self, c: Container, text: str) -> str:
        """Sanitise ``text`` and write it to the console of the running container ``c`` (under the
        server's console lock). Returns the line as written."""
        line = self.sanitise(text)
        key = str((c.labels or {}).get(_ops().LABEL_INSTANCE) or c.id)
        with console_io.console_lock(key):
            self._write_unlocked(c, line)
        return line

    def _write_unlocked(self, c: Container, line: str) -> str | None:
        """Write the line; the exec program's output (exec transport), else None."""
        if self.transport == "exec":
            assert self.exec_argv is not None
            return console_io.exec_line(c, self.exec_argv, line, user=self.exec_user(c))
        console_io.write_stdin(c, line)
        return None

    # --- commands --------------------------------------------------------------------------------------
    def command_sync(self, settings: Settings, c: Container, text: str, timeout: float) -> str:
        line = self.sanitise(text)
        key = str((c.labels or {}).get(_ops().LABEL_INSTANCE) or c.id)
        with console_io.console_lock(key):
            since = time.time() - 0.05
            printed = self._write_unlocked(c, line)
            redact = _ops().log_redactor(settings, c)
            if self.exec_reply == "stdout":
                lines = console_io.output_lines(printed or "")[-console_io.OUTPUT_MAX_LINES :]
                out = "\n".join(lines)
                return redact(out) if redact is not None and out else out
            out = console_io.read_output(c, since, min(float(timeout), self.output_wait), redact=redact)
        return out

    async def command(self, settings: Settings, c: Container, text: str, timeout: float) -> str:
        return await asyncio.to_thread(self.command_sync, settings, c, text, timeout)

    def channel(self, settings: Settings, instance_id: UUID) -> Send | None:
        return ConsoleChannel(self, settings, instance_id)

    # --- saving -------------------------------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return bool(self.save_command)

    def save(self, send: Send, report: Report, wait_log: WaitLog | None = None) -> None:
        """``save_command``, then wait (``save_timeout``) for the end of the save: its log line, or a
        save file written after the command (with the console channel, which knows the server
        folder). 409 ``save_unconfirmed`` when neither shows in time."""
        if not self.save_command:
            super().save(send, report, wait_log)
            return
        if self.save_done_file and not self.save_done_log:
            report(
                0,
                f"Saving the world ({self.save_command}); the game prints no line when the save is done, so the "
                f"backup waits until {self.save_done_file} is written (at most {self.save_timeout:g} s)",
            )
        else:
            report(0, f"Saving the world ({self.save_command})")
        since = time.time() - SAVE_LOG_SLACK
        send(self.save_command, self.save_timeout)
        if self.save_done_log:
            waiter = wait_log or getattr(send, "wait_log", None)
            if waiter is not None and not waiter(self.save_done_log, since, self.save_timeout):
                raise AgentError(
                    f"The server did not report that the save finished within {self.save_timeout:g} s",
                    "save_unconfirmed",
                    409,
                )
        elif self.save_done_file:
            wait_file = getattr(send, "wait_file", None)
            if wait_file is None:
                raise AgentError(
                    "The end of this server's save cannot be observed here", "save_unconfirmed", 409
                )
            if wait_file(self.save_done_file, since, self.save_timeout):
                report(1, f"The world was written ({self.save_done_file})")
            else:
                raise AgentError(
                    f"No save file ({self.save_done_file}) was written within {self.save_timeout:g} s of the save command",
                    "save_unconfirmed",
                    409,
                )

    def wait_file(
        self, settings: Settings, instance_id: UUID, pattern: str, since: float, timeout: float
    ) -> bool:
        """Whether a file matching ``pattern`` (a glob below the server folder, ``/`` separated,
        its folders literal) gets a modification time after ``since`` and keeps it for
        SAVE_FILE_SETTLE seconds, within ``timeout``. No symbolic link is followed."""
        from ..safefs import PathError, SafeRoot

        root = SafeRoot(_ops().instance_dir(settings, instance_id))
        folder, _, name = pattern.rpartition("/")
        deadline = time.monotonic() + max(0.0, timeout)
        stable: dict[str, tuple[float, float]] = {}
        while True:
            try:
                entries = root.list(folder or ".")
            except (PathError, OSError):
                entries = []
            now = time.monotonic()
            for entry in entries:
                if entry.type != "file" or not fnmatch.fnmatchcase(entry.name, name):
                    continue
                rel = entry.path
                mtime = float(entry.modified or 0)
                if mtime <= since:
                    continue
                first = stable.get(rel)
                if first is None or first[0] != mtime:
                    stable[rel] = (mtime, now)
                elif now - first[1] >= SAVE_FILE_SETTLE:
                    return True
            if now >= deadline:
                return False
            time.sleep(SAVE_FILE_POLL)

    # --- broadcast ---------------------------------------------------------------------------------------
    def broadcast(self, settings: Settings, c: Container, text: str) -> None:
        if not self.broadcast_template:
            super().broadcast(settings, c, text)
            return
        message = " ".join(str(text).split())
        self.write(c, self.broadcast_template.replace("{text}", message))


__all__ = ["ConsoleChannel", "ConsoleDriver"]
