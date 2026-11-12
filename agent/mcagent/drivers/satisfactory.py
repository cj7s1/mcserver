"""``satisfactory-api``: Satisfactory's Dedicated Server HTTPS API (and its UDP Lightweight Query).

Observed on ``wolveix/satisfactory-server:v1.9.11`` (game build CL 517554, 2026-11-10; the
spike's section 5):

* Every function is ``POST https://<container>:7777/api/v1`` with ``{"function", "data"}``; answers
  are ``{"data": {...}}`` (keys in camelCase; read case-insensitively here), or HTTP 202/204 without
  a body. **Errors usually come back with HTTP 200** and ``{"errorCode", "errorMessage"}`` (only
  ``insufficient_scope`` was 403 and an invalid token 401), so every answer's body is checked.
* ``HealthCheck`` needs no token. An unclaimed server hands out an ``InitialAdmin`` token through
  ``PasswordlessLogin``; ``ClaimServer`` (server name + admin password) makes it an Administrator
  token; ``RunCommand server.GenerateAPIToken`` prints a non-expiring API token
  (``... Token: <token>``). After the claim a ``Client`` login stays passwordless until
  ``SetClientPassword`` sets one.
* ``CreateNewGame`` needs ``GameModeSettings`` (the wiki does not list it) and answers 202 once the
  map is loaded; ``SaveGame`` answers 204 once the file is written; ``Shutdown`` (204) exits the
  game cleanly with exit code 0. Signals do not: the image's ``run.sh`` (PID 1) forwards SIGINT
  and returns at once, so the game is killed mid-exit. **No stop saves.** So a stop here is
  ``SaveGame`` -> ``Shutdown`` -> wait for the exit, and ``docker stop`` only as the fallback (and
  afterwards, which is a no-op for an exited container but tells Docker the stop was meant).
* The UDP query (``0xF6D5`` envelope on 7777/udp) answers during map loads too: state 1 Idle (no
  game), 2 Loading, 3 Playing, the build (``ServerNetCL``) and the server name.

Saves made by the panel are named ``<session>_mcpanel`` (one per session, overwritten each time),
so the newest save of the session, which the server loads at its next start, is the one the stop
wrote.
"""

from __future__ import annotations

import logging
import re
import struct
import time
import uuid as uuidlib
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from .. import claimmarker
from ..errors import AgentError, game_text
from ..slp import count_or_none
from .base import QueryTarget, Report, Send, WaitLog
from .http_api import GameApiError, HttpApiDriver, _ops

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings
    from ..docker_ops import StopResult

log = logging.getLogger(__name__)

API_PATH = "/api/v1"
API_TIMEOUT = 30.0
HEALTH_TIMEOUT = 1.5
SAVE_TIMEOUT = 120.0
SHUTDOWN_TIMEOUT = 15.0
LOAD_TIMEOUT = 180.0  # CreateNewGame / LoadGame / UploadSaveGame block while the map loads
STOP_POLL = 0.5
MIN_EXIT_WAIT = 5.0  # seconds the stop waits for the exit after Shutdown, at least
CLAIM_WAIT = 20.0  # how long the publish guard waits for the API of a running server to answer
CLAIM_PROBE_TIMEOUT = 5.0  # one claim probe (PasswordlessLogin)
UPLOAD_MAX_BYTES = 1024 * 1024 * 1024
QUERY_MAGIC = 0xF6D5
STATES = {0: "Offline", 1: "Idle", 2: "Loading", 3: "Playing"}
SAVE_SUFFIX = "_mcpanel"
SAVES_DIR = "saved/server"  # the server's save files (``<save name>.sav``), relative to the server folder

NAME_RE = re.compile(r"^[A-Za-z0-9 _.'-]{1,64}$")  # server names the panel sends
SESSION_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OPTION_KEY_RE = re.compile(r"^FG\.[A-Za-z0-9.]{1,64}$")
TOKEN_LINE_RE = re.compile(r"Token:\s*(\S+)")

# UDP query: magic, type 0 (poll), protocol 1, cookie, terminator
QUERY_COOKIE = 0x6D63_7061_6E65_6C21  # "mcpanel!"
POLL_PACKET = struct.pack("<HBBQB", QUERY_MAGIC, 0, 1, QUERY_COOKIE, 1)
# What a reply starts with (magic little-endian, type 1 = response, protocol 1), read as latin-1:
# the pattern of the profile's udp-query readiness probe.
REPLY_PATTERN = r"\A\xd5\xf6\x01\x01"


def _key(data: Any, name: str) -> Any:
    """``data[name]`` with the key compared case-insensitively (the API answers in camelCase, the
    documentation writes PascalCase)."""
    if not isinstance(data, dict):
        return None
    if name in data:
        return data[name]
    lowered = name.lower()
    for k, v in data.items():
        if str(k).lower() == lowered:
            return v
    return None


