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

from .test_games_phase2 import factorio_versions  # noqa: F401  (fixture)
from .test_security_audit_authz import ws_env  # noqa: F401  (fixture)

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


# --- D-4 (I-5) ------------------------------------------------------------------------------------------------
def _grant(make_user: Any, instance: Any, name: str, codes: list[str], role: Any = None) -> Any:
    from apps.accounts.models import InstanceAccess, Role

    grant = Role.objects.create(key=f"r-{name}", name=f"r {name}", permissions=sorted({"instance.view", *codes}))
    user = make_user(name, role=role) if role is not None else make_user(name)
    InstanceAccess.objects.create(user=user, instance=instance, role=grant)
    return user


def _server(make_instance: Any, fake_agent: Any, name: str, *, running: bool, **fields: Any) -> Any:
    from django.utils import timezone

    from apps.instances.models import Instance
    from apps.instances.spec import build_spec, current_spec_hash

    fields.setdefault("desired_state", "running" if running else "stopped")
    inst = make_instance(name=name, **fields)
    fake_agent.apply(inst.id, build_spec(inst), start=running)
    Instance.objects.filter(pk=inst.pk).update(
        container_spec_hash=current_spec_hash(inst),
        installed_at=timezone.now(),
        status="running" if running else "stopped",
        container_state="running" if running else "exited",
    )
    inst.refresh_from_db()
    return inst


@pytest.mark.django_db
def test_d4_only_an_administrator_overrides_the_memory_admission(
    make_instance: Any, fake_agent: Any, make_user: Any, client_for: Any, users: Any, django_capture_on_commit_callbacks: Any
) -> None:
    """Adopted from d-panel-web/poc test_power_control_alone_overrides_the_memory_admission: 16 GiB node, 4 GiB
    reserve, an 8 GiB server runs, a 6 GiB one does not fit."""
    from apps.accounts.models import Role
    from apps.jobs.models import Job, JobStatus

    _server(make_instance, fake_agent, "Big", running=True, memory_mb=8192)
    other = _server(make_instance, fake_agent, "Other", running=False, memory_mb=6144)
    friend = client_for(_grant(make_user, other, "friend", ["instance.control"]))
    url = f"/api/instances/{other.id}/power"
    refused = friend.post(url, {"action": "start"}, format="json")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "insufficient_memory"
    forced = friend.post(url, {"action": "start", "allow_overcommit": True}, format="json")
    assert forced.status_code == 403 and forced.json()["error"]["code"] == "overcommit_not_allowed"
    assert not fake_agent.containers[str(other.id)]["running"]
    # with the global permission to change system settings (an administrator) it is honoured
    admin_role = Role.objects.create(key="settings-admin", name="Settings admin", permissions=["system.settings"])
    admin = client_for(_grant(make_user, other, "admin2", ["instance.control"], role=admin_role))
    with django_capture_on_commit_callbacks(execute=True):
        ok = admin.post(url, {"action": "start", "allow_overcommit": True}, format="json")
    assert ok.status_code == 202, ok.content
    assert Job.objects.get(pk=ok.json()["id"]).status == JobStatus.SUCCEEDED


@pytest.mark.django_db
def test_d4_the_other_places_refuse_it_too(
    make_instance: Any, fake_agent: Any, make_user: Any, client_for: Any, roles: Any
) -> None:
    from apps.accounts.models import Role

    _server(make_instance, fake_agent, "Big", running=True, memory_mb=8192)
    running = _server(make_instance, fake_agent, "Running", running=True, memory_mb=1024)
    creator = Role.objects.create(key="creator", name="Creator", permissions=["instance.create"])
    user = _grant(make_user, running, "builder", ["instance.configure", "instance.control"], role=creator)
    client = client_for(user)
    body = {"name": "Huge", "accept_eula": True, "server_type": "VANILLA", "minecraft_version": "1.20.4"}
    resp = client.post("/api/instances/", {**body, "memory_mb": 10000, "allow_overcommit": True}, format="json")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "overcommit_not_allowed", resp.content
    resp = client.post(f"/api/instances/{running.id}/recreate", {"allow_overcommit": True}, format="json")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "overcommit_not_allowed", resp.content


