"""B-6 of the security review (2026-11-11): every walk of a server folder or of a backup is bounded, with a
``truncated`` flag (or an honest ``complete``) where the answer stops short. The caps are generous: no real
server comes near them (listing 20,000 entries, search 50,000 files / 30 s, zip 200,000 files, one hash pass
64 GiB and one pass per server at a time, backup browser 20,000 entries, 16 KiB per /logs line)."""

from __future__ import annotations

import os
import threading
import uuid
from types import SimpleNamespace

import pytest

from mcagent import archive, backups, docker_ops, hashcache, safefs
from mcagent.backups import Restic
from mcagent.errors import AgentError
from mcagent.safefs import SafeRoot

from . import fake_restic


def _files(base, n, folder="many"):
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"f{i:04d}.txt").write_text("needle" if i % 2 else "x")
    return d


def test_a_listing_stops_at_its_cap(api, auth, agent_settings, monkeypatch):
    iid = uuid.uuid4()
    _files(agent_settings.instances_dir / str(iid), 30)
    monkeypatch.setattr(safefs, "LIST_MAX_ENTRIES", 10)
    monkeypatch.setattr(SafeRoot.list_bounded, "__defaults__", (10,))
    body = api.get(f"/v1/instances/{iid}/files/list?path=many", headers=auth).json()
    assert len(body["entries"]) == 10 and body["truncated"] is True
    monkeypatch.setattr(SafeRoot.list_bounded, "__defaults__", (100,))
    body = api.get(f"/v1/instances/{iid}/files/list?path=many", headers=auth).json()
    assert len(body["entries"]) == 30 and "truncated" not in body


def test_a_search_stops_after_its_files(tmp_path):
    _files(tmp_path, 40)
    results, truncated = SafeRoot(tmp_path).search_bounded("", "needle", max_files=10)
    assert truncated is True and 0 < len(results) <= 10
    results, truncated = SafeRoot(tmp_path).search_bounded("", "needle")
    assert truncated is False and len(results) == 20
    assert SafeRoot(tmp_path).search("", "needle") == results  # the old method is unchanged


def test_a_search_stops_after_its_time(tmp_path):
    _files(tmp_path, 5)
    results, truncated = SafeRoot(tmp_path).search_bounded("", "needle", max_seconds=-1)
    assert truncated is True and results == []


def test_a_zip_of_too_many_files_is_refused(tmp_path, monkeypatch):
    _files(tmp_path, 12)
    monkeypatch.setattr(archive, "MAX_ZIP_FILES", 10)
    with pytest.raises(AgentError) as err:
        archive.stream_zip(SafeRoot(tmp_path), "many")
    assert err.value.code == "archive_too_large" and "more than 10 files" in str(err.value)


def test_a_hash_pass_reads_at_most_its_bytes(tmp_path, agent_settings, monkeypatch):
    d = tmp_path / "srv" / "mods"
    d.mkdir(parents=True)
    for i in range(4):
        (d / f"m{i}.jar").write_bytes(bytes([i]) * 1000)
        os.utime(d / f"m{i}.jar", (1_700_000_000, 1_700_000_000))  # settled files: their digests are cached
    monkeypatch.setattr(hashcache, "HASH_PASS_MAX_BYTES", 2500)
    monkeypatch.setattr(hashcache, "RACY_WINDOW_NS", 0)  # (just written files count as settled)
    cache = hashcache.HashCache(tmp_path / "state" / "cache" / "file-hashes")
    root = SafeRoot(tmp_path / "srv")
    jobs = hashcache.HashJobs(workers=1)
    try:
        first = hashcache.hash_directory(root, cache, jobs, uuid.UUID(int=1), "mods", "sha1")
        assert first["complete"] is False and first["pending"] == 2
        second = hashcache.hash_directory(root, cache, jobs, uuid.UUID(int=1), "mods", "sha1")
        assert second["complete"] is True and all(f["hash"] for f in second["files"])
    finally:
        jobs.shutdown()


def test_one_hash_pass_per_server_at_a_time():
    jobs = hashcache.HashJobs(workers=4)
    release = threading.Event()
    try:
        jobs.ensure(("srv-a", "mods", "sha1"), lambda: release.wait(5) and [])
        with pytest.raises(hashcache.HashingBusy):
            jobs.ensure(("srv-a", "plugins", "sha1"), lambda: [])
        other = jobs.ensure(("srv-b", "mods", "sha1"), lambda: [])  # another server goes on
        assert other.result(timeout=5) == []
        assert jobs.ensure(("srv-a", "mods", "sha1"), lambda: []) is not None  # the same pass: shared
    finally:
        release.set()
        jobs.shutdown()


@pytest.fixture
def restic_env(agent_settings, tmp_path):
    agent_settings.restic_binary = str(fake_restic.install(tmp_path / "bin"))
    r = Restic(agent_settings, uuid.uuid4())
    r.repo.mkdir(parents=True)
    (r.repo / "config").write_text("x")
    yield agent_settings.state_dir, r
    with backups._startup_lock:
        backups._startup_pending.clear()


def test_the_backup_browser_reads_restic_line_by_line_with_a_cap(restic_env, monkeypatch):
    import json

    home, r = restic_env
    snap = "ab" * 32
    monkeypatch.setattr(r, "snapshot", lambda _s: {"id": snap, "paths": ["/srv/x"]})
    monkeypatch.setattr(r, "_root_path", lambda _snap: "/srv/x")
    nodes = [json.dumps({"struct_type": "snapshot", "id": snap})]
    nodes += [
        json.dumps({"struct_type": "node", "name": f"f{i}", "path": f"/srv/x/f{i}", "type": "file"})
        for i in range(50)
    ]
    fake_restic.set_plan(home, {"ls": [{"stdout": "\n".join(nodes) + "\n"}]})
    entries, truncated = r.ls_bounded(snap, [], limit=20)
    assert truncated is True and len(entries) == 20
    entries, truncated = r.ls_bounded(snap, [])
    assert truncated is False and len(entries) == 50
    assert r.node_type(snap, ["f3"]) == "file"


def test_a_long_log_line_is_cut_after_redaction(monkeypatch):
    secret = "S3cretPassw0rdForTheConsole"
    start = docker_ops.LOG_LINE_MAX_BYTES - 5
    long_line = ("x" * start + secret + "y" * 50_000).encode()

    class C:
        labels = {"io.mcpanel.game": "rust"}

        def logs(self, **kwargs):
            assert kwargs["stream"] is True
            return iter([b"short line\n", long_line[:30_000], long_line[30_000:] + b"\n", b"after\n"])

    monkeypatch.setattr(docker_ops, "log_redactor", lambda *_a: lambda text: text.replace(secret, "********"))
    text = docker_ops._container_log(C(), 100, None, SimpleNamespace())
    lines = text.splitlines()
    assert lines[0] == "short line" and lines[2] == "after"
    assert lines[1].endswith(docker_ops.LOG_LINE_CUT) and secret[:5] not in lines[1]
    assert len(lines[1].encode()) <= docker_ops.LOG_LINE_MAX_BYTES + len(docker_ops.LOG_LINE_CUT)
