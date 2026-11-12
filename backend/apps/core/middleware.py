from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from django.core.exceptions import DisallowedHost
from django.http import HttpRequest, HttpResponse

from .logging import request_id_var

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Assigns a request id (honouring a sane incoming X-Request-ID) for log correlation."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get("X-Request-ID", "")
        rid = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            request.request_id = rid  # type: ignore[attr-defined]
            response = self.get_response(request)
            response["X-Request-ID"] = rid
            return response
        finally:
            request_id_var.reset(token)


# --- Security headers ----------------------------------------------------------------
# Content-Security-Policy of the single-page app. The production build (frontend/dist) has no
# inline scripts and evaluates no strings (zod only probes `Function("")` and falls back);
# React, Radix and xterm.js inject <style> elements, so styles need 'unsafe-inline'. Images come
# from provider CDNs and avatar services (https:), downloads use blob: URLs. connect-src gets the
# page's own ws(s):// origin as well because older Safari versions do not match WebSockets
# against 'self'.
SPA_CSP = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "font-src 'self' data:",
    "connect-src 'self'{websocket}",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)
# API responses are JSON or attachments: nothing may load or run from them (C-4/D-7; there is no HTML
# page under /api/: JSON renderer only, no Swagger page). Downloads set their own, stricter policy with
# ``sandbox`` (apps.core.utils.DOWNLOAD_CSP).
API_CSP = "default-src 'none'; frame-ancestors 'none'"


class SecurityHeadersMiddleware:
    """X-Frame-Options and Content-Security-Policy for every response.

    Listed before WhiteNoise so files WhiteNoise serves directly (``/index.html``, assets),
    which never reach Django's XFrameOptionsMiddleware, cannot be framed either (clickjacking).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response.setdefault("X-Frame-Options", "DENY")
        if "Content-Security-Policy" not in response:
            if request.path.startswith("/api/"):
                response["Content-Security-Policy"] = API_CSP
            else:
                response["Content-Security-Policy"] = self.spa_policy(request)
        if not request.path.startswith(("/api/", "/ws/", "/static/")):
            spa_cache_headers(request.path, response)
        return response

    @staticmethod
    def spa_policy(request: HttpRequest) -> str:
        websocket = ""
        try:
            host = request.get_host()
        except DisallowedHost:
            host = ""
        if host and re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
            websocket = f" {'wss' if request.is_secure() else 'ws'}://{host}"
        return "; ".join(SPA_CSP).format(websocket=websocket)


# Files of the Vite build whose name carries a content hash (assets/index-BxYz12_a.js): a new build
# has new names, so they may be cached for good. index.html (and everything else WhiteNoise serves
# from the build, e.g. favicon.svg) names them and must be revalidated, or a browser keeps loading
# the previous build's chunks after an upgrade.
HASHED_ASSET = re.compile(r"^/assets/[^/]+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
IMMUTABLE = "public, max-age=31536000, immutable"


def spa_cache_headers(path: str, response: HttpResponse) -> None:
    if response.status_code not in (200, 304):
        return
    if HASHED_ASSET.match(path):
        response["Cache-Control"] = IMMUTABLE
    elif path in ("/", "/index.html") or response.get("Content-Type", "").startswith("text/html"):
        response["Cache-Control"] = "no-cache"


class UploadLimitMiddleware:
    """Refuse oversized request bodies by their Content-Length (HTTP 413).

    Under ASGI the binding limits are enforced earlier, before Django reads the body
    (apps.core.asgi.RequestBodyLimit, wired in mcpanel/asgi.py); this middleware keeps the same
    answer for other ways of serving Django (WSGI, the test client). The limit is
    MCPANEL_MAX_UPLOAD_BYTES (default 2 GiB); uploads without a Content-Length are refused (411).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.conf import settings
        from django.http import JsonResponse

        limit = settings.MCPANEL_MAX_UPLOAD_BYTES
        if request.method in ("POST", "PUT", "PATCH") and limit:
            raw = request.META.get("CONTENT_LENGTH") or ""
            try:
                length = int(raw) if raw else None
            except ValueError:
                length = None
            # the same routes as the ASGI layer, matched as a whole (D-5: a substring let other paths through)
            from .asgi import UPLOAD_PATH

            is_upload = bool(UPLOAD_PATH.match(request.path))
            if (length is not None and length > limit) or (length is None and is_upload):
                return JsonResponse(
                    {
                        "error": {
                            "code": "file_too_large" if length else "length_required",
                            "message": f"Uploads are limited to {limit // (1024 * 1024)} MB"
                            if length
                            else "Uploads must declare a Content-Length",
                            "details": {},
                        }
                    },
                    status=413 if length else 411,
                )
        return self.get_response(request)


