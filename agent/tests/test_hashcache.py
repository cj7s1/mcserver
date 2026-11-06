"""Persistent digest cache behind GET /files/hashes (agent/mcagent/hashcache.py)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

from mcagent import hashcache
from mcagent.hashcache import HashCache, HashJobs, hash_directory, hash_folder
from mcagent.safefs import SafeRoot


def _write(path: Path, data: bytes, *, age: float = 60.0) -> Path:
    """Write a file whose mtime is ``age`` seconds in the past."""
    path.write_bytes(data)
    when = time.time_ns() - int(age * 1e9)
    os.utime(path, ns=(when, when))
    return path


@pytest.fixture(autouse=True)
def _no_racy_window(monkeypatch):
    """Files written by a test changed just now (ctime); let them be cached unless a test says otherwise."""
    monkeypatch.setattr(hashcache, "RACY_WINDOW_NS", 0)


@pytest.fixture
def setup(tmp_path):
    iid = uuid.uuid4()
    inst = tmp_path / "instances" / str(iid)
    mods = inst / "mods"
    mods.mkdir(parents=True)
    root = SafeRoot(inst)
    cache = HashCache(tmp_path / "state" / "cache" / "file-hashes")
    return iid, root, mods, cache


@pytest.fixture
def reads(monkeypatch):
    """Record which files are actually read for hashing."""
    seen: list[str] = []
    original = hashcache._hash_file

    def counting(root, path, algorithm, stop):
        seen.append(path)
        return original(root, path, algorithm, stop)

    monkeypatch.setattr(hashcache, "_hash_file", counting)
    return seen


def _digests(files):
    return {f["name"]: f["hash"] for f in files}


def test_second_pass_reads_nothing_and_survives_restart(setup, reads):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"alpha")
    _write(mods / "b.jar", b"beta")
    first = hash_folder(root, cache, iid, "mods", "sha1")
    assert _digests(first) == {
        "a.jar": hashlib.sha1(b"alpha").hexdigest(),
        "b.jar": hashlib.sha1(b"beta").hexdigest(),
    }
    assert sorted(reads) == ["mods/a.jar", "mods/b.jar"]
    reads.clear()
    # a new cache object on the same directory = an agent restart
    again = hash_folder(root, HashCache(cache.directory), iid, "/mods/", "sha1")
    assert again == first
    assert reads == []
    assert {f["name"] for f in again} == {"a.jar", "b.jar"}
    assert all(set(f) >= {"name", "path", "type", "size", "modified", "mode", "hash"} for f in again)


def test_cache_lives_outside_the_instance_and_is_private(setup):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"alpha")
    hash_folder(root, cache, iid, "mods", "sha1")
    files = list(cache.directory.iterdir())
    assert len(files) == 1 and files[0].name.startswith(f"{iid}-") and files[0].suffix == ".json"
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"
    assert not any(p.name.endswith(".json") for p in root.root.rglob("*"))
    assert not list(cache.directory.glob(".*.tmp"))


@pytest.mark.parametrize("change", ["size", "mtime", "inode", "mtime_restored"])
def test_changed_file_is_rehashed(setup, reads, change):
    iid, root, mods, cache = setup
    jar = _write(mods / "a.jar", b"one!")
    st = jar.stat()
    hash_folder(root, cache, iid, "mods", "sha1")
    reads.clear()
    if change == "size":
        _write(jar, b"longer", age=0)
        os.utime(jar, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime, different size
        expected = b"longer"
    elif change == "mtime":
        jar.write_bytes(b"two!")  # same size
        os.utime(jar, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
        expected = b"two!"
    elif change == "mtime_restored":  # rewritten in place, then size and mtime put back: only ctime differs
        time.sleep(0.01)
        jar.write_bytes(b"two!")
        os.utime(jar, ns=(st.st_atime_ns, st.st_mtime_ns))
        after = jar.stat()
        assert (after.st_size, after.st_mtime_ns, after.st_ino) == (st.st_size, st.st_mtime_ns, st.st_ino)
        expected = b"two!"
    else:  # replaced by another file with the same size and mtime (e.g. an atomic rename)
        other = mods / "new.tmp"
        other.write_bytes(b"two!")
        os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(other, jar)
        assert jar.stat().st_ino != st.st_ino
        expected = b"two!"
    files = hash_folder(root, cache, iid, "mods", "sha1")
    assert _digests(files) == {"a.jar": hashlib.sha1(expected).hexdigest()}
    assert reads == ["mods/a.jar"]


def test_recently_modified_file_is_not_cached(setup, reads, monkeypatch):
    """Timestamps are coarse: a file changed just now could change again with the same stamps."""
    monkeypatch.setattr(hashcache, "RACY_WINDOW_NS", 2_000_000_000)
    iid, root, mods, cache = setup
    _write(mods / "hot.jar", b"hot")  # an old mtime does not help: utime itself bumps ctime
    hash_folder(root, cache, iid, "mods", "sha1")
    hash_folder(root, cache, iid, "mods", "sha1")
    assert reads == ["mods/hot.jar", "mods/hot.jar"]


def test_file_changing_while_read_is_not_cached(setup, reads, monkeypatch):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"alpha")
    real_fstat = os.fstat
    calls = {"n": 0}

    def drifting_fstat(fd):
        st = real_fstat(fd)
        calls["n"] += 1
        if calls["n"] % 2 == 0:  # the "after" stat sees a different size
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size + 1,
                    *st[7:10],
                )
            )
        return st

    monkeypatch.setattr(hashcache.os, "fstat", drifting_fstat)
    hash_folder(root, cache, iid, "mods", "sha1")
    monkeypatch.setattr(hashcache.os, "fstat", real_fstat)
    reads.clear()
    hash_folder(root, cache, iid, "mods", "sha1")
    assert reads == ["mods/a.jar"]


def test_removed_files_are_dropped_from_the_cache(setup):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"a")
    _write(mods / "b.jar", b"b")
    hash_folder(root, cache, iid, "mods", "sha1")
    (mods / "b.jar").unlink()
    hash_folder(root, cache, iid, "mods", "sha1")
    doc = json.loads(cache.path_for(iid, "mods").read_text())
    assert list(doc["entries"]) == ["a.jar"]


def test_symlink_replacing_a_cached_file_is_not_followed(setup, tmp_path):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"a")
    hash_folder(root, cache, iid, "mods", "sha1")
    secret = _write(tmp_path / "secret.jar", b"outside")
    (mods / "a.jar").unlink()
    (mods / "a.jar").symlink_to(secret)
    assert hash_folder(root, cache, iid, "mods", "sha1") == []


def test_algorithms_share_one_entry(setup, reads):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"alpha")
    hash_folder(root, cache, iid, "mods", "sha1")
    files = hash_folder(root, cache, iid, "mods", "sha512")
    assert _digests(files) == {"a.jar": hashlib.sha512(b"alpha").hexdigest()}
    reads.clear()
    hash_folder(root, cache, iid, "mods", "sha1")
    hash_folder(root, cache, iid, "mods", "sha512")
    assert reads == []
    entry = json.loads(cache.path_for(iid, "mods").read_text())["entries"]["a.jar"]
    assert set(entry) == {"size", "mtime_ns", "ctime_ns", "ino", "sha1", "sha512"}


def test_folders_and_instances_are_cached_separately(setup):
    iid, root, mods, cache = setup
    plugins = mods.parent / "plugins"
    plugins.mkdir()
    _write(mods / "a.jar", b"mod")
    _write(plugins / "a.jar", b"plugin")
    assert (
        _digests(hash_folder(root, cache, iid, "mods", "sha1"))["a.jar"] == hashlib.sha1(b"mod").hexdigest()
    )
    assert (
        _digests(hash_folder(root, cache, iid, "plugins", "sha1"))["a.jar"]
        == hashlib.sha1(b"plugin").hexdigest()
    )
    assert cache.path_for(iid, "mods") != cache.path_for(iid, "plugins")
    assert cache.path_for(iid, "mods") != cache.path_for(uuid.uuid4(), "mods")


@pytest.mark.parametrize(
    "content",
    [
        b"\x00\xffnot json",
        b"[]",
        b'{"version": 99, "entries": {}}',
        b"[" * 100_000,
        "WRONG_OWNER",
    ],
)
def test_corrupted_cache_is_ignored_and_rewritten(setup, reads, content):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"alpha")
    path = cache.path_for(iid, "mods")
    path.parent.mkdir(parents=True)
    if content == "WRONG_OWNER":  # a valid file that claims to be another folder's cache
        st = (mods / "a.jar").stat()
        content = json.dumps(
            {
                "version": 1,
                "instance": str(uuid.uuid4()),
                "folder": "mods",
                "entries": {
                    "a.jar": {
                        "size": st.st_size,
                        "mtime_ns": st.st_mtime_ns,
                        "ctime_ns": st.st_ctime_ns,
                        "ino": st.st_ino,
                        "sha1": "0" * 40,
                    }
                },
            }
        ).encode()
    path.write_bytes(content)
    files = hash_folder(root, cache, iid, "mods", "sha1")
    assert _digests(files) == {"a.jar": hashlib.sha1(b"alpha").hexdigest()}
    assert reads == ["mods/a.jar"]
    doc = json.loads(path.read_text())
    assert doc["version"] == 1 and doc["instance"] == str(iid) and doc["folder"] == "mods"
    assert doc["entries"]["a.jar"]["sha1"] == hashlib.sha1(b"alpha").hexdigest()


def test_invalid_entries_are_dropped_individually(setup, reads):
    iid, root, mods, cache = setup
    names = ["good.jar", "badhash.jar", "negsize.jar", "boolino.jar"]
    for n in names:
        _write(mods / n, n.encode())
    hash_folder(root, cache, iid, "mods", "sha1")
    path = cache.path_for(iid, "mods")
    doc = json.loads(path.read_text())
    doc["entries"]["badhash.jar"]["sha1"] = "z" * 40
    doc["entries"]["negsize.jar"]["size"] = -1
    doc["entries"]["boolino.jar"]["ino"] = True
    doc["entries"]["../escape.jar"] = dict(doc["entries"]["good.jar"])
    doc["entries"]["listed-only.jar"] = "not an object"
    path.write_text(json.dumps(doc))
    reads.clear()
    files = hash_folder(root, cache, iid, "mods", "sha1")
    assert sorted(reads) == ["mods/badhash.jar", "mods/boolino.jar", "mods/negsize.jar"]
    assert _digests(files) == {n: hashlib.sha1(n.encode()).hexdigest() for n in names}
    assert set(json.loads(path.read_text())["entries"]) == set(names)


def test_oversized_cache_file_is_ignored(setup, reads):
    iid, root, mods, _cache = setup
    cache = HashCache(_cache.directory, max_bytes=200)
    _write(mods / "a.jar", b"alpha")
    path = cache.path_for(iid, "mods")
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * 201)
    hash_folder(root, cache, iid, "mods", "sha1")
    assert reads == ["mods/a.jar"]


def test_entries_per_folder_are_bounded(setup, reads):
    iid, root, mods, _cache = setup
    cache = HashCache(_cache.directory, max_entries=3)
    for i in range(5):
        _write(mods / f"m{i}.jar", bytes([i]))
    files = hash_folder(root, cache, iid, "mods", "sha1")
    assert len(files) == 5 and all(f["hash"] for f in files)
    assert len(json.loads(cache.path_for(iid, "mods").read_text())["entries"]) == 3
    reads.clear()
    hash_folder(root, cache, iid, "mods", "sha1")
    assert len(reads) == 2  # only the uncached remainder is read again


def test_number_of_cache_files_is_bounded(tmp_path):
    cache = HashCache(tmp_path / "c", max_files=3)
    cache.directory.mkdir()
    stale_tmp = cache.directory / ".old.json.abc.tmp"
    stale_tmp.write_text("x")
    os.utime(stale_tmp, (time.time() - 7200, time.time() - 7200))
    ids = [uuid.uuid4() for _ in range(5)]
    for n, iid in enumerate(ids):
        fc = hashcache.FolderCache()
        fc.put("a.jar", hashcache.Signature(1, 1, 1, 1), "sha1", "0" * 40)
        cache.save(iid, "mods", fc)
        os.utime(cache.path_for(iid, "mods"), (1_000_000 + n, 1_000_000 + n))
    kept = sorted(p.name for p in cache.directory.iterdir())
    assert kept == sorted(cache.path_for(i, "mods").name for i in ids[-3:])


def test_forget_removes_only_that_instance(setup):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"a")
    hash_folder(root, cache, iid, "mods", "sha1")
    other = uuid.uuid4()
    fc = hashcache.FolderCache()
    fc.put("a.jar", hashcache.Signature(1, 1, 1, 1), "sha1", "0" * 40)
    cache.save(other, "mods", fc)
    cache.forget(iid)
    assert not cache.path_for(iid, "mods").exists()
    assert cache.path_for(other, "mods").exists()


def test_unwritable_cache_does_not_fail_hashing(setup, tmp_path):
    iid, root, mods, _cache = setup
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    cache = HashCache(blocker / "hashes")
    _write(mods / "a.jar", b"alpha")
    assert _digests(hash_folder(root, cache, iid, "mods", "sha1")) == {
        "a.jar": hashlib.sha1(b"alpha").hexdigest()
    }


def test_concurrent_passes_read_each_file_once(setup, reads):
    iid, root, mods, cache = setup
    for i in range(20):
        _write(mods / f"m{i}.jar", os.urandom(64 * 1024))
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(hash_folder(root, cache, iid, "mods", "sha1")))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reads) == 20
    assert all(r == results[0] for r in results)


def test_progressive_returns_partial_then_complete(setup, monkeypatch):
    iid, root, mods, cache = setup
    for i in range(3):
        _write(mods / f"m{i}.jar", bytes([i]) * 10)
    gate = threading.Event()
    original = hashcache._hash_file

    def slow(root_, path, algorithm, stop):
        gate.wait(5)
        return original(root_, path, algorithm, stop)

    monkeypatch.setattr(hashcache, "_hash_file", slow)
    jobs = HashJobs()
    try:
        first = hash_directory(root, cache, jobs, iid, "mods", "sha1", wait=0.05)
        assert first["complete"] is False and first["pending"] == 3
        assert [f["hash"] for f in first["files"]] == [None, None, None]
        assert [f["name"] for f in first["files"]] == ["m0.jar", "m1.jar", "m2.jar"]
        # a second caller joins the same background pass instead of starting another one
        assert hash_directory(root, cache, jobs, iid, "mods", "sha1", wait=0)["complete"] is False
        assert jobs.running() == 1
        gate.set()
        assert jobs.wait_idle()
        done = hash_directory(root, cache, jobs, iid, "mods", "sha1", wait=0)
        assert done["complete"] is True and done["pending"] == 0
        assert _digests(done["files"]) == {
            f"m{i}.jar": hashlib.sha1(bytes([i]) * 10).hexdigest() for i in range(3)
        }
    finally:
        gate.set()
        jobs.shutdown()


def test_progressive_completes_within_wait(setup, monkeypatch):
    monkeypatch.setattr(hashcache, "RACY_WINDOW_NS", 2_000_000_000)
    iid, root, mods, cache = setup
    (mods / "hot.jar").write_bytes(b"just written")  # never cached, but returned when hashed in time
    jobs = HashJobs()
    try:
        out = hash_directory(root, cache, jobs, iid, "mods", "sha1", wait=10)
        assert out["complete"] is True
        assert _digests(out["files"]) == {"hot.jar": hashlib.sha1(b"just written").hexdigest()}
    finally:
        jobs.shutdown()


def test_shutdown_cancels_a_running_pass(setup):
    iid, root, mods, cache = setup
    _write(mods / "a.jar", b"a" * (3 * hashcache.READ_CHUNK))
    jobs = HashJobs()
    jobs.stop.set()
    with pytest.raises(hashcache.HashingCancelled):
        hash_directory(root, cache, jobs, iid, "mods", "sha1")
    jobs.shutdown()


# --- through the API ----------------------------------------------------------------------
def test_api_progressive_and_persistent(api, auth, agent_settings, monkeypatch):
    from mcagent import main

    jobs = HashJobs()
    monkeypatch.setattr(main, "_hash_jobs", jobs)
    iid = uuid.uuid4()
    mods = agent_settings.instances_dir / str(iid) / "mods"
    mods.mkdir(parents=True)
    _write(mods / "a.jar", b"alpha")
    url = f"/v1/instances/{iid}/files/hashes"
    try:
        r = api.get(url, params={"path": "mods", "wait": 10}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["complete"] is True and body["pending"] == 0 and body["algorithm"] == "sha1"
        assert body["files"][0]["hash"] == hashlib.sha1(b"alpha").hexdigest()
        assert agent_settings.hash_cache_dir.is_dir()
        assert list(agent_settings.hash_cache_dir.glob(f"{iid}-*.json"))
        assert not agent_settings.hash_cache_dir.is_relative_to(agent_settings.instances_dir)
        legacy = api.get(url, params={"path": "mods"}, headers=auth).json()  # blocking form
        assert legacy["files"] == body["files"] and legacy["complete"] is True
        for bad in ("-1", "61", "nan", "inf", "soon"):
            assert api.get(url, params={"path": "mods", "wait": bad}, headers=auth).status_code == 422
        assert api.get(url, params={"path": "../x", "wait": 1}, headers=auth).status_code == 400
        missing = api.get(url, params={"path": "plugins", "wait": 1}, headers=auth).json()
        assert missing["files"] == [] and missing["complete"] is True
    finally:
        jobs.shutdown()


def test_delete_data_forgets_cached_digests(api, auth, agent_settings, monkeypatch):
    from mcagent import docker_ops

    monkeypatch.setattr(docker_ops, "remove_container", lambda *a, **k: None)
    iid = uuid.uuid4()
    mods = agent_settings.instances_dir / str(iid) / "mods"
    mods.mkdir(parents=True)
    _write(mods / "a.jar", b"alpha")
    assert (
        api.get(f"/v1/instances/{iid}/files/hashes", params={"path": "mods"}, headers=auth).status_code == 200
    )
    assert list(agent_settings.hash_cache_dir.glob(f"{iid}-*.json"))
    r = api.request("DELETE", f"/v1/instances/{iid}/data", json={"confirm": str(iid)}, headers=auth)
    assert r.status_code == 200, r.text
    op_id = r.json()["operation"]["id"]
    for _ in range(200):
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] not in ("pending", "running"):
            break
        time.sleep(0.02)
    assert op["status"] == "succeeded", op
    assert not list(agent_settings.hash_cache_dir.glob(f"{iid}-*.json"))
