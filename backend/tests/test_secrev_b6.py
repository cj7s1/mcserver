"""The panel's side of the agent's caps and refusals (B-6, B-1/B-11 = I-2 of the security review of 2026-11-11).

The agent caps large answers and says so (``truncated`` on folder listings, search and the backup browser;
``complete: false`` / ``pending`` on hashing; 413 ``archive_too_large`` for a zip of too many files) and refuses
file writes during a lifecycle operation (409 ``instance_busy``) and changes of a mounted folder (403
``managed_file``). The panel passes the flags through, keeps the status and code, and never treats a cut-off
listing as the whole folder.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.backups.models import Backup, BackupStatus
from apps.nodes.client import AgentClient, AgentError

pytestmark = pytest.mark.django_db


def test_listings_search_and_backup_browser_say_when_they_were_cut_off(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    inst = make_instance()
    for n in range(5):
        fake_agent.put_file(inst.id, f"logs/f{n}.log", f"needle {n}\n")
    owner = clients["owner"]
    assert "truncated" not in owner.get(f"/api/instances/{inst.id}/files", {"path": "logs"}).json()
    fake_agent.list_cap, fake_agent.search_cap = 3, 2
    listing = owner.get(f"/api/instances/{inst.id}/files", {"path": "logs"}).json()
    assert listing["truncated"] is True and len(listing["entries"]) == 3
    found = owner.post(f"/api/instances/{inst.id}/files/search", {"query": "needle", "path": ""}, format="json").json()
    assert found["truncated"] is True and len(found["results"]) == 2
    fake_agent.search_cap = None
    assert "truncated" not in owner.post(f"/api/instances/{inst.id}/files/search", {"query": "needle"}, format="json").json()
    # the backup browser
    fake_agent.create_backup(inst.id, "b1", tags=[])
    snap_id = fake_agent.repos[str(inst.id)][-1]["id"]
    backup = Backup.objects.create(instance=inst, name="b", status=BackupStatus.SUCCEEDED, snapshot_id=snap_id)
    browsed = owner.get(f"/api/instances/{inst.id}/backups/{backup.id}/files", {"path": "logs"}).json()
    assert browsed["truncated"] is True and len(browsed["entries"]) == 3


def test_the_agents_refusals_keep_their_status_and_code(
    make_instance: Any, fake_agent: Any, clients: Any, monkeypatch: Any
) -> None:
    inst = make_instance()
    fake_agent.put_file(inst.id, "config/a.txt", "x")
    owner = clients["owner"]
    base = f"/api/instances/{inst.id}/files"

    def busy(*_a: Any, **_k: Any) -> Any:
        raise AgentError("Instance is busy with a 'restore' operation", "instance_busy", 409)

    monkeypatch.setattr(AgentClient, "write_text", busy)
    resp = owner.put(f"{base}/text", {"path": "config/a.txt", "content": "y"}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "instance_busy"
    assert "busy with another operation" in resp.json()["error"]["message"] and "restore" in resp.json()["error"]["message"]

    def mounted(*_a: Any, **_k: Any) -> Any:
        raise AgentError(
            "config is mounted into the server's container; it (or a folder above it) cannot be renamed", "managed_file", 403
        )

    monkeypatch.setattr(AgentClient, "rename", mounted)
    resp = owner.post(f"{base}/rename", {"source": "config", "destination": "config2"}, format="json")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "managed_file"
    assert "mounted" in resp.json()["error"]["message"]

    def too_many(*_a: Any, **_k: Any) -> Any:
        raise AgentError("The folder holds more than 200000 files; it cannot be downloaded as a zip", "archive_too_large", 413)

    monkeypatch.setattr(AgentClient, "download", too_many)
    resp = owner.get(f"{base}/archive", {"path": "config"})
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "archive_too_large"
    assert "200000 files" in resp.json()["error"]["message"]


def test_a_capped_hashing_pass_is_never_taken_for_the_whole_folder(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """The content inventory compares the jars it finds with what it installed: an incomplete pass is answered
    503 hashing_busy to a blocking call (the inventory then lists the jars as pending) and ``complete: False`` to a
    progressive one, never as "the other jars are gone"."""
    inst = make_instance(server_type="PAPER")
    for n in range(3):
        fake_agent.put_file(inst.id, f"plugins/p{n}.jar", f"jar {n}")
    fake_agent.hash_pending = 1
    client = AgentClient(inst.node)
    with pytest.raises(AgentError) as busy:
        client.hash_files(inst.id, "plugins", "sha1")
    assert busy.value.error_code == "hashing_busy" and busy.value.agent_status == 503
    partial = client.hash_files_partial(inst.id, "plugins", "sha1", wait=1)
    assert partial["complete"] is False and partial["pending"] == 1
    inventory = clients["owner"].get(f"/api/instances/{inst.id}/content")
    assert inventory.status_code in (200, 503), inventory.content
    if inventory.status_code == 200:  # every jar is there, as pending or hashed: none is reported missing
        body = str(inventory.json())
        assert all(f"p{n}.jar" in body for n in range(3))


def test_the_real_client_reads_the_agents_flags(monkeypatch: Any, node: Any) -> None:
    """``AgentClient`` itself (not the fake): the agent's ``complete: false`` turns into hashing_busy for a blocking
    call and into ``complete: False`` for a progressive one; search keeps ``truncated``."""
    answers = {
        "/files/hashes": {"files": [{"name": "a.jar", "type": "file", "hash": "a" * 40}], "complete": False, "pending": 4},
        "/files/search": {"results": [{"path": "x", "line": 1, "text": "t"}], "truncated": True},
    }

    def fake_json(self: Any, method: str, path: str, **_kw: Any) -> Any:
        return next(v for k, v in answers.items() if path.endswith(k))

    monkeypatch.setattr(AgentClient, "_json", fake_json)
    client = AgentClient(node)
    with pytest.raises(AgentError) as busy:
        client.hash_files("00000000-0000-0000-0000-000000000001", "mods")
    assert busy.value.error_code == "hashing_busy" and "4 file(s)" in str(busy.value.detail)
    partial = client.hash_files_partial("00000000-0000-0000-0000-000000000001", "mods", wait=1)
    assert partial == {"files": answers["/files/hashes"]["files"], "complete": False, "pending": 4}
    assert client.search_files("00000000-0000-0000-0000-000000000001", "t") == answers["/files/search"]
    busy_error = AgentError("Instance is busy with a 'import' operation", "instance_busy", 409)
    assert busy_error.status_code == 409 and "(import)" in str(busy_error.detail)


def test_emptying_a_migration_target_lists_again_while_the_listing_is_cut_off(
    make_instance: Any, fake_agent: Any, node: Any
) -> None:
    """Migration empties the target folder first; with more leftovers than one listing holds it lists again
    instead of failing (or keeping the rest)."""
    from apps.instances.handlers import _empty_target_folder

    inst = make_instance()
    for n in range(7):
        fake_agent.put_file(inst.id, f"left{n}.txt", "x")
    fake_agent.list_cap = 3

    class Source:  # another node's agent (the fake has one folder: the probe must not be seen there)
        def write_text(self, *a: Any, **k: Any) -> None: ...

        def delete_files(self, *a: Any, **k: Any) -> None: ...

    class Ctx:
        def progress(self, *a: Any, **k: Any) -> None: ...

        def log(self, *a: Any, **k: Any) -> None: ...

    assert _empty_target_folder(inst, Source(), AgentClient(node), Ctx()) == 7  # type: ignore[arg-type]
    assert fake_agent.list_files(inst.id, "")["entries"] == []