# --- D-11 -----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_d11_a_clone_is_visible_to_the_user_who_made_it(
    make_instance: Any, fake_agent: Any, make_user: Any, client_for: Any, roles: Any
) -> None:
    """Adopted from d-panel-web/poc test_clone_is_invisible_to_the_user_who_made_it."""
    from apps.accounts.models import InstanceAccess, Role

    creator_role = Role.objects.create(key="creator", name="Creator", permissions=["instance.create"])
    user = make_user("builder", role=creator_role)
    source = _server(make_instance, fake_agent, "Source", running=False)
    InstanceAccess.objects.create(user=user, instance=source, role=roles["server_manager"])
    client = client_for(user)
    resp = client.post(f"/api/instances/{source.id}/clone", {"name": "My copy"}, format="json")
    assert resp.status_code == 202, resp.content
    clone_id = resp.json()["instance"]["id"]
    assert client.get(f"/api/instances/{clone_id}").status_code == 200
    assert "My copy" in [i["name"] for i in client.get("/api/instances/").json()]
    grant = InstanceAccess.objects.get(instance_id=clone_id)
    assert grant.user == user and grant.role == roles["server_manager"]  # what they hold on the source, no more


@pytest.mark.django_db
def test_d11_restore_as_new_gives_the_same_role_and_admins_get_no_grant(
    make_instance: Any,
    fake_agent: Any,
    make_user: Any,
    client_for: Any,
    roles: Any,
    users: Any,
    django_capture_on_commit_callbacks: Any,
) -> None:
    from apps.accounts.models import InstanceAccess, Role
    from apps.backups.models import Backup, BackupStatus

    source = _server(make_instance, fake_agent, "Source", running=False)
    backup = Backup.objects.create(instance=source, name="b", status=BackupStatus.SUCCEEDED, snapshot_id="abc123")
    restorer_role = Role.objects.create(key="restorer", name="Restorer", permissions=["instance.create"])
    operator = Role.objects.create(key="op-restore", name="Op", permissions=["instance.view", "instance.restore"])
    user = make_user("restorer", role=restorer_role)
    InstanceAccess.objects.create(user=user, instance=source, role=operator)
    resp = client_for(user).post(f"/api/instances/{source.id}/backups/{backup.id}/restore-as-new", {"name": "R"}, format="json")
    assert resp.status_code == 202, resp.content
    new_id = resp.json()["instance"]["id"]
    assert InstanceAccess.objects.get(instance_id=new_id, user=user).role == operator
    # the owner sees every server: no grant
    other = _server(make_instance, fake_agent, "Other", running=False)
    resp = client_for(users["owner"]).post(f"/api/instances/{other.id}/clone", {"name": "Owner copy"}, format="json")
    assert resp.status_code == 202 and not InstanceAccess.objects.filter(instance_id=resp.json()["instance"]["id"]).exists()


