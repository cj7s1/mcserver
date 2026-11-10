"""Regression tests for the content-manager findings of the 2026-11-09 audit (backend-content#1, #4,
#6, #7): pack files kept from being re-installed, bulk updates above 100 files, plugin.yml
parsing that cannot freeze the panel, Hangar updates and records."""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from typing import Any

import pytest
import respx
from httpx import Response

from apps.content import packfiles, service
from apps.instances.spec import build_env

from . import test_content as _content
from .test_content import ContentAgent, FakeModrinth, url

# the content tests' fixtures (fake Modrinth, fake agent, servers, an owner's API client)
mr = _content.mr
agent = _content.agent
api = _content.api
owner = _content.owner
ct_node = _content.ct_node
make_server = _content.make_server
_ct_clear_cache = _content._ct_clear_cache

MANIFEST = {
    "fileId": 5001,
    "modpackName": "Better MC",
    "files": ["mods/packmod-1.0.jar", "mods/other.jar", "config/packmod.toml"],
}


def cf_pack(make_server: Any, agent: ContentAgent, **fields: Any) -> Any:
    inst = make_server(
        "AUTO_CURSEFORGE", minecraft_version="1.20.1", modpack_loader="fabric", modpack_version_id="5001", **fields
    )
    agent.texts[(str(inst.id), ".curseforge-manifest.json")] = json.dumps(MANIFEST)
    agent.put(inst, "mods/packmod-1.0.jar", hashlib.sha1(b"p").hexdigest())
    agent.put(inst, "mods/mine.jar", hashlib.sha1(b"m").hexdigest())
    return inst


def refreshed(inst: Any) -> Any:
    inst.refresh_from_db()
    return inst


# --------------------------------------------------------------------------------------
# backend-content#1: pack files the user disabled, deleted or replaced stay that way
# --------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_disabling_a_pack_mod_keeps_the_runtime_from_reinstalling_it(api, make_server, agent, mr):
    inst = cf_pack(make_server, agent)
    inventory = api.get(url(inst)).json()
    assert inventory["pack_manifest_found"] is True
    managed = {f["filename"]: f["pack_managed"] for f in inventory["files"]}
    assert managed == {"mine.jar": False, "packmod-1.0.jar": True}
    revision = refreshed(inst).config_revision

    off = api.post(url(inst, "toggle"), {"filename": "packmod-1.0.jar", "enabled": False}, format="json").json()
    assert off["pack_managed"] is True
    inst = refreshed(inst)
    assert build_env(inst)["CF_IGNORE_MISSING_FILES"] == "mods/packmod-1.0.jar"
    assert inst.config_revision == revision + 1
    # the disabled jar is still the pack's jar in the inventory
    assert {f["filename"]: f["pack_managed"] for f in api.get(url(inst)).json()["files"]}["packmod-1.0.jar.disabled"] is True

    on = api.post(url(inst, "toggle"), {"filename": "packmod-1.0.jar.disabled", "enabled": True}, format="json").json()
    assert on["pack_managed"] is True
    assert "CF_IGNORE_MISSING_FILES" not in build_env(refreshed(inst))


@pytest.mark.django_db
def test_deleting_only_records_files_of_the_pack(api, make_server, agent, mr):
    inst = make_server("MODRINTH", minecraft_version="1.20.1", modpack_loader="fabric", modpack_version_id="vXYZ")
    agent.texts[(str(inst.id), ".modrinth-modpack-manifest.json")] = json.dumps(
        {"versionId": "vXYZ", "files": ["mods/packmod-1.0.jar"]}
    )
    agent.put(inst, "mods/packmod-1.0.jar", hashlib.sha1(b"p").hexdigest())
    agent.put(inst, "mods/mine.jar", hashlib.sha1(b"m").hexdigest())
    assert api.delete(url(inst, "files?filename=mine.jar")).json()["pack_managed"] is False
    assert packfiles.recorded(refreshed(inst)) == {}
    assert api.delete(url(inst, "files?filename=packmod-1.0.jar")).json()["pack_managed"] is True
    assert build_env(refreshed(inst))["MODRINTH_IGNORE_MISSING_FILES"] == "mods/packmod-1.0.jar"