# --- WebSocket origin check --------------------------------------------------------------
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin_key(origin: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an http(s) origin, ports defaulted; None if it is not one."""
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname or parts.path not in ("", "/") or parts.query:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return scheme, parts.hostname.lower(), port if port is not None else _DEFAULT_PORTS[scheme]


def _header_name(meta_key: str) -> bytes:
    """Django META key (HTTP_X_FORWARDED_PROTO) -> ASGI header name (x-forwarded-proto)."""
    return meta_key.removeprefix("HTTP_").lower().replace("_", "-").encode("latin-1")


def websocket_origin_allowed(scope: dict[str, Any]) -> bool:
    """Same-origin check for a WebSocket handshake, mirroring Django's CSRF Origin check.

    The browser's ``Origin`` must equal the panel's own origin (scheme, host *and* port: the
    ``Host`` header, validated against ALLOWED_HOSTS, with the scheme taken like
    ``request.is_secure()``, i.e. from SECURE_PROXY_SSL_HEADER behind a TLS proxy) or one of
    CSRF_TRUSTED_ORIGINS. Channels' AllowedHostsOriginValidator only compares the host name,
    so a page served from the same machine on another port (any other web app on the NAS)
    could open the console socket with the user's cookies.
    """
    from django.conf import settings
    from django.http.request import is_same_domain, split_domain_port, validate_host

    headers: dict[bytes, list[str]] = {}
    for name, value in scope.get("headers", []):
        try:
            headers.setdefault(bytes(name).lower(), []).append(bytes(value).decode("latin-1"))
        except (TypeError, ValueError):
            return False

    def single(name: bytes) -> str | None:
        values = headers.get(name)
        if values is None:
            return None
        return values[0] if len(values) == 1 else ""  # duplicated header: unusable

    origin = single(b"origin")
    if not origin:
        return False
    origin_key = _origin_key(origin)
    if origin_key is None:
        return False

    # The panel's own origin, computed like HttpRequest.is_secure() / get_host().
    secure = scope.get("scheme") in ("wss", "https")
    proxy_header = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
    if proxy_header:
        header, secure_value = proxy_header
        forwarded = single(_header_name(header))
        if forwarded is not None:
            secure = forwarded.split(",", 1)[0].strip() == secure_value
    host = single(b"x-forwarded-host") if settings.USE_X_FORWARDED_HOST else None
    if host is None:
        host = single(b"host")
    if host is None and scope.get("server"):
        server_host, server_port = scope["server"][0], scope["server"][1]
        host = server_host if server_port in (None, 443 if secure else 80) else f"{server_host}:{server_port}"
    if host:
        allowed_hosts = settings.ALLOWED_HOSTS
        if settings.DEBUG and not allowed_hosts:
            allowed_hosts = [".localhost", "127.0.0.1", "[::1]"]
        domain, _port = split_domain_port(host)
        if domain and validate_host(domain, allowed_hosts):
            if origin_key == _origin_key(f"{'https' if secure else 'http'}://{host}"):
                return True

    for trusted in settings.CSRF_TRUSTED_ORIGINS:
        if "*" in trusted:
            # Django semantics: https://*.example.com matches example.com and its subdomains
            parts = urlsplit(trusted)
            pattern = parts.netloc[parts.netloc.index("*") + 1 :] if "*" in parts.netloc else ""
            netloc = urlsplit(origin).netloc
            if pattern and parts.scheme.lower() == origin_key[0] and is_same_domain(netloc, pattern):
                return True
        elif origin_key == _origin_key(trusted):
            return True
    return False


class WebSocketOriginValidator:
    """ASGI middleware: refuse WebSocket handshakes from foreign origins (see above)."""

    def __init__(self, application: Any) -> None:
        self.application = application

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope["type"] == "websocket" and not websocket_origin_allowed(scope):
            from channels.security.websocket import WebsocketDenier

            return await WebsocketDenier()(scope, receive, send)
        return await self.application(scope, receive, send)