def parse_query_reply(reply: bytes) -> dict[str, Any]:
    """The Lightweight Query API's response (all little-endian): magic, type 1, protocol, cookie,
    state (u8), build (u32), flags (u64), sub-state count (u8) and that many (id u8, version u16),
    name length (u16), the name (UTF-8), terminator 0x01."""
    if len(reply) < 26:
        raise ValueError("short reply")
    magic, kind, _proto, cookie, state, net_cl, flags, count = struct.unpack_from("<HBBQBIQB", reply, 0)
    if magic != QUERY_MAGIC or kind != 1:
        raise ValueError("not a Satisfactory query reply")
    offset = 26
    substates = []
    for _ in range(count):
        if offset + 3 > len(reply):
            raise ValueError("truncated sub-states")
        sid, version = struct.unpack_from("<BH", reply, offset)
        substates.append([sid, version])
        offset += 3
    if offset + 2 > len(reply):
        raise ValueError("truncated name length")
    (length,) = struct.unpack_from("<H", reply, offset)
    offset += 2
    name = reply[offset : offset + length].decode("utf-8", errors="replace")
    return {
        "cookie_ok": cookie == QUERY_COOKIE,
        "state": state,
        "state_name": STATES.get(state, str(state)),
        "net_cl": net_cl,
        "modded": bool(flags & 1),
        "substates": substates,
        "name": name,
    }


