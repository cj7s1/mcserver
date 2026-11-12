"""Fixes of the security and bug review of 2026-11-11, slice D (audit-2026-11-09/security-review/d-panel-web).

The reviewers' proofs (``d-panel-web/poc/``) passed while each weakness was present; the tests here assert the
fixed behaviour.

* D-5: every upload route takes uploads up to ``MCPANEL_MAX_UPLOAD_BYTES`` through ``RequestBodyLimit`` (the save
  import was cut off at 25 MB), and the Django-side check matches the same routes, not a substring.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from django.test import RequestFactory
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.parsers import FileUploadParser, MultiPartParser
from rest_framework.views import APIView

from apps.core.asgi import UPLOAD_PATH, RequestBodyLimit
from apps.core.middleware import UploadLimitMiddleware

MiB = 1024 * 1024


def _walk(patterns: Any = None, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for p in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(p, URLResolver):
            out += _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            out.append((prefix + str(p.pattern), p.callback))
    return out


def upload_routes() -> list[str]:
    """Every route whose view parses multipart or raw file bodies, with a server id filled in."""
    iid = str(uuid.uuid4())
    routes = []
    for pattern, callback in _walk():
        cls = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        # parsers the view declares itself (DRF's default list, which every view has, takes multipart too)
        parsers = next((vars(c)["parser_classes"] for c in getattr(cls, "__mro__", ()) if "parser_classes" in vars(c)), ())
        if cls is None or parsers is APIView.parser_classes:
            continue
        if any(issubclass(p, (MultiPartParser, FileUploadParser)) for p in parsers):
            routes.append("/" + pattern.replace("<uuid:pk>", iid))
    return sorted(routes)


def _through_body_limit(path: str, length: int) -> tuple[int, bytes, list[str]]:
    reached: list[str] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:  # Django: reached only past the limit
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def call() -> tuple[int, bytes]:
        sent: list[dict[str, Any]] = []
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-length", str(length).encode()), (b"content-type", b"multipart/form-data; boundary=x")],
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await RequestBodyLimit(app)(scope, receive, send)
        return sent[0]["status"], sent[1].get("body", b"")

    status, body = asyncio.run(call())
    return status, body, reached


# --- D-5 ------------------------------------------------------------------------------------------------------
def test_d5_the_upload_routes_are_the_known_five() -> None:
    suffixes = sorted(r.split("/", 4)[4] for r in upload_routes())
    assert suffixes == ["content/upload", "files/upload", "import", "saves/import", "world/replace"]


@pytest.mark.parametrize("path", upload_routes())
def test_d5_every_upload_route_is_an_upload_for_the_body_limit(path: str, settings: Any) -> None:
    """30 MB without a session: the answer is the upload rule's ("sign in"), never the 25 MB limit of other paths."""
    assert UPLOAD_PATH.match(path), path
    assert settings.MCPANEL_MAX_UPLOAD_BYTES == 2 * 1024**3
    status, body, reached = _through_body_limit(path, 30 * MiB)
    assert status == 413 and b"Sign in to upload" in body and b"Requests are limited to" not in body, body
    assert not reached


def test_d5_other_paths_keep_the_small_limit() -> None:
    iid = uuid.uuid4()
    for path in (f"/api/instances/{iid}/saves/import/x", f"/api/instances/{iid}/files/importer", "/api/import"):
        assert not UPLOAD_PATH.match(path)
        status, body, reached = _through_body_limit(path, 30 * MiB)
        assert status == 413 and b"Requests are limited to" in body and not reached


def test_d5_the_django_side_check_matches_the_routes_not_a_substring() -> None:
    """Without a Content-Length an upload is refused (411); a path that only contains "/import" is not an upload."""
    middleware = UploadLimitMiddleware(lambda request: "passed")
    factory = RequestFactory()
    for path in upload_routes():
        request = factory.post(path)
        request.META.pop("CONTENT_LENGTH", None)
        assert middleware(request).status_code == 411, path
    for path in ("/api/notifications/import", f"/api/instances/{uuid.uuid4()}/files/importer"):
        request = factory.post(path)
        request.META.pop("CONTENT_LENGTH", None)
        assert middleware(request) == "passed", path
