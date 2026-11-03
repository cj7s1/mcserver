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


def test_hash_cache_reuses_and_invalidates(tmp_path, monkeypatch):
    import hashlib
    import os
    import uuid

    from fastapi.testclient import TestClient

    from mcagent import main
    from mcagent.config import Settings, get_settings

    settings = Settings(data_root=tmp_path, token="t" * 32)
    iid = uuid.uuid4()
    mods = settings.instances_dir / str(iid) / "mods"
    mods.mkdir(parents=True)
    jar = mods / "a.jar"
    jar.write_bytes(b"one")
    main.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(main, "_blocked_networks", [])
    main._hash_cache.clear()
    try:
        c = TestClient(main.app)
        h = {"Authorization": "Bearer " + "t" * 32}
        r1 = c.get(f"/v1/instances/{iid}/files/hashes", params={"path": "mods"}, headers=h).json()
        assert r1["files"][0]["hash"] == hashlib.sha1(b"one").hexdigest()
        assert len(main._hash_cache) == 1
        jar.write_bytes(b"two!")  # size + mtime change -> recomputed
        st = jar.stat()
        os.utime(jar, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
        r2 = c.get(f"/v1/instances/{iid}/files/hashes", params={"path": "mods"}, headers=h).json()
        assert r2["files"][0]["hash"] == hashlib.sha1(b"two!").hexdigest()
    finally:
        main.app.dependency_overrides.clear()