@pytest.mark.django_db
def test_updating_a_pack_mod_records_the_replaced_file(api, make_server, agent, mr):
    mr.project("Pack0001", "packmod", "PackMod", ["fabric"])
    mr.version("PackV001", "Pack0001", "1.0", loaders=["fabric"], game_versions=["1.20.1"], published="2024-01-01T00:00:00Z")
    mr.version("PackV002", "Pack0001", "2.0", loaders=["fabric"], game_versions=["1.20.1"], published="2024-02-01T00:00:00Z")
    inst = cf_pack(make_server, agent)
    agent.put(inst, "mods/packmod-1.0.jar", mr.sha1("PackV001"))
    resp = api.post(url(inst, "update"), {"files": ["packmod-1.0.jar"]}, format="json").json()
    assert resp["results"][0]["new_filename"] == "packmod-2.0.jar"
    assert build_env(refreshed(inst))["CF_IGNORE_MISSING_FILES"] == "mods/packmod-1.0.jar"


@pytest.mark.django_db
def test_records_are_merged_with_the_users_value_and_end_with_the_pack_release(api, make_server, agent, mr):
    inst = cf_pack(make_server, agent, env={"CF_IGNORE_MISSING_FILES": "mods/gregtech.jar, mods/other.jar"})
    api.post(url(inst, "toggle"), {"filename": "packmod-1.0.jar", "enabled": False}, format="json")
    inst = refreshed(inst)
    assert build_env(inst)["CF_IGNORE_MISSING_FILES"] == "mods/gregtech.jar\nmods/other.jar\nmods/packmod-1.0.jar"
    # after a modpack update to another release the record no longer applies
    inst.modpack_version_id = "6002"
    assert build_env(inst)["CF_IGNORE_MISSING_FILES"] == "mods/gregtech.jar, mods/other.jar"
    # servers without records keep their configuration (and spec hash) unchanged
    plain = make_server("FABRIC", minecraft_version="1.20.1")
    assert not any(k.endswith("IGNORE_MISSING_FILES") for k in build_env(plain))


def test_file_manager_records_only_one_jar_removed_directly_in_mods(clients, make_instance, fake_agent):
    """(review 2026-11-10 #1) a folder delete or a config file never keeps the pack from putting its
    files back; one jar deleted or renamed directly in mods/ does, and renaming it back undoes it."""
    inst = make_instance(server_type="AUTO_CURSEFORGE", modpack_loader="fabric", modpack_version_id="5001")
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps(MANIFEST))
    fake_agent.put_file(inst.id, "config/packmod.toml", "a = 1")
    fake_agent.put_file(inst.id, "config/mine.toml", "b = 2")
    fake_agent.put_file(inst.id, "mods/other.jar", b"jar")
    c = clients["owner"]
    assert (
        c.post(f"/api/instances/{inst.id}/files/delete", {"paths": ["config"], "recursive": True}, format="json").status_code
        == 200
    )
    inst.refresh_from_db()
    assert packfiles.recorded(inst) == {}
    moved = c.post(
        f"/api/instances/{inst.id}/files/rename", {"source": "mods/other.jar", "destination": "mods/other.jar.off"}, format="json"
    )
    assert moved.status_code == 200
    inst.refresh_from_db()
    assert packfiles.recorded(inst)["paths"] == ["mods/other.jar"]
    back = c.post(
        f"/api/instances/{inst.id}/files/rename", {"source": "mods/other.jar.off", "destination": "mods/other.jar"}, format="json"
    )
    assert back.status_code == 200
    inst.refresh_from_db()
    assert packfiles.recorded(inst)["paths"] == []


