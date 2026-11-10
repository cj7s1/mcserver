"""Deleting btrfs subvolumes and snapshots with and without CAP_SYS_ADMIN.

The default stack runs the agent as root in a container without CAP_SYS_ADMIN on a pool that is
not mounted with ``user_subvol_rm_allowed``: the kernel then refuses ``btrfs subvolume delete``
(EPERM) while creating subvolumes and snapshots works. The agent must still be able to delete
what it creates (file by file), say truthfully which of the two ways it uses, and say
"unavailable" when neither works.

Every test runs in up to three environments:

* ``real``: a btrfs mount given by ``MCPANEL_TEST_BTRFS`` (skipped without it), with whatever
  privileges the test process has. Run the suite twice to cover both ways, once as full root and
  once with CAP_SYS_ADMIN dropped, e.g.::

      MCPANEL_TEST_BTRFS=/mnt/btrfs MCPANEL_TEST_BTRFS_EXPECT=native pytest tests/
      MCPANEL_TEST_BTRFS=/mnt/btrfs MCPANEL_TEST_BTRFS_EXPECT=fallback \\
          setpriv --bounding-set -sys_admin --inh-caps -sys_admin pytest tests/

  ``MCPANEL_TEST_BTRFS_EXPECT`` states which way the run is meant to exercise, so that a run with
  the wrong privileges fails instead of quietly testing the other way; without it the expectation
  is derived from the process's capabilities and the mount options.
* ``fake-native``, ``fake-fallback`` and ``fake-rmallowed``: ``tests/fake_btrfs.py`` stands in
  for the ``btrfs`` command (no btrfs filesystem needed). It accepts ``subvolume delete``, refuses
  it, or refuses it only for read-only snapshots, which is what a mount with
  ``user_subvol_rm_allowed`` does for a process without CAP_SYS_ADMIN: there the agent clears the
  read-only flag and the delete ioctl then works, so that counts as native.

Failures that cannot be produced on demand (a kernel that also refuses the file-by-file way, an
interrupted delete) are simulated by replacing the module's low-level helpers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcagent import snapshots, storage
from mcagent.config import Settings
from mcagent.docker_ops import AgentError
from mcagent.snapshots import DELETE_FALLBACK, DELETE_NATIVE, DELETING_PREFIX, PROBE_DIR, BtrfsSnapshots
from mcagent.zfs import has_sys_admin

ROOT = os.environ.get("MCPANEL_TEST_BTRFS")
EXPECT = os.environ.get("MCPANEL_TEST_BTRFS_EXPECT")
FAKE = Path(__file__).with_name("fake_btrfs.py")
FAKE_MARKER = ".fake-btrfs-subvolume"
TOKEN = "t" * 32
# What btrfs-progs 6.14 prints when the kernel refuses the delete ioctl (from a real run).
EPERM = "ERROR: Could not destroy subvolume/snapshot: Operation not permitted"


def _mount_options(path: str) -> str:
    real, best, options = os.path.realpath(path), "", ""
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        pre, _, post = line.partition(" - ")
        fields = pre.split()
        if len(fields) < 6 or not post:
            continue
        point = fields[4]
        if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, options = point, f"{fields[5]},{post.split()[-1]}"
    return options


def _wipe(base: Path) -> None:
    for path in sorted(base.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_dir() and not path.is_symlink() and snapshots.is_subvolume(path):
            snapshots.remove_tree(path)
    shutil.rmtree(base, ignore_errors=True)
    assert not base.exists(), f"left behind: {sorted(str(p) for p in base.rglob('*'))[:8]}"


@pytest.fixture(params=["real", "fake-native", "fake-fallback", "fake-rmallowed"])
def env(request, tmp_path, monkeypatch):
    """A data root with one server (``inst``, a subvolume holding world/level.dat = "v1")."""
    kind = request.param
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    if kind == "real":
        if not ROOT or not shutil.which("btrfs"):
            pytest.skip("needs MCPANEL_TEST_BTRFS btrfs mount")
        base = Path(ROOT) / f"t-delete-{uuid.uuid4().hex[:8]}"
        native = has_sys_admin() or "user_subvol_rm_allowed" in _mount_options(ROOT)
        mode = EXPECT or (DELETE_NATIVE if native else DELETE_FALLBACK)
        is_subvolume = snapshots.is_subvolume
    else:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "btrfs"
        exe.write_text(f"#!{sys.executable} -I\n" + FAKE.read_text())
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_BTRFS_LOG", str(tmp_path / "btrfs.log"))
        monkeypatch.delenv("FAKE_BTRFS_PROPERTY", raising=False)
        refusal = {"fake-fallback": "eperm", "fake-rmallowed": "rofs"}.get(kind)
        if refusal:
            monkeypatch.setenv("FAKE_BTRFS_DELETE", refusal)
        else:
            monkeypatch.delenv("FAKE_BTRFS_DELETE", raising=False)
        mount = {
            "mount_point": str(tmp_path),
            "fstype": "btrfs",
            "source": "/dev/fake",
            "root": "/",
            "propagation": "",
        }
        monkeypatch.setattr(storage, "mount_for", lambda _path: mount)

        def is_subvolume(path: Path) -> bool:
            return path.is_dir() and not path.is_symlink() and (path / FAKE_MARKER).is_file()

        monkeypatch.setattr(snapshots, "is_subvolume", is_subvolume)
        base = tmp_path / "data"
        mode = DELETE_FALLBACK if kind == "fake-fallback" else DELETE_NATIVE
    settings = Settings(token=TOKEN, data_root=base, runtime_uid=os.getuid(), runtime_gid=os.getgid())
    for folder in (settings.instances_dir, settings.staging_dir, settings.trash_dir, settings.state_dir):
        folder.mkdir(parents=True, exist_ok=True)
    snapshots._probe_cache.clear()
    iid = uuid.uuid4()
    inst = settings.instances_dir / str(iid)
    assert snapshots.ensure_instance_dir(settings, inst) is True
    (inst / "world").mkdir()
    (inst / "world" / "level.dat").write_text("v1")
    yield SimpleNamespace(
        kind=kind,
        mode=mode,
        settings=settings,
        iid=iid,
        inst=inst,
        snaps=BtrfsSnapshots(settings, iid),
        is_subvolume=is_subvolume,
    )
    _wipe(base)
    snapshots._probe_cache.clear()


@pytest.fixture
def api(env, monkeypatch):
    """The agent's HTTP API on the test data root; ``api.run(method, path, **kw)`` waits for the operation."""
    from fastapi.testclient import TestClient

    from mcagent import docker_ops
    from mcagent.config import get_settings
    from mcagent.main import app

    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": False})
    monkeypatch.setattr(docker_ops, "remove_container", lambda *_a, **_k: False)
    app.dependency_overrides[get_settings] = lambda: env.settings
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    def run(method: str, path: str, **kwargs) -> dict:
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == 200, response.text
        op_id = response.json()["operation"]["id"]
        deadline = time.monotonic() + 60
        while True:
            state = client.get(f"/v1/operations/{op_id}", headers=headers).json()
            if state["status"] in ("succeeded", "failed"):
                return state
            assert time.monotonic() < deadline, state
            time.sleep(0.05)

    try:
        yield SimpleNamespace(client=client, headers=headers, run=run)
    finally:
        app.dependency_overrides.pop(get_settings, None)


