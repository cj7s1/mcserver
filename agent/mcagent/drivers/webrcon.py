"""``webrcon``: Rust's WebSocket RCON (didstopia/rust-server with ``+rcon.web 1``).

Protocol (Facepunch's webrcon, ``rconService.js`` shipped in the image; design/probe-6games.md 6):

* connect ``ws://<container address>:<rcon port>/<password>``: the password is the URL path, so it
  must be ``[A-Za-z0-9]{16,64}`` (anything else is refused before connecting) and the URL is never
  logged, put into an error message or handed to a logger (the WebSocket library gets a logger that
  drops everything);
* a request is the JSON ``{"Identifier": n, "Message": "<command>", "Name": "WebRcon"}``; the reply
  is the frame with the same ``Identifier`` (``Message``, ``Type``, ``Stacktrace``). Console output
  arrives on the same socket with Identifier 0 or -1 and is ignored. Ids start at 1001;
* one connection per action, bounded: connect ``CONNECT_TIMEOUT``, the reply within the caller's
  timeout (at most ``MAX_TIMEOUT``), frames of at most ``MAX_FRAME`` bytes, at most ``MAX_FRAMES``
  frames read while waiting.

The password is the container's ``RUST_RCON_PASSWORD`` (read back from its configuration). The
saving pause has no "save off" in Rust: a backup saves first (``server.save``). The stop saves
(``server.save``, bounded; a failure is a warning) and then sends the signal: the image's SIGTERM
trap sends ``quit`` over WebRCON itself, which saves again and exits. The status query (``GET
/ping``) asks A2S on the query port, so no RCON connection is made for it.

Operations (``POST /instances/{id}/game/{action}``): ``players`` (``playerlist``, JSON), ``info``
(``serverinfo``, JSON), ``save``, ``say {message}``, ``kick {steamid, reason}``, ``ban {steamid,
name, reason}`` (``banid``), ``unban {steamid}``, ``owner_add {steamid, name, reason}``
(``ownerid`` + ``server.writecfg``), ``owner_remove {steamid}`` (``removeowner`` +
``server.writecfg``). Errors: 409 ``rcon_unavailable`` (not reachable yet, no password), 409
``rcon_refused`` (the handshake was refused: wrong password), 504 ``rcon_timeout``, 502
``rcon_failed`` (connection lost, not WebRCON), 400 ``invalid_request``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from ..errors import AgentError
from .base import ControlDriver, QueryTarget, Report, Send, WaitLog

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings
    from ..docker_ops import StopResult

log = logging.getLogger(__name__)

PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
STEAMID_RE = re.compile(r"^7656\d{13}$")
TEXT_RE = re.compile(r"^[^\"\\;\x00-\x1f\x7f]{0,128}$")  # names and reasons inside "..."
CONNECT_TIMEOUT = 5.0
MAX_TIMEOUT = 120.0
MAX_FRAME = 4 * 1024 * 1024
MAX_FRAMES = 2000
MAX_COMMAND_LEN = 1024
SAVE_TIMEOUT = 120.0
FIRST_ID = 1001
_ids = itertools.count(FIRST_ID)
_ids_lock = threading.Lock()

# the WebSocket library's logger: it would print the request line (the password) at debug level
_ws_log = logging.getLogger("mcagent.webrcon.websocket")
_ws_log.addHandler(logging.NullHandler())
_ws_log.propagate = False
_ws_log.setLevel(logging.CRITICAL + 1)


def _ops() -> Any:
    from .. import docker_ops  # imported late: docker_ops imports the drivers

    return docker_ops


def next_id() -> int:
    """The Identifier of the next request: FIRST_ID upwards, wrapping below 2**31."""
    with _ids_lock:
        return FIRST_ID + (next(_ids) - FIRST_ID) % (2**31 - 1 - FIRST_ID)


def _text(params: dict[str, Any], key: str, *, default: str = "") -> str:
    value = params.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str) or not TEXT_RE.match(value):
        raise AgentError(
            f"'{key}' must be at most 128 characters without quotes, backslashes, ';' or control characters",
            "invalid_request",
            400,
        )
    return " ".join(value.split())


def _steamid(params: dict[str, Any]) -> str:
    value = params.get("steamid")
    if not isinstance(value, str) or not STEAMID_RE.match(value):
        raise AgentError("'steamid' must be a SteamID64 (17 digits)", "invalid_request", 400)
    return value


def exchange(host: str, port: int, password: str, command: str, timeout: float) -> dict[str, Any]:
    """One command over a new WebRCON connection; the reply frame (a dict with ``Message``,
    ``Type``, ...). Raises AgentError (see the module docstring); never mentions the URL."""
    from websockets.exceptions import InvalidHandshake, InvalidURI, WebSocketException
    from websockets.sync.client import connect

    if not PASSWORD_RE.match(password or ""):
        raise AgentError(
            "The server's WebRCON password is not usable (16 to 64 letters and digits)",
            "rcon_unavailable",
            409,
        )
    timeout = max(0.5, min(float(timeout), MAX_TIMEOUT))
    deadline = time.monotonic() + timeout
    url_host = f"[{host}]" if ":" in host else host
    ident = next_id()
    talked = False
    try:
        with connect(
            f"ws://{url_host}:{int(port)}/{password}",
            open_timeout=min(CONNECT_TIMEOUT, timeout),
            close_timeout=1.0,
            ping_interval=None,
            max_size=MAX_FRAME,
            compression=None,
            proxy=None,  # the instance network, never a proxy from the environment
            logger=_ws_log,
            user_agent_header=None,
        ) as ws:
            talked = True
            return _talk(ws, ident, command, deadline, timeout)
    except AgentError:
        raise
    except InvalidHandshake as exc:
        raise AgentError(
            f"Rust refused the WebRCON connection ({type(exc).__name__}); the password may be wrong",
            "rcon_refused",
            409,
        ) from None
    except (InvalidURI, ValueError) as exc:
        raise AgentError(f"Invalid WebRCON address ({type(exc).__name__})", "rcon_unavailable", 409) from None
    except (OSError, TimeoutError, WebSocketException) as exc:
        if talked:  # closing the connection after the exchange failed: the answer is what counts
            raise AgentError(f"WebRCON failed ({type(exc).__name__})", "rcon_failed", 502) from None
        raise AgentError(
            f"WebRCON is not reachable (the server is probably still starting): {type(exc).__name__}",
            "rcon_unavailable",
            409,
        ) from None


def _talk(ws: Any, ident: int, command: str, deadline: float, timeout: float) -> dict[str, Any]:
    """Send the request and read frames until the one with ``ident`` (bounded); AgentError otherwise."""
    from websockets.exceptions import ConnectionClosed, WebSocketException

    try:
        ws.send(json.dumps({"Identifier": ident, "Message": command, "Name": "WebRcon"}))
        for _ in range(MAX_FRAMES):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            raw = ws.recv(timeout=left)
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue  # not JSON: console noise, never a reply
            if isinstance(frame, dict) and frame.get("Identifier") == ident:
                return frame
    except TimeoutError:
        pass
    except ConnectionClosed as exc:
        raise AgentError(
            f"The WebRCON connection was closed before the answer ({type(exc).__name__})", "rcon_failed", 502
        ) from None
    except (WebSocketException, OSError) as exc:
        raise AgentError(f"WebRCON failed ({type(exc).__name__})", "rcon_failed", 502) from None
    raise AgentError(f"Rust did not answer the command within {timeout:g} s", "rcon_timeout", 504)


class WebRconChannel:
    """The saving pause's ``send``: one WebRCON command to the running container."""

    def __init__(self, driver: WebRconDriver, settings: Settings, instance_id: UUID) -> None:
        self.driver = driver
        self.settings = settings
        self.instance_id = instance_id

    def __call__(self, command: str, timeout: float) -> str:
        c = _ops().running_container(self.instance_id)
        return self.driver.run(self.settings, c, command, timeout)


