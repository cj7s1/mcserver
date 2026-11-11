"""``rcon``: a control driver for games that speak Source RCON (Minecraft, Factorio, ...).

What differs between games is data: the container port RCON listens on, where the password is
(``secret_env``: a variable of the container, read back from its own configuration; or
``secret_file``: a file of the server folder the image reads, e.g. Factorio's ``config/rconpw``,
read without following symbolic links), so the secret never travels in agent requests; the
longest command the server accepts; the commands for saving and stopping, and the log line that
says a save is complete (``save_done_log``) where the reply to the save command comes before it. Minecraft's driver
(:mod:`mcagent.drivers.minecraft`) is this class with itzg's values.

The stop helpers (the bounded save before the stop, the courier that delivers the stop command
while ``docker stop`` waits, ``run_command_sync`` and the RCON timing constants) are looked up in
``docker_ops`` when they are used: that is where they always were, and where tests and the
bounds in tests/test_graceful_stop.py reach them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from .. import rcon as rcon_client
from ..errors import AgentError
from .base import ControlDriver, Report, Send, WaitLog

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings
    from ..docker_ops import StopResult

log = logging.getLogger(__name__)


def _ops() -> Any:
    from .. import docker_ops  # imported late: docker_ops imports the drivers

    return docker_ops


@dataclass(frozen=True)
class RconStep:
    command: str
    timeout: float
    label: str  # the progress message while it runs


@dataclass(frozen=True)
class RconCommands:
    # sent before ``docker stop`` when the server answers quickly (bounded by docker_ops.flush_timeout)
    save_before_stop: str | None = None
    # delivered through RCON while ``docker stop`` waits, when RCON did not answer before the stop
    # (the image's own SIGTERM handling could not reach the server yet)
    stop: str | None = None
    # save on request, for a consistent copy without a pause
    save: tuple[RconStep, ...] = ()
    # around backups and snapshots: stop writing the world (and flush it), and write again
    pause_saving: tuple[RconStep, ...] = ()
    resume_saving: tuple[RconStep, ...] = ()
    # a regular expression of the log line that says a save is complete, for games whose save
    # command answers before the save is written (Factorio: "Saving finished")
    save_done_log: str | None = None
    # sent through RCON before ``docker stop`` when RCON answers, without waiting for a reply (the
    # server closes the connection as it quits): games with nothing to save whose shell entrypoint
    # does not pass the stop signal on in a useful way (CS2: ``quit``)
    quit_before_stop: str | None = None


def a2s_query(target: tuple[str, int]) -> Any:
    """The async query of a :class:`~mcagent.drivers.base.QueryTarget` that asks A2S at ``target``:
    what ``GET /ping`` answers (``online``, ``max``, ``sample``, ``version``, ``motd``, ...); 502
    ``ping_failed`` when the server does not answer A2S (yet)."""

    async def query(timeout: float) -> dict[str, Any]:
        import asyncio

        from .. import a2s

        try:
            return await asyncio.to_thread(a2s.status, target[0], target[1], timeout)
        except a2s.A2SError as exc:
            raise AgentError(
                f"The server did not answer the status query: {exc}", "ping_failed", 502
            ) from exc

    return query


SECRET_FILE_MAX_BYTES = 256
QUIT_REPLY_WAIT = 2.0  # quit_before_stop: how long to wait for a reply (usually the connection drops)
QUIT_GRACE = 10.0  # ... and how long the server may take to exit before the stop signal
QUIT_POLL = 0.25
# how far back (seconds) the wait for save_done_log looks from the moment the command is sent
SAVE_LOG_SLACK = 0.2


class RconDriver(ControlDriver):
    id: ClassVar[str] = "rcon"

    def __init__(
        self,
        *,
        port: int,
        secret_env: str | None = None,
        commands: RconCommands,
        max_command_len: int = rcon_client.MAX_COMMAND_LEN,
        secret_file: str | None = None,
        port_env: str | None = None,
        a2s_port: int | None = None,
        a2s_port_env: str | None = None,
    ) -> None:
        if not 1 <= port <= 65535 or not (secret_env or secret_file):
            raise ValueError(
                "An RCON driver needs a port and where its password is (secret_env or secret_file)"
            )
        if secret_file is not None and (secret_file.startswith("/") or ".." in secret_file.split("/")):
            raise ValueError(f"Invalid RCON secret file {secret_file!r}")
        if a2s_port is not None and not 1 <= a2s_port <= 65535:
            raise ValueError("Invalid A2S port")
        self.port = port
        self.secret_env = secret_env
        self.secret_file = secret_file
        self.commands = commands
        self.max_command_len = max_command_len
        self.resume_label = commands.resume_saving[0].command if commands.resume_saving else "resume saving"
        # the container variable that names the RCON port (CS2: RCON listens on the game port's
        # number over TCP, and the game port is set 1:1 through CS2_PORT); ``port`` without it
        self.port_env = port_env
        # where the status query (GET /ping) asks A2S; none: no status query
        self.a2s_port = a2s_port
        self.a2s_port_env = a2s_port_env

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> RconDriver:
        """The driver a profile describes with ``control="rcon"`` and ``control_options``:
        ``port``, ``secret_env`` or ``secret_file``, ``max_command_len`` (optional) and ``commands``
        (an :class:`RconCommands`, or a dict of its fields with steps as (command, timeout, label))."""
        commands = options.get("commands") or RconCommands()
        if isinstance(commands, dict):

            def steps(key: str) -> tuple[RconStep, ...]:
                return tuple(s if isinstance(s, RconStep) else RconStep(*s) for s in commands.get(key) or ())

            commands = RconCommands(
                save_before_stop=commands.get("save_before_stop"),
                stop=commands.get("stop"),
                save=steps("save"),
                pause_saving=steps("pause_saving"),
                resume_saving=steps("resume_saving"),
                save_done_log=commands.get("save_done_log"),
                quit_before_stop=commands.get("quit_before_stop"),
            )
        return cls(
            port=int(options["port"]),
            secret_env=str(options["secret_env"]) if options.get("secret_env") else None,
            secret_file=str(options["secret_file"]) if options.get("secret_file") else None,
            commands=commands,
            max_command_len=int(options.get("max_command_len") or rcon_client.MAX_COMMAND_LEN),
            port_env=str(options["port_env"]) if options.get("port_env") else None,
            a2s_port=int(options["a2s_port"]) if options.get("a2s_port") else None,
            a2s_port_env=str(options["a2s_port_env"]) if options.get("a2s_port_env") else None,
        )

    # --- where RCON is ---------------------------------------------------------------------------
    def secret(self, settings: Settings, c: Container) -> str | None:
        """The RCON password: the container's ``secret_env`` variable, or the content of
        ``secret_file`` in the server folder of the container's instance (read through SafeRoot:
        no symbolic link is followed, at most SECRET_FILE_MAX_BYTES, surrounding blanks
        stripped). None when there is none."""
        if self.secret_file is not None:
            from ..safefs import PathError, SafeRoot

            ops = _ops()
            instance = (c.labels or {}).get(ops.LABEL_INSTANCE)
            if not instance:
                return None
            try:
                root = SafeRoot(ops.instance_dir(settings, UUID(instance)))
                data = root.read_bytes(self.secret_file, SECRET_FILE_MAX_BYTES)
            except (PathError, ValueError, OSError) as exc:
                log.debug("RCON secret file of %s not readable: %s", c.name, exc)
                return None
            text = data.decode("utf-8", errors="replace").strip()
            return text or None
        prefix = f"{self.secret_env}="
        password = ""
        for item in c.attrs.get("Config", {}).get("Env") or []:
            if item.startswith(prefix):
                password = item.split("=", 1)[1]
        return password or None

    def endpoint(self, settings: Settings, c: Container) -> tuple[str, int, str]:
        """(host, port, password) from the attributes of a running container that was just reloaded."""
        ops = _ops()
        password = self.secret(settings, c)
        if not password:
            raise AgentError("Container has no RCON password configured", "rcon_unavailable", 409)
        port = self.rcon_port(c)
        info = ops.describe(c)
        if settings.rcon_mode == "published":
            for p in info["ports"]:
                if p["container_port"] == port and (self.port_env is None or p["protocol"] == "tcp"):
                    return "127.0.0.1", p["host_port"], password
            raise AgentError("RCON port is not published", "rcon_unavailable", 409)
        if c.attrs.get("HostConfig", {}).get("NetworkMode") == "host":
            return "127.0.0.1", port, password
        if not info["ip_address"]:
            raise AgentError("Container has no IP address", "rcon_unavailable", 409)
        return info["ip_address"], port, password

    def rcon_port(self, c: Container) -> int:
        """The container port RCON listens on: the value of ``port_env`` when the profile names one
        (and it is a port), else ``port``."""
        if self.port_env is None:
            return self.port
        return int(_ops().env_port(c, self.port_env, self.port) or self.port)

    # --- status query (A2S) ------------------------------------------------------------------------
    def query_endpoint(self, settings: Settings, c: Container) -> tuple[str, int]:
        """With ``a2s_port``/``a2s_port_env``: the A2S query of the game (players, limit, map,
        version) for ``GET /ping``; without: no status query (409 ``ping_unavailable``)."""
        if self.a2s_port is None and self.a2s_port_env is None:
            return super().query_endpoint(settings, c)
        from .base import QueryTarget

        target = _ops().a2s_target(c, self.a2s_port, self.a2s_port_env, settings)
        if target is None:
            raise AgentError("The server's query port cannot be reached", "ping_unavailable", 409)
        return QueryTarget(target[0], target[1], a2s_query(target))

    # --- commands --------------------------------------------------------------------------------
    async def command(self, settings: Settings, c: Container, text: str, timeout: float) -> str:
        host, port, password = self.endpoint(settings, c)
        return await rcon_client.run_command(
            host, port, password, text, timeout=timeout, max_command_len=self.max_command_len
        )

    # --- stop ------------------------------------------------------------------------------------
    def stop(self, settings: Settings, c: Container, timeout: int, result: StopResult) -> bool:
        """Save first when RCON answers, then ``docker stop``, and make sure the stop arrives.

        1. Running and RCON answers quickly: ``save_before_stop``, waiting up to
           ``flush_timeout()`` for the answer. A failure is a warning, never an error.
        2. ``docker stop`` with the requested timeout, always: it tells Docker the stop is
           intended, so the restart policy does not start the server again.
        3. If RCON did not answer in step 1, the courier delivers ``stop`` as soon as it does,
           until the container has exited or ``docker stop`` has returned.
        """
        ops = _ops()
        cmds = self.commands
        courier = None
        if c.status == "running":  # restarting / paused: no server that could answer
            try:
                target = self.endpoint(settings, c)
            except AgentError as exc:
                target = None
                result.warnings.append(f"The world was not flushed before the stop: {exc}")
            except Exception as exc:
                # Whatever went wrong while reading the container's RCON address (e.g. a port entry
                # Docker reports in a form describe() does not expect): `docker stop` still runs.
                log.exception(
                    "Unexpected error while working out the RCON address of %s before a stop", c.name
                )
                target = None
                result.warnings.append(
                    f"The world was not flushed before the stop: {type(exc).__name__}: {exc}"
                )
            if target is not None:
                since = time.time() - SAVE_LOG_SLACK
                answered = cmds.save_before_stop is not None and ops._flush_before_stop(
                    target, ops.flush_timeout(timeout), result, cmds.save_before_stop
                )
                if result.flushed and cmds.save_done_log:
                    # the reply came before the save was written: wait for the game to say so
                    try:
                        done = ops.wait_for_log(c, cmds.save_done_log, since, ops.flush_timeout(timeout))
                    except Exception as exc:
                        log.warning("Could not read the log of %s while it saved: %s", c.name, exc)
                        done = False
                    if not done:
                        result.flushed = False
                        result.warnings.append(
                            f"The server did not report that {cmds.save_before_stop} finished before the stop; "
                            "it saves again when it stops"
                        )
                if not answered and cmds.stop is not None:
                    courier = ops._RconStopCourier(c.id, c.name, target, cmds.stop)
                if cmds.quit_before_stop is not None:
                    self._quit_first(c, target, cmds.quit_before_stop, timeout, result)
        if courier is not None:
            courier.start()
        try:
            c.stop(timeout=timeout)
        finally:
            if courier is not None:
                courier.finish(result)
        return True

    @staticmethod
    def _quit_first(
        c: Container, target: tuple[str, int, str], command: str, timeout: int, result: StopResult
    ) -> None:
        """Send ``command`` (CS2: ``quit``) and give the server up to QUIT_GRACE seconds to exit
        before the signal. Never raises: the signal follows in any case."""
        ops = _ops()
        host, port, password = target
        try:
            rcon_client.run_command_sync(
                host, port, password, command, timeout=QUIT_REPLY_WAIT, connect_timeout=ops.RCON_PROBE_TIMEOUT
            )
        except rcon_client.RconUnavailable as exc:
            result.warnings.append(
                f"{command} could not be sent before the stop (RCON is not available): {exc}"
            )
            return
        except rcon_client.RconError:
            pass  # sent; a server that quits closes the connection before it answers
        except Exception as exc:
            log.warning("Sending %s to %s before its stop failed: %s", command, c.name, exc)
            return
        result.stop_sent_via_rcon = True
        deadline = time.monotonic() + min(QUIT_GRACE, max(1.0, timeout / 2))
        while time.monotonic() < deadline:
            try:
                c.reload()
            except Exception:
                return  # gone or Docker trouble: docker stop decides
            if c.status != "running":
                return
            time.sleep(QUIT_POLL)

    # --- saving ----------------------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return bool(self.commands.save)

    def save(self, send: Send, report: Report, wait_log: WaitLog | None = None) -> None:
        """The save steps in order. With ``save_done_log`` and ``wait_log`` each step waits (up
        to its timeout) for the log line that says the save is complete; when it does not come
        the save counts as failed (409 ``save_unconfirmed``)."""
        if not self.commands.save:
            super().save(send, report, wait_log)
        pattern = self.commands.save_done_log
        for i, step in enumerate(self.commands.save):
            report(i, step.label)
            since = time.time() - SAVE_LOG_SLACK
            send(step.command, step.timeout)
            if pattern and wait_log is not None and not wait_log(pattern, since, step.timeout):
                raise AgentError(
                    f"The server did not report that the save finished within {step.timeout:g} s ({step.command})",
                    "save_unconfirmed",
                    409,
                )

    @property
    def can_pause_saving(self) -> bool:
        return bool(self.commands.pause_saving)

    def pause_saving(self, send: Send, report: Report) -> None:
        if not self.commands.pause_saving:
            super().pause_saving(send, report)
        for i, step in enumerate(self.commands.pause_saving):
            report(i, step.label)
            send(step.command, step.timeout)

    def resume_saving(self, send: Send) -> None:
        if not self.commands.resume_saving:
            super().resume_saving(send)
        for step in self.commands.resume_saving:
            send(step.command, step.timeout)
