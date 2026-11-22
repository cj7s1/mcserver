"""A pack release tagged with several Minecraft versions (2026-11-22, first seen on a real server).

CurseForge tags Better MC [BMC5] v54, a 1.21.1 pack, as ``["1.21", "1.21.1", "NeoForge"]``. The panel
took the first tag, recorded 1.21 and refused Distant Horizons for 1.21.1 on that server
("supports Minecraft 1.21.1, not 1.21"). Now the newest tag is taken, and the install manifest
(``minecraftVersion``) corrects a server whose recorded version is still wrong.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from django.core.cache import cache

CF = "https://api.curseforge.com"

BMC5_FILE = {
    "id": 9359712,
    "modId": 462042,
    "displayName": "Better MC [BMC5] v54",
    "fileName": "Better-MC-BMC5-v54.zip",
    "gameVersions": ["1.21", "1.21.1", "NeoForge"],
    "fileDate": "2026-11-01T00:00:00Z",
}
BMC5_PROJECT = {
    "id": 462042,
    "name": "Better MC [NEOFORGE] - BMC5",
    "slug": "better-mc-neoforge-bmc5",
    "summary": "",
    "classId": 4471,
    "allowModDistribution": True,
}
MANIFEST = {
    "fileId": 9359712,
    "modId": 462042,
    "modpackVersion": "v54",
    "minecraftVersion": "1.21.1",
    "modLoaderId": "neoforge-21.1.234",
}


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["1.21", "1.21.1"], "1.21.1"),
        (["1.21.1", "1.21"], "1.21.1"),
        (["1.20.1"], "1.20.1"),
        (["1.20.10", "1.20.9"], "1.20.10"),
        (["26.1", "1.21.11"], "26.1"),
    ],
)
def test_pack_minecraft_version_is_the_newest_tag(tags, expected):
    from apps.modpacks.providers import pack_minecraft_version

    assert pack_minecraft_version(tags) == expected


@respx.mock
def test_curseforge_pack_tagged_121_and_1211_resolves_to_1211(db):
    from apps.core.models import SystemSetting
    from apps.modpacks.providers import get_provider

    SystemSetting.set_secret("curseforge_api_key", "k")
    cache.clear()
    respx.get(f"{CF}/v1/mods/462042").mock(return_value=httpx.Response(200, json={"data": BMC5_PROJECT}))
    respx.get(f"{CF}/v1/mods/462042/files/9359712").mock(return_value=httpx.Response(200, json={"data": BMC5_FILE}))
    resolved = get_provider("curseforge").resolve_modpack("462042", "9359712")
    assert resolved["minecraft_version"] == "1.21.1"
    assert resolved["loader"] == "neoforge"


def _bmc5(make_instance, **kw):
    return make_instance(
        server_type="AUTO_CURSEFORGE",
        modpack_provider="curseforge",
        modpack_project_id="462042",
        modpack_version_id="9359712",
        modpack_loader="neoforge",
        minecraft_version="1.21",
        **kw,
    )


def test_manifest_corrects_the_recorded_version_once_per_file(make_instance, fake_agent):
    from apps.modpacks.pinning import sync_minecraft_version

    cache.clear()
    inst = _bmc5(make_instance)
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps(MANIFEST))
    assert sync_minecraft_version(inst) is True
    inst.refresh_from_db()
    assert inst.minecraft_version == "1.21.1"
    assert inst.modpack_meta["minecraft_from_manifest"] == "9359712"
    cache.clear()
    assert sync_minecraft_version(inst) is False  # checked for this file: not read again


def test_manifest_of_another_release_is_not_trusted(make_instance, fake_agent):
    from apps.modpacks.pinning import sync_minecraft_version

    cache.clear()
    inst = _bmc5(make_instance)
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps({**MANIFEST, "fileId": 1}))
    assert sync_minecraft_version(inst) is False
    inst.refresh_from_db()
    assert inst.minecraft_version == "1.21"
    assert "minecraft_from_manifest" not in (inst.modpack_meta or {})  # tried again after the update installs


def test_missing_manifest_changes_nothing_and_is_rate_limited(make_instance, fake_agent):
    from apps.modpacks.pinning import sync_minecraft_version

    cache.clear()
    inst = _bmc5(make_instance)
    assert sync_minecraft_version(inst) is False
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps(MANIFEST))
    assert sync_minecraft_version(inst) is False  # within RETRY_SECONDS of the last read
    cache.clear()
    assert sync_minecraft_version(inst) is True


def test_other_server_types_are_left_alone(make_instance, fake_agent):
    from apps.modpacks.pinning import sync_minecraft_version

    cache.clear()
    inst = make_instance(server_type="MODRINTH", modpack_provider="modrinth", modpack_version_id="abc")
    assert sync_minecraft_version(inst) is False


def test_corrected_version_lets_the_1211_mod_through(make_instance, fake_agent):
    from apps.content.service import context_for, incompatibility
    from apps.modpacks.pinning import sync_minecraft_version

    cache.clear()
    inst = _bmc5(make_instance)
    release = {
        "version_number": "3.3.3-1.21.1",
        "loaders": ["fabric", "neoforge"],
        "game_versions": ["1.21.1"],
        "file": {
            "filename": "DistantHorizons-3.3.3-1.21.1-fabric-neoforge.jar",
            "url": "https://cdn.modrinth.com/data/uCdwusMi/versions/x/DistantHorizons-3.3.3-1.21.1-fabric-neoforge.jar",
            "sha1": "0" * 40,
        },
    }
    before = incompatibility(context_for(inst), release)
    assert before is not None and before[0] == "incompatible_minecraft_version"
    fake_agent.put_file(inst.id, ".curseforge-manifest.json", json.dumps(MANIFEST))
    assert sync_minecraft_version(inst) is True
    inst.refresh_from_db()
    assert incompatibility(context_for(inst), release) is None
