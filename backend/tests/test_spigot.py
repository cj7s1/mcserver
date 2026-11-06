"""SpigotMC plugin source (Spiget API + cdn.spiget.org mirror), with every upstream call mocked.

Response shapes are the ones recorded from the live API on 2026-11-06 (docs/reference/spiget-api.md).
"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile

import pytest
import respx
from httpx import Response

API = "https://api.spiget.org/v2"
CDN = "https://cdn.spiget.org/file/spiget-resources"
MR = "https://api.modrinth.com/v2"
RID = 81534
LATEST_TS = 1793762275  # 2026-11-04
DESCRIPTION_HTML = (
    '<b><span style="color:red">Chunky</span></b>\n<br>Pre-generates chunks &amp; more'
    '<script>alert("x")</script><br><a href="javascript:alert(1)">click</a><img src=x onerror=alert(1)>'
)


def jar(
    descriptor: str | None = "name: Chunky\nversion: '1.5.3'\nmain: org.popcraft.chunky.ChunkyBukkit\n", **extra: bytes
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if descriptor is not None:
            zf.writestr("plugin.yml", descriptor)
        for name, data in extra.items():
            zf.writestr(name.replace("__", "."), data)
        zf.writestr("org/popcraft/Chunky.class", b"\xca\xfe\xba\xbe")
    return buf.getvalue()


JAR = jar()


def resource(**over):
    data = {
        "id": RID,
        "name": "Chunky",
        "tag": "Pre-generates chunks, quickly, efficiently, and safely",
        "external": False,
        "premium": False,
        "file": {"type": ".jar", "size": 390.2, "sizeUnit": "KB", "url": f"resources/chunky.{RID}/download?version=658870"},
        "testedVersions": ["1.20", "1.21", "26.1"],
        "version": {"id": 658870},
        "author": {"id": 9610},
        "icon": {"url": f"data/resource_icons/81/{RID}.jpg?1612919814", "data": "AAAA"},
        "downloads": 650713,
        "likes": 83,
        "rating": {"count": 176, "average": 5},
        "updateDate": LATEST_TS,
        "releaseDate": 1594853100,
        "description": base64.b64encode(DESCRIPTION_HTML.encode()).decode(),
        "sourceCodeLink": "https://github.com/pop4959/Chunky",
    }
    data.update(over)
    return data


VERSIONS = [
    {"id": 658870, "name": "1.5.66", "releaseDate": LATEST_TS, "downloads": 7},
    {"id": 635794, "name": "1.5.3", "releaseDate": 1777875780, "downloads": 20754},
]


def cdn_headers(data: bytes, uploaded_ms: int) -> dict[str, str]:
    return {
        "content-type": "application/java-archive",
        "content-length": str(len(data)),
        "x-bz-content-sha1": hashlib.sha1(data).hexdigest(),
        "x-bz-upload-timestamp": str(uploaded_ms),
    }


@pytest.fixture
def spiget():
    """A respx router with Chunky's live shapes. ``state`` lets a test swap the mirrored bytes."""
    state = {"jar": JAR, "uploaded_ms": (LATEST_TS + 3600) * 1000, "res": resource(), "identified": {}}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/resources/{RID}").mock(side_effect=lambda _r: Response(200, json=state["res"]))
        router.get(f"{API}/resources/{RID}/versions").mock(return_value=Response(200, json=VERSIONS))
        router.get(f"{API}/authors/9610").mock(return_value=Response(200, json={"id": 9610, "name": "pop4959"}))
        router.get(f"{API}/resources/{RID}/download", name="redirect").mock(
            return_value=Response(302, headers={"location": f"{CDN}/{RID}.jar", "x-spiget-file-source": "cdn"})
        )
        router.head(f"{CDN}/{RID}.jar", name="head").mock(
            side_effect=lambda _r: Response(200, headers=cdn_headers(state["jar"], state["uploaded_ms"]))
        )
        router.get(f"{CDN}/{RID}.jar", name="cdn").mock(
            side_effect=lambda _r: Response(200, content=state["jar"], headers=cdn_headers(state["jar"], state["uploaded_ms"]))
        )
        router.post(f"{MR}/version_files").mock(side_effect=lambda _r: Response(200, json=state["identified"]))
        router.get(f"{MR}/projects", name="mr_projects").mock(
            side_effect=lambda _r: Response(200, json=state.get("projects", []))
        )
        yield router, state