def _raiser(exc: BaseException):
    def fail(*_args, **_kwargs):
        raise exc

    return fail


def _refused() -> AgentError:
    return AgentError("btrfs failed: ERROR: Operation not permitted", "btrfs_failed", 500)


def _names(env) -> list[str]:
    return [snap["name"] for snap in env.snaps.list()]


# -- the probe and the capability ---------------------------------------------------------
def test_probe_reports_how_subvolumes_are_deleted(env, monkeypatch):
    root = env.settings.data_root
    support = snapshots.delete_support(root)
    assert (support["mode"], support["reason"]) == (env.mode, None)
    assert not (root / PROBE_DIR).exists()  # the test subvolume and its snapshot are gone again

    cap = env.snaps.capability()
    assert cap["available"] and cap["instance_ready"] and cap["instance_is_subvolume"]
    assert cap["delete_mode"] == env.mode and cap["reason"] is None
    if env.mode == DELETE_FALLBACK:
        for part in ("CAP_SYS_ADMIN", "slower", "docs/UNRAID.md"):
            assert part in cap["note"]
    else:
        assert cap["note"] is None
    node = storage.capabilities(env.settings)
    for volume in ("instances", "backups"):  # same filesystem: the node view says the same
        snap = node[volume]["snapshot"]
        assert (snap["provider"], snap["available"], snap["delete_mode"]) == ("btrfs", True, env.mode)
        assert snap["note"] == cap["note"] and snap["reason"] is None

    # Measured once per process, again on demand.
    calls = []
    real_probe = snapshots._probe
    with monkeypatch.context() as m:
        m.setattr(snapshots, "_probe", lambda r: calls.append(r) or real_probe(r))
        assert snapshots.delete_support(root)["mode"] == env.mode
        assert env.snaps.capability()["delete_mode"] == env.mode
        assert calls == []
        assert env.snaps.capability(refresh=True)["delete_mode"] == env.mode
        assert len(calls) == 1
    assert not (root / PROBE_DIR).exists()


