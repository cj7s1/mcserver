# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Review of the multi-game branch (2026-11-10), item 5: the control secrets in the server folder.

Factorio's ``config/rconpw``, Satisfactory's ``.mcpanel/api-token`` and its claim
``saved/ServerSettings.7777.sav``, TShock's REST token inside ``config.json``: users with file
permissions could download them (also from a backup or inside a zip), overwrite, rename or delete
them. Only the text editor hid them before.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from apps.backups.models import Backup, BackupStatus
from apps.nodes.client import AgentClient

from .test_games_phase2 import factorio  # noqa: F401
from .test_games_satisfactory import TOKEN, _claimed_instance
from .test_terraria import tshock  # noqa: F401


def code(resp: Any) -> str:
    return resp.json()["error"]["code"]


def files(client: Any, inst: Any, suffix: str) -> str:
    return f"/api/instances/{inst.id}/files/{suffix}"


@pytest.fixture
def sf(make_instance, fake_agent):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access", "new_game", "publish"))
    fake_agent.put_file(inst.id, ".mcpanel/api-token", TOKEN + "\n")
    fake_agent.put_file(inst.id, "saved/ServerSettings.7777.sav", b"CLAIM")
    fake_agent.put_file(inst.id, "saved/server/s_autosave_0.sav", b"SAVE")
    return inst


def _zip_names(resp: Any) -> list[str]:
    return sorted(zipfile.ZipFile(io.BytesIO(b"".join(resp.streaming_content))).namelist())


@pytest.mark.parametrize("who", ["owner", "operator"])
def test_secret_files_are_never_downloaded(clients, factorio, sf, fake_agent, who):
    fake_agent.put_file(factorio.id, "config/rconpw", "r" * 32 + "\n")
    c = clients[who]
    for inst, path in ((factorio, "config/rconpw"), (sf, ".mcpanel/api-token"), (sf, "saved/ServerSettings.7777.sav")):
        for variant in (path, f"./{path}", f"/{path}", path.replace("/", "//")):
            resp = c.get(files(c, inst, "download"), {"path": variant})
            assert resp.status_code == 403 and code(resp) == "managed_file", (path, variant)
    # the rest of the folder still downloads
    assert c.get(files(c, sf, "download"), {"path": "saved/server/s_autosave_0.sav"}).status_code == 200


def test_zips_leave_the_secret_files_out(clients, factorio, sf, fake_agent):
    fake_agent.put_file(factorio.id, "config/rconpw", "r" * 32 + "\n")
    fake_agent.put_file(factorio.id, "config/server-settings.json", "{}")
    resp = clients["operator"].get(files(None, factorio, "archive"), {"path": ""})
    assert resp.status_code == 200
    names = _zip_names(resp)
    assert "server/config/server-settings.json" in names and "server/config/rconpw" not in names
    resp = clients["operator"].get(files(None, sf, "archive"), {"path": "saved"})
    names = _zip_names(resp)
    assert names == ["saved/server/s_autosave_0.sav"]
    resp = clients["owner"].get(files(None, sf, "archive"), {"path": ""})
    assert not [n for n in _zip_names(resp) if "api-token" in n or "ServerSettings" in n]


def test_zip_with_secrets_needs_an_agent_that_leaves_them_out(clients, factorio, fake_agent):
    fake_agent.put_file(factorio.id, "config/rconpw", "r" * 32 + "\n")
    fake_agent.put_file(factorio.id, "saves/world.zip", b"x")
    fake_agent.features = [f for f in fake_agent.features if f != "archive_excludes"]
    resp = clients["owner"].get(files(None, factorio, "archive"), {"path": ""})
    assert resp.status_code == 409 and code(resp) == "agent_outdated"
    # a folder without a secret inside is fine on that agent
    assert clients["owner"].get(files(None, factorio, "archive"), {"path": "saves"}).status_code == 200


