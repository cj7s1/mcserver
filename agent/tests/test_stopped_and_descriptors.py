"""B-4 of the security review (2026-11-11): "the server is stopped" is checked again inside the operations
that need it (and /start, /restart check the operations inside the lock), and clone, the restore's
carry-over and the size walk work through descriptors, never resolving a path string again."""

from __future__ import annotations

import contextlib
import os
import stat
import threading
import time
import uuid
from pathlib import Path

import pytest

from mcagent import docker_ops, main, storage
from mcagent.safefs import copy_tree_fd, open_dir_chain


def _wait(op_id: str, api, auth) -> dict:
    for _ in range(200):
        body = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        op = body.get("operation", body)
        if op["status"] not in ("pending", "running"):
            return op
        time.sleep(0.02)
    raise AssertionError("operation did not finish")


# --- the state, checked again ------------------------------------------------------------------------------
def test_a_start_that_slipped_in_before_the_operation_stops_it(api, auth, agent_settings, monkeypatch):
    iid = uuid.uuid4()
    folder = agent_settings.instances_dir / str(iid)
    folder.mkdir(parents=True)
    (folder / "pack.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    states = iter([{"running": False}, {"running": True}])  # the route's look, then the operation's
    monkeypatch.setattr(docker_ops, "status", lambda _iid, *_a: next(states))
    r = api.post(
        f"/v1/instances/{iid}/files/extract", json={"path": "pack.zip", "destination": "x"}, headers=auth
    )
    assert r.status_code == 202 or r.status_code == 200, r.text
    op = _wait(r.json()["operation"]["id"], api, auth)
    assert op["status"] == "failed" and op["error_code"] == "must_stop"
    assert not (folder / "x").exists()


def test_start_checks_for_operations_inside_the_lock(api, auth, monkeypatch):
    iid = uuid.uuid4()
    release = threading.Event()
    started: list = []

    @contextlib.contextmanager
    def lock(_iid, wait=None):
        # while this request waited for the lock, an operation that needs the server stopped registered
        main.registry.start("restore", [iid], lambda _op: release.wait(5) and None)
        yield

    monkeypatch.setattr(docker_ops, "instance_lock", lock)
    monkeypatch.setattr(docker_ops, "start", lambda *_a, **_k: started.append(True))
    try:
        r = api.post(f"/v1/instances/{iid}/start", headers=auth)
        assert r.status_code == 409 and r.json()["error"]["code"] == "instance_busy"
        assert started == []
    finally:
        release.set()


# --- carry-over and its matches ----------------------------------------------------------------------------
def _tree(base: Path) -> None:
    for rel in (
        "game/csgo/pak01.vpk",
        "game/csgo/maps/de_x.vpk",
        "game/bin/x.so",
        "game/top.vpk",
        "keep.txt",
    ):
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(rel)


@pytest.mark.parametrize("pattern", ["game/**/*.vpk", "game/*", "game/**", "*.txt", "game/csgo/*.vpk"])
def test_matches_are_what_path_glob_found(tmp_path, pattern):
    _tree(tmp_path)
    expected = sorted({str(p.relative_to(tmp_path)) for p in tmp_path.glob(pattern) if not p.is_symlink()})
    assert main._excluded_matches(tmp_path, pattern) == expected


def test_matches_never_go_through_or_return_a_link(tmp_path):
    outside = tmp_path / "outside"
    (outside / "csgo").mkdir(parents=True)
    (outside / "csgo" / "secret.vpk").write_text("x")
    current = tmp_path / "current"
    (current / "game").mkdir(parents=True)
    os.symlink(outside / "csgo", current / "game" / "csgo")
    os.symlink(outside / "csgo" / "secret.vpk", current / "game" / "link.vpk")
    assert main._excluded_matches(current, "game/**/*.vpk") == []


def test_carry_one_moves_between_pinned_folders_only(tmp_path):
    current, restored, outside = tmp_path / "c", tmp_path / "r", tmp_path / "o"
    (current / "gamefiles" / "sub").mkdir(parents=True)
    (current / "gamefiles" / "sub" / "big.pak").write_text("data")
    restored.mkdir()
    outside.mkdir()
    assert main._carry_one(current, restored, "gamefiles/sub/big.pak") is None
    assert (restored / "gamefiles" / "sub" / "big.pak").read_text() == "data"
    assert not (current / "gamefiles" / "sub" / "big.pak").exists()
    # a link in the way in the restored tree: nothing is moved through it
    (current / "gamefiles" / "sub" / "b.pak").write_text("b")
    (current / "x").mkdir()
    (current / "x" / "f").write_text("f")
    os.symlink(outside, restored / "x")
    assert main._carry_one(current, restored, "x/f") is None
    assert not (outside / "f").exists() and (current / "x" / "f").exists()
    # a link in the source's path: not followed
    os.symlink(outside, current / "y")
    (outside / "g").write_text("g")
    assert main._carry_one(current, restored, "y/g") is None
    assert (outside / "g").exists() and not (restored / "y").exists()
    # already in the restored tree: left as it is
    (restored / "keep").write_text("restored")
    (current / "keep").write_text("current")
    assert (
        main._carry_one(current, restored, "keep") is None and (restored / "keep").read_text() == "restored"
    )


def test_open_dir_chain(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    os.symlink(tmp_path / "a", tmp_path / "l")
    fd = open_dir_chain(tmp_path, ["a", "b"])
    assert fd is not None
    os.close(fd)
    assert open_dir_chain(tmp_path, ["l", "b"]) is None
    assert open_dir_chain(tmp_path, ["missing"]) is None
    fd = open_dir_chain(tmp_path, ["new", "deeper"], create=True)
    assert fd is not None and (tmp_path / "new" / "deeper").is_dir()
    os.close(fd)


# --- clone's copy -------------------------------------------------------------------------------------------
def test_copy_tree_fd_copies_like_copytree(tmp_path):
    src, dst, outside = tmp_path / "src", tmp_path / "dst", tmp_path / "outside"
    (src / "world" / "region").mkdir(parents=True)
    (src / "world" / "region" / "r.0.0.mca").write_bytes(b"\x01" * 5000)
    (src / "server.properties").write_text("motd=x\n")
    os.chmod(src / "server.properties", 0o640)
    os.utime(src / "server.properties", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    outside.mkdir()
    (outside / "secret").write_text("s")
    os.symlink(outside, src / "link-out")
    os.symlink("server.properties", src / "rel-link")
    os.mkfifo(src / "fifo")
    skipped_world = src / "skipme"
    skipped_world.mkdir()
    seen: list = []

    def ignore(directory: str, names: list[str]) -> set[str]:
        seen.append((directory, sorted(names)))
        return {"skipme"} if directory == str(src) else set()

    copy_tree_fd(src, dst, ignore)
    assert (dst / "world" / "region" / "r.0.0.mca").read_bytes() == b"\x01" * 5000
    assert stat.S_IMODE((dst / "server.properties").stat().st_mode) == 0o640
    assert (dst / "server.properties").stat().st_mtime_ns == 1_700_000_000_000_000_000
    assert (
        os.readlink(dst / "link-out") == str(outside) and os.readlink(dst / "rel-link") == "server.properties"
    )
    assert not (dst / "fifo").exists() and not (dst / "skipme").exists()
    assert seen[0] == (
        str(src),
        sorted(["world", "server.properties", "link-out", "rel-link", "fifo", "skipme"]),
    )


def test_copy_tree_fd_never_reads_a_fifo_put_in_place_of_a_file(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "level.dat").write_text("x")

    def swap(directory: str, names: list[str]) -> set[str]:  # the game swaps it between listing and copy
        os.unlink(src / "level.dat")
        os.mkfifo(src / "level.dat")
        return set()

    copy_tree_fd(src, dst, swap)  # returns at once: a FIFO is never opened for reading
    assert not (dst / "level.dat").exists()


# --- the size walk ---------------------------------------------------------------------------------------------
def test_dir_size_does_not_follow_links(tmp_path):
    (tmp_path / "s" / "d").mkdir(parents=True)
    (tmp_path / "s" / "d" / "f").write_bytes(b"x" * 10000)
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "huge").write_bytes(b"y" * 200000)
    plain = storage.dir_size(tmp_path / "s")
    os.symlink(tmp_path / "big", tmp_path / "s" / "link")
    assert storage.dir_size(tmp_path / "s") - plain < 200000  # only the link itself
    assert storage.dir_size(tmp_path / "missing") == 0