def test_probe_clears_what_a_killed_agent_left(env):
    root = env.settings.data_root
    work = root / PROBE_DIR
    work.mkdir()
    BtrfsSnapshots.create_subvolume(work / "src")
    (work / "src" / "file").write_text("x")
    subprocess.run(
        ["btrfs", "subvolume", "snapshot", "-r", str(work / "src"), str(work / "snap")], check=True
    )
    assert snapshots.delete_support(root, refresh=True)["mode"] == env.mode
    assert not work.exists()


def test_unavailable_when_subvolumes_cannot_be_deleted_at_all(env, monkeypatch):
    root = env.settings.data_root
    work = root / PROBE_DIR
    plain_id = uuid.uuid4()
    plain = env.settings.instances_dir / str(plain_id)
    plain.mkdir()
    (plain / "server.properties").write_text("motd=x\n")
    calls = []
    real_probe = snapshots._probe
    with monkeypatch.context() as m:
        # Neither way works, e.g. no CAP_SYS_ADMIN on a kernel older than 4.18, where rmdir does
        # not remove a subvolume either.
        m.setattr(snapshots, "_native_delete", lambda _path: EPERM)
        m.setattr(snapshots, "_remove_files", _raiser(PermissionError(1, "Operation not permitted")))
        m.setattr(snapshots, "_probe", lambda r: calls.append(r) or real_probe(r))
        cap = env.snaps.capability(refresh=True)
        assert cap["available"] is False and cap["instance_ready"] is False
        assert cap["delete_mode"] is None and cap["note"] is None
        assert cap["instance_is_subvolume"] is True  # a fact about the folder, whatever the agent may do
        for part in ("Operation not permitted", "CAP_SYS_ADMIN", "docs/UNRAID.md"):
            assert part in cap["reason"]

        with pytest.raises(AgentError) as err:
            env.snaps.create("refused")
        assert (err.value.code, err.value.status) == ("snapshots_unavailable", 409)
        assert env.snaps.list() == [] and not env.snaps.snap_dir.exists()
        with pytest.raises(AgentError) as err:
            env.snaps.restore("20260101T000000Z")
        assert err.value.code == "snapshots_unavailable"
        with pytest.raises(AgentError) as err:
            BtrfsSnapshots(env.settings, plain_id).convert()
        assert err.value.code == "snapshots_unavailable"
        assert not env.is_subvolume(plain) and (plain / "server.properties").read_text() == "motd=x\n"
        assert [p.name for p in plain.parent.iterdir() if ".mcpanel-" in p.name] == []

        node = storage.capabilities(env.settings)["instances"]["snapshot"]
        assert node["available"] is False and node["reason"] == cap["reason"]
        assert len(calls) == 1  # the answer is kept: asking again creates nothing

        # In this simulation the probe cannot remove its own test subvolumes either. They are
        # named, and the next probe starts by trying to remove them instead of adding more.
        assert sorted(p.name for p in work.iterdir()) == ["snap", "src"]
        assert str(work) in cap["reason"]
        m.setattr(snapshots, "PROBE_RETRY_SECONDS", 0.0)  # a failed probe is repeated after a while
        again = env.snaps.capability()
        assert again["available"] is False and len(calls) == 2
        assert sorted(p.name for p in work.iterdir()) == ["snap", "src"]

    cap = env.snaps.capability(refresh=True)  # the cause is gone
    assert cap["available"] and cap["delete_mode"] == env.mode
    assert not work.exists()
    assert env.snaps.create("works again")["name"]