class WebRconDriver(ControlDriver):
    id: ClassVar[str] = "webrcon"
    resume_label = "server.save"
    ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"players", "info", "save", "say", "kick", "ban", "unban", "owner_add", "owner_remove"}
    )

    def __init__(
        self,
        *,
        port: int = 28016,
        secret_env: str = "RUST_RCON_PASSWORD",  # noqa: S107 - a variable name
        a2s_port: int | None = None,
        a2s_port_env: str | None = None,
        save_command: str = "server.save",
    ) -> None:
        if not 1 <= int(port) <= 65535 or not secret_env:
            raise ValueError("A WebRCON driver needs a port and the variable that holds its password")
        self.port = int(port)
        self.secret_env = secret_env
        self.a2s_port = a2s_port
        self.a2s_port_env = a2s_port_env
        self.save_command = save_command

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> WebRconDriver:
        return cls(**dict(options))

    # --- where WebRCON is ----------------------------------------------------------------------------
    def secret(self, settings: Settings, c: Container) -> str | None:
        return _ops().container_env(c).get(self.secret_env) or None

    def target(self, settings: Settings, c: Container) -> tuple[str, int, str]:
        password = self.secret(settings, c)
        if not password:
            raise AgentError("The server has no WebRCON password configured", "rcon_unavailable", 409)
        address = _ops().container_address(c, self.port, "tcp", settings)
        if address is None:
            raise AgentError("The server's WebRCON port cannot be reached", "rcon_unavailable", 409)
        return address[0], address[1], password

    def run(self, settings: Settings, c: Container, command: str, timeout: float) -> str:
        """One console command; its output (the reply's ``Message``)."""
        line = str(command).strip()
        if not line or len(line) > MAX_COMMAND_LEN or any(ord(ch) < 32 or ord(ch) == 127 for ch in line):
            raise AgentError(
                f"A command is one line of at most {MAX_COMMAND_LEN} printable characters",
                "invalid_request",
                400,
            )
        host, port, password = self.target(settings, c)
        frame = exchange(host, port, password, line, timeout)
        message = frame.get("Message")
        return message if isinstance(message, str) else ""

    # --- commands --------------------------------------------------------------------------------------
    async def command(self, settings: Settings, c: Container, text: str, timeout: float) -> str:
        return await asyncio.to_thread(self.run, settings, c, text, timeout)

    def channel(self, settings: Settings, instance_id: UUID) -> Send:
        return WebRconChannel(self, settings, instance_id)

    # --- saving, broadcast, stop -----------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return True

    def save(self, send: Send, report: Report, wait_log: WaitLog | None = None) -> None:
        report(0, f"Saving the world ({self.save_command})")
        send(self.save_command, SAVE_TIMEOUT)

    def broadcast(self, settings: Settings, c: Container, text: str) -> None:
        message = " ".join(str(text).split()).replace('"', "'").replace("\\", "/").replace(";", ",")
        if not message:
            raise AgentError("The message is empty", "invalid_request", 400)
        self.run(settings, c, f'say "{message[:256]}"', 10.0)

    def stop(self, settings: Settings, c: Container, timeout: int, result: StopResult) -> bool:
        """``server.save`` when WebRCON answers (bounded; a failure is a warning), then ``docker
        stop``: the image's SIGTERM trap sends ``quit`` (which saves) over WebRCON itself."""
        if c.status == "running":
            try:
                self.run(settings, c, self.save_command, _ops().flush_timeout(timeout))
                result.flushed = True
            except AgentError as exc:
                result.warnings.append(
                    f"The world was not saved before the stop ({self.save_command}): {exc}"
                )
        c.stop(timeout=timeout)
        return True

    # --- status query ----------------------------------------------------------------------------------
    def query_endpoint(self, settings: Settings, c: Container) -> tuple[str, int]:
        if self.a2s_port is None and self.a2s_port_env is None:
            return super().query_endpoint(settings, c)
        from .rcon import a2s_query

        target = _ops().a2s_target(c, self.a2s_port, self.a2s_port_env, settings)
        if target is None:
            raise AgentError("The server's query port cannot be reached", "ping_unavailable", 409)
        return QueryTarget(target[0], target[1], a2s_query(target))

    # --- operations ------------------------------------------------------------------------------------
    def game_action(
        self, settings: Settings, c: Container, action: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if action not in self.ACTIONS:
            return super().game_action(settings, c, action, params, timeout)
        params = params or {}
        timeout = min(float(timeout), MAX_TIMEOUT)
        if action in ("players", "info"):
            text = self.run(settings, c, "playerlist" if action == "players" else "serverinfo", timeout)
            try:
                data = json.loads(text) if text.strip() else ([] if action == "players" else {})
            except ValueError as exc:
                raise AgentError(
                    f"Rust answered {action} with something that is not JSON", "rcon_failed", 502
                ) from exc
            if action == "players":
                return {"players": data if isinstance(data, list) else []}
            return data if isinstance(data, dict) else {}
        if action == "save":
            return {"ok": True, "output": self.run(settings, c, self.save_command, timeout)}
        if action == "say":
            message = _text(params, "message")
            if not message:
                raise AgentError("'message' is required", "invalid_request", 400)
            return {"ok": True, "output": self.run(settings, c, f'say "{message}"', timeout)}
        steamid = _steamid(params)
        if action == "kick":
            reason = _text(params, "reason")
            return {"ok": True, "output": self.run(settings, c, f'kick {steamid} "{reason}"', timeout)}
        if action == "ban":
            name, reason = _text(params, "name"), _text(params, "reason")
            return {
                "ok": True,
                "output": self.run(settings, c, f'banid {steamid} "{name}" "{reason}"', timeout),
            }
        if action == "unban":
            return {"ok": True, "output": self.run(settings, c, f"unban {steamid}", timeout)}
        if action == "owner_add":
            name, reason = _text(params, "name"), _text(params, "reason")
            output = self.run(settings, c, f'ownerid {steamid} "{name}" "{reason}"', timeout)
        else:  # owner_remove
            output = self.run(settings, c, f"removeowner {steamid}", timeout)
        self.run(settings, c, "server.writecfg", timeout)  # keep it after a restart (cfg/users.cfg)
        return {"ok": True, "output": output}


__all__ = ["WebRconChannel", "WebRconDriver", "exchange"]
