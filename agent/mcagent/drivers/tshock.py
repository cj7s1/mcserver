"""``tshock-rest``: TShock (Terraria server with an admin layer) through its REST API, with its
console (stdin) as the fallback.

What the spike of 2026-11-10 and the probe of this driver observed (``ryshe/terraria:tshock-*`` run
directly, as the runtime uid:gid, with no capabilities):

* The REST API is switched on in ``config.json`` (the panel writes ``RestApiEnabled``,
  ``RestApiPort`` 7878, ``LogRest`` false and one application token for the user ``mcpanel``
  in ``ApplicationRestTokens`` before every start). It listens on every interface of the
  container; the profile never publishes 7878, and the agent reaches it over the instance network.
* Calls are ``GET`` with ``token=`` in the query (TShock's design); never logged here, and
  ``LogRest`` stays off so TShock does not log them either. The token is read from ``config.json``
  in the server folder (no symbolic link followed, size-capped), so it never travels in requests.
* ``/v3/server/rawcmd?cmd=/<command>`` runs a console command as the token's user (superadmin) and
  answers its output lines (with colour tags ``[c/RRGGBB:text]``); ``/v2/server/broadcast``;
  ``/v2/world/save`` answers after the world was written (synchronous); ``/v2/server/status``
  (``players=true``) answers players and limits without a token; ``/v2/users/create`` and
  ``/v2/users/update`` manage accounts (the panel's owner account, so the one-time ``/setup``
  code is never needed).
* Without the API (not configured, still starting, broken config): commands and saves go to the
  console through stdin (the profile opens it); a save then waits for ``Backing up world file``.
* SIGINT saves and exits 0 (the profile's stop signal), so the stop is a plain ``docker stop``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from ..errors import AgentError, game_text
from .base import ControlDriver, Report, Send, WaitLog
from .console import ConsoleDriver
from .http_api import GameApiError, http_request

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings

log = logging.getLogger(__name__)

CONFIG_MAX_BYTES = 2 * 1024 * 1024
REST_TIMEOUT = 10.0
REST_MAX_BYTES = 4 * 1024 * 1024
# never through a proxy from the environment: the API is on the instance network
SAVE_TIMEOUT = 120.0
_COLOUR_TAG = re.compile(r"\[c/[0-9A-Fa-f]{6}:([^\]]*)\]")
_ACCOUNT = re.compile(r"^[^\s\"]{1,32}$")
_GROUP = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _ops() -> Any:
    from .. import docker_ops  # imported late: docker_ops imports the drivers

    return docker_ops


class RestUnavailable(AgentError):
    """The REST API cannot be used now (no token configured, not listening yet)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "rest_unavailable", 409)


def plain(text: str) -> str:
    """TShock's colour tags ``[c/RRGGBB:text]`` as their text."""
    return _COLOUR_TAG.sub(r"\1", str(text))


class TShockChannel:
    """The saving pause's ``send`` for TShock: ``save`` through the API (synchronous), any other
    command as a raw command; the console when the API cannot be used."""

    def __init__(self, driver: TShockDriver, settings: Settings, instance_id: UUID) -> None:
        self.driver = driver
        self.settings = settings
        self.instance_id = instance_id

    def container(self) -> Container:
        return _ops().running_container(self.instance_id)

    def __call__(self, command: str, timeout: float) -> None:
        c = self.container()
        if command.strip().lstrip("/") == "save":
            self.driver.save_now(self.settings, c, timeout)
        else:
            self.driver.command_sync(self.settings, c, command, timeout)