def test_unavailable_when_the_test_subvolume_cannot_be_created(env, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(snapshots, "_run", _raiser(AgentError("btrfs failed: ERROR: No space left on device")))
        cap = env.snaps.capability(refresh=True)
    assert cap["available"] is False and "No space left on device" in cap["reason"]
    assert not (env.settings.data_root / PROBE_DIR).exists()
    assert env.snaps.capability(refresh=True)["available"] is True


# -- deleting one snapshot, pruning -------------------------------------------------------
def test_delete_read_only_snapshot(env):
    (env.inst / "world" / "region").mkdir()
    (env.inst / "world" / "region" / "r.0.0.mca").write_bytes(b"\x01" * 8192)
    os.symlink("/etc/passwd", env.inst / "link")  # removed as a link, never followed
    BtrfsSnapshots.create_subvolume(env.inst / "nested")  # shows up in the snapshot as an empty folder
    (env.inst / "nested" / "file").write_text("n")
    name = env.snaps.create("Before upgrade!")["name"]
    snap = env.snaps.snap_dir / name
    assert env.is_subvolume(snap) and (snap / "world" / "level.dat").read_text() == "v1"
    if env.kind == "real" or os.geteuid() != 0:  # the stand-in's read-only flag does not bind root
        with pytest.raises(OSError):
            (snap / "world" / "level.dat").unlink()
    if env.mode == DELETE_FALLBACK:
        # The production situation: the delete ioctl is refused and the snapshot is read-only.
        proc = subprocess.run(["btrfs", "subvolume", "delete", str(snap)], capture_output=True, text=True)
        assert proc.returncode != 0 and "Operation not permitted" in proc.stderr
        assert (snap / "world" / "level.dat").read_text() == "v1"

    env.snaps.delete(name)
    assert env.snaps.list() == []
    assert list(env.snaps.snap_dir.iterdir()) == []  # no snapshot, no label file, nothing half-deleted
    assert Path("/etc/passwd").exists()
    with pytest.raises(AgentError) as err:
        env.snaps.delete(name)
    assert err.value.code == "snapshot_not_found"
    assert (env.inst / "world" / "level.dat").read_text() == "v1"  # the server itself is untouched


def test_delete_snapshot_through_the_api(env, api):
    name = env.snaps.create("manual")["name"]
    listed = api.client.get(f"/v1/instances/{env.iid}/snapshots", headers=api.headers).json()
    assert listed["capability"]["delete_mode"] == env.mode
    assert [snap["name"] for snap in listed["snapshots"]] == [name]
    response = api.client.delete(f"/v1/instances/{env.iid}/snapshots/{name}", headers=api.headers)
    assert response.status_code == 200 and response.json() == {"deleted": name}
    assert api.client.get(f"/v1/instances/{env.iid}/snapshots", headers=api.headers).json()["snapshots"] == []


def test_pruning_deletes_the_oldest_and_leaves_nothing_behind(env):
    for number in range(5):
        (env.inst / "world" / "level.dat").write_text(f"hour {number}")
        env.snaps.create(f"hourly {number}", {"kind": "scheduled"})
    manual = env.snaps.create("manual", {"kind": "manual"})["name"]
    # What the panel's retention does after a scheduled snapshot: keep the newest, delete the rest.
    scheduled = sorted((s["name"] for s in env.snaps.list() if s.get("kind") == "scheduled"), reverse=True)
    assert len(scheduled) == 5
    keep = 2
    for old in scheduled[keep:]:
        env.snaps.delete(old)
    kept = sorted([*scheduled[:keep], manual])
    assert sorted(_names(env)) == kept
    assert sorted(p.name for p in env.snaps.snap_dir.iterdir()) == sorted(
        [*kept, *(f"{name}.json" for name in kept)]
    )
    for name in kept:  # the survivors are intact and still read-only snapshots
        assert env.is_subvolume(env.snaps.snap_dir / name)
    assert (env.snaps.snap_dir / scheduled[0] / "world" / "level.dat").read_text().startswith("hour ")


def test_snapshot_that_cannot_be_made_writable_stays_listed(env, monkeypatch):
    name = env.snaps.create("keep me")["name"]
    snap = env.snaps.snap_dir / name
    with monkeypatch.context() as m:
        m.setattr(snapshots, "_native_delete", lambda _path: EPERM)
        m.setattr(snapshots, "_make_writable", _raiser(_refused()))
        with pytest.raises(AgentError) as err:
            env.snaps.delete(name)
    assert err.value.code == "btrfs_failed"
    for part in ("Operation not permitted", "docs/UNRAID.md"):
        assert part in str(err.value)
    # Nothing was changed: it is still listed, with its label, and complete.
    assert [(s["name"], s["label"]) for s in env.snaps.list()] == [(name, "keep me")]
    assert (snap / "world" / "level.dat").read_text() == "v1"
    assert sorted(p.name for p in env.snaps.snap_dir.iterdir()) == [name, f"{name}.json"]
    env.snaps.delete(name)
    assert list(env.snaps.snap_dir.iterdir()) == []


def test_half_deleted_snapshot_is_never_listed_and_is_removed_later(env, monkeypatch):
    first = env.snaps.create("first")["name"]
    second = env.snaps.create("second")["name"]
    hidden = env.snaps.snap_dir / f"{DELETING_PREFIX}{first}"
    with monkeypatch.context() as m:  # the file-by-file delete stops half-way
        m.setattr(snapshots, "_native_delete", lambda _path: EPERM)
        m.setattr(snapshots, "_remove_files", _raiser(OSError("Input/output error")))
        with pytest.raises(AgentError) as err:
            env.snaps.delete(first)
    assert str(hidden) in str(err.value) and "Input/output error" in str(err.value)
    assert hidden.is_dir()
    assert _names(env) == [second]  # not offered for a rollback any more
    with pytest.raises(AgentError) as err:
        env.snaps.restore(first)
    assert err.value.code == "snapshot_not_found"
    assert (env.inst / "world" / "level.dat").read_text() == "v1"
    env.snaps.delete(second)  # the next delete finishes the interrupted one
    assert list(env.snaps.snap_dir.iterdir()) == []


# -- rollback -----------------------------------------------------------------------------
def _break_the_world(env) -> None:
    (env.inst / "world" / "level.dat").write_text("v2-broken")
    (env.inst / "added-later.txt").write_text("new")


def _assert_rolled_back(env) -> None:
    assert (env.inst / "world" / "level.dat").read_text() == "v1"
    assert not (env.inst / "added-later.txt").exists()
    assert env.is_subvolume(env.inst)
    (env.inst / "writable.txt").write_text("ok")
    (env.inst / "writable.txt").unlink()


def _siblings(env) -> list[str]:
    return sorted(p.name for p in env.inst.parent.iterdir() if p.name != str(env.iid))


def test_rollback_removes_the_previous_folder(env):
    name = env.snaps.create("good")["name"]
    _break_the_world(env)
    assert env.snaps.restore(name) == {"restored": name, "warnings": [], "leftovers": []}
    _assert_rolled_back(env)
    assert _siblings(env) == []
    assert _names(env) == [name]  # the snapshot is still there and still usable
    _break_the_world(env)
    assert env.snaps.restore(name)["warnings"] == []
    _assert_rolled_back(env)


def test_rollback_is_done_with_a_warning_when_tidying_up_fails(env, monkeypatch):
    name = env.snaps.create("good")["name"]
    _break_the_world(env)
    # What an interrupted rollback of an older agent left: it used to block every later rollback.
    stale = env.inst.with_name(f"{env.iid}.mcpanel-rollback")
    BtrfsSnapshots.create_subvolume(stale)
    (stale / "half").write_text("copy")
    with monkeypatch.context() as m:
        m.setattr(
            snapshots, "delete_subvolume", _raiser(AgentError(f"btrfs failed: {EPERM}", "btrfs_failed", 500))
        )
        result = env.snaps.restore(name)  # must not raise: the swap is done
    assert result["restored"] == name
    _assert_rolled_back(env)
    leftovers = sorted(Path(p) for p in result["leftovers"])
    assert [p.name.rsplit("-", 1)[0] for p in leftovers] == [f"{env.iid}.mcpanel-old", f"{env.iid}.mcpanel"]
    old = leftovers[0]
    assert leftovers[1] == stale
    assert (old / "world" / "level.dat").read_text() == "v2-broken"  # the files from before the rollback
    assert len(result["warnings"]) == 2
    for path, warning in zip(leftovers, result["warnings"], strict=True):
        assert str(path) in warning and "Operation not permitted" in warning
    assert _siblings(env) == sorted(p.name for p in leftovers)

    # The next rollback removes what this one had to leave.
    _break_the_world(env)
    assert env.snaps.restore(name) == {"restored": name, "warnings": [], "leftovers": []}
    _assert_rolled_back(env)
    assert _siblings(env) == []


def test_rollback_operation_succeeds_when_tidying_up_is_refused(env, api, monkeypatch):
    """The job in the panel: 'succeeded' with a warning (it then starts the server again), not 'failed'."""
    name = env.snaps.create("good")["name"]
    _break_the_world(env)
    with monkeypatch.context() as m:
        m.setattr(
            snapshots, "delete_subvolume", _raiser(AgentError(f"btrfs failed: {EPERM}", "btrfs_failed", 500))
        )
        op = api.run("POST", f"/v1/instances/{env.iid}/snapshots/{name}/restore")
    assert op["status"] == "succeeded", op
    assert op["result"]["restored"] == name
    [warning] = op["result"]["warnings"]
    [leftover] = op["result"]["leftovers"]
    assert leftover in warning and Path(leftover).is_dir()
    _assert_rolled_back(env)
    op = api.run("POST", f"/v1/instances/{env.iid}/snapshots/{name}/restore")
    assert op["status"] == "succeeded" and op["result"]["warnings"] == []
    assert _siblings(env) == []


def test_failed_swap_puts_the_server_folder_back(env, monkeypatch):
    name = env.snaps.create("good")["name"]
    _break_the_world(env)
    real_rename = Path.rename

    def rename(self: Path, target):
        if ".mcpanel-rollback-" in self.name:
            raise OSError(5, "Input/output error")
        return real_rename(self, target)

    with monkeypatch.context() as m:
        m.setattr(Path, "rename", rename)
        with pytest.raises(OSError, match="Input/output error"):
            env.snaps.restore(name)
    assert (env.inst / "world" / "level.dat").read_text() == "v2-broken"  # nothing happened
    assert (env.inst / "added-later.txt").exists() and env.is_subvolume(env.inst)
    assert _siblings(env) == []  # the prepared copy is gone too
    assert env.snaps.restore(name)["warnings"] == []
    _assert_rolled_back(env)


# -- deleting a server ----------------------------------------------------------------------
def test_destroy_all_removes_snapshots_and_what_rollbacks_left(env):
    names = sorted([env.snaps.create("one")["name"], env.snaps.create("two")["name"]])
    half = env.snaps.snap_dir / f"{DELETING_PREFIX}20260101T000000Z-gone"  # an interrupted delete
    BtrfsSnapshots.create_subvolume(half)
    (half / "rest").write_text("x")
    old = env.inst.with_name(f"{env.iid}.mcpanel-old-1700000000")  # an earlier rollback's leftover
    BtrfsSnapshots.create_subvolume(old)
    (old / "level.dat").write_text("previous")
    other = env.inst.with_name(f"{uuid.uuid4()}.mcpanel-old-1700000000")  # another server's: not ours
    other.mkdir()

    result = env.snaps.destroy_all()
    assert result == {"destroyed": names, "snapshots": 2, "warnings": [], "leftovers": []}
    assert not env.snaps.snap_dir.exists() and not old.exists()
    assert _siblings(env) == [other.name]
    assert (env.inst / "world" / "level.dat").read_text() == "v1"  # the server's files are the caller's
    assert env.snaps.destroy_all() == {"destroyed": [], "snapshots": 0, "warnings": [], "leftovers": []}


def test_delete_server_with_snapshots(env, api):
    env.snaps.create("one")
    env.snaps.create("two")
    op = api.run("DELETE", f"/v1/instances/{env.iid}/data", json={"confirm": str(env.iid)})
    assert op["status"] == "succeeded", op
    result = op["result"]
    assert (result["deleted"], result["snapshots_deleted"], result["warnings"]) == (True, 2, [])
    assert not env.inst.exists() and not env.snaps.snap_dir.exists()


def test_undeletable_snapshot_does_not_block_deleting_the_server(env, api, monkeypatch):
    name = env.snaps.create("stuck")["name"]
    snap = env.snaps.snap_dir / name
    with monkeypatch.context() as m:
        m.setattr(snapshots, "_native_delete", lambda _path: EPERM)
        m.setattr(snapshots, "_make_writable", _raiser(_refused()))
        result = env.snaps.destroy_all()  # must not raise
        assert (result["destroyed"], result["snapshots"], result["leftovers"]) == ([], 0, [str(snap)])
        [warning] = result["warnings"]
        assert str(snap) in warning and "Operation not permitted" in warning
        assert _names(env) == [name] and (snap / "world" / "level.dat").read_text() == "v1"  # untouched

        op = api.run("DELETE", f"/v1/instances/{env.iid}/data", json={"confirm": str(env.iid)})
    assert op["status"] == "succeeded", op  # the server is gone; the job names what stayed
    assert op["result"]["deleted"] is True and op["result"]["snapshots_deleted"] == 0
    assert any(str(snap) in warning for warning in op["result"]["warnings"])
    assert not env.inst.exists()
    assert snap.is_dir() and _names(env) == [name]
    assert (env.snaps.snap_dir / f"{name}.json").is_file()  # still labelled


# -- other callers of the delete helper -----------------------------------------------------
def test_remove_tree_removes_subvolumes_snapshots_and_plain_trees(env):
    name = env.snaps.create("ro")["name"]
    snap = env.snaps.snap_dir / name
    writable = env.settings.instances_dir / "staging.mcpanel-restore-1"
    BtrfsSnapshots.create_subvolume(writable)
    (writable / "file").write_text("x")
    plain = env.settings.staging_dir / "plain"
    (plain / "sub").mkdir(parents=True)
    link = env.settings.staging_dir / "link"
    link.symlink_to(env.inst)
    for path in (snap, writable, plain, link, env.settings.staging_dir / "missing"):
        snapshots.remove_tree(path)
        assert not os.path.lexists(path)
    assert (env.inst / "world" / "level.dat").read_text() == "v1"  # the link's target is untouched
    assert snapshots.delete_support(env.settings.data_root)["mode"] == env.mode


def test_remove_tree_never_raises(env, monkeypatch, caplog):
    stuck = env.settings.instances_dir / "stuck.mcpanel-old-1"
    BtrfsSnapshots.create_subvolume(stuck)
    with monkeypatch.context() as m:
        m.setattr(snapshots, "_native_delete", lambda _path: EPERM)
        m.setattr(snapshots, "_remove_files", _raiser(PermissionError(1, "Operation not permitted")))
        snapshots.remove_tree(stuck)
    assert stuck.is_dir()
    assert str(stuck) in caplog.text and "Operation not permitted" in caplog.text


def test_convert_cleans_up_after_a_failed_copy(env, monkeypatch):
    plain_id = uuid.uuid4()
    plain = env.settings.instances_dir / str(plain_id)
    plain.mkdir()
    (plain / "server.properties").write_text("motd=x\n")
    snaps = BtrfsSnapshots(env.settings, plain_id)
    cap = snaps.capability()
    assert cap["available"] and not cap["instance_ready"] and "convert" in cap["reason"]
    real_run = subprocess.run

    def run(args, **kwargs):
        if args[0] == "cp":
            return subprocess.CompletedProcess(args, 1, "", "cp: error writing: No space left on device")
        return real_run(args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(snapshots.subprocess, "run", run)
        with pytest.raises(AgentError) as err:
            snaps.convert()
    assert err.value.code == "convert_failed"
    assert [
        p.name for p in plain.parent.iterdir() if ".mcpanel-" in p.name
    ] == []  # the half-made copy is gone
    assert not env.is_subvolume(plain) and (plain / "server.properties").read_text() == "motd=x\n"
    assert snaps.convert() == {"converted": True}
    assert env.is_subvolume(plain) and (plain / "server.properties").read_text() == "motd=x\n"
    name = snaps.create("after convert")["name"]
    snaps.delete(name)
    assert snaps.list() == []


def test_delete_runs_the_expected_commands(env):
    """Stand-in only: the btrfs calls for one snapshot delete, in order."""
    if env.kind == "real":
        pytest.skip("uses the stand-in's command log")
    import json

    name = env.snaps.create("x")["name"]
    snap = env.snaps.snap_dir / name
    hidden = env.snaps.snap_dir / f"{DELETING_PREFIX}{name}"
    log = Path(os.environ["FAKE_BTRFS_LOG"])
    log.write_text("")
    env.snaps.delete(name)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    expected = [["subvolume", "delete", str(snap)]]
    if env.kind != "fake-native":  # refused: out of the list, writable, then the ioctl once more
        expected += [
            ["property", "set", "-ts", str(hidden), "ro", "false"],
            ["subvolume", "delete", str(hidden)],  # works with user_subvol_rm_allowed; else file by file
        ]
    assert calls == expected
    assert list(env.snaps.snap_dir.iterdir()) == []
