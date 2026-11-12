"""``http-api``: the base of control drivers for games that are run through an HTTP(S) JSON API inside
their container (Satisfactory's Dedicated Server API; TShock's REST API would be another).

**Where the agent connects.** Only to the server's own running container: its address on the
instance network as Docker reports it right before each request (``docker_ops.container_address``),
127.0.0.1 with host networking, or the published host port on 127.0.0.1 with
``AGENT_RCON_MODE=published``. Neither the panel nor a user ever names an address or a URL path
beyond the driver's fixed API path.

**TLS.** Game servers make a self-signed certificate for their API (Satisfactory: ``CN=FactoryGame``,
one year). Certificate verification is off for exactly this hop: the TLS context below is the
driver's own and used for nothing else (the panel's connection to the agent and every other HTTPS
request of the agent keep full verification). What it guards against instead: the address is the
container's own and is looked up right before the request, and the certificate's SHA-256 is pinned
for the run of the container on first contact (``_pins``, keyed by container id + start time), so a
different peer answering at that address during the run is refused (409 ``api_tls_changed``).

**Bounds.** Connecting gives up after ``API_CONNECT_TIMEOUT`` seconds; the whole request (sending,
waiting, reading) is bounded by the caller's timeout, at most ``API_MAX_TIMEOUT``; answers larger
than ``API_MAX_RESPONSE`` bytes are refused.

**Token.** Read from ``secret_file`` in the server folder (written there by the panel from its
encrypted copy before every apply), through SafeRoot without following symbolic links, at most
``TOKEN_FILE_MAX_BYTES``. It travels only in the ``Authorization`` header of this hop and is never
logged.

Errors are :class:`GameApiError` (an ``AgentError``): ``api_unavailable`` (409: nothing answers yet),
``api_timeout`` (504), ``api_tls_changed`` (409), ``api_error`` (502: the game answered with an
error; ``api_code`` holds the game's own code), ``api_unauthorized`` (409: the stored token was
refused).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import socket
import ssl
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from ..errors import AgentError, game_text
from .base import ControlDriver

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings

log = logging.getLogger(__name__)

API_CONNECT_TIMEOUT = 3.0
API_MAX_TIMEOUT = 600.0
API_MAX_RESPONSE = 8 * 1024 * 1024
TOKEN_FILE_MAX_BYTES = 4096
_READ_CHUNK = 64 * 1024
_SEND_CHUNK = 256 * 1024
_PINS_MAX = 512


def _ops() -> Any:
    from .. import docker_ops  # imported late: docker_ops imports the drivers

    return docker_ops


class GameApiError(AgentError):
    """An error of a game's HTTP API (or of reaching it); ``api_code`` is the game's own error code
    (Satisfactory: ``server_claimed``, ``wrong_password``, ...), ``http_status`` its HTTP status."""

    def __init__(
        self,
        message: str,
        code: str = "api_error",
        status: int = 502,
        *,
        api_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message, code, status)
        self.api_code = api_code
        self.http_status = http_status


@dataclass
class ApiResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        """The body as JSON; None when it is empty or not JSON."""
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):  # deep nesting raises RecursionError
            return None


def _internal_tls_context() -> ssl.SSLContext:
    """TLS for the hop to the game's own container only (see the module docstring)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


_TLS = _internal_tls_context()
_pins_lock = threading.Lock()
# (container id, StartedAt) -> SHA-256 of the API's certificate (DER) seen first in that run
_pins: dict[tuple[str, str], str] = {}


def _check_pin(key: tuple[str, str] | None, der: bytes | None) -> None:
    if key is None or der is None:
        return
    fingerprint = hashlib.sha256(der).hexdigest()
    with _pins_lock:
        known = _pins.get(key)
        if known is None:
            if len(_pins) >= _PINS_MAX:
                _pins.clear()  # old runs; the next request of each pins again
            _pins[key] = fingerprint
            return
    if known != fingerprint:
        raise GameApiError(
            "The game's API presented a different TLS certificate than earlier in this run; the request was not sent",
            "api_tls_changed",
            409,
        )