@pytest.fixture
def paper(make_instance, fake_agent):
    inst = make_instance(server_type="PAPER", minecraft_version="1.21.1")
    fake_agent.prepare(inst.id)
    return inst


def url(inst, sub: str) -> str:
    return f"/api/instances/{inst.id}/content/spigot/{sub}"


# --------------------------------------------------------------------------------------
# search / details
# --------------------------------------------------------------------------------------
def test_search_marks_premium_external_and_untested(paper, client_for, users):
    hits = [
        resource(),
        resource(id=987, name="VoteParty", premium=True, price=7, currency="usd"),
        resource(
            id=53460,
            name="LuckPermsGUI",
            external=True,
            file={"type": "external", "size": 0, "sizeUnit": "", "externalUrl": "https://download.example.org/x/"},
        ),
        resource(id=140306, name="TradeGUI", file={"type": ".zip", "size": 45.5, "sizeUnit": "KB"}),
        resource(id=5, name="Old", testedVersions=["1.8", "1.12"]),
    ]
    with respx.mock() as router:
        route = router.get(f"{API}/search/resources/chunky").mock(
            return_value=Response(200, json=hits, headers={"x-page-count": "3"})
        )
        r = client_for(users["owner"]).get(url(paper, "search"), {"q": "chunky", "page": 2})
    assert r.status_code == 200, r.json()
    assert route.calls.last.request.url.params["page"] == "2"
    assert route.calls.last.request.url.params["sort"] == "-downloads"
    body = r.json()
    assert body["pages"] == 3 and body["page"] == 2 and body["minecraft_version"] == "1.21.1"
    by_id = {h["id"]: h for h in body["results"]}
    assert by_id[RID]["installable"] is True and by_id[RID]["tested"] is True
    assert by_id[RID]["icon_url"] == f"https://www.spigotmc.org/data/resource_icons/81/{RID}.jpg?1612919814"
    assert by_id[RID]["url"] == f"https://www.spigotmc.org/resources/{RID}/"
    assert [by_id[i]["reason"] for i in (987, 53460, 140306)] == ["premium", "external", "not_a_jar"]
    assert "download.example.org" in by_id[53460]["reason_message"]
    assert by_id[5]["tested"] is False and by_id[5]["installable"] is True
    assert all(not str(h.get("icon_url") or "").startswith("data:") for h in body["results"])  # no inline base64


def test_search_without_hits_is_empty_not_an_error(paper, client_for, users):
    """Spiget answers a search without matches with 404 and []."""
    with respx.mock() as router:
        router.get(f"{API}/search/resources/zzqq").mock(return_value=Response(404, json=[]))
        r = client_for(users["owner"]).get(url(paper, "search"), {"q": "zzqq"})
    assert r.status_code == 200
    assert r.json()["results"] == [] and r.json()["pages"] == 0


def test_empty_query_lists_popular_free_resources(paper, client_for, users):
    with respx.mock() as router:
        route = router.get(f"{API}/resources/free").mock(return_value=Response(200, json=[resource()]))
        r = client_for(users["owner"]).get(url(paper, "search"))
    assert r.status_code == 200 and route.called
    assert r.json()["results"][0]["id"] == RID


def test_dot_segments_are_encoded(paper, client_for, users):
    with respx.mock() as router:
        route = router.get(f"{API}/search/resources/%2E%2E").mock(return_value=Response(200, json=[]))
        r = client_for(users["owner"]).get(url(paper, "search"), {"q": ".."})
    assert r.status_code == 200 and route.called


