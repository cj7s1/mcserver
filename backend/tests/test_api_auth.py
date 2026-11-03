"""Auth API: first-run setup, login/logout, CSRF, throttling, user + role management guards."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent

from .conftest import PASSWORD

pytestmark = pytest.mark.django_db


def err(resp) -> str:
    return resp.json()["error"]["code"]


# --- setup ---------------------------------------------------------------------------------
def test_setup_only_once(roles):
    client = APIClient()
    state = client.get("/api/auth/state").json()
    assert state == {"setup_required": True, "authenticated": False, "user": None}

    resp = client.post("/api/auth/setup", {"username": "first", "password": "a-Very-long-passphrase"})
    assert resp.status_code == 201, resp.content
    assert resp.json()["is_owner"] is True
    owner = User.objects.get(username="first")
    assert owner.is_owner and owner.check_password("a-Very-long-passphrase")
    assert client.get("/api/auth/me").status_code == 200  # setup logs the owner in
    assert set(Role.objects.filter(is_system=True).values_list("key", flat=True)) >= {"administrator", "operator", "viewer"}

    again = APIClient().post("/api/auth/setup", {"username": "second", "password": "another-Long-passphrase"})
    assert again.status_code == 409
    assert err(again) == "setup_complete"
    assert not User.objects.filter(username="second").exists()
    assert APIClient().get("/api/auth/state").json()["setup_required"] is False


@pytest.mark.django_db(transaction=True)
def test_concurrent_setups_cannot_both_create_an_owner():
    """While another setup transaction holds the lock with an uncommitted owner, a second
    setup must wait and then fail (an empty-table row lock would let both through)."""
    import threading

    from django.db import connections, transaction

    from apps.accounts.views import SETUP_LOCK_KEY

    holding, release, result = threading.Event(), threading.Event(), {}

    def first_setup() -> None:
        try:
            with transaction.atomic():
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SETUP_LOCK_KEY])
                User.objects.create(username="winner", is_owner=True)
                holding.set()
                release.wait(10)
        finally:
            connections.close_all()

    def second_setup() -> None:
        try:
            resp = APIClient().post("/api/auth/setup", {"username": "racer", "password": "a-Very-long-passphrase"})
            result["status"] = resp.status_code
        finally:
            connections.close_all()

    first = threading.Thread(target=first_setup)
    first.start()
    assert holding.wait(10)
    second = threading.Thread(target=second_setup)
    second.start()
    second.join(1.0)
    blocked = second.is_alive()
    release.set()
    first.join(10)
    second.join(10)
    assert blocked, "second setup did not wait for the first one"
    assert result["status"] == 409
    assert list(User.objects.values_list("username", flat=True)) == ["winner"]


def test_setup_rejects_weak_password(roles):
    resp = APIClient().post("/api/auth/setup", {"username": "first", "password": "password12"})
    assert resp.status_code == 400
    assert not User.objects.exists()


# --- login / logout --------------------------------------------------------------------------
def test_login_logout_flow(users):
    client = APIClient()
    assert client.get("/api/auth/me").status_code == 403
    bad = client.post("/api/auth/login", {"username": "operator", "password": "wrong-password"})
    assert bad.status_code == 401
    assert err(bad) == "invalid_credentials"
    assert AuditEvent.objects.filter(action="auth.login_failed", success=False).exists()

    ok = client.post("/api/auth/login", {"username": "operator", "password": PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["username"] == "operator"
    assert "mcpanel_session" in ok.cookies
    me = client.get("/api/auth/me").json()
    assert "instance.control" in me["permissions"] and me["is_owner"] is False

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 403
    assert client.get("/api/auth/state").json()["authenticated"] is False


def test_inactive_user_cannot_login(make_user):
    make_user("gone", active=False)
    resp = APIClient().post("/api/auth/login", {"username": "gone", "password": PASSWORD})
    assert resp.status_code == 401


def test_unknown_user_gets_same_error_as_wrong_password(users):
    resp = APIClient().post("/api/auth/login", {"username": "nobody-here", "password": PASSWORD})
    assert resp.status_code == 401
    assert err(resp) == "invalid_credentials"


def test_login_is_throttled(users):
    client = APIClient()
    codes = [client.post("/api/auth/login", {"username": "admin", "password": "nope-nope-nope"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429
    # even the right password is refused while throttled
    assert client.post("/api/auth/login", {"username": "admin", "password": PASSWORD}).status_code == 429


# --- CSRF ------------------------------------------------------------------------------------
def test_csrf_enforced_for_session_requests(users, make_instance, fake_agent):
    inst = make_instance()
    client = APIClient(enforce_csrf_checks=True)
    assert client.get("/api/auth/state").status_code == 200
    pre_token = client.cookies["mcpanel_csrftoken"].value
    # login itself is CSRF-protected (prevents login CSRF)
    assert client.post("/api/auth/login", {"username": "owner", "password": PASSWORD}).status_code == 403
    assert (
        client.post("/api/auth/login", {"username": "owner", "password": PASSWORD}, HTTP_X_CSRFTOKEN=pre_token).status_code == 200
    )
    token = client.cookies["mcpanel_csrftoken"].value

    # safe methods need no token
    assert client.get(f"/api/instances/{inst.id}").status_code == 200
    # unsafe methods without the header are refused
    for method, url, body in [
        ("post", f"/api/instances/{inst.id}/power", {"action": "start"}),
        ("patch", f"/api/instances/{inst.id}", {"memory_mb": 1024}),
        ("post", "/api/auth/logout", None),
    ]:
        resp = getattr(client, method)(url, body, format="json")
        assert resp.status_code == 403, (method, url)
        assert "CSRF" in resp.json()["error"]["message"]
    inst.refresh_from_db()
    assert inst.memory_mb == 2048
    assert not inst.jobs.exists()

    # with the token from the cookie the request goes through
    resp = client.patch(f"/api/instances/{inst.id}", {"memory_mb": 1024}, format="json", HTTP_X_CSRFTOKEN=token)
    assert resp.status_code == 200
    # a wrong token is still refused
    bad = client.post("/api/auth/logout", HTTP_X_CSRFTOKEN="x" * 32)
    assert bad.status_code == 403
    assert client.post("/api/auth/logout", HTTP_X_CSRFTOKEN=token).status_code == 204


# --- users & roles -----------------------------------------------------------------------------
@pytest.mark.parametrize("who", ["operator", "viewer", "norole", "manager"])
def test_user_endpoints_require_system_users(clients, users, who):
    c = clients[who]
    assert c.get("/api/users").status_code == 403
    assert c.post("/api/users", {"username": "x", "password": "Some-long-passphrase"}).status_code == 403
    assert c.patch(f"/api/users/{users['viewer'].id}", {"email": "a@b.c"}).status_code == 403
    assert c.delete(f"/api/users/{users['viewer'].id}").status_code == 403
    assert c.post(f"/api/users/{users['viewer'].id}/reset-link").status_code == 403
    assert c.post("/api/roles", {"key": "r", "name": "R", "permissions": []}).status_code == 403


def test_anonymous_user_endpoints(clients):
    assert clients["anon"].get("/api/users").status_code == 403
    assert clients["anon"].get("/api/roles").status_code == 403


def test_admin_manages_users(clients, roles):
    admin = clients["admin"]
    listing = admin.get("/api/users")
    assert listing.status_code == 200
    assert {u["username"] for u in listing.json()} >= {"owner", "admin", "operator"}
    created = admin.post("/api/users", {"username": "newop", "password": "Fresh-passphrase-77", "role": roles["operator"].id})
    assert created.status_code == 201, created.content
    assert "password" not in created.json()
    assert User.objects.get(username="newop").check_password("Fresh-passphrase-77")
    weak = admin.post("/api/users", {"username": "weak", "password": "1234567890"})
    assert weak.status_code == 400


def test_only_owner_creates_or_promotes_administrators(clients, users, roles):
    admin_role = roles["administrator"].id
    denied = clients["admin"].post("/api/users", {"username": "admin2", "password": "Fresh-passphrase-77", "role": admin_role})
    assert denied.status_code == 403
    assert not User.objects.filter(username="admin2").exists()
    # the same rule applies when changing an existing user's role
    promote = clients["admin"].patch(f"/api/users/{users['operator'].id}", {"role": admin_role}, format="json")
    assert promote.status_code == 403
    users["operator"].refresh_from_db()
    assert users["operator"].role.key == "operator"

    ok = clients["owner"].post("/api/users", {"username": "admin2", "password": "Fresh-passphrase-77", "role": admin_role})
    assert ok.status_code == 201
    assert clients["owner"].patch(f"/api/users/{users['operator'].id}", {"role": admin_role}, format="json").status_code == 200


def test_admin_cannot_modify_owner(clients, users):
    owner = users["owner"]
    admin = clients["admin"]
    assert admin.get(f"/api/users/{owner.id}").status_code == 200  # visible, not editable
    assert admin.patch(f"/api/users/{owner.id}", {"is_active": False}, format="json").status_code == 403
    assert admin.patch(f"/api/users/{owner.id}", {"email": "evil@example.com"}, format="json").status_code == 403
    assert admin.delete(f"/api/users/{owner.id}").status_code == 403
    assert admin.post(f"/api/users/{owner.id}/reset-link").status_code == 403
    owner.refresh_from_db()
    assert owner.is_active and owner.email == ""
    # is_owner is read-only even for the owner's own update
    clients["owner"].patch(f"/api/users/{users['admin'].id}", {"is_owner": True}, format="json")
    users["admin"].refresh_from_db()
    assert users["admin"].is_owner is False


def test_self_protection(clients, users, roles):
    admin = clients["admin"]
    me = users["admin"]
    assert admin.patch(f"/api/users/{me.id}", {"is_active": False}, format="json").status_code == 400
    assert admin.patch(f"/api/users/{me.id}", {"role": roles["viewer"].id}, format="json").status_code == 400
    assert admin.delete(f"/api/users/{me.id}").status_code == 400
    me.refresh_from_db()
    assert me.is_active and me.role.key == "administrator"


def test_owner_manages_admins(clients, users):
    owner = clients["owner"]
    assert owner.patch(f"/api/users/{users['admin'].id}", {"is_active": False}, format="json").status_code == 200
    link = owner.post(f"/api/users/{users['admin'].id}/reset-link")
    assert link.status_code == 201 and len(link.json()["token"]) > 30
    assert owner.delete(f"/api/users/{users['viewer'].id}").status_code == 204


@pytest.fixture
def user_manager(make_user, client_for):
    role = Role.objects.create(key="user_manager", name="User manager", permissions=["system.users", "instance.view"])
    return client_for(make_user("usermgr", role=role)), role


def test_user_manager_cannot_escalate(user_manager, users, roles):
    client, own_role = user_manager
    # cannot hand out roles carrying permissions it lacks
    resp = client.post(
        "/api/users", {"username": "puppet", "password": "Fresh-passphrase-77", "role": roles["server_manager"].id}
    )
    assert resp.status_code == 403
    assert client.patch(f"/api/users/{users['norole'].id}", {"role": roles["operator"].id}, format="json").status_code == 403
    # cannot mint or widen custom roles beyond its own permissions
    assert (
        client.post("/api/roles", {"key": "god", "name": "God", "permissions": ["system.settings"]}, format="json").status_code
        == 403
    )
    assert (
        client.patch(f"/api/roles/{own_role.id}", {"permissions": ["system.users", "system.settings"]}, format="json").status_code
        == 403
    )
    own_role.refresh_from_db()
    assert own_role.permissions == ["system.users", "instance.view"]
    # cannot take over higher-privileged accounts (reset link, disable)
    assert client.post(f"/api/users/{users['admin'].id}/reset-link").status_code == 403
    assert client.patch(f"/api/users/{users['operator'].id}", {"is_active": False}, format="json").status_code == 403
    # but can manage roles and users within its own permissions
    watch = client.post("/api/roles", {"key": "watch", "name": "Watch", "permissions": ["instance.view"]}, format="json")
    assert watch.status_code == 201
    ok = client.post("/api/users", {"username": "plain", "password": "Fresh-passphrase-77", "role": watch.json()["id"]})
    assert ok.status_code == 201
    assert client.patch(f"/api/users/{users['norole'].id}", {"first_name": "Nora"}, format="json").status_code == 200


def test_roles(clients, roles):
    assert clients["viewer"].get("/api/roles").status_code == 200  # role names are needed in the UI
    builtin = roles["operator"].id
    resp = clients["admin"].patch(f"/api/roles/{builtin}", {"permissions": ["instance.view"]}, format="json")
    assert resp.status_code == 400
    assert clients["admin"].delete(f"/api/roles/{builtin}").status_code == 400
    bad = clients["admin"].post("/api/roles", {"key": "x", "name": "X", "permissions": ["not.a.code"]}, format="json")
    assert bad.status_code == 400
    created = clients["admin"].post(
        "/api/roles", {"key": "ops2", "name": "Ops 2", "permissions": ["instance.view"]}, format="json"
    )
    assert created.status_code == 201
    assert created.json()["is_system"] is False


def test_password_change(clients, users):
    c = clients["viewer"]
    wrong = c.post("/api/auth/password", {"old_password": "nope-nope-nope", "new_password": "Brand-new-passphrase-1"})
    assert wrong.status_code == 400
    ok = c.post("/api/auth/password", {"old_password": PASSWORD, "new_password": "Brand-new-passphrase-1"})
    assert ok.status_code == 204
    users["viewer"].refresh_from_db()
    assert users["viewer"].check_password("Brand-new-passphrase-1")
    assert c.get("/api/auth/me").status_code == 200  # session survives the change


def test_password_reset_token_is_single_use(clients, users):
    raw = clients["owner"].post(f"/api/users/{users['viewer'].id}/reset-link").json()["token"]
    anon = APIClient()
    assert anon.post("/api/auth/reset", {"token": raw, "new_password": "Reset-passphrase-99"}).status_code == 204
    assert anon.post("/api/auth/reset", {"token": raw, "new_password": "Another-passphrase-9"}).status_code == 400
    users["viewer"].refresh_from_db()
    assert users["viewer"].check_password("Reset-passphrase-99")


def test_user_manager_cannot_take_over_accounts_with_server_grants(user_manager, make_user, make_instance, roles):
    """A reset link (or editing/disabling) would hand the target's per-server access to the caller."""
    from apps.accounts.models import InstanceAccess

    client, _role = user_manager
    inst = make_instance()
    victim = make_user("victim")
    InstanceAccess.objects.create(user=victim, instance=inst, role=roles["server_manager"])
    assert client.post(f"/api/users/{victim.id}/reset-link").status_code == 403
    assert client.patch(f"/api/users/{victim.id}", {"is_active": False}, format="json").status_code == 403
    assert client.delete(f"/api/users/{victim.id}").status_code == 403
    victim.refresh_from_db()
    assert victim.is_active
    # a grant within the caller's own permissions (instance.view) does not block management
    harmless = make_user("harmless")
    watch = Role.objects.create(key="watch_only", name="Watch only", permissions=["instance.view"])
    InstanceAccess.objects.create(user=harmless, instance=inst, role=watch)
    assert client.post(f"/api/users/{harmless.id}/reset-link").status_code == 201


def test_superuser_accounts_count_as_root(user_manager, make_user):
    client, _role = user_manager
    root = make_user("root2")
    User.objects.filter(pk=root.pk).update(is_superuser=True)  # e.g. made with `manage.py createsuperuser`
    assert client.post(f"/api/users/{root.id}/reset-link").status_code == 403


@pytest.mark.django_db
def test_only_one_owner_can_exist(users):
    from django.db import IntegrityError, transaction

    from apps.accounts.models import User

    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("second-owner", password="x" * 12, is_owner=True)


@pytest.mark.django_db
def test_oversized_upload_rejected_before_parsing(users, make_instance, settings, client_for):
    settings.MCPANEL_MAX_UPLOAD_BYTES = 1000
    inst = make_instance()
    c = client_for(users["owner"])
    r = c.generic("POST", f"/api/instances/{inst.id}/files/upload", b"x" * 2000, content_type="application/octet-stream")
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"