# --------------------------------------------------------------------------------------
# backend-content#4: "Update all" above 100 files, in batches, one result per file
# --------------------------------------------------------------------------------------
def _seed(mr: FakeModrinth, agent: ContentAgent, inst: Any, count: int) -> list[str]:
    names = []
    for i in range(count):
        pid, slug = f"Proj{i:04d}", f"mod{i:04d}"
        mr.project(pid, slug, f"Mod {i}", ["fabric"])
        mr.version(f"V1{i:04d}xx", pid, "1.0", loaders=["fabric"], game_versions=["1.20.4"], published="2024-01-01T00:00:00Z")
        mr.version(f"V2{i:04d}xx", pid, "2.0", loaders=["fabric"], game_versions=["1.20.4"], published="2024-02-01T00:00:00Z")
        agent.put(inst, f"mods/{slug}-1.0.jar", mr.sha1(f"V1{i:04d}xx"))
        names.append(f"{slug}-1.0.jar")
    return names


@pytest.mark.django_db
def test_bulk_update_of_more_than_100_files(api, make_server, agent, mr):
    inst = make_server("FABRIC")
    names = _seed(mr, agent, inst, 120)
    resp = api.post(url(inst, "update"), {"files": names}, format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["requested"], body["updated"], body["failed"], body["skipped"]) == (120, 120, 0, 0)
    assert body["batches"] == 3 and body["stopped"] is None
    assert len(body["results"]) == 120 and all(r["status"] == "updated" for r in body["results"])
    assert agent.names(inst, "mods")[0] == "mod0000-2.0.jar"