def test_search_text_cannot_escape_the_path(paper, client_for, users):
    with respx.mock() as router:
        route = router.get(url__regex=rf"^{API}/search/resources/[^/]+$").mock(return_value=Response(200, json=[]))
        r = client_for(users["owner"]).get(url(paper, "search"), {"q": "../../resources/1/download?x=#"})
    assert r.status_code == 200
    assert route.called
    assert "/search/resources/" in str(route.calls.last.request.url)
    assert "download" in str(route.calls.last.request.url)  # stays text inside the one path segment


def test_details_are_plain_text_with_mirror_state(paper, client_for, users, spiget):
    _, state = spiget
    state["uploaded_ms"] = (1777875780 + 60) * 1000  # stored after 1.5.3, before 1.5.66
    r = client_for(users["owner"]).get(url(paper, f"resources/{RID}"))
    assert r.status_code == 200, r.json()
    body = r.json()
    text = body["resource"]["description_text"]
    assert "Chunky" in text and "Pre-generates chunks & more" in text
    assert "<" not in text and "alert" not in text and "javascript" not in text
    assert body["resource"]["author"] == "pop4959"
    assert body["latest_version"]["name"] == "1.5.66"
    assert body["mirror"]["available"] is True and body["mirror"]["freshness"] == "outdated"
    assert [w["code"] for w in body["warnings"]] == ["mirror_outdated"]
    assert body["versions"][1]["url"] == f"https://www.spigotmc.org/resources/{RID}/download?version=635794"
    assert body["installed_files"] == []


def test_details_of_premium_resource_do_not_touch_the_mirror(paper, client_for, users, spiget):
    router, state = spiget
    state["res"] = resource(premium=True, price=7)
    r = client_for(users["owner"]).get(url(paper, f"resources/{RID}"))
    assert r.status_code == 200
    assert r.json()["resource"]["installable"] is False and r.json()["mirror"] is None
    assert not router.routes["redirect"].called and not router.routes["head"].called


# --------------------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------------------
def test_install_streams_verified_bytes_to_the_agent(paper, fake_agent, client_for, users, spiget):
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["installed"]["filename"] == "Chunky-1.5.3.jar"
    assert body["installed"]["version"] == "1.5.3" and body["installed"]["mirror"] == "current"
    assert body["installed"]["sha256"] == hashlib.sha256(JAR).hexdigest()
    assert fake_agent.get_file(paper.id, "plugins/Chunky-1.5.3.jar") == JAR
    (_args, kwargs), *_ = fake_agent.called("upload")
    assert kwargs["sha256"] == hashlib.sha256(JAR).hexdigest()  # the agent re-checks the bytes
    assert not fake_agent.called("fetch_file")  # nothing is fetched by the agent
    paper.refresh_from_db()
    rec = paper.modpack_meta["spigot"]["Chunky-1.5.3.jar"]
    assert rec["resource_id"] == RID and rec["version"] == "1.5.3" and rec["spigot_version"] == "1.5.66"
    from apps.audit.models import AuditEvent

    event = AuditEvent.objects.filter(action="content.spigot_install").latest("created_at")
    assert event.success and event.details["sha256"] == rec["sha256"]

    inv = client_for(users["owner"]).get(f"/api/instances/{paper.id}/content/").json()
    item = next(f for f in inv["files"] if f["filename"] == "Chunky-1.5.3.jar")
    assert item["source"] == "spigot" and item["spigot"]["resource_id"] == RID
    assert inv["summary"]["spigot"] == 1 and inv["summary"]["manual"] == 0

    again = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_installed"