class TShockDriver(ControlDriver):
    id: ClassVar[str] = "tshock-rest"
    resume_label = "save"

    def __init__(
        self,
        *,
        port: int = 7878,
        config_file: str = "config.json",
        username: str = "mcpanel",
        save_done_log: str = r"Backing up world file",
        max_line_len: int = 256,
    ) -> None:
        if not 1 <= int(port) <= 65535:
            raise ValueError("Invalid TShock REST port")
        if config_file.startswith("/") or ".." in config_file.split("/"):
            raise ValueError(f"Invalid TShock config file {config_file!r}")
        self.port = int(port)
        self.config_file = config_file
        self.username = username
        self.console = ConsoleDriver(
            transport="stdin", save_done_log=save_done_log, max_line_len=max_line_len, broadcast="say {text}"
        )

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> TShockDriver:
        return cls(**dict(options))

    # --- the token and the address ---------------------------------------------------------------------
    def secret(self, settings: Settings, c: Container) -> str | None:
        """The application token of ``username`` in the server's ``config.json``; None without one."""
        from ..safefs import PathError, SafeRoot

        ops = _ops()
        instance = (c.labels or {}).get(ops.LABEL_INSTANCE)
        if not instance:
            return None
        try:
            root = SafeRoot(ops.instance_dir(settings, UUID(instance)))
            data = json.loads(root.read_bytes(self.config_file, CONFIG_MAX_BYTES).decode("utf-8-sig"))
        except (PathError, ValueError, OSError, RecursionError) as exc:
            log.debug("TShock config of %s not readable: %s", c.name, game_text(exc))
            return None
        tokens = ((data.get("Settings") or {}) if isinstance(data, dict) else {}).get("ApplicationRestTokens")
        if not isinstance(tokens, dict):
            return None
        for token, entry in tokens.items():
            if isinstance(entry, dict) and entry.get("Username") == self.username and len(str(token)) >= 16:
                return str(token)
        return None

    def address(self, settings: Settings, c: Container) -> tuple[str, int]:
        ops = _ops()
        finder = getattr(ops, "container_address", None)
        target = finder(c, self.port, "tcp", settings) if finder is not None else None
        if target is None:
            if (c.attrs.get("HostConfig") or {}).get("NetworkMode") == "host":
                return "127.0.0.1", self.port
            for net in ((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}).values():
                if net.get("IPAddress"):
                    return str(net["IPAddress"]), self.port
            raise RestUnavailable("The server's container has no address on its network")
        return target

    # --- REST -------------------------------------------------------------------------------------------
    def call(
        self,
        settings: Settings,
        c: Container,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        token: bool = True,
        timeout: float = REST_TIMEOUT,
    ) -> dict[str, Any]:
        """One ``GET`` of the API; the answer's JSON. RestUnavailable when it cannot be reached (or
        has no token), 502 ``rest_failed`` when TShock answers with an error. The token never
        appears in an error message."""
        query = dict(params or {})
        if token:
            secret = self.secret(settings, c)
            if not secret:
                raise RestUnavailable("TShock's REST API has no token for the panel (config.json)")
            query["token"] = secret
        host, port = self.address(settings, c)
        try:
            # a plain HTTP GET on the instance network (TShock's REST API has no TLS); no proxy. One
            # deadline for the whole exchange (B-2: an answer trickled a byte at a time ends there too)
            resp = http_request(
                host,
                port,
                "GET",
                f"{path}?{urllib.parse.urlencode(query)}",
                scheme="http",
                timeout=timeout,
                max_response=REST_MAX_BYTES,
            )
        except GameApiError as exc:
            if exc.code == "api_timeout":
                raise AgentError(f"TShock did not answer {path} in time", "rest_failed", 504) from None
            if exc.code == "api_unavailable":
                raise RestUnavailable("TShock's REST API is not reachable") from None
            raise AgentError(f"TShock's answer to {path} was refused: {exc}", "rest_failed", 502) from None
        code, body = resp.status, resp.body  # TShock answers errors as JSON too
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError) as exc:
            raise AgentError(
                f"TShock answered {path} with something that is not JSON", "rest_failed", 502
            ) from exc
        status = str(data.get("status", code)) if isinstance(data, dict) else str(code)
        if status != "200":
            error = game_text(
                plain(str((data or {}).get("error") or (data or {}).get("response") or f"HTTP {status}"))
            )
            raise AgentError(f"TShock: {error}", "rest_failed", 502)
        return data

    # --- commands ---------------------------------------------------------------------------------------
    def command_sync(self, settings: Settings, c: Container, text: str, timeout: float) -> str:
        line = self.console.sanitise(text)
        command = line if line.startswith("/") else f"/{line}"
        try:
            data = self.call(settings, c, "/v3/server/rawcmd", {"cmd": command}, timeout=min(timeout, 60.0))
        except RestUnavailable as exc:
            log.info("TShock REST not usable for a command (%s); using the console", exc)
            return self.console.command_sync(settings, c, line.lstrip("/"), timeout)
        reply = data.get("response")
        lines = reply if isinstance(reply, list) else [reply] if reply else []
        return "\n".join(plain(x) for x in lines)

    async def command(self, settings: Settings, c: Container, text: str, timeout: float) -> str:
        return await asyncio.to_thread(self.command_sync, settings, c, text, timeout)

    def channel(self, settings: Settings, instance_id: UUID) -> Send | None:
        return TShockChannel(self, settings, instance_id)

    # --- saving -----------------------------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return True

    def save_now(self, settings: Settings, c: Container, timeout: float = SAVE_TIMEOUT) -> str:
        """Save the world: ``/v2/world/save`` (answers when written), else ``save`` on the console and
        its "Backing up world file" line. Returns how it was saved."""
        try:
            self.call(settings, c, "/v2/world/save", timeout=timeout)
            return "rest"
        except RestUnavailable as exc:
            log.info("TShock REST not usable for a save (%s); using the console", exc)
        since = time.time() - 0.2
        self.console.write(c, "save")
        if not _ops().wait_for_log(c, str(self.console.save_done_log), since, timeout):
            raise AgentError(
                f"The server did not report that the save finished within {timeout:g} s",
                "save_unconfirmed",
                409,
            )
        return "console"

    def save(self, send: Send, report: Report, wait_log: WaitLog | None = None) -> None:
        report(0, "Saving the world (TShock)")
        send("save", SAVE_TIMEOUT)

    # --- broadcast, status ------------------------------------------------------------------------------
    def broadcast(self, settings: Settings, c: Container, text: str) -> None:
        message = self.console.sanitise(" ".join(str(text).split()))
        try:
            self.call(settings, c, "/v2/server/broadcast", {"msg": message})
        except RestUnavailable:
            self.console.write(c, f"say {message}")

    def status(self, settings: Settings, c: Container, timeout: float = REST_TIMEOUT) -> dict[str, Any]:
        """Players and limits like the server list ping answers them (``online``, ``max``,
        ``sample``, ``version``, ``motd``, ``latency_ms``), from ``/v2/server/status``."""
        start = time.monotonic()
        data = self.call(settings, c, "/v2/server/status", {"players": "true"}, token=False, timeout=timeout)
        players = []
        for p in data.get("players") or []:
            name = p.get("nickname") or p.get("username") if isinstance(p, dict) else p
            if name:
                players.append(str(name))
        return {
            "online": int(data.get("playercount") or len(players)),
            "max": int(data.get("maxplayers") or 0),
            "sample": players,
            "version": f"TShock {data.get('tshockversion', '?')} (Terraria {str(data.get('serverversion', '?')).lstrip('v')})",
            "motd": str(data.get("name") or ""),
            "world": str(data.get("world") or ""),
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
        }

    def query_endpoint(self, settings: Settings, c: Container) -> tuple[str, int]:
        host, port = self.address(settings, c)
        from . import base

        target_cls = getattr(base, "QueryTarget", None)
        if target_cls is None:
            return host, port

        async def query(timeout: float) -> dict[str, Any]:
            return await asyncio.to_thread(self.status, settings, c, timeout)

        return target_cls(host, port, query)

    def ready(self, settings: Settings, c: Container) -> bool | None:
        try:
            self.call(settings, c, "/v2/server/status", token=False, timeout=2.0)
            return True
        except AgentError:
            return False

    # --- the owner account (setup) ------------------------------------------------------------------------
    def game_action(
        self, settings: Settings, c: Container, action: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """``owner_account``: create (or set the password of) the account ``user`` in group ``group``
        (default ``owner``) with ``password``. The password is never logged or returned."""
        if action != "owner_account":
            return super().game_action(settings, c, action, params, timeout)
        user = str(params.get("user") or "")
        password = str(params.get("password") or "")
        group = str(params.get("group") or "owner")
        if not _ACCOUNT.match(user) or not _GROUP.match(group):
            raise AgentError("Invalid account or group name", "invalid_request", 400)
        if not 4 <= len(password) <= 128 or any(ord(ch) < 32 for ch in password):
            raise AgentError("The password must be 4 to 128 printable characters", "invalid_request", 400)
        existing = self.call(settings, c, "/v2/users/list", timeout=timeout).get("users") or []
        if any(isinstance(u, dict) and str(u.get("name")) == user for u in existing):
            self.call(
                settings,
                c,
                "/v2/users/update",
                {"type": "name", "user": user, "password": password, "group": group},
                timeout=timeout,
            )
            return {"account": user, "group": group, "created": False}
        self.call(
            settings,
            c,
            "/v2/users/create",
            {"user": user, "password": password, "group": group},
            timeout=timeout,
        )
        return {"account": user, "group": group, "created": True}


__all__ = ["RestUnavailable", "TShockChannel", "TShockDriver", "plain"]