def test_secret_files_are_never_written_renamed_or_deleted(clients, factorio, sf, fake_agent):
    fake_agent.put_file(factorio.id, "config/rconpw", "r" * 32 + "\n")
    c = clients["owner"]
    for inst, path in ((factorio, "config/rconpw"), (sf, ".mcpanel/api-token"), (sf, "saved/ServerSettings.7777.sav")):
        folder, name = path.rsplit("/", 1)
        f = io.BytesIO(b"mine")
        f.name = name
        resp = c.post(files(c, inst, "upload") + f"?path={folder}&overwrite=true", {"file": f}, format="multipart")
        assert resp.status_code == 403 and code(resp) == "managed_file", path
        resp = c.put(files(c, inst, "text"), {"path": path, "content": "x"}, format="json")
        assert resp.status_code == 403, path
        # the file, or the folder holding it, moved elsewhere would be downloadable there
        for source in (path, folder):
            resp = c.post(files(c, inst, "rename"), {"source": source, "destination": "elsewhere"}, format="json")
            assert resp.status_code == 403 and code(resp) == "managed_file", source
            resp = c.post(files(c, inst, "delete"), {"paths": [source], "recursive": True}, format="json")
            assert resp.status_code == 403 and code(resp) == "managed_file", source
        # nothing can be moved onto it either
        fake_agent.put_file(inst.id, "other.txt", b"x")
        resp = c.post(files(c, inst, "rename"), {"source": "other.txt", "destination": path, "overwrite": True}, format="json")
        assert resp.status_code == 403
    assert fake_agent.get_file(factorio.id, "config/rconpw") == ("r" * 32 + "\n").encode()
    assert fake_agent.get_file(sf.id, "saved/ServerSettings.7777.sav") == b"CLAIM"


def test_tshock_token_is_masked_in_downloads_search_and_zips(clients, tshock, fake_agent):
    secret = tshock.rcon_password
    text = f'{{"Settings": {{"ApplicationRestTokens": {{"{secret}": {{"Username": "mcpanel"}}}}}}}}'
    fake_agent.put_file(tshock.id, "config.json", text)
    fake_agent.put_file(tshock.id, "copy.json", text)  # a copy under another name
    for who in ("owner", "operator"):
        resp = clients[who].get(files(None, tshock, "download"), {"path": "config.json"})
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content) if hasattr(resp, "streaming_content") else resp.content
        assert secret.encode() not in body and b"********" in body
        resp = clients[who].post(files(None, tshock, "search"), {"query": "ApplicationRestTokens"}, format="json")
        assert resp.status_code == 200 and resp.json()["results"]
        assert secret not in str(resp.json())
        resp = clients[who].get(files(None, tshock, "text"), {"path": "copy.json"})
        assert secret not in resp.json()["content"]
    # zips: without instance.configure config.json is left out; with it, the owner gets the whole folder
    names = _zip_names(clients["operator"].get(files(None, tshock, "archive"), {"path": ""}))
    assert "server/config.json" not in names
    names = _zip_names(clients["owner"].get(files(None, tshock, "archive"), {"path": ""}))
    assert "server/config.json" in names


def test_backup_browser_never_serves_secret_files(clients, factorio, tshock, fake_agent, monkeypatch):
    served: list[str] = []

    def download(self: Any, instance_id: Any, snapshot_id: str, path: str) -> Any:
        served.append(path)
        if path == "config.json":
            return iter([(f'{{"t": "{tshock.rcon_password}"}}').encode()]), None
        return iter([b"data"]), None

    monkeypatch.setattr(AgentClient, "backup_download", download)
    for inst in (factorio, tshock):
        Backup.objects.create(instance=inst, name="b", status=BackupStatus.SUCCEEDED, snapshot_id="a" * 64)
    b = Backup.objects.get(instance=factorio)
    resp = clients["operator"].get(f"/api/instances/{factorio.id}/backups/{b.id}/download", {"path": "config/rconpw"})
    assert resp.status_code == 403 and code(resp) == "managed_file" and served == []
    resp = clients["operator"].get(f"/api/instances/{factorio.id}/backups/{b.id}/download", {"path": "saves/world.zip"})
    assert resp.status_code == 200
    b = Backup.objects.get(instance=tshock)
    resp = clients["operator"].get(f"/api/instances/{tshock.id}/backups/{b.id}/download", {"path": "config.json"})
    assert resp.status_code == 200 and tshock.rcon_password.encode() not in resp.content and b"********" in resp.content
