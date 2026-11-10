"""``rcon``: a control driver for games that speak Source RCON (Minecraft, Factorio, ...).

What differs between games is data: the container port RCON listens on, the environment
variable of the container that holds the password (read back from the container's own
configuration, so the secret never travels in agent requests), the longest command the server
accepts and the commands for saving and stopping. Minecraft's driver
(:mod:`mcagent.drivers.minecraft`) is this class with itzg's values.

The stop helpers (the bounded save before the stop, the courier that delivers the stop command
while ``docker stop`` waits, ``run_command_sync`` and the RCON timing constants) are looked up in
``docker_ops`` when they are used: that is where they always were, and where tests and the
bounds in tests/test_graceful_stop.py reach them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from .. import rcon as rcon_client
from ..errors import AgentError
from .base import ControlDriver, Report, Send

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


class RconDriver(ControlDriver):
    id: ClassVar[str] = "rcon"

    def __init__(
        self,
        *,
        port: int,
        secret_env: str,
        commands: RconCommands,
        max_command_len: int = rcon_client.MAX_COMMAND_LEN,
    ) -> None:
        if not 1 <= port <= 65535 or not secret_env:
            raise ValueError("An RCON driver needs a port and the name of its password variable")
        self.port = port
        self.secret_env = secret_env
        self.commands = commands
        self.max_command_len = max_command_len
        self.resume_label = commands.resume_saving[0].command if commands.resume_saving else "resume saving"

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> RconDriver:
        """The driver a profile describes with ``control="rcon"`` and ``control_options``:
        ``port``, ``secret_env``, ``max_command_len`` (optional) and ``commands`` (an
        :class:`RconCommands`, or a dict of its fields with steps as (command, timeout, label))."""
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
            )
        return cls(
            port=int(options["port"]),
            secret_env=str(options["secret_env"]),
            commands=commands,
            max_command_len=int(options.get("max_command_len") or rcon_client.MAX_COMMAND_LEN),
        )

    # --- where RCON is ---------------------------------------------------------------------------
    def endpoint(self, settings: Settings, c: Container) -> tuple[str, int, str]:
        """(host, port, password) from the attributes of a running container that was just reloaded."""
        ops = _ops()
        password = ""
        prefix = f"{self.secret_env}="
        for item in c.attrs.get("Config", {}).get("Env") or []:
            if item.startswith(prefix):
                password = item.split("=", 1)[1]
        if not password:
            raise AgentError("Container has no RCON password configured", "rcon_unavailable", 409)
        info = ops.describe(c)
        if settings.rcon_mode == "published":
            for p in info["ports"]:
                if p["container_port"] == self.port:
                    return "127.0.0.1", p["host_port"], password
            raise AgentError("RCON port is not published", "rcon_unavailable", 409)
        if c.attrs.get("HostConfig", {}).get("NetworkMode") == "host":
            return "127.0.0.1", self.port, password
        if not info["ip_address"]:
            raise AgentError("Container has no IP address", "rcon_unavailable", 409)
        return info["ip_address"], self.port, password

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
                answered = cmds.save_before_stop is not None and ops._flush_before_stop(
                    target, ops.flush_timeout(timeout), result, cmds.save_before_stop
                )
                if not answered and cmds.stop is not None:
                    courier = ops._RconStopCourier(c.id, c.name, target, cmds.stop)
        if courier is not None:
            courier.start()
        try:
            c.stop(timeout=timeout)
        finally:
            if courier is not None:
                courier.finish(result)
        return True

    # --- saving ----------------------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return bool(self.commands.save)

    def save(self, send: Send, report: Report) -> None:
        if not self.commands.save:
            super().save(send, report)
        for i, step in enumerate(self.commands.save):
            report(i, step.label)
            send(step.command, step.timeout)

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
