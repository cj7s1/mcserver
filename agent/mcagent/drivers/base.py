"""The control driver interface: how the agent talks to the server inside a container.

One driver per protocol family (docs/design/GAME-DEFINITIONS.md, section 5.2). Every operation
is optional except ``stop`` (which may return False: plain ``docker stop``); an operation a driver
does not have answers 409 ``*_unsupported``, or is skipped where the caller has a fallback (the
saving pause, the stop).

The I/O of the saving pause is the caller's: ``send(command, timeout)`` delivers one command over
the driver's channel (``main._rcon`` for RCON drivers), ``report(step, message)`` reports progress.
That keeps the marker files and the retry rules (``main._world_saving_paused``) in one place for
every game.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from ..errors import AgentError

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings
    from ..docker_ops import StopResult

Send = Callable[[str, float], Any]
Report = Callable[[int, str], None]
# wait_log(pattern, since, timeout): whether the running container printed a line matching the
# regular expression ``pattern`` after ``since`` (epoch seconds), waiting up to ``timeout`` s.
WaitLog = Callable[[str, float, float], bool]


class ControlDriver:
    """No control channel at all: ``docker stop`` only, no commands, no saving pause."""

    id: ClassVar[str] = "none"
    # The label of the command that resumes saving, for warnings and log lines.
    resume_label: str = "resume saving"

    # --- commands -----------------------------------------------------------------------------
    async def command(self, settings: Settings, c: Container, text: str, timeout: float) -> str:
        raise AgentError("This server's game does not take console commands", "command_unsupported", 409)

    # --- stop ---------------------------------------------------------------------------------
    def stop(self, settings: Settings, c: Container, timeout: int, result: StopResult) -> bool:
        """Stop the running container ``c`` the game's way, recording what happened in ``result``.

        Returns True when it ran ``docker stop`` itself, False to leave a plain ``docker stop``
        with ``timeout`` to the caller (``docker_ops.graceful_stop``).
        """
        return False

    # --- saving -------------------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return False

    def save(self, send: Send, report: Report, wait_log: WaitLog | None = None) -> None:
        """Make the server write its world to disk now (before a copy without a pause); with
        ``wait_log`` a driver whose game reports the end of a save in its log waits for it."""
        raise AgentError("This server's game cannot save on request", "save_unsupported", 409)

    @property
    def can_pause_saving(self) -> bool:
        return False

    def pause_saving(self, send: Send, report: Report) -> None:
        """Stop the server from writing its world until :meth:`resume_saving`, everything flushed."""
        raise AgentError("This server's game cannot pause saving", "save_unsupported", 409)

    def resume_saving(self, send: Send) -> None:
        raise AgentError("This server's game cannot pause saving", "save_unsupported", 409)

    # --- broadcast, query, readiness ----------------------------------------------------------
    def broadcast(self, settings: Settings, c: Container, text: str) -> None:
        raise AgentError("This server's game has no in-game broadcast", "broadcast_unsupported", 409)

    def query_endpoint(self, settings: Settings, c: Container) -> tuple[str, int]:
        """(host, port) of the status query (Minecraft: server list ping on the game port)."""
        raise AgentError("This server's game does not answer a status query", "ping_unavailable", 409)

    async def query(self, host: str, port: int, timeout: float) -> dict[str, Any]:
        raise AgentError("This server's game does not answer a status query", "ping_unavailable", 409)

    def ready(self, settings: Settings, c: Container) -> bool | None:
        """Readiness beyond Docker's health check; None: Docker's health check decides."""
        return None

    # --- the control secret ---------------------------------------------------------------------
    def secret(self, settings: Settings, c: Container) -> str | None:
        """The secret the agent uses to reach the server (RCON password); None without one."""
        return None

    # --- drivers with a channel of their own (HTTP APIs) ------------------------------------------
    def channel(self, settings: Settings, instance_id: UUID) -> Send | None:
        """The ``send`` the saving pause and the save use for this driver; None: RCON
        (``main._rcon``). An HTTP API driver returns a sender that runs its own operations."""
        return None

    def game_action(
        self, settings: Settings, c: Container, action: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """An operation of the game's own control API (``POST /instances/{id}/game/{action}``),
        e.g. Satisfactory's claim; 409 ``action_unsupported`` for a driver without it."""
        raise AgentError(f"This server's game has no operation '{action}'", "action_unsupported", 409)

    def publish_guard(self, settings: Settings, instance_id: UUID, existing: Container | None) -> None:
        """Called before a container that publishes ports is created (apply): raise to refuse it
        (Satisfactory: never publish a server that is not claimed). Default: nothing."""
        return None


class QueryTarget(tuple):
    """(host, port) of a status query, with the driver's own query attached: ``query(timeout)``
    is an async callable answering like the server list ping (``online``, ``max``, ``sample``,
    ``version``, ``motd``, ``latency_ms``). Unpacks like a plain (host, port)."""

    query: Any

    def __new__(cls, host: str, port: int, query: Any) -> QueryTarget:
        obj = super().__new__(cls, (host, port))
        obj.query = query
        return obj


class NoControlDriver(ControlDriver):
    """``none``: games stopped by signal only; also what a container of an unknown game gets."""

    id: ClassVar[str] = "none"
