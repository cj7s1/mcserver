"""R-3 of the review of the fix branch: safefs.copy_tree_fd (B-4) copies the same tree as the old
shutil.copytree(src, dst, symlinks=True, ignore=<skip fifo>, dirs_exist_ok=True), and like copytree it does not
report success with files missing: a source error or a folder too deep fails it at the end (CopyIncomplete);
only what changed underneath is skipped. Adopted from audit-2026-11-09/fixes/review/agent-fs/test_copy_tree_fd.py."""

from __future__ import annotations

import errno
import os
import shutil
import stat

import pytest

from mcagent import safefs


def _old_ignore(directory, names):
    out = set()
    for n in names:
        m = os.lstat(os.path.join(directory, n)).st_mode
        if not (stat.S_ISREG(m) or stat.S_ISDIR(m) or stat.S_ISLNK(m)):
            out.add(n)
    return out


def _snapshot(root):
    rows = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            kind = stat.S_IFMT(st.st_mode)
            row = [kind, stat.S_IMODE(st.st_mode), st.st_mtime_ns]
            if stat.S_ISLNK(st.st_mode):
                row.append(os.readlink(p))
            elif stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    row.append(f.read())
            rows[rel] = row
    st = os.stat(root)
    rows["."] = [stat.S_IMODE(st.st_mode), st.st_mtime_ns]
    return rows


def _build(src):
    os.makedirs(src / "a/b/empty")
    (src / "a/f.txt").write_bytes(b"x" * 100)
    os.chmod(src / "a/f.txt", 0o4755)
    (src / "ro").mkdir()
    (src / "ro/in.txt").write_bytes(b"in")
    os.chmod(src / "ro", 0o555)
    os.symlink("/etc/passwd", src / "abs-link")
    os.symlink("missing", src / "dangling")
    os.symlink("a", src / "dirlink")
    os.mkfifo(src / "fifo")
    os.link(src / "a/f.txt", src / "hard")
    with open(src / "sparse", "wb") as f:
        f.truncate(8 * 1024 * 1024)
        f.write(b"head")
    odd = os.fsdecode(b"odd-\xff-name")
    (src / odd).write_bytes(b"odd")
    d = src / "deep"
    for _ in range(40):
        d = d / "d"
    d.mkdir(parents=True)
    (d / "leaf").write_bytes(b"leaf")
    for p in sorted(src.rglob("*"), key=lambda p: -len(p.parts)):
        if not p.is_symlink():
            os.utime(p, ns=(1_000_000_000, 1_234_567_891_000))
    os.utime(src, ns=(1_000_000_000, 1_234_567_891_000))


def test_same_tree_as_copytree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _build(src)
    old, new = tmp_path / "old", tmp_path / "new"
    shutil.copytree(src, old, symlinks=True, ignore=_old_ignore, dirs_exist_ok=True)
    safefs.copy_tree_fd(src, new, _old_ignore)
    a, b = _snapshot(old), _snapshot(new)
    assert set(a) == set(b)
    diff = {k: (a[k], b[k]) for k in a if a[k] != b[k]}
    assert not diff


def test_a_tree_deeper_than_the_limit_fails_the_copy(tmp_path):
    src = tmp_path / "src"
    d = src
    for _ in range(300):
        d = d / "d"
    d.mkdir(parents=True)
    (d / "leaf").write_bytes(b"leaf")
    with pytest.raises(safefs.CopyIncomplete) as err:
        safefs.copy_tree_fd(src, tmp_path / "new")
    assert "deeper than 256 levels" in str(err.value) and err.value.failed[0][0].count("/") == 256


def test_a_read_error_fails_the_copy_naming_the_paths(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "world.dat").write_bytes(b"w")
    (src / "sub/region.mca").write_bytes(b"r")
    (src / "ok.txt").write_bytes(b"ok")
    real_open = os.open

    def flaky(path, flags, *a, **kw):
        if path in ("world.dat", "sub") and kw.get("dir_fd") is not None and not flags & os.O_WRONLY:
            raise OSError(errno.EIO, "Input/output error")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", flaky)
    with pytest.raises(safefs.CopyIncomplete) as err:
        safefs.copy_tree_fd(src, tmp_path / "new")
    assert sorted(p for p, _why in err.value.failed) == ["sub", "world.dat"]
    assert "Input/output error" in str(err.value) and err.value.code == "copy_incomplete"
    assert (tmp_path / "new" / "ok.txt").read_bytes() == b"ok"  # the rest was copied


def test_an_entry_that_vanished_is_skipped_quietly(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "gone.tmp").write_bytes(b"g")
    (src / "keep").write_bytes(b"k")
    real_open = os.open

    def vanished(path, flags, *a, **kw):
        if path == "gone.tmp" and kw.get("dir_fd") is not None and not flags & os.O_WRONLY:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", vanished)
    safefs.copy_tree_fd(src, tmp_path / "new")  # no error: the game removed it meanwhile
    assert (tmp_path / "new" / "keep").exists() and not (tmp_path / "new" / "gone.tmp").exists()


def test_a_clone_with_files_missing_fails(api, auth, agent_settings, monkeypatch):
    import time
    import uuid

    from mcagent import docker_ops
    from mcagent import safefs as sf

    src, dst = uuid.uuid4(), uuid.uuid4()
    (agent_settings.instances_dir / str(src)).mkdir(parents=True)
    (agent_settings.instances_dir / str(src) / "level.dat").write_bytes(b"x")
    monkeypatch.setattr(docker_ops, "status", lambda *_a, **_k: {"running": False})

    def broken(*_a, **_k):
        raise sf.CopyIncomplete([("level.dat", "OSError: Input/output error")])

    monkeypatch.setattr(sf, "copy_tree_fd", broken)
    r = api.post(f"/v1/instances/{src}/clone", json={"target_instance_id": str(dst)}, headers=auth)
    assert r.status_code in (200, 202), r.text
    op_id = r.json()["operation"]["id"]
    for _ in range(200):
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        op = op.get("operation", op)
        if op["status"] not in ("pending", "running"):
            break
        time.sleep(0.02)
    assert op["status"] == "failed" and op["error_code"] == "clone_incomplete"
    assert "level.dat" in op["error"]
