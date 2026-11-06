"""GET /files/hashes (used by the panel's mods/plugins inventory)."""

from __future__ import annotations

import hashlib
import uuid

from mcagent import safefs


def _mods(agent_settings, iid):
    mods = agent_settings.instances_dir / str(iid) / "mods"
    mods.mkdir(parents=True)
    return mods


def test_hashes_lists_regular_files_directly_in_folder(api, auth, agent_settings):
    iid = uuid.uuid4()
    mods = _mods(agent_settings, iid)
    (mods / "a.jar").write_bytes(b"a")
    (mods / "nested").mkdir()
    (mods / "nested" / "b.jar").write_bytes(b"b")
    (mods / "link.jar").symlink_to(mods / "a.jar")
    r = api.get(f"/v1/instances/{iid}/files/hashes", params={"path": "mods"}, headers=auth)
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert [f["name"] for f in files] == ["a.jar"]
    assert files[0]["hash"] == hashlib.sha1(b"a").hexdigest()


def test_hashes_missing_folder_is_empty(api, auth, agent_settings):
    iid = uuid.uuid4()
    (agent_settings.instances_dir / str(iid)).mkdir()
    r = api.get(f"/v1/instances/{iid}/files/hashes", params={"path": "plugins"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["files"] == []


def test_hashes_skip_file_removed_after_listing(api, auth, agent_settings, monkeypatch):
    """A file deleted between listing and hashing must not make the whole folder look empty."""
    iid = uuid.uuid4()
    mods = _mods(agent_settings, iid)
    (mods / "a.jar").write_bytes(b"a")
    (mods / "gone.jar").write_bytes(b"g")
    original = safefs.SafeRoot.list

    def racing_list(self, rel):
        entries = original(self, rel)
        (mods / "gone.jar").unlink()
        return entries

    monkeypatch.setattr(safefs.SafeRoot, "list", racing_list)
    r = api.get(f"/v1/instances/{iid}/files/hashes", params={"path": "mods"}, headers=auth)
    assert r.status_code == 200, r.text
    assert [f["name"] for f in r.json()["files"]] == ["a.jar"]


def test_hash_cache_reuses_and_invalidates(api, auth, agent_settings, monkeypatch):
    from mcagent import hashcache

    monkeypatch.setattr(hashcache, "RACY_WINDOW_NS", 0)  # the file was written just now
    iid = uuid.uuid4()
    mods = _mods(agent_settings, iid)
    jar = mods / "a.jar"
    jar.write_bytes(b"one")
    url = f"/v1/instances/{iid}/files/hashes"
    r1 = api.get(url, params={"path": "mods"}, headers=auth).json()
    assert r1["files"][0]["hash"] == hashlib.sha1(b"one").hexdigest()
    assert r1["complete"] is True
    assert len(list(agent_settings.hash_cache_dir.glob("*.json"))) == 1
    jar.write_bytes(b"two!")  # size + mtime change -> recomputed
    r2 = api.get(url, params={"path": "mods"}, headers=auth).json()
    assert r2["files"][0]["hash"] == hashlib.sha1(b"two!").hexdigest()