def http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    scheme: str = "https",
    body: bytes | Iterable[bytes] | None = None,
    length: int | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    pin: tuple[str, str] | None = None,
    max_response: int = API_MAX_RESPONSE,
) -> ApiResponse:
    """One HTTP(S) request, bounded in time (``timeout`` for everything, the connect at most
    API_CONNECT_TIMEOUT) and size. ``body`` is bytes or an iterable of chunks (then ``length`` is
    its total size). Raises GameApiError for transport problems; HTTP errors are returned.

    The deadline holds for the whole exchange, not only per socket read (B-2): a timer shuts the
    socket down when it passes, so an answer trickled a byte at a time ends there too."""
    timeout = max(0.1, min(float(timeout), API_MAX_TIMEOUT))
    deadline = time.monotonic() + timeout
    expired = threading.Event()
    timer: threading.Timer | None = None

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline")
        return left

    conn: http.client.HTTPConnection
    if scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port, timeout=min(API_CONNECT_TIMEOUT, timeout), context=_TLS
        )
    elif scheme == "http":
        conn = http.client.HTTPConnection(host, port, timeout=min(API_CONNECT_TIMEOUT, timeout))
    else:
        raise ValueError(f"Unsupported scheme {scheme!r}")

    def expire() -> None:
        expired.set()
        raw = conn.sock
        if raw is not None:
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    try:
        conn.connect()
        sock = conn.sock
        timer = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        if scheme == "https" and isinstance(sock, ssl.SSLSocket):
            _check_pin(pin, sock.getpeercert(binary_form=True))
        sock.settimeout(remaining())
        conn.putrequest(method, path, skip_accept_encoding=True)
        all_headers = dict(headers or {})
        if isinstance(body, bytes | bytearray):
            all_headers["Content-Length"] = str(len(body))
        elif body is not None:
            if length is None:
                raise ValueError("a streamed body needs its length")
            all_headers["Content-Length"] = str(length)
        elif method in ("POST", "PUT"):
            all_headers["Content-Length"] = "0"
        for key, value in all_headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if isinstance(body, bytes | bytearray):
            for i in range(0, len(body), _SEND_CHUNK):
                sock.settimeout(remaining())
                sock.sendall(body[i : i + _SEND_CHUNK])
        elif body is not None:
            for chunk in body:
                sock.settimeout(remaining())
                sock.sendall(chunk)
        sock.settimeout(remaining())
        resp = conn.getresponse()
        data = bytearray()
        while True:
            # a server that closes the connection after its answer (HTTP/1.0, "Connection: close")
            # leaves http.client to close the socket once the body is read: no timeout to set then
            if sock.fileno() != -1:
                sock.settimeout(remaining())
            elif time.monotonic() >= deadline:
                raise TimeoutError("deadline")
            chunk = resp.read(_READ_CHUNK)
            if not chunk:
                break
            data += chunk
            if len(data) > max_response:
                raise GameApiError(
                    f"The game's API answered with more than {max_response} bytes", "api_error", 502
                )
        if expired.is_set():  # cut off by the deadline: what was read is not the whole answer
            raise TimeoutError("deadline")
        return ApiResponse(resp.status, {k.lower(): v for k, v in resp.getheaders()}, bytes(data))
    except GameApiError:
        raise
    except TimeoutError as exc:
        raise GameApiError(f"The game's API did not answer within {timeout:g} s", "api_timeout", 504) from exc
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        if expired.is_set():
            raise GameApiError(
                f"The game's API did not answer within {timeout:g} s", "api_timeout", 504
            ) from exc
        raise GameApiError(
            "The game's API is not reachable (the server is probably still starting): "
            f"{type(exc).__name__}: {game_text(exc)}",
            "api_unavailable",
            409,
        ) from exc
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


class HttpApiDriver(ControlDriver):
    """A game run through an HTTP(S) API in its container (the base; ``SatisfactoryDriver`` adds the
    game's functions). Options: ``port`` (container port of the API, TCP), ``scheme`` (https),
    ``secret_file`` (the token, relative to the server folder)."""

    id: ClassVar[str] = "http-api"

    def __init__(self, *, port: int, scheme: str = "https", secret_file: str | None = None) -> None:
        if not 1 <= int(port) <= 65535 or scheme not in ("http", "https"):
            raise ValueError("An HTTP API driver needs a port and the scheme http or https")
        if secret_file is not None and (
            not secret_file or secret_file.startswith("/") or ".." in secret_file.split("/")
        ):
            raise ValueError(f"Invalid API token file {secret_file!r}")
        self.port = int(port)
        self.scheme = scheme
        self.secret_file = secret_file

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> HttpApiDriver:
        return cls(
            port=int(options["port"]),
            scheme=str(options.get("scheme") or "https"),
            secret_file=str(options["secret_file"]) if options.get("secret_file") else None,
        )

    # --- the token -------------------------------------------------------------------------------
    def token_for(self, settings: Settings, instance_id: UUID | str | None) -> str | None:
        """The API token of the server folder of ``instance_id`` (None when there is none)."""
        if self.secret_file is None or not instance_id:
            return None
        from ..safefs import PathError, SafeRoot

        try:
            root = SafeRoot(_ops().instance_dir(settings, UUID(str(instance_id))))
            data = root.read_bytes(self.secret_file, TOKEN_FILE_MAX_BYTES)
        except (PathError, ValueError, OSError) as exc:
            log.debug("API token file of %s not readable: %s", instance_id, exc)
            return None
        text = data.decode("utf-8", errors="replace").strip()
        return text or None

    def secret(self, settings: Settings, c: Container) -> str | None:
        return self.token_for(settings, (c.labels or {}).get(_ops().LABEL_INSTANCE))

    # --- requests --------------------------------------------------------------------------------
    def address(self, settings: Settings, c: Container) -> tuple[str, int]:
        target = _ops().container_address(c, self.port, "tcp", settings)
        if target is None:
            raise GameApiError(
                "The server's API cannot be reached: its container has no address"
                + (" (and the API port is not published)" if settings.rcon_mode == "published" else ""),
                "api_unavailable",
                409,
            )
        return target

    @staticmethod
    def _pin_key(c: Container) -> tuple[str, str]:
        started = str(((c.attrs or {}).get("State") or {}).get("StartedAt") or "")
        return str(c.id), started

    def request(
        self,
        settings: Settings,
        c: Container,
        method: str,
        path: str,
        *,
        body: bytes | Iterable[bytes] | None = None,
        length: int | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> ApiResponse:
        host, port = self.address(settings, c)
        return http_request(
            host,
            port,
            method,
            path,
            scheme=self.scheme,
            body=body,
            length=length,
            headers=headers,
            timeout=timeout,
            pin=self._pin_key(c) if self.scheme == "https" else None,
        )
