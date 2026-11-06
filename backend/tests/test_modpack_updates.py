"""Modpack "update available" checks (apps/modpacks/updates.py) against mocked provider APIs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import respx
from django.utils import timezone
from httpx import Response

from apps.audit.models import AuditEvent
from apps.core.models import SystemSetting
from apps.modpacks import updates
from apps.modpacks.models import ModpackUpdateCheck
from apps.notifications.models import NotificationChannel

pytestmark = pytest.mark.django_db

MR = "https://api.modrinth.com/v2"
CF = "https://api.curseforge.com"
FTB = "https://api.feed-the-beast.com/v1/modpacks/public"


def mr_version(vid, mc, date, *, loader="fabric", vtype="release"):
    return {
        "id": vid,
        "project_id": "P1",
        "name": vid,
        "version_number": f"{vid}+{mc}",
        "version_type": vtype,
        "status": "listed",
        "loaders": [loader],
        "game_versions": [mc],
        "date_published": date,
        "files": [],
        "dependencies": [],
    }


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr("apps.notifications.service.deliver.delay", lambda *a: calls.append(a))
    NotificationChannel.objects.create(
        name="ops", kind="webhook", url="https://hooks.test/x", events=["modpack.update_available"]
    )
    return calls


@pytest.fixture
def pack(make_instance):
    return make_instance(
        name="Pack Server",
        server_type="MODRINTH",
        minecraft_version="1.20.1",
        modpack_provider="modrinth",
        modpack_project_id="P1",
        modpack_name="Pack",
        modpack_version_id="V1",
        modpack_version_name="V1+1.20.1",
        modpack_loader="fabric",
    )


def _mock_versions(*versions):
    return respx.get(f"{MR}/project/P1/version").mock(return_value=Response(200, json=list(versions)))


# --- pure helpers ------------------------------------------------------------------------------
def test_published_at_normalises_provider_dates():
    assert updates.published_at("2024-08-23T23:35:03.592433Z") == datetime(2024, 8, 23, 23, 35, 3, 592433, tzinfo=UTC)
    assert updates.published_at(1700000000) == datetime.fromtimestamp(1700000000, tz=UTC)
    assert updates.published_at(None) is None and updates.published_at("not a date") is None


def test_pick_latest_respects_minecraft_loader_and_release_type():
    releases = [
        {"id": "C", "game_versions": ["1.21.1"], "loaders": ["fabric"], "version_type": "release"},
        {"id": "B2", "game_versions": ["1.20.1"], "loaders": ["fabric"], "version_type": "beta"},
        {"id": "Q", "game_versions": ["1.20.1"], "loaders": ["quilt"], "version_type": "release"},
        {"id": "B", "game_versions": ["1.20.1"], "loaders": ["fabric"], "version_type": "release"},
        {"id": "A", "game_versions": ["1.20.1"], "loaders": ["fabric"], "version_type": "release"},
    ]
    latest, pinned = updates.pick_latest(releases, pinned_id="A", minecraft_version="1.20.1", loader="fabric")
    assert latest["id"] == "B" and pinned["id"] == "A"
    # a server pinned to a beta also sees newer betas
    releases[4]["version_type"] = "beta"
    latest, _ = updates.pick_latest(releases, pinned_id="A", minecraft_version="1.20.1", loader="fabric")
    assert latest["id"] == "B2"
    # releases that declare no loader are not excluded by the loader filter (CurseForge files)
    latest, _ = updates.pick_latest(
        [{"id": "X", "game_versions": ["1.20.1"], "loaders": []}], pinned_id="A", minecraft_version="1.20.1", loader="forge"
    )
    assert latest["id"] == "X"
    assert updates.pick_latest([], pinned_id="A", minecraft_version="1.20.1", loader="")[0] is None


def test_is_newer():
    old = datetime(2026, 1, 1, tzinfo=UTC)
    assert updates.is_newer({"id": "B", "published": "2026-06-01T00:00:00Z"}, "A", old)
    assert not updates.is_newer({"id": "B", "published": "2025-06-01T00:00:00Z"}, "A", old)
    assert not updates.is_newer({"id": "A", "published": "2027-01-01T00:00:00Z"}, "A", old)
    assert updates.is_newer({"id": "B", "published": None}, "A", old)


# --- checks against providers --------------------------------------------------------------------
@respx.mock
def test_modrinth_check_records_latest_compatible_and_notifies_once(pack, sent):
    route = _mock_versions(
        mr_version("V9", "1.21.1", "2026-09-01T00:00:00Z"),  # other Minecraft line: ignored
        mr_version("V2", "1.20.1", "2026-06-01T00:00:00Z"),
        mr_version("V1", "1.20.1", "2026-01-01T00:00:00Z"),
    )
    check = updates.check_instance(pack)
    assert (check.status, check.latest_version_id, check.latest_version_name) == ("ok", "V2", "V2+1.20.1")
    assert check.update_available and check.latest_minecraft_version == "1.20.1"
    assert check.latest_published == datetime(2026, 6, 1, tzinfo=UTC)
    assert len(sent) == 1
    _channel, event, title, _message, data = sent[0]
    assert event == "modpack.update_available" and "V2+1.20.1" in title
    assert data["server"] == "Pack Server" and data["detail"] == "V1+1.20.1 → V2+1.20.1"

    updates.check_instance(pack)  # same release again: no second notification
    assert len(sent) == 1
    assert route.call_count == 1  # the adapter's release cache absorbed the second check

    from django.core.cache import cache

    cache.clear()
    _mock_versions(
        mr_version("V3", "1.20.1", "2026-08-01T00:00:00Z"),
        mr_version("V2", "1.20.1", "2026-06-01T00:00:00Z"),
        mr_version("V1", "1.20.1", "2026-01-01T00:00:00Z"),
    )
    check = updates.check_instance(pack)
    assert check.latest_version_id == "V3" and len(sent) == 2 and check.notified_version_id == "V3"


@respx.mock
def test_up_to_date_server(pack, sent):
    _mock_versions(mr_version("V1", "1.20.1", "2026-01-01T00:00:00Z"))
    check = updates.check_instance(pack)
    assert check.status == "ok" and not check.update_available and sent == []
    s = updates.summary(pack)
    assert s["available"] is False and s["latest"]["id"] == "V1"


@respx.mock
def test_pinned_release_missing_from_list_is_looked_up(pack, sent):
    _mock_versions(mr_version("V2", "1.20.1", "2025-06-01T00:00:00Z"))
    respx.get(f"{MR}/version/V1").mock(return_value=Response(200, json=mr_version("V1", "1.20.1", "2026-01-01T00:00:00Z")))
    check = updates.check_instance(pack)
    assert check.latest_version_id == "V2" and check.update_available is False  # V2 is older than the pin


@respx.mock
def test_provider_failure_is_recorded_not_raised(pack, sent):
    _mock_versions(mr_version("V2", "1.20.1", "2026-06-01T00:00:00Z"), mr_version("V1", "1.20.1", "2026-01-01T00:00:00Z"))
    updates.check_instance(pack)
    from django.core.cache import cache

    cache.clear()
    respx.get(f"{MR}/project/P1/version").mock(return_value=Response(503))
    check = updates.check_instance(pack)
    assert check.status == "error" and "HTTP 503" in check.detail
    assert check.latest_version_id == "V2" and check.update_available  # last success is kept
    assert updates.summary(pack)["available"] is True


def test_unexpected_exception_is_recorded(pack, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bad payload")

    monkeypatch.setattr("apps.modpacks.providers.Modrinth.releases", boom)
    check = updates.check_instance(pack)
    assert check.status == "error" and "RuntimeError" in check.detail


def test_keyless_curseforge_is_unavailable_without_any_request(make_instance, settings, sent):
    settings.CURSEFORGE_API_KEY = ""
    inst = make_instance(
        server_type="AUTO_CURSEFORGE",
        minecraft_version="1.20.1",
        modpack_provider="curseforge",
        modpack_project_id="123",
        modpack_version_id="9104992",
    )
    with respx.mock(assert_all_called=False) as router:
        route = router.route(host="api.curseforge.com")
        check = updates.check_instance(inst)
    assert check.status == "unavailable"
    assert check.detail == "Update check unavailable without a CurseForge API key"
    assert not route.called and sent == []
    s = updates.summary(inst)
    assert s["status"] == "unavailable" and s["available"] is False and s["latest"] is None


def test_modpack_without_project_is_unavailable(make_instance):
    inst = make_instance(server_type="AUTO_CURSEFORGE", modpack_provider="curseforge", modpack_project_id="")
    assert updates.check_instance(inst).status == "unavailable"


@respx.mock
def test_curseforge_with_key_filters_by_minecraft_version(make_instance, settings, sent):
    settings.CURSEFORGE_API_KEY = ""
    SystemSetting.set_secret("curseforge_api_key", "test-key")
    inst = make_instance(
        server_type="AUTO_CURSEFORGE",
        minecraft_version="1.20.1",
        modpack_provider="curseforge",
        modpack_project_id="123",
        modpack_version_id="100",
        modpack_loader="forge",
    )

    def cf_file(fid, date):
        return {"id": fid, "modId": 123, "displayName": f"Pack {fid}", "gameVersions": ["1.20.1", "Forge"], "fileDate": date}

    route = respx.get(f"{CF}/v1/mods/123/files").mock(
        return_value=Response(200, json={"data": [cf_file(200, "2026-05-01T00:00:00Z"), cf_file(100, "2026-01-01T00:00:00Z")]})
    )
    check = updates.check_instance(inst)
    assert route.calls.last.request.url.params["gameVersion"] == "1.20.1"
    assert route.calls.last.request.headers["x-api-key"] == "test-key"
    assert (check.status, check.latest_version_id, check.update_available) == ("ok", "200", True)


@respx.mock
def test_ftb_check_uses_unix_timestamps(make_instance, sent):
    inst = make_instance(
        server_type="FTBA",
        minecraft_version="1.18.2",
        modpack_provider="ftb",
        modpack_project_id="100",
        modpack_version_id="100359",
        modpack_loader="forge",
    )

    def ver(vid, ts, mc="1.18.2"):
        return {
            "id": vid,
            "name": f"v{vid}",
            "type": "release",
            "updated": ts,
            "targets": [{"type": "game", "version": mc}, {"type": "modloader", "name": "forge", "version": "40"}],
        }

    respx.get(f"{FTB}/modpack/100").mock(
        return_value=Response(
            200,
            json={
                "status": "success",
                "id": 100,
                "name": "StoneBlock",
                "versions": [ver(100359, 1_700_000_000), ver(100400, 1_710_000_000), ver(100500, 1_720_000_000, "1.20.1")],
            },
        )
    )
    check = updates.check_instance(inst)
    assert (check.latest_version_id, check.latest_version_name, check.update_available) == ("100400", "v100400", True)


# --- scheduling ----------------------------------------------------------------------------------
def test_due_instances_and_periodic_task(make_instance, pack, monkeypatch):
    now = timezone.now()
    recent = make_instance(server_type="MODRINTH", modpack_provider="modrinth", modpack_project_id="P2")
    ModpackUpdateCheck.objects.create(instance=recent, checked_at=now - timedelta(hours=1))
    old = make_instance(server_type="FTBA", modpack_provider="ftb", modpack_project_id="7")
    ModpackUpdateCheck.objects.create(instance=old, checked_at=now - timedelta(hours=7))
    make_instance(server_type="MODRINTH", modpack_provider="modrinth", modpack_project_id="P3", archived_at=now)
    make_instance(server_type="PAPER")
    due = updates.due_instances(now)
    assert set(due) == {pack.pk, old.pk}

    checked = []
    monkeypatch.setattr("apps.modpacks.updates.check_instance", lambda inst: checked.append(inst.pk) or ModpackUpdateCheck())
    from apps.modpacks.tasks import check_updates_due

    assert check_updates_due() == 2
    assert set(checked) == {pack.pk, old.pk}


def test_beat_schedule_registers_the_check(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["modpack-update-check"]
    assert entry["task"] == "apps.modpacks.tasks.check_updates_due"
    from apps.modpacks import tasks  # noqa: F401  (task names resolve)
    from mcpanel.celery import app

    assert "apps.modpacks.tasks.check_updates_due" in app.tasks


# --- API -------------------------------------------------------------------------------------------
@respx.mock
def test_api_exposes_and_runs_the_check(pack, clients, sent):
    r = clients["viewer"].get(f"/api/instances/{pack.id}")
    assert r.json()["modpack_update"]["status"] == "pending"

    _mock_versions(mr_version("V2", "1.20.1", "2026-06-01T00:00:00Z"), mr_version("V1", "1.20.1", "2026-01-01T00:00:00Z"))
    r = clients["viewer"].post(f"/api/instances/{pack.id}/modpack/check")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["available"] is True and body["latest"]["id"] == "V2" and body["throttled"] is False
    assert AuditEvent.objects.filter(action="modpack.update_checked").exists()

    r = clients["viewer"].post(f"/api/instances/{pack.id}/modpack/check")
    assert r.json()["throttled"] is True  # repeated clicks do not hit the provider again

    listed = next(i for i in clients["owner"].get("/api/instances/").json() if i["id"] == str(pack.id))
    assert listed["modpack_update"]["available"] is True
    assert listed["modpack_update"]["latest"]["name"] == "V2+1.20.1"
    modpack = clients["owner"].get(f"/api/instances/{pack.id}/modpack").json()
    assert modpack["update_check"]["latest"]["id"] == "V2"

    assert clients["norole"].post(f"/api/instances/{pack.id}/modpack/check").status_code == 404


def test_api_check_refuses_standard_servers(make_instance, clients):
    inst = make_instance(server_type="PAPER")
    assert clients["owner"].get(f"/api/instances/{inst.id}").json()["modpack_update"] is None
    r = clients["owner"].post(f"/api/instances/{inst.id}/modpack/check")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_modpack"


def test_summary_after_the_pin_changes(pack):
    now = timezone.now()
    ModpackUpdateCheck.objects.create(
        instance=pack,
        checked_at=now,
        last_success_at=now,
        checked_version_id="V1",
        latest_version_id="V3",
        latest_version_name="V3",
        update_available=True,
    )
    pack.refresh_from_db()
    assert updates.summary(pack)["available"] is True
    pack.modpack_version_id = "V3"  # updated to the latest release
    assert updates.summary(pack)["available"] is False and updates.summary(pack)["stale"] is False
    pack.modpack_version_id = "V2"  # updated (or rolled back) to something else: unknown until re-checked
    s = updates.summary(pack)
    assert s["available"] is False and s["stale"] is True