# --- E-6 (slice E; the requests are here with the other D tests of the same views) ------------------------------
@pytest.mark.django_db
def test_e6_named_ports_are_assigned_under_the_nodes_lock(
    clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from tests.test_games_six import body, create

    def locked(sqls: list[str]) -> bool:
        return any("FOR UPDATE" in q and '"nodes_node"' in q for q in sqls)

    with CaptureQueriesContext(connection) as queries, django_capture_on_commit_callbacks(execute=False):
        assert clients["owner"].post("/api/instances/", body("rust"), format="json").status_code == 201
    assert locked([q["sql"] for q in queries.captured_queries])
    src = create(clients, django_capture_on_commit_callbacks, "palworld")
    with CaptureQueriesContext(connection) as queries, django_capture_on_commit_callbacks(execute=False):
        assert clients["owner"].post(f"/api/instances/{src.id}/clone", {"name": "Copy"}, format="json").status_code == 202
    assert locked([q["sql"] for q in queries.captured_queries])
    with CaptureQueriesContext(connection) as queries:
        resp = clients["owner"].patch(
            f"/api/instances/{src.id}", {"ports": [{"name": src.ports[0]["name"], "host_port": 28211}]}, format="json"
        )
    assert resp.status_code == 200, resp.content
    assert locked([q["sql"] for q in queries.captured_queries])


# --- D-12 -----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_d12_malformed_ids_answer_400_or_404(users: Any, client_for: Any, make_instance: Any, fake_agent: Any) -> None:
    """Adopted from d-panel-web/poc test_malformed_ids_answer_500 (it counted 500s)."""
    inst = make_instance()
    c = client_for(users["owner"])

    def code(resp: Any) -> tuple[int, str]:
        return resp.status_code, resp.json()["error"]["code"]

    assert code(c.get("/api/jobs?instance=not-a-uuid")) == (400, "invalid_instance")
    assert code(c.get("/api/audit?instance=not-a-uuid")) == (400, "invalid_instance")
    assert code(c.get("/api/audit?before=abc")) == (400, "invalid_cursor")
    assert c.get("/api/audit?before=10").status_code == 200
    migrate = c.post(f"/api/instances/{inst.id}/migrate", {"node": {"a": 1}, "confirm": True}, format="json")
    assert code(migrate) == (400, "validation_error") and "node" in migrate.json()["error"]["details"]["fields"]
    software = c.post(f"/api/instances/{inst.id}/software", {"minecraft_version": "LATEST", "java_version": "abc"}, format="json")
    assert software.status_code in (400, 409) and software.json()["error"]["code"] != "internal_error"
    assert code(c.post(f"/api/instances/{inst.id}/backups", [], format="json")) == (400, "validation_error")


# --- D-13 -----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_d13_console_output_of_schedules_and_addresses_in_the_trail(
    make_instance: Any, make_user: Any, client_for: Any, users: Any
) -> None:
    from apps.audit.models import AuditEvent
    from apps.instances.models import Schedule

    inst = make_instance()
    Schedule.objects.create(
        instance=inst,
        name="say",
        action="command",
        cron="0 * * * *",
        payload={"command": "op mallory"},
        last_result="Made mallory a server operator",
    )
    Schedule.objects.create(instance=inst, name="b", action="backup", cron="0 3 * * *", payload={}, last_result="Backup done")
    viewer = client_for(_grant(make_user, inst, "only-view", []))
    rows = {r["name"]: r for r in viewer.get(f"/api/instances/{inst.id}/schedules").json()}
    assert rows["say"]["last_result"] is None and rows["say"]["payload"]["command"] is None
    assert rows["b"]["last_result"] == "Backup done"
    consoler = client_for(_grant(make_user, inst, "console", ["instance.console"]))
    rows = {r["name"]: r for r in consoler.get(f"/api/instances/{inst.id}/schedules").json()}
    assert rows["say"]["payload"]["command"] == "op mallory" and rows["say"]["last_result"]
    # the trail of a server for a configure-only user: no IP addresses; the global auditor sees them
    AuditEvent.objects.create(action="instance.updated", instance=inst, ip_address="192.168.1.77", details={})
    configurer = client_for(_grant(make_user, inst, "cfg", ["instance.configure"]))
    trail = configurer.get(f"/api/audit?instance={inst.id}").json()
    assert trail and all(r["ip_address"] is None for r in trail)
    assert any(r["ip_address"] == "192.168.1.77" for r in client_for(users["owner"]).get(f"/api/audit?instance={inst.id}").json())


