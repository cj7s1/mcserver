"""ASGI layer in front of Django (wired in mcpanel/asgi.py).

Django's ASGI handler reads the whole request body into a spool file *before* any middleware,
authentication or URL routing runs, and uvicorn sets no body limit of its own. A Django
middleware (``UploadLimitMiddleware``) can therefore only pick the status code after every byte
was received and written to disk. :class:`RequestBodyLimit` enforces the limits before Django
sees the request:

* a declared ``Content-Length`` above the limit is answered with 413 without reading the body;
* a body without a declared length (chunked) is counted while it arrives and the request is
  ended with 413 as soon as it passes the limit;
* the upload endpoints (:data:`UPLOAD_PATH`) accept up to ``MCPANEL_MAX_UPLOAD_BYTES``, but more
  than ``MCPANEL_MAX_REQUEST_BYTES`` only with a signed-in session, and must declare their length
  (411 otherwise, as before); every other path accepts at most ``MCPANEL_MAX_REQUEST_BYTES``.

:class:`PanelASGIHandler` is Django's handler with the body spool file in
``FILE_UPLOAD_TEMP_DIR`` (``MCPANEL_UPLOAD_TMP``) instead of the system temp directory.
"""

from __future__ import annotations

import json
import re
import tempfile
from importlib import import_module
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import RequestAborted
from django.core.handlers.asgi import ASGIHandler

UPLOAD_PATH = re.compile(
    r"^/api/instances/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"/(?:files/upload|import|content/upload|world/replace|saves/import)/?$"
)
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_SESSION_KEY = re.compile(r"^[a-z0-9]{8,64}$")


class PanelASGIHandler(ASGIHandler):
    async def read_body(self, receive: Any) -> Any:
        """Django's ``read_body``, spooling to FILE_UPLOAD_TEMP_DIR."""
        body_file = tempfile.SpooledTemporaryFile(
            max_size=settings.FILE_UPLOAD_MAX_MEMORY_SIZE, mode="w+b", dir=settings.FILE_UPLOAD_TEMP_DIR or None
        )
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                body_file.close()
                raise RequestAborted()
            if "body" in message:
                body_file.write(message["body"])
            if not message.get("more_body", False):
                break
        body_file.seek(0)
        return body_file


def _header(scope: dict[str, Any], name: bytes) -> list[str]:
    return [bytes(v).decode("latin-1") for k, v in scope.get("headers", []) if bytes(k).lower() == name]


def _session_key(scope: dict[str, Any]) -> str | None:
    """The session cookie, parsed the way Django parses it (``parse_cookie``): one cookie that the
    standard library's ``SimpleCookie`` cannot parse (a JSON value another app on the same host
    set) must not hide the session cookie after it."""
    from django.http.cookie import parse_cookie

    raw = "; ".join(_header(scope, b"cookie"))
    value = parse_cookie(raw).get(settings.SESSION_COOKIE_NAME) if raw else None
    return value if value is not None and _SESSION_KEY.match(value) else None


def _signed_in(session_key: str) -> bool:
    """Whether ``session_key`` is a live session of a signed-in user, as Django's ``get_user``
    decides it: a known authentication backend, an active user, and a session made with the
    user's current password (the session auth hash; a password change ends other sessions).
    The session is read as usual, the user with one database read."""
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
    from django.db import connections
    from django.utils.crypto import constant_time_compare

    try:
        data = import_module(settings.SESSION_ENGINE).SessionStore(session_key).load()
        user_id, backend = data.get(SESSION_KEY), data.get(BACKEND_SESSION_KEY)
        if not user_id or backend not in settings.AUTHENTICATION_BACKENDS:
            return False
        model = get_user_model()
        user = model._default_manager.only("password", "is_active").filter(pk=model._meta.pk.to_python(user_id)).first()
        if user is None or not user.is_active:
            return False
        session_hash = str(data.get(HASH_SESSION_KEY) or "")
        if not session_hash:
            return False
        hashes = [user.get_session_auth_hash(), *getattr(user, "get_session_auth_fallback_hash", lambda: [])()]
        return any(constant_time_compare(session_hash, h) for h in hashes)
    except Exception:
        return False
    finally:
        # outside Django's request cycle nothing else closes this thread's connection
        connections.close_all()


async def _reject(send: Any, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
    body = json.dumps({"error": {"code": code, "message": message, "details": details or {}}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"x-content-type-options", b"nosniff"),
                # the rest of the body is not read: the connection cannot be reused
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _mb(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB"


class RequestBodyLimit:
    """ASGI middleware for HTTP: the request body limits (see the module docstring)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        small = int(settings.MCPANEL_MAX_REQUEST_BYTES)
        upload_limit = int(settings.MCPANEL_MAX_UPLOAD_BYTES or 0)
        is_upload = bool(UPLOAD_PATH.match(scope.get("path") or ""))
        declared = _header(scope, b"content-length")
        length: int | None = None
        if declared:
            if len(set(declared)) != 1 or not declared[0].strip().isdigit():
                return await _reject(send, 400, "bad_request", "Invalid Content-Length header")
            length = int(declared[0].strip())

        cap = small
        if is_upload and scope.get("method") in BODY_METHODS:
            if length is None:
                return await _reject(send, 411, "length_required", "Uploads must declare a Content-Length")
            if upload_limit and length > upload_limit:
                return await _reject(
                    send, 413, "file_too_large", f"Uploads are limited to {_mb(upload_limit)}", {"max_bytes": upload_limit}
                )
            if length > small:
                key = _session_key(scope)
                if key is None or not await sync_to_async(_signed_in, thread_sensitive=False)(key):
                    return await _reject(
                        send,
                        413,
                        "request_too_large",
                        f"Sign in to upload more than {_mb(small)}",
                        {"max_bytes": small},
                    )
                cap = length
        elif length is not None and length > small:
            return await _reject(send, 413, "request_too_large", f"Requests are limited to {_mb(small)}", {"max_bytes": small})

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > cap:
                    # Django sees a client that went away and stops reading; the 413 follows below
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, limited_receive, tracking_send)
        if exceeded and not started:
            await _reject(send, 413, "request_too_large", f"Requests are limited to {_mb(cap)}", {"max_bytes": cap})
