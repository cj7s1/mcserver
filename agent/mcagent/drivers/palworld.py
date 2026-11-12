"""``palworld-api``: Palworld's REST API (thijsvanloef/palworld-server-docker), an ``http-api`` driver.

What the probe of 2026-11-10 observed (``v2.8.1``, game v1.0.5.102999; design/probe-6games.md 1.5):

* The API listens on 8212/tcp of the container (``REST_API_ENABLED=true``, ``REST_API_PORT=8212``,
  injected by the profile), plain HTTP, on the instance network only (8212 is never published).
* HTTP Basic ``admin:<AdminPassword>``; the password is the container's ``ADMIN_PASSWORD`` (read
  back from the container's own configuration, like an RCON password; a ``secret_file`` of the server
  folder is the fallback). Wrong or no password: 401.
* ``GET info|players|settings|metrics`` answer JSON (``settings`` with a text/plain content type);
  ``POST announce {message}``, ``kick|ban {userid, message}``, ``unban {userid}``, ``save``,
  ``shutdown {waittime, message}`` answer 200 with an empty body; a request the game refuses (kick of
  a player who is not online, ``announce`` without a message) answers 400. ``POST stop`` exits
  without saving and is not offered.
* Every call prints ``REST accessed endpoint /v1/api/<x>`` into the log, so readiness is the log
  probe and the status query (``GET /ping``) asks ``metrics`` only.
* SIGTERM saves (the image's trap calls ``save`` and ``shutdown``), so the stop is ``save`` (bounded,
  a failure is a warning) and then ``docker stop``.

There is no console (``command`` answers 409 ``command_unsupported``). Operations
(``POST /instances/{id}/game/{action}``): ``info``, ``players``, ``settings``, ``metrics``,
``announce``, ``kick``, ``ban``, ``unban``, ``save``, ``shutdown``. Errors: the ``http-api`` codes
(``api_unavailable``, ``api_timeout``, ``api_error``, ``api_unauthorized``) plus 409
``api_rejected`` (the game answered 400) and 400 ``invalid_request`` (parameters).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from ..errors import AgentError, game_text
from ..slp import count_or_none
from .base import QueryTarget, Report, Send, WaitLog
from .http_api import GameApiError, HttpApiDriver

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings
    from ..docker_ops import StopResult

log = logging.getLogger(__name__)

API_PATH = "/v1/api"
API_TIMEOUT = 15.0
SAVE_TIMEOUT = 120.0
QUERY_TIMEOUT = 5.0
USERID_RE = re.compile(r"^[A-Za-z0-9_.:-]{3,64}$")  # steam_7656..., xbox/ps ids
MESSAGE_MAX = 500
WAITTIME_MAX = 3600
SHUTDOWN_MESSAGE = "The server is shutting down"


def _ops() -> Any:
    from .. import docker_ops  # imported late: docker_ops imports the drivers

    return docker_ops


def _message(params: dict[str, Any], key: str = "message", *, required: bool) -> str | None:
    value = params.get(key)
    if value is None or value == "":
        if required:
            raise AgentError(f"'{key}' is required", "invalid_request", 400)
        return None
    if not isinstance(value, str):
        raise AgentError(f"'{key}' must be text", "invalid_request", 400)
    text = " ".join(value.split())
    if not text or len(text) > MESSAGE_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise AgentError(f"'{key}' must be 1 to {MESSAGE_MAX} printable characters", "invalid_request", 400)
    return text


def _userid(params: dict[str, Any]) -> str:
    value = params.get("userid")
    if not isinstance(value, str) or not USERID_RE.match(value):
        raise AgentError("'userid' must be a Palworld user id such as steam_7656...", "invalid_request", 400)
    return value


class PalworldChannel:
    """The saving pause's ``send`` for Palworld: ``save`` is ``POST save``."""

    def __init__(self, driver: PalworldDriver, settings: Settings, instance_id: UUID) -> None:
        self.driver = driver
        self.settings = settings
        self.instance_id = instance_id

    def __call__(self, command: str, timeout: float) -> Any:
        if command != "save":
            raise AgentError(f"Unknown operation {command!r}", "command_unsupported", 409)
        self.driver.call(
            self.settings, _ops().running_container(self.instance_id), "POST", "save", timeout=timeout
        )
        return "saved"