# --- D-10 -----------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_d10_wizard_secrets_do_not_stay_in_the_install_jobs_parameters(
    clients: Any,
    fake_agent: Any,
    factorio_versions: Any,  # noqa: F811  (the fixture imported above)
    django_capture_on_commit_callbacks: Any,
) -> None:
    """Adopted from d-panel-web/poc test_wizard_secret_settings_stay_in_clear_in_the_job_parameters."""
    import json

    from apps.instances.models import Instance
    from apps.jobs.models import Job, JobStatus

    body = {
        "game": "factorio",
        "name": "Factory",
        "version": "2.1.23",
        "settings": {
            "server-settings": {"game_password": "CANARY-join-pw", "token": "CANARY-factorio-com-token", "username": "someone"}
        },
    }
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body, format="json")
    assert resp.status_code == 201, resp.content
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    job = Job.objects.get(instance=inst, kind="install")
    assert job.status == JobStatus.SUCCEEDED
    stored = json.dumps(job.params)
    assert "CANARY" not in stored and "someone" in stored  # the other settings stay
    written = fake_agent.text(inst.id, "config/server-settings.json")
    assert "CANARY-join-pw" in written and "CANARY-factorio-com-token" in written  # (the server got them)


# --- slice D suspicion: job events of a deleted server --------------------------------------------------------
def _job_event_users() -> tuple[str, str, str, str]:
    from apps.accounts.models import Role, User
    from apps.accounts.views import ensure_system_roles

    from .test_security_audit_authz import _session_cookie

    ensure_system_roles()
    me = User.objects.create_user("wsjob-me", password="Correct-Horse-Battery-42")
    other = User.objects.create_user("wsjob-other", password="Correct-Horse-Battery-42")
    admin = User.objects.create_user("wsjob-admin", password="Correct-Horse-Battery-42", role=Role.objects.get(key="operator"))
    return str(me.pk), str(other.pk), _session_cookie(me), _session_cookie(admin)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_job_events_without_a_server_reach_only_their_user_and_admins(ws_env: Any) -> None:  # noqa: F811
    from channels.layers import get_channel_layer

    from apps.console.events import EVENTS_GROUP

    from .test_security_audit_authz import _communicator, _db

    me, other, my_key, admin_key = await _db(_job_event_users)
    mine = _communicator("/ws/events/", my_key)
    admin = _communicator("/ws/events/", admin_key)
    assert (await mine.connect())[0] and (await admin.connect())[0]
    layer = get_channel_layer()

    def event(owner: str) -> dict[str, Any]:
        return {"type": "panel.event", "event": "job.finished", "payload": {"o": owner}, "instance_id": None, "owner_id": owner}

    await layer.group_send(EVENTS_GROUP, event(other))  # another user's delete job
    assert await mine.receive_nothing(timeout=0.3)
    assert (await admin.receive_json_from(timeout=2))["payload"] == {"o": other}
    await layer.group_send(EVENTS_GROUP, event(me))
    assert (await mine.receive_json_from(timeout=2))["payload"] == {"o": me}
    await mine.disconnect()
    await admin.disconnect()


@pytest.mark.django_db
def test_the_runner_names_the_owner_of_a_job_without_a_server(users: Any, monkeypatch: Any) -> None:
    from apps.jobs import runner
    from apps.jobs.models import Job

    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(runner, "broadcast", lambda *a, **k: sent.append((a, k)))
    job = Job.objects.create(kind="delete", title="Delete x", user=users["manager"], instance=None)
    runner._broadcast_job("job.finished", job)
    assert sent[-1][0][2] is None and sent[-1][1] == {"owner_id": str(users["manager"].pk)}


# --- D-3, refined: a known browser is not locked out --------------------------------------------------------
def _lock(name: str) -> None:
    from rest_framework.test import APIClient

    for n in range(10):
        APIClient(REMOTE_ADDR=f"172.20.0.{n}").post("/api/auth/login", {"username": name, "password": "wrong-password"})