def test_outdated_mirror_requires_confirmation_before_download(paper, fake_agent, client_for, users, spiget):
    _router, state = spiget
    state["uploaded_ms"] = (1777875780 + 60) * 1000
    c = client_for(users["owner"])
    r = c.post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 409, r.json()
    err = r.json()["error"]
    assert err["code"] == "confirmation_required"
    assert [w["code"] for w in err["details"]["warnings"]] == ["mirror_outdated"]
    assert "1.5.66" in err["message"]
    assert not fake_agent.called("upload")

    r = c.post(url(paper, "install"), {"resource_id": RID, "confirm": ["mirror_outdated"]}, format="json")
    assert r.status_code == 201, r.json()
    assert r.json()["installed"]["mirror"] == "outdated"
    paper.refresh_from_db()
    assert paper.modpack_meta["spigot"]["Chunky-1.5.3.jar"]["spigot_version"] is None  # not claimed as latest


def test_untested_minecraft_version_requires_confirmation(make_instance, fake_agent, client_for, users, spiget):
    old = make_instance(server_type="PAPER", minecraft_version="1.12.2")
    fake_agent.prepare(old.id)
    c = client_for(users["owner"])
    r = c.post(url(old, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 409 and r.json()["error"]["details"]["warnings"][0]["code"] == "untested_minecraft_version"
    r = c.post(url(old, "install"), {"resource_id": RID, "confirm": ["untested_minecraft_version"]}, format="json")
    assert r.status_code == 201, r.json()


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"premium": True, "price": 7}, "premium"),
        ({"premium": False, "price": 4.99}, "premium"),
        ({"external": True, "file": {"type": "external", "externalUrl": "https://example.org/"}}, "external"),
        ({"file": {"type": ".zip", "size": 1, "sizeUnit": "KB"}}, "not_a_jar"),
    ],
)
def test_premium_external_and_archives_are_never_downloaded(paper, fake_agent, client_for, users, spiget, override, reason):
    router, state = spiget
    state["res"] = resource(**override)
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 400, r.json()
    assert r.json()["error"]["code"] == "not_downloadable" and r.json()["error"]["details"]["reason"] == reason
    assert not any(c.request.url.host == "cdn.spiget.org" for c in router.calls)
    assert not fake_agent.called("upload")


def test_redirect_to_another_host_is_refused(paper, fake_agent, client_for, users):
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{API}/resources/{RID}").mock(return_value=Response(200, json=resource()))
        router.get(f"{API}/resources/{RID}/versions").mock(return_value=Response(200, json=VERSIONS))
        router.get(f"{API}/resources/{RID}/download").mock(
            return_value=Response(302, headers={"location": "https://evil.example/file/spiget-resources/81534.jar"})
        )
        evil = router.get(url__regex=r"^https://evil\.example/").mock(return_value=Response(200, content=JAR))
        router.post(f"{MR}/version_files").mock(return_value=Response(200, json={}))
        r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_mirrored"
    assert not evil.called and not fake_agent.called("upload")


def test_cdn_redirects_are_not_followed(paper, fake_agent, client_for, users, spiget):
    router, _ = spiget
    router.routes["cdn"].mock(side_effect=None, return_value=Response(302, headers={"location": "http://169.254.169.254/"}))
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 502 and r.json()["error"]["code"] == "provider_error"
    assert not fake_agent.called("upload")


def test_sha1_mismatch_with_mirror_storage_is_refused(paper, fake_agent, client_for, users, spiget):
    router, state = spiget
    headers = {**cdn_headers(JAR, state["uploaded_ms"]), "x-bz-content-sha1": "0" * 40}
    router.routes["cdn"].mock(side_effect=None, return_value=Response(200, content=JAR, headers=headers))
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 502 and r.json()["error"]["code"] == "hash_mismatch"
    assert not fake_agent.called("upload")
    from apps.audit.models import AuditEvent

    assert AuditEvent.objects.filter(action="content.spigot_install", success=False).exists()


