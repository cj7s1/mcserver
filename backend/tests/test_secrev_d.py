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


# --- C-4 + D-7 ------------------------------------------------------------------------------------------------
def _body(resp: Any) -> bytes:
    return b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content


@pytest.mark.django_db
def test_c4_downloads_are_octet_streams_in_a_sandbox(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """Adopted from c-untrusted-data/poc test_c4_download_content_type_follows_the_file_name."""
    inst = make_instance()
    fake_agent.put_file(inst.id, "plugins/evil.js", "alert(document.domain)")
    fake_agent.put_file(inst.id, "plugins/evil.html", "<script>alert(1)</script>")
    fake_agent.put_file(inst.id, "plugins/evil.svg", "<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>")
    for name in ("evil.js", "evil.html", "evil.svg"):
        resp = clients["owner"].get(f"/api/instances/{inst.id}/files/download", {"path": f"plugins/{name}"})
        assert resp.status_code == 200 and _body(resp)
        assert resp["Content-Type"] == "application/octet-stream", name
        assert resp["Content-Disposition"].startswith("attachment;") and resp["X-Content-Type-Options"] == "nosniff"
        assert resp["Content-Security-Policy"] == "default-src 'none'; sandbox"
    zipped = clients["owner"].get(f"/api/instances/{inst.id}/files/archive", {"path": "plugins"})
    assert zipped.status_code == 200 and zipped["Content-Security-Policy"] == "default-src 'none'; sandbox"


@pytest.mark.django_db
def test_d7_the_api_answers_json_only_under_a_strict_policy(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """Adopted from c-untrusted-data/poc test_c4_browsable_api... and d-panel-web/poc test_d5_browsable_api."""
    inst = make_instance(name="<img src=x onerror=alert(1)>")
    fake_agent.put_file(inst.id, "notes.txt", "see https://evil.example/login <b>hi</b>")
    for url in (f"/api/instances/{inst.id}/files/text?path=notes.txt", f"/api/instances/{inst.id}"):
        resp = clients["owner"].get(url, HTTP_ACCEPT="text/html")
        # no HTML page of the panel's origin: JSON, or 406 when only HTML is acceptable
        assert not resp["Content-Type"].startswith("text/html"), url
        assert resp["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert clients["owner"].get(f"/api/instances/{inst.id}").json()["name"] == "<img src=x onerror=alert(1)>"


@pytest.mark.django_db
def test_d7_no_swagger_page_and_the_schema_only_when_switched_on(clients: Any, settings: Any) -> None:
    from rest_framework.test import APIClient

    assert clients["owner"].get("/api/docs").status_code == 404
    assert settings.MCPANEL_API_DOCS is False
    assert clients["owner"].get("/api/schema").status_code == 404
    assert APIClient().get("/api/schema").status_code in (401, 403)
    settings.MCPANEL_API_DOCS = True
    resp = clients["owner"].get("/api/schema", HTTP_ACCEPT="application/json")
    assert resp.status_code == 200 and b"cdn.jsdelivr.net" not in resp.content
    assert APIClient().get("/api/schema").status_code in (401, 403)


# --- D-8 ------------------------------------------------------------------------------------------------------
def _settings_of(env: dict[str, str], *names: str) -> list[str]:
    """Settings values of a process started with ``env`` (the module reads the environment once)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import os, django\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mcpanel.settings')\n"
        "django.setup()\n"
        "from django.conf import settings\n"
        f"print('|'.join(str(getattr(settings, n)) for n in {list(names)!r}))\n"
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("MCPANEL_SECURE", "MCPANEL_BEHIND", "MCPANEL_HSTS"))}
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={**clean, "MCPANEL_TESTING": "1", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip().splitlines()[-1].split("|")


def test_d8_cookies_are_secure_behind_the_tls_proxy_and_hsts_is_opt_in() -> None:
    names = ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_HSTS_SECONDS")
    assert _settings_of({}, *names) == ["False", "False", "0"]
    assert _settings_of({"MCPANEL_BEHIND_TLS_PROXY": "true"}, *names) == ["True", "True", "0"]
    # an explicit choice still wins
    assert _settings_of({"MCPANEL_BEHIND_TLS_PROXY": "true", "MCPANEL_SECURE_COOKIES": "false"}, *names)[:2] == ["False", "False"]
    assert _settings_of({"MCPANEL_HSTS_SECONDS": "31536000"}, "SECURE_HSTS_SECONDS", "SECURE_HSTS_INCLUDE_SUBDOMAINS") == [
        "31536000",
        "False",
    ]


@pytest.mark.django_db
def test_d8_hsts_header_on_https_answers_when_set(settings: Any) -> None:
    from rest_framework.test import APIClient

    settings.SECURE_HSTS_SECONDS = 31536000
    assert APIClient().get("/api/health", secure=True)["Strict-Transport-Security"] == "max-age=31536000"
    assert "Strict-Transport-Security" not in APIClient().get("/api/health")


# --- D-3 ------------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_d3_failed_logins_are_limited_per_account_from_any_address(users: Any) -> None:
    """Adopted from d-panel-web/poc test_login_throttle_is_per_address_only: 10 failures from 5 addresses."""
    from rest_framework.test import APIClient

    from tests.conftest import PASSWORD

    for n in range(10):
        client = APIClient(REMOTE_ADDR=f"192.168.1.{50 + n % 5}")
        assert client.post("/api/auth/login", {"username": "Owner", "password": "wrong-password"}).status_code == 401
    fresh = APIClient(REMOTE_ADDR="192.168.1.99")
    refused = fresh.post("/api/auth/login", {"username": "owner", "password": "wrong-password"})
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "throttled"
    assert 0 < refused.json()["error"]["details"]["wait"] <= 600 and refused["Retry-After"]
    # the right password is refused as well while the account is locked (or the limit would not limit guessing)
    assert fresh.post("/api/auth/login", {"username": "owner", "password": PASSWORD}).status_code == 429
    # other accounts are not affected
    assert fresh.post("/api/auth/login", {"username": "manager", "password": PASSWORD}).status_code == 200


@pytest.mark.django_db
def test_d3_the_password_change_check_is_limited(users: Any, client_for: Any) -> None:
    from tests.conftest import PASSWORD

    client = client_for(users["manager"])
    for _ in range(10):
        resp = client.post("/api/auth/password", {"old_password": "wrong-password", "new_password": "A-new-Passphrase-77"})
        assert resp.status_code == 400
    resp = client.post("/api/auth/password", {"old_password": PASSWORD, "new_password": "A-new-Passphrase-77"})
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "throttled"


@pytest.mark.django_db
def test_d3_successes_do_not_count(users: Any) -> None:
    from rest_framework.test import APIClient

    from tests.conftest import PASSWORD

    for n in range(15):
        assert (
            APIClient(REMOTE_ADDR=f"10.0.0.{n}").post("/api/auth/login", {"username": "owner", "password": PASSWORD}).status_code
            == 200
        )


@pytest.mark.django_db
def test_d3_old_failed_logins_are_pruned(users: Any) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.accounts.tasks import prune_login_failures
    from apps.audit.models import AuditEvent

    old = AuditEvent.objects.create(action="auth.login_failed", success=False, details={"username": "x"})
    new = AuditEvent.objects.create(action="auth.login_failed", success=False, details={"username": "y"})
    other = AuditEvent.objects.create(action="auth.login", details={})
    AuditEvent.objects.filter(pk__in=[old.pk, other.pk]).update(created_at=timezone.now() - timedelta(days=91))
    assert prune_login_failures() == 1
    assert set(AuditEvent.objects.values_list("pk", flat=True)) >= {new.pk, other.pk}
    assert not AuditEvent.objects.filter(pk=old.pk).exists()