def udp_query(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            sock.send(POLL_PACKET)
            reply = sock.recv(4096)
        except OSError as exc:
            raise GameApiError(
                f"The server does not answer the UDP query: {exc}", "api_unavailable", 409
            ) from exc
    try:
        return parse_query_reply(reply)
    except (ValueError, struct.error) as exc:
        raise GameApiError(f"Unexpected UDP query reply: {exc}", "api_error", 502) from exc


_HASHED_TAIL_RE = re.compile(r"_[0-9a-f]{8}\Z")


def save_name_for(session: str) -> str:
    """The name of the panel's save of a session (``SaveGame``'s SaveName), different for every
    session name (two sessions must never write each other's save).

    A session name of letters, digits, ``_`` and ``-`` that fits keeps ``<session>_mcpanel`` (the
    name servers had before). Any other one (spaces, other characters, too long, or one that
    already ends like a hashed name, ``_<8 hex>``) becomes its sanitised start + ``_`` + 8 hex
    characters of the SHA-256 of the exact session name + ``_mcpanel``: never the name of a plain
    session, at most 64 characters, matching SAVE_NAME_RE."""
    import hashlib

    if (
        re.fullmatch(r"[A-Za-z0-9_-]+", session)
        and len(session) + len(SAVE_SUFFIX) <= 64
        and not _HASHED_TAIL_RE.search(session)
    ):
        return session + SAVE_SUFFIX
    digest = hashlib.sha256(session.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]
    room = 64 - len(SAVE_SUFFIX) - 1 - len(digest)
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", session).strip("_")[:room].rstrip("_") or "session"
    return f"{base}_{digest}{SAVE_SUFFIX}"


def claim_file_for(game_port: int) -> str:
    """Where the game keeps the claim of a server with this game port, relative to the server
    folder (``saved/`` is the game's SaveGames)."""
    return f"saved/ServerSettings.{int(game_port)}.sav"


def _api_not_up(exc: GameApiError) -> bool:
    """The API does not answer (yet): nothing listens, no answer in time, or an HTTP 5xx without
    a game error code (the server is loading). Anything else is an answer."""
    if exc.code in ("api_unavailable", "api_timeout"):
        return True
    return exc.api_code is None and exc.http_status is not None and exc.http_status >= 500


def _text(value: Any, what: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AgentError(f"Invalid {what}", "invalid_request", 400)
    return value


class SatisfactoryDriver(HttpApiDriver):
    id: ClassVar[str] = "satisfactory-api"

    def __init__(
        self,
        *,
        port: int,
        scheme: str = "https",
        secret_file: str | None = None,
        claim_file: str | None = None,
    ) -> None:
        super().__init__(port=port, scheme=scheme, secret_file=secret_file)
        # the file of the server folder that holds the claim (ServerSettings.<game port>.sav)
        claim_file = claim_file or claim_file_for(self.port)
        if claim_file.startswith("/") or ".." in claim_file.split("/"):
            raise ValueError(f"Invalid claim file {claim_file!r}")
        self.claim_file = claim_file

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> SatisfactoryDriver:
        return cls(
            port=int(options.get("port") or 7777),
            scheme="https",
            secret_file=str(options["secret_file"]) if options.get("secret_file") else None,
            claim_file=str(options["claim_file"]) if options.get("claim_file") else None,
        )

    # --- the API ---------------------------------------------------------------------------------------
    def call(
        self,
        settings: Settings,
        c: Container,
        function: str,
        data: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        timeout: float = API_TIMEOUT,
    ) -> dict[str, Any] | None:
        """One API function; its ``data`` (None for an answer without a body). ``token`` None: no
        Authorization header. An ``errorCode`` in the body is an error whatever the HTTP status."""
        import json

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps({"function": function, "data": data or {}}).encode()
        resp = self.request(settings, c, "POST", API_PATH, body=body, headers=headers, timeout=timeout)
        return self._result(function, resp.status, resp.json())

    @staticmethod
    def _result(function: str, status: int, parsed: Any) -> dict[str, Any] | None:
        if isinstance(parsed, dict) and _key(parsed, "errorCode"):
            # the game's own text: one line, clipped (B-9)
            code = game_text(_key(parsed, "errorCode"), 100)
            message = game_text(_key(parsed, "errorMessage") or code)
            agent_code, agent_status = "api_error", 502
            if status == 401 or code in ("invalid_token", "token_invalid"):
                agent_code, agent_status = "api_unauthorized", 409
            elif code == "insufficient_scope":
                agent_code, agent_status = "api_forbidden", 409
            elif code in (
                "server_claimed",
                "passwordless_login_not_possible",
                "wrong_password",
                "missing_params",
                "invalid_params",
            ):
                agent_status = 409
            raise GameApiError(
                f"{function}: {message} ({code})", agent_code, agent_status, api_code=code, http_status=status
            )
        if status == 401:
            raise GameApiError(
                f"{function}: the server refused the API token (run the setup's claim step again)",
                "api_unauthorized",
                409,
                http_status=status,
            )
        if status >= 400:
            raise GameApiError(f"{function}: HTTP {status}", "api_error", 502, http_status=status)
        if isinstance(parsed, dict):
            data = _key(parsed, "data")
            return data if isinstance(data, dict) else {}
        return None

    def api_token(self, settings: Settings, c: Container) -> str:
        token = self.secret(settings, c)
        if not token:
            raise GameApiError(
                "The panel has no API token for this server yet: run its setup (claim) first",
                "api_unauthorized",
                409,
            )
        return token

    def _container_for(self, instance_id: UUID) -> Container:
        return _ops().running_container(instance_id)

    # --- readiness, query --------------------------------------------------------------------------------
    def health(self, settings: Settings, c: Container, timeout: float = HEALTH_TIMEOUT) -> str | None:
        data = self.call(settings, c, "HealthCheck", {"ClientCustomData": ""}, timeout=timeout)
        health = _key(data, "health")
        return str(health) if health is not None else None

    def ready(self, settings: Settings, c: Container) -> bool | None:
        """The API answers ``HealthCheck`` with "healthy" (or "slow": up, low tick rate)."""
        try:
            return self.health(settings, c) in ("healthy", "slow")
        except AgentError:
            return False

    def wait_ready(self, settings: Settings, c: Container, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while True:
            try:
                if self.health(settings, c, timeout=min(5.0, max(0.5, deadline - time.monotonic()))) in (
                    "healthy",
                    "slow",
                ):
                    return
            except AgentError as exc:
                last = exc
            if time.monotonic() >= deadline:
                raise GameApiError(
                    f"The server's API did not answer within {timeout:g} s" + (f": {last}" if last else ""),
                    "api_unavailable",
                    409,
                )
            time.sleep(1.0)

    def state(self, settings: Settings, c: Container, token: str | None = None) -> dict[str, Any]:
        data = self.call(settings, c, "QueryServerState", {}, token=token or self.api_token(settings, c))
        state = _key(data, "serverGameState")
        return state if isinstance(state, dict) else {}

    def udp(self, settings: Settings, c: Container, timeout: float = 2.0) -> dict[str, Any]:
        target = _ops().container_address(c, self.port, "udp", settings)
        if target is None:
            raise GameApiError("The server's UDP query port cannot be reached", "api_unavailable", 409)
        return udp_query(target[0], target[1], timeout)

    def query_endpoint(self, settings: Settings, c: Container) -> tuple[str, int]:
        host, port = self.address(settings, c)

        async def query(timeout: float) -> dict[str, Any]:
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(self.status_answer, settings, c, timeout)

        return QueryTarget(host, port, query)

    def status_answer(self, settings: Settings, c: Container, timeout: float) -> dict[str, Any]:
        """What ``GET /ping`` answers for a Satisfactory server: the player count (no names: the
        API has none), the limit, the build, the server name, the state and the session."""
        started = time.monotonic()
        info = self.udp(settings, c, timeout=min(timeout, 2.0))
        latency = round((time.monotonic() - started) * 1000, 1)
        answer: dict[str, Any] = {
            "online": None,
            "max": None,
            "sample": [],
            "version": f"CL {info['net_cl']}",
            "motd": info["name"],
            "latency_ms": latency,
            "state": info["state_name"],
        }
        token = self.secret(settings, c)
        if token and info["state"] != 2:  # the HTTPS API is down while a map loads
            state = self.state(settings, c, token)
            answer.update(
                online=count_or_none(_key(state, "numConnectedPlayers")),  # None when not a count (F-2)
                max=count_or_none(_key(state, "playerLimit")),
                session=_key(state, "activeSessionName"),
                game_running=_key(state, "isGameRunning"),
                tick_rate=_key(state, "averageTickRate"),
            )
        return answer

    # --- saving ----------------------------------------------------------------------------------------
    @property
    def can_save(self) -> bool:
        return True

    @staticmethod
    def game_loaded(state: dict[str, Any]) -> bool:
        """Whether ``QueryServerState`` describes a loaded game. Without one (state 1 Idle, e.g. after
        the settings file with ``autoLoadSessionName`` was lost) the server still names a default
        session (``SERVER-SESSION<digits>``) but ``isGameRunning`` is false, and ``SaveGame`` does not
        answer (the real-container gate of 2026-11-11: 60-120 s timeouts)."""
        running = _key(state, "isGameRunning")
        if running is not None:
            return bool(running)
        return bool(_key(state, "activeSessionName"))

    def save_now(self, settings: Settings, c: Container, timeout: float = SAVE_TIMEOUT) -> str | None:
        """``SaveGame`` of the loaded session as ``<session>_mcpanel``; the API answers once the file
        is written. None when no game is loaded (nothing to save; ``SaveGame`` is not called)."""
        token = self.api_token(settings, c)
        state = self.state(settings, c, token)
        session = str(_key(state, "activeSessionName") or "")
        if not session or not self.game_loaded(state):
            return None
        name = save_name_for(session)
        self.call(settings, c, "SaveGame", {"SaveName": name}, token=token, timeout=timeout)
        return name

    def channel(self, settings: Settings, instance_id: UUID) -> Send:
        def send(command: str, timeout: float) -> Any:
            if command != "save":
                raise AgentError(f"Unknown operation {command!r}", "command_unsupported", 409)
            return self.save_now(settings, self._container_for(instance_id), timeout)

        return send

    def save(self, send: Send, report: Report, wait_log: WaitLog | None = None) -> str | None:
        """``"nothing_loaded"`` when no game was loaded (nothing was saved; the files are at rest)."""
        report(0, "Saving the game (SaveGame)")
        if send("save", SAVE_TIMEOUT) is None:
            return "nothing_loaded"
        return None

    # --- stop ----------------------------------------------------------------------------------------
    def stop(self, settings: Settings, c: Container, timeout: int, result: StopResult) -> bool:
        """SaveGame, then Shutdown, then wait for the container to exit (exit code 0); ``docker
        stop`` when the API cannot be used (still starting or downloading), when Shutdown failed or
        when the server has not exited within ``timeout``, and once more afterwards (nothing for an
        exited container; tells Docker the stop was intended)."""
        if c.status != "running":
            return False
        token = self.secret(settings, c)
        if not token:
            result.warnings.append(
                "The game was not saved before the stop: the server has no API token yet (not set up)"
            )
            return False
        ops = _ops()
        try:
            saved = self.save_now(settings, c, ops.flush_timeout(timeout))
            if saved is None:
                result.warnings.append("No game was loaded, so there was nothing to save before the stop")
            else:
                result.flushed = True
        except Exception as exc:  # also a reply the parser did not expect: the stop goes on (B-3)
            result.warnings.append(f"The game was not saved before the stop (SaveGame): {game_text(exc)}")
        try:
            self.call(settings, c, "Shutdown", {}, token=token, timeout=SHUTDOWN_TIMEOUT)
        except Exception as exc:
            result.warnings.append(
                f"The Shutdown API call failed ({game_text(exc)}); the server was stopped with a signal instead"
            )
            c.stop(timeout=timeout)
            return True
        deadline = time.monotonic() + max(MIN_EXIT_WAIT, timeout)
        while time.monotonic() < deadline:
            time.sleep(STOP_POLL)
            try:
                c.reload()
            except Exception as exc:  # removed meanwhile, or Docker trouble: docker stop decides below
                log.debug("reload of %s while it shut down: %s", c.name, exc)
                break
            if c.status != "running":
                break
        else:
            result.warnings.append(
                f"The server did not exit within {timeout} s after Shutdown; it was stopped with a signal"
            )
        c.stop(timeout=timeout)
        return True

    # --- claiming and the other operations of the panel -----------------------------------------------------
    guards_publish: ClassVar[bool] = True

    def is_unclaimed(self, settings: Settings, c: Container, timeout: float = CLAIM_PROBE_TIMEOUT) -> bool:
        """Whether an InitialAdmin login works (only while nobody claimed the server)."""
        try:
            self.call(
                settings, c, "PasswordlessLogin", {"MinimumPrivilegeLevel": "InitialAdmin"}, timeout=timeout
            )
        except GameApiError as exc:
            if exc.api_code == "passwordless_login_not_possible":
                return False
            raise
        return True

    def claim_state(self, settings: Settings, c: Container) -> str:
        """``"down"`` while the API does not answer (nobody can claim through it either),
        ``"unclaimed"`` while an InitialAdmin login works, ``"claimed"`` when the server refuses it
        with ``passwordless_login_not_possible``. Any other answer (a different TLS certificate
        than earlier in the run, another error code) raises: the caller fails closed."""
        try:
            self.health(settings, c)
        except GameApiError as exc:
            if _api_not_up(exc):
                return "down"
            raise
        try:
            return "unclaimed" if self.is_unclaimed(settings, c) else "claimed"
        except GameApiError as exc:
            if _api_not_up(exc):
                return "down"
            raise

    def claim_file_present(self, settings: Settings, instance_id: UUID | str) -> bool:
        """Whether the server folder holds the claim file as a regular, non-empty file. Checked
        with lstat through SafeRoot: a symbolic link (of the file or of ``saved/``) counts as
        missing."""
        from ..safefs import PathError, SafeRoot

        try:
            entry = SafeRoot(_ops().instance_dir(settings, UUID(str(instance_id)))).stat(self.claim_file)
        except (PathError, ValueError, OSError) as exc:
            log.debug("claim file of %s: %s", instance_id, exc)
            return False
        return entry.type == "file" and entry.size > 0

    def publish_guard(self, settings: Settings, instance_id: UUID, existing: Container | None) -> None:
        """Never publish an unclaimed server (409 ``server_unclaimed``):

        * the server folder must hold the claim (``saved/ServerSettings.7777.sav``, a regular,
          non-empty file, no link), the claim marker must match it (``claimmarker``: the agent
          verified the claim through the API since the file last changed; the game writes the file
          before anybody claimed it) and the API token the panel writes before every apply must be
          there (a token file alone proves nothing: it stays when the claim is lost);
        * a running (or restarting) container must refuse an InitialAdmin login. The guard waits
          up to CLAIM_WAIT seconds for its API; when the claim cannot be verified (no answer,
          an unexpected answer) the publish is refused too (409 ``claim_unverified``).

        A stopped container or none is judged by the files; its next run is checked by the claim
        guard loop (``claimguard``) as soon as its API answers."""
        message = (
            "This Satisfactory server is not claimed, so its ports are not published "
            "(whoever reaches an unclaimed server first owns it). Run its setup (claim) first."
        )
        if not self.claim_file_present(settings, instance_id):
            raise AgentError(
                message + f" Its claim ({self.claim_file}) is missing from the server folder.",
                "server_unclaimed",
                409,
            )
        if not claimmarker.matches(settings, UUID(str(instance_id)), self.claim_file):
            # the file is written before anybody claims the server: only the agent's own verification
            # (on an unpublished container) counts, recorded in the claim marker
            raise AgentError(
                message + f" Its claim has not been verified since {self.claim_file} last changed "
                f"({claimmarker.MARKER} is missing or does not match).",
                "server_unclaimed",
                409,
            )
        if not self.token_for(settings, instance_id):
            raise AgentError(message, "server_unclaimed", 409)
        if existing is None or getattr(existing, "status", None) not in ("running", "restarting"):
            return
        deadline = time.monotonic() + CLAIM_WAIT
        while True:
            try:
                state = self.claim_state(settings, existing)
            except AgentError as exc:
                log.warning("claim check of %s before publishing failed: %s", instance_id, exc)
                raise AgentError(
                    "The claim of this Satisfactory server could not be verified (its API answered "
                    f"unexpectedly: {exc}), so its ports are not published. Try again when the server answers.",
                    "claim_unverified",
                    409,
                ) from exc
            if state == "unclaimed":
                raise AgentError(message, "server_unclaimed", 409)
            if state == "claimed":
                return
            if time.monotonic() >= deadline:
                raise AgentError(
                    "The claim of this Satisfactory server could not be verified: its API did not answer "
                    f"within {CLAIM_WAIT:g} s, so its ports are not published. Try again when the server answers.",
                    "claim_unverified",
                    409,
                )
            time.sleep(1.0)

    ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "status",
            "claim",
            "verify_claim",
            "client_password",
            "new_game",
            "sessions",
            "save",
            "upload_save",
            "load_game",
            "options",
            "apply_options",
        }
    )

    # actions after which the game has written its settings file (claim, client password, the
    # autoload session of a new or loaded game): the claim marker is verified and written again
    CHANGES_SETTINGS: ClassVar[frozenset[str]] = frozenset(
        {"claim", "client_password", "new_game", "load_game", "upload_save"}
    )

    def game_action(
        self, settings: Settings, c: Container, action: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if action not in self.ACTIONS:
            return super().game_action(settings, c, action, params, timeout)
        params = params or {}
        if action not in self.CHANGES_SETTINGS:
            return getattr(self, f"_action_{action}")(settings, c, params, timeout)
        instance_id = self._instance_of(c)
        with claimmarker.panel_change(instance_id):
            if action == "claim":
                claimmarker.remove(settings, instance_id, "the claim runs again")
            valid_before = claimmarker.matches(settings, instance_id, self.claim_file)
            generation = claimmarker.generation(instance_id)
            result = getattr(self, f"_action_{action}")(settings, c, params, timeout)
            if action == "upload_save" and not result.get("loaded"):
                return result  # stored, not loaded: the settings file is unchanged
            try:
                result["claim_verified"] = self._refresh_marker(
                    settings,
                    c,
                    params.get("admin_password"),
                    generation=generation,
                    valid_before=valid_before,
                )
            except AgentError as exc:
                if action == "claim":
                    raise
                log.warning("The claim of %s could not be verified after %s: %s", c.name, action, exc)
                claimmarker.remove(settings, instance_id, f"the claim could not be verified after {action}")
                result["claim_verified"] = False
            return result

    def _instance_of(self, c: Container) -> UUID:
        try:
            return UUID(str((c.labels or {}).get(_ops().LABEL_INSTANCE)))
        except ValueError as exc:
            raise AgentError("The container names no server", "invalid_request", 400) from exc

    def verify_claim(self, settings: Settings, c: Container, admin_password: Any) -> None:
        """The claim holds and is this panel's: an InitialAdmin login is refused, an Administrator
        login with the panel's admin password (the server's control secret) works. Raises 409
        ``server_unclaimed`` / ``claimed_elsewhere`` / ``claim_unverified`` otherwise."""
        if not isinstance(admin_password, str) or not 8 <= len(admin_password) <= 128:
            raise AgentError(
                "The panel sent no admin password, so the claim cannot be verified", "claim_unverified", 409
            )
        try:
            unclaimed = self.is_unclaimed(settings, c)
        except GameApiError as exc:
            raise AgentError(f"The claim could not be verified: {exc}", "claim_unverified", 409) from exc
        if unclaimed:
            raise AgentError(
                "The server is not claimed (an InitialAdmin login works)", "server_unclaimed", 409
            )
        try:
            data = self.call(
                settings,
                c,
                "PasswordLogin",
                {"MinimumPrivilegeLevel": "Administrator", "Password": admin_password},
            )
        except GameApiError as exc:
            if exc.api_code == "wrong_password":
                raise AgentError(
                    "This server is claimed with another admin password, not by this panel",
                    "claimed_elsewhere",
                    409,
                ) from exc
            raise AgentError(f"The claim could not be verified: {exc}", "claim_unverified", 409) from exc
        if not _key(data, "authenticationToken"):
            raise AgentError("The admin login returned no token", "claim_unverified", 409)

    def _refresh_marker(
        self, settings: Settings, c: Container, admin_password: Any, *, generation: int, valid_before: bool
    ) -> bool:
        """Verify the claim and record the settings file in the claim marker. On a published
        container only when the marker matched right before the panel's operation (the claim was
        verified; the operation went through the panel's own token); otherwise the marker is removed
        and the next published start goes through the panel's setup on an unpublished container."""
        instance_id = self._instance_of(c)
        c.reload()
        if _ops().exposes_ports(c) and not valid_before:
            claimmarker.remove(
                settings,
                instance_id,
                "the settings file changed on a published server whose claim was not verified",
            )
            return False
        fp = claimmarker.settle(settings, instance_id, self.claim_file)
        if fp is None:
            claimmarker.remove(settings, instance_id, f"{self.claim_file} is missing")
            return False
        self.verify_claim(settings, c, admin_password)
        return claimmarker.write(settings, instance_id, self.claim_file, fp, since_generation=generation)

    def _action_verify_claim(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Verify the claim of a running server without published ports and write the claim marker
        (the panel's ``publish`` step, before the ports are opened)."""
        c.reload()
        if _ops().exposes_ports(c):
            raise AgentError(
                "The claim is verified on a server without published ports only", "claim_unverified", 409
            )
        instance_id = self._instance_of(c)
        generation = claimmarker.generation(instance_id)
        self.wait_ready(settings, c, timeout=max(5.0, min(timeout, 120.0)))
        fp = claimmarker.settle(settings, instance_id, self.claim_file)
        if fp is None:
            raise AgentError(f"{self.claim_file} is missing", "server_unclaimed", 409)
        self.verify_claim(settings, c, params.get("admin_password"))
        if not claimmarker.write(settings, instance_id, self.claim_file, fp, since_generation=generation):
            raise AgentError(
                "The server's files changed while its claim was verified; try again", "claim_unverified", 409
            )
        return {"claim_verified": True}

    def _action_status(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"api": None, "udp": None, "state": None}
        try:
            out["api"] = self.health(settings, c, timeout=min(timeout, 5.0))
        except AgentError as exc:
            out["api_error"] = str(exc)
        try:
            out["udp"] = self.udp(settings, c)
        except AgentError as exc:
            out["udp_error"] = str(exc)
        if self.secret(settings, c):
            try:
                out["state"] = self.state(settings, c)
            except AgentError as exc:
                out["state_error"] = str(exc)
        return out

    def _action_claim(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Claim the server (or log in when this panel claimed it before) and make an API token.

        The answer carries the token once, for the panel to store encrypted; nothing else ever
        returns it."""
        name = _text(params.get("server_name"), "server name", NAME_RE)
        password = params.get("admin_password")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise AgentError("Invalid admin password", "invalid_request", 400)
        self.wait_ready(settings, c, timeout=max(5.0, timeout - 30))
        claimed = "new"
        try:
            login = self.call(settings, c, "PasswordlessLogin", {"MinimumPrivilegeLevel": "InitialAdmin"})
            initial = str(_key(login, "authenticationToken") or "")
            data = self.call(
                settings, c, "ClaimServer", {"ServerName": name, "AdminPassword": password}, token=initial
            )
        except GameApiError as exc:
            if exc.api_code not in ("passwordless_login_not_possible", "server_claimed"):
                raise
            claimed = "existing"
            try:
                data = self.call(
                    settings,
                    c,
                    "PasswordLogin",
                    {"MinimumPrivilegeLevel": "Administrator", "Password": password},
                )
            except GameApiError as login_exc:
                if login_exc.api_code == "wrong_password":
                    raise AgentError(
                        "This server was claimed with another admin password, not by this panel. Restore a "
                        "backup taken while this panel owned it, or set the panel's control secret to that "
                        "admin password; do not delete the server settings file of a published server.",
                        "claimed_elsewhere",
                        409,
                    ) from login_exc
                raise
        admin = str(_key(data, "authenticationToken") or "")
        if not admin:
            raise GameApiError("The server did not return an admin token", "api_error", 502)
        result = self.call(settings, c, "RunCommand", {"Command": "server.GenerateAPIToken"}, token=admin)
        match = TOKEN_LINE_RE.search(str(_key(result, "commandResult") or ""))
        if not match:
            raise GameApiError("The server did not print an API token", "api_error", 502)
        token = match.group(1)
        # check the new token (VerifyAuthenticationToken answers missing_params even with a bearer token
        # on build 517554, so a function an API token may call is used)
        self.call(settings, c, "QueryServerState", {}, token=token)
        return {"token": token, "claimed": claimed}

    def _action_client_password(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        password = params.get("password")
        if not isinstance(password, str) or len(password) > 128 or any(ord(ch) < 32 for ch in password):
            raise AgentError("Invalid client password", "invalid_request", 400)
        token = self.api_token(settings, c)
        self.call(settings, c, "SetClientPassword", {"Password": password}, token=token)
        # check it took effect: a passwordless Client login works exactly when there is no password
        try:
            self.call(settings, c, "PasswordlessLogin", {"MinimumPrivilegeLevel": "Client"})
            open_now = True
        except GameApiError as exc:
            if exc.api_code != "passwordless_login_not_possible":
                raise
            open_now = False
        if open_now == bool(password):
            raise GameApiError("The server did not apply the client password", "api_error", 502)
        return {"protected": not open_now}

    def _wait_session(
        self, settings: Settings, c: Container, token: str, session: str | None, timeout: float
    ) -> dict[str, Any]:
        """Wait until the server plays (UDP state 3) and, with ``session``, that session is active."""
        deadline = time.monotonic() + timeout
        state: dict[str, Any] = {}
        while True:
            try:
                if self.udp(settings, c)["state"] == 3:
                    state = self.state(settings, c, token)
                    active = str(_key(state, "activeSessionName") or "")
                    if active and (session is None or active == session):
                        return state
            except AgentError:
                pass  # loading: the API is down for a moment
            if time.monotonic() >= deadline:
                raise GameApiError(
                    f"The server did not finish loading {session or 'the save'} within {timeout:g} s",
                    "api_timeout",
                    504,
                )
            time.sleep(1.0)

    def _action_new_game(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        session = _text(params.get("session_name"), "session name", SESSION_RE)
        token = self.api_token(settings, c)
        # a new game under the name of an existing session would mix its saves into that session
        existing = self.call(settings, c, "EnumerateSessions", {}, token=token)
        if any(_key(entry, "sessionName") == session for entry in _key(existing, "sessions") or []):
            raise AgentError(
                f"A session named {session!r} already exists on this server; choose another name",
                "session_exists",
                409,
            )
        data = {
            "NewGameData": {
                "SessionName": session,
                "MapName": "",
                "StartingLocation": "",
                "bSkipOnboarding": bool(params.get("skip_onboarding", True)),
                "AdvancedGameSettings": {},
                "CustomOptionsOnlyForModding": {},
                # required since 1.2 (missing_params without it), not in the wiki's list
                "GameModeSettings": {},
            }
        }
        self.call(settings, c, "CreateNewGame", data, token=token, timeout=min(timeout, LOAD_TIMEOUT))
        state = self._wait_session(settings, c, token, session, min(timeout, 60.0))
        return {"session": session, "state": state}

    def _action_sessions(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        token = self.api_token(settings, c)
        data = self.call(settings, c, "EnumerateSessions", {}, token=token)
        sessions = []
        for entry in _key(data, "sessions") or []:
            saves = []
            for header in _key(entry, "saveHeaders") or []:
                saves.append(
                    {
                        "name": _key(header, "saveName"),
                        "session": _key(header, "sessionName"),
                        "saved_at": _key(header, "saveDateTime"),
                        "play_seconds": _key(header, "playDurationSeconds"),
                        "build": _key(header, "buildVersion"),
                        "modded": _key(header, "isModdedSave"),
                    }
                )
            sessions.append({"name": _key(entry, "sessionName"), "saves": saves})
        current = _key(data, "currentSessionIndex")
        active = None
        if isinstance(current, int) and 0 <= current < len(sessions):
            active = sessions[current]["name"]
        # the session that is loaded now, as the server says (None while no game is loaded: the
        # index above still points at a session then)
        loaded = None
        try:
            state = self.state(settings, c, token)
            loaded = self.game_loaded(state)
            active = str(_key(state, "activeSessionName") or "") or None if loaded else None
        except AgentError as exc:
            log.debug("QueryServerState for the sessions of %s: %s", c.name, exc)
        return {"sessions": sessions, "current": active, "game_loaded": loaded}

    def _action_save(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        return {"save_name": self.save_now(settings, c, min(timeout, SAVE_TIMEOUT))}

    def _action_upload_save(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """``UploadSaveGame`` of a file the panel put into the server folder (multipart: the ``data``
        part with the function's JSON, the ``saveGameFile`` part with the file), loaded at once when
        ``load`` (the session name comes from the save's header)."""
        from ..safefs import PathError, SafeRoot

        path = params.get("path")
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
            raise AgentError("Invalid path", "invalid_request", 400)
        name = _text(params.get("save_name"), "save name", SAVE_NAME_RE)
        load = bool(params.get("load", True))
        token = self.api_token(settings, c)
        instance = (c.labels or {}).get(_ops().LABEL_INSTANCE)
        try:
            root = SafeRoot(_ops().instance_dir(settings, UUID(str(instance))))
        except ValueError as exc:
            raise AgentError("The container names no server", "invalid_request", 400) from exc
        # never overwrite a save the server has (the game would replace the file without asking)
        target = f"{SAVES_DIR}/{name}.sav"
        try:
            root.stat(target)
            exists = True
        except PathError as exc:
            if exc.code != "not_found":
                raise AgentError(f"Cannot check {target}: {exc}", "save_exists", 409) from exc
            exists = False
        if exists:
            raise AgentError(
                f"A save named {name!r} already exists on this server ({target}); choose another name",
                "save_exists",
                409,
            )
        if load:
            # loading the upload replaces the game that runs now: save it first (SaveGame answers once
            # the file is written; a failure stops the upload)
            self.save_now(settings, c, min(timeout, SAVE_TIMEOUT))
        import json

        boundary = f"mcpanel-{uuidlib.uuid4().hex}"
        meta = json.dumps(
            {
                "function": "UploadSaveGame",
                "data": {"SaveName": name, "LoadSaveGame": load, "EnableAdvancedGameSettings": False},
            }
        ).encode()
        head = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="data"\r\nContent-Type: application/json\r\n\r\n'
            ).encode()
            + meta
            + (
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="saveGameFile"; '
                f'filename="{name}.sav"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
        )
        tail = f"\r\n--{boundary}--\r\n".encode()
        try:
            with root.open_read(path) as (fh, size):
                if size > UPLOAD_MAX_BYTES:
                    raise AgentError("The save file is too large", "too_large", 413)

                def chunks() -> Any:
                    yield head
                    left = size
                    while left > 0:
                        block = fh.read(min(256 * 1024, left))
                        if not block:
                            raise GameApiError(
                                "The uploaded save changed while it was sent", "api_error", 502
                            )
                        left -= len(block)
                        yield block
                    yield tail

                resp = self.request(
                    settings,
                    c,
                    "POST",
                    API_PATH,
                    body=chunks(),
                    length=len(head) + size + len(tail),
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    timeout=min(timeout, LOAD_TIMEOUT + 60),
                )
        except PathError as exc:
            raise AgentError(f"Cannot read the uploaded save: {exc}", exc.code, exc.status) from exc
        self._result("UploadSaveGame", resp.status, resp.json())
        out: dict[str, Any] = {"save_name": name, "loaded": load, "bytes": size}
        if load:
            out["state"] = self._wait_session(settings, c, token, None, min(timeout, 60.0))
            out["session"] = _key(out["state"], "activeSessionName")
        return out

    def _action_load_game(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        name = _text(params.get("save_name"), "save name", SAVE_NAME_RE)
        token = self.api_token(settings, c)
        self.call(
            settings,
            c,
            "LoadGame",
            {"SaveName": name, "EnableAdvancedGameSettings": False},
            token=token,
            timeout=min(timeout, LOAD_TIMEOUT),
        )
        state = self._wait_session(settings, c, token, None, min(timeout, 60.0))
        return {"state": state, "session": _key(state, "activeSessionName")}

    def _action_options(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        data = self.call(settings, c, "GetServerOptions", {}, token=self.api_token(settings, c))
        return {
            "options": dict(_key(data, "serverOptions") or {}),
            "pending": dict(_key(data, "pendingServerOptions") or {}),
        }

    def _action_apply_options(
        self, settings: Settings, c: Container, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        options = params.get("options")
        if not isinstance(options, dict) or not options or len(options) > 32:
            raise AgentError("Invalid server options", "invalid_request", 400)
        clean: dict[str, str] = {}
        for key, value in options.items():
            if not isinstance(key, str) or not OPTION_KEY_RE.fullmatch(key):
                raise AgentError(f"Invalid server option {key!r}", "invalid_request", 400)
            if not isinstance(value, str) or len(value) > 64 or any(ord(ch) < 32 for ch in value):
                raise AgentError(f"Invalid value for {key}", "invalid_request", 400)
            clean[key] = value
        token = self.api_token(settings, c)
        self.call(settings, c, "ApplyServerOptions", {"UpdatedServerOptions": clean}, token=token)
        return self._action_options(settings, c, {}, timeout)


__all__ = [
    "POLL_PACKET",
    "REPLY_PATTERN",
    "SatisfactoryDriver",
    "parse_query_reply",
    "save_name_for",
    "udp_query",
]