@pytest.mark.django_db
def test_bulk_update_stops_between_batches_when_another_job_starts(api, make_server, agent, mr, monkeypatch):
    from apps.instances.models import Instance
    from apps.jobs.models import Job

    inst = make_server("FABRIC")
    names = _seed(mr, agent, inst, 5)
    monkeypatch.setattr(service, "UPDATE_BATCH", 2)
    update_one = service._update_one

    def then_a_restart_starts(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = update_one(*args, **kwargs)
        if not Job.objects.exists():
            job = Job.objects.create(kind="restart", title="Restart server", instance=inst, status="running")
            Instance.objects.filter(pk=inst.pk).update(active_job=job)
        return result

    monkeypatch.setattr(service, "_update_one", then_a_restart_starts)
    body = api.post(url(inst, "update"), {"files": names}, format="json").json()
    assert [r["status"] for r in body["results"]] == ["updated", "updated", "skipped", "skipped", "skipped"]
    assert {r["reason"] for r in body["results"][2:]} == {"instance_busy"}
    assert body["stopped"] == "Another operation is in progress: Restart server" and body["batches"] == 1


@pytest.mark.django_db
def test_bulk_update_still_has_a_bound(api, make_server, agent, mr):
    inst = make_server("FABRIC")
    resp = api.post(url(inst, "update"), {"files": [f"m{i}.jar" for i in range(service.MAX_FILES_PER_UPDATE + 1)]}, format="json")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_files"


# --------------------------------------------------------------------------------------
# backend-content#6: plugin.yml is parsed in linear time
# --------------------------------------------------------------------------------------
def _jar(entries: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


def test_crafted_plugin_yml_cannot_freeze_the_panel():
    from apps.content import spigot_source

    # within the 256 KiB that are read; the old pattern took ~20 s on this (two minutes at 255 KiB)
    crafted = b"name:x" + b" " * 100_000 + b"y\nversion:" + b"\t" * 100_000 + b"z\n"
    started = time.monotonic()
    meta = spigot_source._descriptor(_jar({"plugin.yml": crafted}))
    assert time.monotonic() - started < 1.0  # before: about two minutes with the GIL held
    assert meta["name"] is None and meta["version"] is None  # such lines are not read at all
    normal = spigot_source._descriptor(
        _jar({"plugin.yml": b"\xef\xbb\xbfname: 'Essentials X'\nmain: a.B\n  name: nested\nversion: 2.21.0 # build\n"})
    )
    assert (normal["name"], normal["version"]) == ("Essentials X", "2.21.0")


# --------------------------------------------------------------------------------------
# backend-content#7: Hangar updates replace the old jar; records are cleaned; bodies checked
# --------------------------------------------------------------------------------------
def _hangar_version(number: str, jar: bytes) -> dict[str, Any]:
    name = f"Chunky-Bukkit-{number}.jar"
    return {
        "name": number,
        "createdAt": f"2026-0{1 + int(number[-1]) % 8}-01T00:00:00Z",
        "visibility": "public",
        "channel": {"name": "Release"},
        "platformDependencies": {"PAPER": ["1.21.1"]},
        "downloads": {
            "PAPER": {
                "fileInfo": {"name": name, "sizeBytes": len(jar), "sha256Hash": hashlib.sha256(jar).hexdigest()},
                "downloadUrl": f"https://hangarcdn.papermc.io/plugins/pop4959/Chunky/versions/{number}/PAPER/{name}",
                "externalUrl": None,
            }
        },
        "pluginDependencies": {},
    }


@respx.mock
def test_hangar_install_of_a_newer_version_replaces_the_old_jar(make_instance, fake_agent, clients):
    paper = make_instance(server_type="PAPER", minecraft_version="1.21.1")
    fake_agent.prepare(paper.id)
    old, new = b"PK\x03\x04 old", b"PK\x03\x04 new"
    route = respx.get("https://hangar.papermc.io/api/v1/projects/Chunky/versions")
    route.mock(return_value=Response(200, json={"pagination": {"count": 1}, "result": [_hangar_version("1.4.40", old)]}))
    fake_agent.remote_files[_hangar_version("1.4.40", old)["downloads"]["PAPER"]["downloadUrl"]] = old
    c = clients["owner"]
    base = f"/api/instances/{paper.id}/content/hangar/install"
    assert c.post(base, {"slug": "Chunky"}, format="json").status_code == 200
    again = c.post(base, {"slug": "Chunky"}, format="json")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_installed"

    from django.core.cache import cache

    cache.clear()  # Hangar's version list is cached
    route.mock(return_value=Response(200, json={"pagination": {"count": 1}, "result": [_hangar_version("1.4.41", new)]}))
    fake_agent.remote_files[_hangar_version("1.4.41", new)["downloads"]["PAPER"]["downloadUrl"]] = new
    resp = c.post(base, {"slug": "Chunky"}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["replaced"] == {"filename": "Chunky-Bukkit-1.4.40.jar", "version": "1.4.40"}
    assert sorted(e["name"] for e in fake_agent.list_files(paper.id, "plugins")["entries"]) == ["Chunky-Bukkit-1.4.41.jar"]
    paper.refresh_from_db()
    assert list(paper.modpack_meta["hangar"]) == ["Chunky-Bukkit-1.4.41.jar"]


def test_hangar_records_of_removed_jars_are_dropped(make_instance, fake_agent, clients, monkeypatch):
    paper = make_instance(
        server_type="PAPER",
        minecraft_version="1.21.1",
        modpack_meta={
            "hangar": {"Gone-1.0.jar": {"slug": "Gone", "version": "1.0"}, "Kept-1.0.jar": {"slug": "Kept", "version": "1.0"}}
        },
    )
    fake_agent.put_file(paper.id, "plugins/Kept-1.0.jar.disabled", b"jar")
    monkeypatch.setattr(service, "identify", lambda hashes: {})
    resp = clients["owner"].get(f"/api/instances/{paper.id}/content/")
    assert resp.status_code == 200, resp.content
    paper.refresh_from_db()
    assert list(paper.modpack_meta["hangar"]) == ["Kept-1.0.jar"]
    # deleting the jar in the content manager drops its record at once
    assert clients["owner"].delete(f"/api/instances/{paper.id}/content/files?filename=Kept-1.0.jar.disabled").status_code == 200
    paper.refresh_from_db()
    assert paper.modpack_meta["hangar"] == {}


@pytest.mark.parametrize("body", [[], ["Chunky"], "Chunky"])
def test_hangar_install_refuses_a_body_that_is_not_an_object(make_instance, fake_agent, clients, body):
    paper = make_instance(server_type="PAPER", minecraft_version="1.21.1")
    resp = clients["owner"].post(f"/api/instances/{paper.id}/content/hangar/install", body, format="json")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_body"