class PalworldDriver(HttpApiDriver):
    id: ClassVar[str] = "palworld-api"
    ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"info", "players", "settings", "metrics", "announce", "kick", "ban", "unban", "save", "shutdown"}
    )

    def __init__(
        self,
        *,
        port: int = 8212,
        secret_env: str | None = "ADMIN_PASSWORD",  # noqa: S107 - a variable name
        secret_file: str | None = None,
        username: str = "admin",
    ) -> None:
        super().__init__(port=port, scheme="http", secret_file=secret_file)
        if not (secret_env or secret_file):
            raise ValueError(
                "The Palworld driver needs where the admin password is (secret_env or secret_file)"
            )
        self.secret_env = secret_env
        self.username = username

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> PalworldDriver:
        return cls(
            port=int(options.get("port") or 8212),
            secret_env=str(options["secret_env"]) if options.get("secret_env") else None,
            secret_file=str(options["secret_file"]) if options.get("secret_file") else None,
            username=str(options.get("username") or "admin"),
        )

    # --- the password ----------------------------------------------------------------------------------
    def secret(self, settings: Settings, c: Container) -> str | None:
        """The admin password: the container's ``secret_env`` variable (what the running server was
        started with), else ``secret_file`` of the server folder."""
        if self.secret_env:
            value = _ops().container_env(c).get(self.secret_env, "")
            if value:
                return value
        return super().secret(settings, c)

    def _auth(self, settings: Settings, c: Container) -> str:
        password = self.secret(settings, c)
        if not password:
            raise GameApiError("The server has no admin password for its REST API", "api_unauthorized", 409)
        token = base64.b64encode(f"{self.username}:{password}".encode()).decode("ascii")
        return f"Basic {token}"

    # --- requests --------------------------------------------------------------------------------------
    def call(
        self,
        settings: Settings,
        c: Container,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = API_TIMEOUT,
    ) -> Any:
        """One API request; the answer's JSON (None for an empty body). 409 ``api_unauthorized`` on
        401, 409 ``api_rejected`` on 400 (the game refused the request), 502 ``api_error`` otherwise."""
        headers = {"Authorization": self._auth(settings, c), "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        resp = self.request(
            settings, c, method, f"{API_PATH}/{endpoint}", body=data, headers=headers, timeout=timeout
        )
        if resp.status == 401:
            raise GameApiError(
                f"Palworld refused the admin password ({endpoint})", "api_unauthorized", 409, http_status=401
            )
        if resp.status == 400:
            raise GameApiError(
                f"Palworld refused {endpoint} (HTTP 400: the player may not be online, or a value is missing)",
                "api_rejected",
                409,
                http_status=400,
            )
        if resp.status >= 300:
            raise GameApiError(
                f"Palworld answered {endpoint} with HTTP {resp.status}",
                "api_error",
                502,
                http_status=resp.status,
            )
        if not resp.body:
            return None
        parsed = resp.json()
        if parsed is None:
            raise GameApiError(
                f"Palworld answered {endpoint} with something that is not JSON", "api_error", 502
            )
        return parsed

    # --- readiness, status query -----------------------------------------------------------------------
    def ready(self, settings: Settings, c: Container) -> bool | None:
        try:
            return isinstance(self.call(settings, c, "GET", "info", timeout=2.0), dict)
        except AgentError:
            return False

    def status_answer(self, settings: Settings, c: Container, timeout: float) -> dict[str, Any]:
        """What ``GET /ping`` answers: players online and the limit from ``metrics`` (one request,
        one log line; names come from the ``players`` operation), the server's uptime and days."""
        started = time.monotonic()
        metrics = self.call(settings, c, "GET", "metrics", timeout=timeout) or {}
        return {
            "online": count_or_none(metrics.get("currentplayernum")),  # None when not a count (F-2)
            "max": count_or_none(metrics.get("maxplayernum")),
            "sample": [],
            "version": None,
            "motd": None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "fps": metrics.get("serverfps"),
            "days": metrics.get("days"),
            "uptime": metrics.get("uptime"),
        }

    def query_endpoint(self, settings: Settings, c: Container) -> tuple[str, int]:
        host, port = self.address(settings, c)

        async def query(timeout: float) -> dict[str, Any]:
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(self.status_answer, settings, c, min(timeout, QUERY_TIMEOUT))

        return QueryTarget(host, port, query)

    # --- saving, broadcast, stop -----------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return True

    def channel(self, settings: Settings, instance_id: UUID) -> Send:
        return PalworldChannel(self, settings, instance_id)

    def save(self, send: Send, report: Report, wait_log: WaitLog | None = None) -> None:
        report(0, "Saving the world (REST save)")
        send("save", SAVE_TIMEOUT)

    def broadcast(self, settings: Settings, c: Container, text: str) -> None:
        message = _message({"message": text}, required=True)
        self.call(settings, c, "POST", "announce", {"message": message})

    def stop(self, settings: Settings, c: Container, timeout: int, result: StopResult) -> bool:
        """``save`` when the API answers (bounded; a failure is a warning), then ``docker stop`` (the
        image's SIGTERM trap saves and shuts the game down as well)."""
        if c.status == "running":
            try:
                self.call(settings, c, "POST", "save", timeout=_ops().flush_timeout(timeout))
                result.flushed = True
            except Exception as exc:  # also a reply the parser did not expect: the stop goes on (B-3)
                result.warnings.append(
                    f"The world was not saved through the REST API before the stop: {game_text(exc)}"
                )
        c.stop(timeout=timeout)
        return True

    # --- operations ------------------------------------------------------------------------------------
    def game_action(
        self, settings: Settings, c: Container, action: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if action not in self.ACTIONS:
            return super().game_action(settings, c, action, params, timeout)
        params = params or {}
        timeout = min(float(timeout), SAVE_TIMEOUT)
        if action in ("info", "players", "settings", "metrics"):
            data = self.call(settings, c, "GET", action, timeout=timeout)
            if action == "players":
                players = data.get("players") if isinstance(data, dict) else None
                return {"players": players if isinstance(players, list) else []}
            return data if isinstance(data, dict) else {}
        if action == "announce":
            self.call(
                settings, c, "POST", "announce", {"message": _message(params, required=True)}, timeout=timeout
            )
            return {"ok": True}
        if action in ("kick", "ban"):
            body: dict[str, Any] = {"userid": _userid(params)}
            if (message := _message(params, required=False)) is not None:
                body["message"] = message
            self.call(settings, c, "POST", action, body, timeout=timeout)
            return {"ok": True, "userid": body["userid"]}
        if action == "unban":
            userid = _userid(params)
            self.call(settings, c, "POST", "unban", {"userid": userid}, timeout=timeout)
            return {"ok": True, "userid": userid}
        if action == "save":
            self.call(settings, c, "POST", "save", timeout=timeout)
            return {"ok": True}
        # shutdown: the server saves and exits after waittime seconds (the panel turns this into its
        # own stop, or Docker's restart policy would start it again)
        waittime = params.get("waittime", 1)
        if not isinstance(waittime, int) or isinstance(waittime, bool) or not 0 <= waittime <= WAITTIME_MAX:
            raise AgentError(f"'waittime' must be 0 to {WAITTIME_MAX} seconds", "invalid_request", 400)
        body = {"waittime": waittime, "message": _message(params, required=False) or SHUTDOWN_MESSAGE}
        self.call(settings, c, "POST", "shutdown", body, timeout=timeout)
        return {"ok": True, "waittime": waittime}


__all__ = ["PalworldChannel", "PalworldDriver"]
