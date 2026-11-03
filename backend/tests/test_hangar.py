import hashlib

import pytest
import respx
from httpx import Response

JAR = b"PK\x03\x04 chunky"
URL = "https://hangarcdn.papermc.io/plugins/pop4959/Chunky/versions/1.4.40/PAPER/Chunky-Bukkit-1.4.40.jar"
VERSIONS = {
    "pagination": {"count": 2},
    "result": [
        {
            "name": "1.4.40",
            "createdAt": "2026-01-01T00:00:00Z",
            "visibility": "public",
            "channel": {"name": "Release"},
            "platformDependencies": {"PAPER": ["1.21", "1.21.1"]},
            "downloads": {
                "PAPER": {
                    "fileInfo": {
                        "name": "Chunky-Bukkit-1.4.40.jar",
                        "sizeBytes": len(JAR),
                        "sha256Hash": hashlib.sha256(JAR).hexdigest(),
                    },
                    "downloadUrl": URL,
                    "externalUrl": None,
                }
            },
            "pluginDependencies": {"PAPER": [{"name": "Vault", "required": True, "externalUrl": None}]},
        },
        {
            "name": "1.4.39-ext",
            "createdAt": "2025-12-01T00:00:00Z",
            "visibility": "public",
            "channel": {"name": "Release"},
            "platformDependencies": {"PAPER": ["1.21.1"]},
            "downloads": {"PAPER": {"fileInfo": None, "downloadUrl": None, "externalUrl": "https://example.org/x"}},
        },
    ],
}


@pytest.fixture
def paper(make_instance):
    return make_instance(server_type="PAPER", minecraft_version="1.21.1")


@respx.mock
def test_hangar_install_records_and_labels(paper, fake_agent, client_for, users):
    respx.get("https://hangar.papermc.io/api/v1/projects/Chunky/versions").mock(return_value=Response(200, json=VERSIONS))
    fake_agent.remote_files[URL] = JAR
    fake_agent.prepare(paper.id)
    c = client_for(users["owner"])
    vs = c.get(f"/api/instances/{paper.id}/content/hangar/projects/Chunky/versions").json()["versions"]
    assert [v["installable"] for v in vs] == [True, False]
    r = c.post(f"/api/instances/{paper.id}/content/hangar/install", {"slug": "Chunky"}, format="json")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["installed"]["filename"] == "Chunky-Bukkit-1.4.40.jar"
    assert body["required_dependencies"][0]["name"] == "Vault"
    r2 = c.post(f"/api/instances/{paper.id}/content/hangar/install", {"slug": "Chunky", "version": "1.4.39-ext"}, format="json")
    assert r2.status_code == 400 and r2.json()["error"]["code"] == "not_downloadable"
    paper.refresh_from_db()
    assert paper.modpack_meta["hangar"]["Chunky-Bukkit-1.4.40.jar"]["version"] == "1.4.40"


def test_hangar_refused_for_non_plugin_servers(make_instance, fake_agent, client_for, users):
    fabric = make_instance(server_type="FABRIC", minecraft_version="1.21.1")
    c = client_for(users["owner"])
    r = c.get(f"/api/instances/{fabric.id}/content/hangar/search", {"q": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "content_unsupported"


def test_hangar_install_requires_content_permission(paper, fake_agent, client_for, users):
    r = client_for(users["viewer"]).post(f"/api/instances/{paper.id}/content/hangar/install", {"slug": "Chunky"}, format="json")
    assert r.status_code in (403, 404)


def test_invalid_slug_rejected(paper, fake_agent, client_for, users):
    r = client_for(users["owner"]).get(f"/api/instances/{paper.id}/content/hangar/projects/..%2Fx/versions")
    assert r.status_code in (400, 404)