def test_agent_hash_mismatch_is_reported(paper, fake_agent, client_for, users, spiget):
    from apps.nodes.client import AgentError

    fake_agent.failures["upload"] = AgentError("Uploaded file failed SHA-256 verification", "hash_mismatch", 422)
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 422 and r.json()["error"]["code"] == "hash_mismatch"
    paper.refresh_from_db()
    assert "spigot" not in (paper.modpack_meta or {})


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (jar(descriptor=None, bungee__yml=b"name: Proxy\nversion: 1\n"), "incompatible_platform"),
        (jar(descriptor=None, fabric__mod__json=b"{}"), "incompatible_platform"),
        (b"not a zip at all", "invalid_jar"),
    ],
)
def test_non_plugin_files_are_refused(paper, fake_agent, client_for, users, spiget, data, code):
    _, state = spiget
    state["jar"] = data
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 400, r.json()
    assert r.json()["error"]["code"] == code
    if data.startswith(b"PK") and b"bungee" in data:
        assert "proxy plugin" in r.json()["error"]["message"]
    assert not fake_agent.called("upload")


def test_folia_needs_folia_support(make_instance, fake_agent, client_for, users, spiget):
    folia = make_instance(server_type="FOLIA", minecraft_version="1.21.1")
    fake_agent.prepare(folia.id)
    r = client_for(users["owner"]).post(url(folia, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "incompatible_platform"
    _, state = spiget
    state["jar"] = jar("name: Chunky\nversion: 1.5.3\nfolia-supported: true\n")
    r = client_for(users["owner"]).post(url(folia, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 201, r.json()


def test_identical_file_already_present_is_a_duplicate(paper, fake_agent, client_for, users, spiget):
    fake_agent.put_file(paper.id, "plugins/chunky-manual.jar", JAR)
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_installed"
    assert "chunky-manual.jar" in r.json()["error"]["message"]


def test_installed_modrinth_project_is_a_duplicate(paper, fake_agent, client_for, users, spiget):
    """The same plugin from Modrinth (another jar of the same project) is already installed."""
    _, state = spiget
    other = jar("name: Chunky\nversion: 1.5.2\n")
    fake_agent.put_file(paper.id, "plugins/Chunky-Bukkit-1.5.2.jar", other)
    release = {
        "id": "v1",
        "project_id": "fALzjamp",
        "name": "Chunky",
        "version_number": "1.5.2",
        "version_type": "release",
        "loaders": ["paper"],
        "game_versions": ["1.21.1"],
        "files": [],
    }
    state["identified"] = {hashlib.sha1(other).hexdigest(): release, hashlib.sha1(JAR).hexdigest(): {**release, "id": "v2"}}
    state["projects"] = [{"id": "fALzjamp", "slug": "chunky", "title": "Chunky"}]
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 409, r.json()
    assert r.json()["error"]["code"] == "already_installed" and "Modrinth" in r.json()["error"]["message"]


def test_filename_clash_is_refused(paper, fake_agent, client_for, users, spiget):
    fake_agent.put_file(paper.id, "plugins/Chunky-1.5.3.jar.disabled", b"PK older bytes")
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "file_exists"


def test_oversized_mirror_file_is_refused(paper, fake_agent, client_for, users, spiget, monkeypatch):
    from apps.content import spigot_source

    monkeypatch.setattr(spigot_source, "MAX_JAR_BYTES", 100)
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"


# --------------------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------------------
def test_update_replaces_the_recorded_file(paper, fake_agent, client_for, users, spiget):
    _, state = spiget
    c = client_for(users["owner"])
    assert c.post(url(paper, "install"), {"resource_id": RID}, format="json").status_code == 201
    # disable it, then Spiget's mirror picks up a new release
    assert (
        c.post(
            f"/api/instances/{paper.id}/content/toggle", {"filename": "Chunky-1.5.3.jar", "enabled": False}, format="json"
        ).status_code
        == 200
    )
    r = c.post(url(paper, "update"), {"filename": "Chunky-1.5.3.jar.disabled"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "up_to_date"

    new = jar("name: Chunky\nversion: 1.5.66\n")
    state["jar"] = new
    state["uploaded_ms"] = (LATEST_TS + 7200) * 1000
    from django.core.cache import cache

    cache.delete(f"spiget:mirror-head:{RID}")  # the details view caches the mirror's HEAD for two minutes
    details = c.get(url(paper, f"resources/{RID}")).json()
    assert details["installed_files"][0]["update_available"] is True
    r = c.post(url(paper, "update"), {"filename": "Chunky-1.5.3.jar.disabled"}, format="json")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["status"] == "updated" and body["new_filename"] == "Chunky-1.5.66.jar.disabled"
    assert body["from_version"] == "1.5.3" and body["to_version"] == "1.5.66"
    assert fake_agent.get_file(paper.id, "plugins/Chunky-1.5.66.jar.disabled") == new
    assert fake_agent.get_file(paper.id, "plugins/Chunky-1.5.3.jar.disabled") is None
    paper.refresh_from_db()
    assert list(paper.modpack_meta["spigot"]) == ["Chunky-1.5.66.jar"]


def test_update_refuses_files_not_from_spigot(paper, fake_agent, client_for, users, spiget):
    fake_agent.put_file(paper.id, "plugins/Other.jar", JAR)
    r = client_for(users["owner"]).post(url(paper, "update"), {"filename": "Other.jar"}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_from_spigot"


# --------------------------------------------------------------------------------------
# permissions and server types
# --------------------------------------------------------------------------------------
def test_refused_for_mod_servers(make_instance, fake_agent, client_for, users):
    fabric = make_instance(server_type="FABRIC", minecraft_version="1.21.1")
    r = client_for(users["owner"]).get(url(fabric, "search"), {"q": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "content_unsupported"


def test_refused_while_following_latest(make_instance, fake_agent, client_for, users):
    latest = make_instance(server_type="PAPER", minecraft_version="LATEST")
    r = client_for(users["owner"]).get(url(latest, "search"), {"q": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "minecraft_version_unpinned"


def test_install_requires_content_permission(paper, client_for, users):
    r = client_for(users["viewer"]).post(url(paper, "install"), {"resource_id": RID}, format="json")
    assert r.status_code in (403, 404)


@pytest.mark.parametrize("value", ["abc", -1, 0, "1e9", None, 10**12])
def test_invalid_resource_ids(paper, client_for, users, value):
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": value}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_resource_id"


def test_invalid_confirm_codes(paper, client_for, users):
    r = client_for(users["owner"]).post(url(paper, "install"), {"resource_id": RID, "confirm": ["everything"]}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_parameter"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def test_html_to_text_never_returns_markup():
    from apps.modpacks import spiget

    text = spiget.html_to_text("<p>a<b>b</b>\nc</p><style>x{}</style><ul><li>one</li><li>two</li></ul>&lt;i&gt;")
    # entities decode to literal text (rendered as text by React, never as markup)
    assert text == "ab c\n\n\u2022 one\n\n\u2022 two\n\n<i>"
    assert spiget.description_text("not base64 <i>x</i>") == "not base64 x"
    assert spiget.description_text(base64.b64encode("<div>\x00a\u200b</div>".encode()).decode()) == "a"
    assert len(spiget.html_to_text("x" * 10000)) <= spiget.DESCRIPTION_LIMIT + 2


@pytest.mark.parametrize(
    ("mc", "tested", "expected"),
    [
        ("1.21.1", ["1.21"], True),
        ("1.21", ["1.21"], True),
        ("1.21.1", ["1.2"], False),
        ("26.1.2", ["26.1"], True),
        ("1.20.6", ["1.20.6"], True),
        ("1.21", [], None),
    ],
)
def test_tested_for(mc, tested, expected):
    from apps.modpacks import spiget

    assert spiget.tested_for(mc, tested) is expected