def _known_browser(user_name: str, password: str, addr: str = "192.168.1.20") -> tuple[Any, Any]:
    from rest_framework.test import APIClient

    client = APIClient(REMOTE_ADDR=addr)
    resp = client.post("/api/auth/login", {"username": user_name, "password": password})
    assert resp.status_code == 200, resp.content
    return client, resp


@pytest.mark.django_db
def test_d3_a_known_browser_signs_in_to_a_locked_account(users: Any) -> None:
    from rest_framework.test import APIClient

    from tests.conftest import PASSWORD

    browser, resp = _known_browser("owner", PASSWORD)
    cookie = resp.cookies["mcpanel_known_browser"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and cookie["path"] == "/api/auth/login"
    assert int(cookie["max-age"]) >= 360 * 86400 and not cookie["secure"]  # (Secure like the session cookie)
    assert resp.cookies["mcpanel_known_browser_pw"]["path"] == "/api/auth/password"
    _lock("owner")
    stranger = APIClient(REMOTE_ADDR="10.9.9.9").post("/api/auth/login", {"username": "owner", "password": PASSWORD})
    assert stranger.status_code == 429 and "unlock_login" in stranger.json()["error"]["message"]
    again = browser.post("/api/auth/login", {"username": "owner", "password": PASSWORD})
    assert again.status_code == 200
    assert again.cookies["mcpanel_known_browser"].value != cookie.value  # rotated at each sign-in
    # a wrong password from the known browser is still just wrong (and still counted)
    assert browser.post("/api/auth/login", {"username": "owner", "password": "nope-nope-1"}).status_code == 401


@pytest.mark.django_db
def test_d3_the_cookie_of_one_account_does_not_help_another(users: Any) -> None:
    from tests.conftest import PASSWORD

    browser, _resp = _known_browser("manager", PASSWORD)
    _lock("owner")
    assert browser.post("/api/auth/login", {"username": "owner", "password": PASSWORD}).status_code == 429
    assert browser.post("/api/auth/login", {"username": "Manager", "password": PASSWORD}).status_code in (401, 429)


@pytest.mark.django_db
def test_d3_a_forged_or_expired_cookie_does_not_help(users: Any, monkeypatch: Any) -> None:
    import time as real_time
    from types import SimpleNamespace

    from django.core import signing
    from rest_framework.test import APIClient

    from apps.accounts import throttle
    from tests.conftest import PASSWORD

    _browser, resp = _known_browser("owner", PASSWORD)
    valid = resp.cookies["mcpanel_known_browser"].value
    _lock("owner")
    forged_value = signing.dumps({"u": users["owner"].pk, "n": "x"}, salt="something-else")
    for value in (forged_value, valid[:-2] + "xx", "garbage"):
        client = APIClient(REMOTE_ADDR="10.1.1.1")
        client.cookies["mcpanel_known_browser"] = value
        assert client.post("/api/auth/login", {"username": "owner", "password": PASSWORD}).status_code == 429, value
    # a year and a day later the real cookie no longer counts
    later = real_time.time() + throttle.KNOWN_BROWSER_AGE + 86400
    monkeypatch.setattr(signing, "time", SimpleNamespace(time=lambda: later))
    assert throttle.known_browser_user_id(SimpleNamespace(COOKIES={"mcpanel_known_browser": valid}), "login") is None
    monkeypatch.undo()
    assert throttle.known_browser_user_id(SimpleNamespace(COOKIES={"mcpanel_known_browser": valid}), "login") == users["owner"].pk


@pytest.mark.django_db
def test_d3_a_known_browser_may_change_the_password_of_a_locked_account(users: Any) -> None:
    from tests.conftest import PASSWORD

    browser, _resp = _known_browser("manager", PASSWORD)
    for _ in range(10):
        browser.post("/api/auth/password", {"old_password": "wrong-password", "new_password": "A-new-Passphrase-77"})
    resp = browser.post("/api/auth/password", {"old_password": PASSWORD, "new_password": "A-new-Passphrase-77"})
    assert resp.status_code == 204, resp.content


@pytest.mark.django_db
def test_d3_unlock_login_clears_the_counters(users: Any) -> None:
    from io import StringIO

    from django.core.management import call_command
    from rest_framework.test import APIClient

    from apps.audit.models import AuditEvent
    from tests.conftest import PASSWORD

    _lock("Owner")
    client = APIClient(REMOTE_ADDR="10.2.2.2")
    assert client.post("/api/auth/login", {"username": "owner", "password": PASSWORD}).status_code == 429
    out = StringIO()
    call_command("unlock_login", "owner", stdout=out)
    assert "Unlocked 'owner'" in out.getvalue()
    assert client.post("/api/auth/login", {"username": "owner", "password": PASSWORD}).status_code == 200
    assert AuditEvent.objects.filter(action="auth.login_unlocked").exists()


@pytest.mark.django_db
def test_d13_every_answer_with_a_schedule_hides_the_console_fields(
    make_instance: Any, make_user: Any, client_for: Any, users: Any
) -> None:
    """Follow-up of the frontend's round 1: PATCH and POST answered the command unhidden."""
    from apps.instances.models import Schedule

    inst = make_instance()
    sched = Schedule.objects.create(
        instance=inst,
        name="op",
        action="command",
        cron="0 * * * *",
        payload={"command": "op mallory"},
        last_result="Made mallory op",
    )
    pauser = client_for(_grant(make_user, inst, "pauser", ["instance.schedules", "instance.command"]))
    resp = pauser.patch(f"/api/instances/{inst.id}/schedules/{sched.id}", {"enabled": False}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["payload"]["command"] is None and resp.json()["last_result"] is None
    resp = pauser.post(
        f"/api/instances/{inst.id}/schedules",
        {"name": "say", "action": "command", "cron": "0 3 * * *", "payload": {"command": "say hi"}},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["payload"]["command"] is None
    # the audit log keeps the whole schedule; its answer hides it from a configurer without the console
    configurer = client_for(_grant(make_user, inst, "cfg2", ["instance.configure"]))
    trail = [r for r in configurer.get(f"/api/audit?instance={inst.id}").json() if r["action"].startswith("schedule.")]
    assert trail and all(r["details"]["payload"]["command"] is None for r in trail)
    owner_trail = client_for(users["owner"]).get(f"/api/audit?instance={inst.id}").json()
    assert {"op mallory", "say hi"} <= {
        r["details"]["payload"]["command"] for r in owner_trail if r["action"].startswith("schedule.")
    }
    # with the console permission everything is shown
    consoler = client_for(_grant(make_user, inst, "con2", ["instance.schedules", "instance.console"]))
    resp = consoler.patch(f"/api/instances/{inst.id}/schedules/{sched.id}", {"enabled": False}, format="json")
    assert resp.json()["payload"]["command"] == "op mallory" and resp.json()["last_result"] == "Made mallory op"


@pytest.mark.django_db
def test_d4_the_refusal_offers_the_override_only_to_who_may_use_it(
    make_instance: Any, fake_agent: Any, make_user: Any, client_for: Any, users: Any
) -> None:
    _server(make_instance, fake_agent, "Big", running=True, memory_mb=8192)
    other = _server(make_instance, fake_agent, "Other", running=False, memory_mb=6144)
    friend = client_for(_grant(make_user, other, "friend3", ["instance.control"]))
    url = f"/api/instances/{other.id}/power"
    refused = friend.post(url, {"action": "start"}, format="json").json()["error"]
    assert refused["code"] == "insufficient_memory"
    assert (
        "allow_overcommit" not in refused["message"] and "anyway" in refused["message"] and "administrator" in refused["message"]
    )
    owner = client_for(users["owner"]).post(url, {"action": "start"}, format="json").json()["error"]
    assert owner["code"] == "insufficient_memory" and "start it anyway (allow_overcommit)" in owner["message"]
