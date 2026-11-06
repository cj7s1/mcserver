"""ZFS snapshot provider, driven against a fake ``zfs`` executable (tests/fake_zfs.py).

No ZFS kernel module exists in CI or the sandbox, so these tests check the argv the agent
builds, the order of operations, the undo paths and the end state (files, datasets,
snapshots) as emulated by the fake. They are NOT a substitute for a run on real ZFS.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

from mcagent import snapshots, storage, zfs
from mcagent.docker_ops import AgentError
from mcagent.snapshots import ensure_instance_dir, is_volume_root, snapshot_provider, swap_contents
from mcagent.zfs import ZfsSnapshots

FAKE = Path(__file__).with_name("fake_zfs.py")
PARENT = "tank/appdata"


class FakeZfs:
    def __init__(self, root: Path, data_root: Path) -> None:
        self.state_dir = root / "zfs-state"
        self.state_dir.mkdir()
        self.state_file = self.state_dir / "state.json"
        self.propagation = "shared:42"
        self.save(
            {
                "clock": 1_800_000_000,
                "datasets": {
                    PARENT: {
                        "mountpoint": str(data_root),
                        "mounted": True,
                        "canmount": "on",
                        "origin": "-",
                        "props": {},
                        "external": True,
                    }
                },
                "snapshots": {},
                "fail": [],
            }
        )

    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def save(self, state: dict) -> None:
        self.state_file.write_text(json.dumps(state))

    def fail(
        self, match: str, message: str = "injected failure", mode: str = "before", times: int = 1
    ) -> None:
        state = self.state()
        state["fail"].append({"match": match, "message": message, "mode": mode, "times": times})
        self.save(state)

    def calls(self) -> list[list[str]]:
        path = self.state_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def clear_calls(self) -> None:
        (self.state_dir / "calls.jsonl").unlink(missing_ok=True)

    def mutating(self) -> list[list[str]]:
        return [c for c in self.calls() if c[0] not in ("list", "version")]

    def datasets(self) -> list[str]:
        return sorted(self.state()["datasets"])

    def snapshots(self) -> list[str]:
        return sorted(self.state()["snapshots"])

    def mount_for(self, path: Path) -> dict[str, str]:
        real = os.path.realpath(path)
        best = None
        for name, d in self.state()["datasets"].items():
            mp = d["mountpoint"]
            if d["mounted"] and (real == mp or real.startswith(mp.rstrip("/") + "/")):
                if best is None or len(mp) > len(best["mount_point"]):
                    best = {
                        "mount_point": mp,
                        "fstype": "zfs",
                        "source": name,
                        "root": "/",
                        "propagation": self.propagation,
                    }
        return best or {
            "mount_point": "/",
            "fstype": "ext4",
            "source": "/dev/sda1",
            "root": "/",
            "propagation": "",
        }


@pytest.fixture
def fake(agent_settings, tmp_path, monkeypatch) -> FakeZfs:
    assert os.path.realpath(tmp_path) == str(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "zfs"
    exe.write_text(f"#!{sys.executable} -I\n" + FAKE.read_text())
    exe.chmod(0o755)
    device = tmp_path / "dev-zfs"
    device.touch()
    fz = FakeZfs(tmp_path, agent_settings.data_root)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ZFS_STATE", str(fz.state_dir))
    monkeypatch.setattr(storage, "mount_for", fz.mount_for)
    monkeypatch.setattr(zfs, "ZFS_DEVICE", str(device))
    monkeypatch.setattr(zfs, "in_container", lambda: True)
    return fz


@pytest.fixture
def inst(agent_settings, fake):
    """A new instance whose folder was created as a dataset (like /prepare does)."""
    iid = uuid.uuid4()
    path = agent_settings.instances_dir / str(iid)
    assert ensure_instance_dir(agent_settings, path) is True
    return iid, path, ZfsSnapshots(agent_settings, iid)


def write(path: Path, rel: str, text: str) -> None:
    (path / rel).parent.mkdir(parents=True, exist_ok=True)
    (path / rel).write_text(text)


# -- environment / capability -------------------------------------------------------------
def test_environment_problems(fake, monkeypatch):
    base = {"fstype": "zfs", "source": PARENT, "mount_point": "/x", "propagation": "shared:1"}
    assert zfs.environment_problem(base) is None
    assert "not ZFS" in zfs.environment_problem({**base, "fstype": "xfs"})
    assert "rshared" in zfs.environment_problem({**base, "propagation": "master:3"})
    assert "rshared" in zfs.environment_problem({**base, "propagation": ""})
    assert "same path" in zfs.environment_problem(base, same_host_path=False)
    assert "characters" in zfs.environment_problem({**base, "source": "tank/app data"})
    monkeypatch.setattr(zfs, "in_container", lambda: False)  # agent on the host: no propagation needed
    assert zfs.environment_problem({**base, "propagation": ""}) is None
    monkeypatch.setattr(zfs, "ZFS_DEVICE", "/nonexistent/zfs")
    assert "/dev/zfs" in zfs.environment_problem(base)
    monkeypatch.setattr(zfs.shutil, "which", lambda name: None)
    assert "zfs tools are not available" in zfs.environment_problem(base)


def test_storage_reports_zfs(agent_settings, fake):
    info = storage.describe_path(agent_settings.instances_dir)
    assert info["fstype"] == "zfs"
    assert info["snapshot"] == {"provider": "zfs", "available": True, "reason": None}
    fake.propagation = ""
    assert "rshared" in storage.describe_path(agent_settings.instances_dir)["snapshot"]["reason"]


def test_provider_is_picked_by_filesystem(agent_settings, fake, monkeypatch):
    assert isinstance(snapshot_provider(agent_settings, uuid.uuid4()), ZfsSnapshots)
    monkeypatch.setattr(
        storage, "mount_for", lambda p: {"fstype": "btrfs", "mount_point": "/", "source": "x"}
    )
    assert isinstance(snapshot_provider(agent_settings, uuid.uuid4()), snapshots.BtrfsSnapshots)


def test_capability_without_propagation_is_unavailable(agent_settings, fake):
    fake.propagation = "master:7"
    cap = ZfsSnapshots(agent_settings, uuid.uuid4()).capability()
    assert cap["available"] is False and "rshared" in cap["reason"]
    # ... and new instances then stay plain directories instead of datasets nobody else sees
    path = agent_settings.instances_dir / str(uuid.uuid4())
    assert ensure_instance_dir(agent_settings, path) is False
    assert path.is_dir() and fake.mutating() == []


def test_capability_reports_kernel_errors(agent_settings, fake):
    fake.fail("list -H -o name tank/appdata", "The ZFS modules cannot be auto-loaded.")
    cap = ZfsSnapshots(agent_settings, uuid.uuid4()).capability()
    assert cap["available"] is False and "cannot be auto-loaded" in cap["reason"]


# -- dataset per instance -----------------------------------------------------------------
def test_new_instance_dir_is_a_dataset(agent_settings, fake, inst):
    iid, path, snaps = inst
    name = f"{PARENT}/mcpanel-{iid}"
    assert fake.mutating() == [["create", "-o", f"mountpoint={path}", "-o", f"mcpanel:instance={iid}", name]]
    assert fake.state()["datasets"][name]["mounted"] is True
    cap = snaps.capability()
    assert cap["available"] and cap["instance_ready"] and cap["instance_is_subvolume"]
    assert cap["dataset"] == name and cap["provider"] == "zfs" and "zfs-kmod" in cap["zfs_version"]
    assert is_volume_root(path)
    assert ensure_instance_dir(agent_settings, path) is False  # already exists: nothing to do


def test_create_that_does_not_mount_is_cleaned_up(agent_settings, fake):
    fake.fail("create", mode="nomount")
    iid = uuid.uuid4()
    path = agent_settings.instances_dir / str(iid)
    assert ensure_instance_dir(agent_settings, path) is False
    assert path.is_dir() and fake.datasets() == [PARENT]  # plain directory, no dataset left behind
    assert ["destroy", f"{PARENT}/mcpanel-{iid}"] in fake.calls()


def test_stale_dataset_with_the_same_name_is_refused(agent_settings, fake):
    iid = uuid.uuid4()
    state = fake.state()
    state["datasets"][f"{PARENT}/mcpanel-{iid}"] = {
        "mountpoint": "none",
        "mounted": False,
        "canmount": "on",
        "origin": "-",
        "props": {},
    }
    fake.save(state)
    (fake.state_dir / "data").mkdir(exist_ok=True)
    with pytest.raises(AgentError) as err:
        ensure_instance_dir(agent_settings, agent_settings.instances_dir / str(iid))
    assert err.value.code == "dataset_conflict"


# -- snapshots ----------------------------------------------------------------------------
def test_create_list_delete(fake, inst):
    iid, path, snaps = inst
    write(path, "world/level.dat", "v1")
    fake.clear_calls()
    info = snaps.create("Before upgrade!\tnow\n", {"kind": "manual", "consistent": True})
    name = info["name"]
    assert name.endswith("-before-upgrade-now") and info["kind"] == "manual" and info["consistent"] is True
    snap_calls = [c for c in fake.calls() if c[0] == "snapshot"]
    assert snap_calls == [
        [
            "snapshot",
            "-o",
            f"mcpanel:instance={iid}",
            "-o",
            "mcpanel:label=Before upgrade! now",  # control characters never reach 'zfs list -H' output
            "-o",
            "mcpanel:kind=manual",
            "-o",
            "mcpanel:consistent=true",
            f"{PARENT}/mcpanel-{iid}@{name}",
        ]
    ]
    [item] = snaps.list()
    assert item["name"] == name and item["label"] == "Before upgrade! now"
    assert item["kind"] == "manual" and item["consistent"] is True and item["used_bytes"] == 8192
    assert item["created_at"].startswith("2027-01-15T")  # the fake's 'creation' clock
    assert "retained" not in item
    snaps.delete(name)
    assert snaps.list() == [] and fake.snapshots() == []


def test_foreign_snapshots_are_ignored(fake, inst):
    iid, _path, snaps = inst
    state = fake.state()
    state["snapshots"][f"{PARENT}/mcpanel-{iid}@autosnap_2026-11-01_daily"] = {"creation": 1, "props": {}}
    fake.save(state)
    assert snaps.list() == []
    with pytest.raises(AgentError) as err:
        snaps.delete("autosnap_2026-11-01_daily")
    assert err.value.code == "invalid_snapshot"


@pytest.mark.parametrize(
    "bad",
    ["../x", "20260101T000000Z/../../etc", "nope", "-r", "20260101T000000Z@x", "20260101T000000Z -r", ""],
)
def test_invalid_snapshot_names_never_reach_zfs(fake, inst, bad):
    _iid, _path, snaps = inst
    fake.clear_calls()
    for action in (snaps.delete, snaps.restore):
        with pytest.raises(AgentError) as err:
            action(bad)
        assert err.value.code == "invalid_snapshot"
    assert fake.mutating() == []


def test_unknown_snapshot_is_404(fake, inst):
    with pytest.raises(AgentError) as err:
        inst[2].delete("20260101T000000Z")
    assert err.value.status == 404


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("tank/appdata/mcpanel-1", True),
        ("cache/a_b.c:d-e", True),
        ("tank", False),  # a pool root is never an instance dataset
        ("tank/-rf", False),
        ("-tank/x", False),
        ("tank/a@b", False),
        ("tank/a b", False),
        ("tank/../etc", False),
        ("tank/%recv", False),
        ("tank//x", False),
        ("tank/x\n", False),
        ("tank/" + "x" * 300, False),
    ],
)
def test_dataset_name_validation(name, ok):
    assert zfs.valid_dataset(name) is ok


def test_weird_dataset_at_instance_path_is_refused(agent_settings, fake, inst):
    iid, path, snaps = inst
    state = fake.state()
    state["datasets"][f"{PARENT}/mcpanel-{iid}"]["mountpoint"] = str(path)
    state["datasets"]["tank/app data"] = state["datasets"].pop(f"{PARENT}/mcpanel-{iid}")
    fake.save(state)
    cap = snaps.capability()
    assert cap["available"] is False and "Unexpected ZFS dataset name" in cap["reason"]
    with pytest.raises(AgentError):
        snaps.list()


def test_create_requires_a_dataset(agent_settings, fake):
    iid = uuid.uuid4()
    (agent_settings.instances_dir / str(iid)).mkdir()
    snaps = ZfsSnapshots(agent_settings, iid)
    cap = snaps.capability()
    assert cap["available"] and not cap["instance_ready"] and "plain directory" in cap["reason"]
    with pytest.raises(AgentError) as err:
        snaps.create("x")
    assert err.value.code == "not_subvolume"
    assert snaps.list() == []


# -- rollback -----------------------------------------------------------------------------
def _safety(snaps: ZfsSnapshots, target: str) -> str:
    """What the panel does before a rollback (apps/backups/handlers.py snapshot_restore)."""
    time.sleep(1.01)  # snapshot names have one-second resolution
    return snaps.create(f"before rollback to {target}", {"consistent": True, "kind": "pre_restore"})["name"]


def test_rollback_restores_files_and_keeps_newer_snapshots(fake, inst):
    iid, path, snaps = inst
    live = f"{PARENT}/mcpanel-{iid}"
    write(path, "world/level.dat", "v1")
    s1 = snaps.create("one")["name"]
    write(path, "world/level.dat", "v2")
    write(path, "mods/new.jar", "jar")
    safety = _safety(snaps, s1)
    fake.clear_calls()

    result = snaps.restore(s1)

    assert (path / "world/level.dat").read_text() == "v1" and not (path / "mods").exists()
    history = result["history_dataset"]
    assert history.startswith(f"{live}-old-") and result["dataset"] == live and result["warnings"] == []
    calls = fake.mutating()
    clone = f"{live}-new-{history.rsplit('-', 1)[1]}"
    assert calls[0] == [
        "clone",
        "-o",
        "canmount=noauto",
        "-o",
        f"mountpoint={path}",
        "-o",
        f"mcpanel:instance={iid}",
        f"{live}@{s1}",
        clone,
    ]
    assert calls[1:6] == [
        ["unmount", live],
        ["set", "mountpoint=none", live],
        ["rename", live, history],
        ["rename", clone, live],
        ["set", "canmount=on", live],
    ]
    assert ["promote", live] in calls
    # never 'zfs rollback' (it would destroy the newer snapshots) and never a snapshot destroy
    assert not any(c[0] == "rollback" or (c[0] == "destroy" and "@" in c[-1]) for c in calls)
    # no automatic safety snapshot: the panel's one already captured the current state
    assert not any(c[0] == "snapshot" for c in calls)

    listed = {s["name"]: s for s in snaps.list()}
    assert set(listed) == {s1, safety}
    assert listed[safety].get("retained") is True and "retained" not in listed[s1]
    state = fake.state()
    assert state["datasets"][live]["origin"] == "-"  # promoted: depends on nothing
    assert state["datasets"][history]["mountpoint"] == "none" and not state["datasets"][history]["mounted"]
    assert state["datasets"][history]["props"]["mcpanel:instance"] == str(iid)
    assert snaps.capability()["instance_ready"]

    # undo the rollback via the safety snapshot (it lives on the history dataset)
    time.sleep(1.01)
    safety2 = _safety(snaps, safety)
    snaps.restore(safety)
    assert (path / "world/level.dat").read_text() == "v2" and (path / "mods/new.jar").read_text() == "jar"
    assert {s["name"] for s in snaps.list()} == {s1, safety, safety2}
    # the first history dataset lost its only snapshot to the promote and had no own changes
    assert history not in fake.datasets()
    assert fake.state()["datasets"][live]["origin"] == "-"
    # the new dataset is writable and snapshots work after rollbacks
    write(path, "after.txt", "ok")
    time.sleep(1.01)
    assert snaps.create("again")["name"] in {s["name"] for s in snaps.list()}


def test_rollback_takes_its_own_safety_snapshot_if_state_is_uncaptured(fake, inst):
    _iid, path, snaps = inst
    write(path, "a.txt", "one")
    s1 = snaps.create("one")["name"]
    write(path, "a.txt", "changed-without-snapshot")
    time.sleep(1.01)
    snaps.restore(s1)
    assert (path / "a.txt").read_text() == "one"
    auto = [s for s in snaps.list() if s["name"].endswith("-before-rollback")]
    assert len(auto) == 1 and auto[0]["kind"] == "pre_restore" and auto[0]["retained"] is True
    time.sleep(1.01)
    snaps.restore(auto[0]["name"])
    assert (path / "a.txt").read_text() == "changed-without-snapshot"


def test_rollback_busy_dataset_is_undone(fake, inst):
    iid, path, snaps = inst
    live = f"{PARENT}/mcpanel-{iid}"
    write(path, "a.txt", "current")
    s1 = snaps.create("one")["name"]
    fake.fail(f"unmount {live}", "cannot unmount '/x': pool or dataset is busy")
    with pytest.raises(AgentError) as err:
        snaps.restore(s1)
    assert err.value.code == "dataset_busy" and "busy" in str(err.value)
    assert fake.datasets() == [PARENT, live]  # the clone was removed again
    assert fake.state()["datasets"][live]["mounted"] and (path / "a.txt").read_text() == "current"


@pytest.mark.parametrize("failing", ["rename-clone", "mount-new", "set-canmount"])
def test_rollback_failure_midway_is_undone(fake, inst, failing):
    iid, path, snaps = inst
    live = f"{PARENT}/mcpanel-{iid}"
    write(path, "a.txt", "current")
    s1 = snaps.create("one")["name"]
    write(path, "a.txt", "newer")
    _safety(snaps, s1)
    match = {
        "rename-clone": f"rename {live}-new-",
        "mount-new": f"mount {live}",
        "set-canmount": "set canmount=on",
    }[failing]
    fake.fail(match, "boom")
    with pytest.raises(AgentError) as err:
        snaps.restore(s1)
    assert "boom" in str(err.value) and err.value.code != "rollback_failed"
    state = fake.state()
    assert sorted(state["datasets"]) == [PARENT, live]
    assert state["datasets"][live]["mounted"] and state["datasets"][live]["mountpoint"] == str(path)
    assert (path / "a.txt").read_text() == "newer"
    assert len(snaps.list()) == 2


def test_rollback_that_cannot_be_undone_says_where_the_files_are(fake, inst):
    iid, _path, snaps = inst
    live = f"{PARENT}/mcpanel-{iid}"
    s1 = snaps.create("one")["name"]
    _safety(snaps, s1)
    fake.fail(f"rename {live}-new-", "boom")
    fake.fail(f"rename {live}-old-", "cannot rename: busy")  # the undo of 'rename live -> history'
    with pytest.raises(AgentError) as err:
        snaps.restore(s1)
    assert err.value.code == "rollback_failed"
    assert f"{live}-old-" in str(err.value) and "zfs list" in str(err.value)


# -- delete with dependents ---------------------------------------------------------------
def test_delete_refuses_snapshots_others_depend_on(fake, inst):
    iid, path, snaps = inst
    live = f"{PARENT}/mcpanel-{iid}"
    write(path, "a.txt", "1")
    s1 = snaps.create("one")["name"]
    write(path, "a.txt", "2")
    safety = _safety(snaps, s1)
    history = snaps.restore(s1)["history_dataset"]
    fake.clear_calls()
    with pytest.raises(AgentError) as err:
        snaps.delete(s1)
    assert err.value.code == "snapshot_has_dependents" and safety in str(err.value)
    assert fake.mutating() == []  # refused before calling zfs destroy
    snaps.delete(safety)
    assert history not in fake.datasets()  # its last snapshot is gone, so is the dataset
    assert ["destroy", history] in fake.calls()
    snaps.delete(s1)
    assert snaps.list() == [] and fake.datasets() == [PARENT, live]


def test_delete_refuses_foreign_clones(fake, inst):
    iid, _path, snaps = inst
    s1 = snaps.create("one")["name"]
    state = fake.state()
    state["datasets"]["tank/mytest"] = {
        "mountpoint": "none",
        "mounted": False,
        "canmount": "on",
        "origin": f"{PARENT}/mcpanel-{iid}@{s1}",
        "props": {},
    }
    fake.save(state)
    (fake.state_dir / "data" / "tank%2Fmytest").mkdir(parents=True)
    with pytest.raises(AgentError) as err:
        snaps.delete(s1)
    assert err.value.code == "snapshot_has_dependents" and "tank/mytest" in str(err.value)
    with pytest.raises(AgentError) as err:
        snaps.destroy_all()
    assert err.value.code == "dataset_has_dependents"


def test_destroy_all_removes_history_and_never_uses_capital_r(fake, inst):
    iid, path, snaps = inst
    live = f"{PARENT}/mcpanel-{iid}"
    write(path, "a.txt", "1")
    s1 = snaps.create("one")["name"]
    write(path, "a.txt", "2")
    _safety(snaps, s1)
    snaps.restore(s1)
    assert len(fake.datasets()) == 3
    fake.clear_calls()
    result = snaps.destroy_all()
    assert fake.datasets() == [PARENT] and fake.snapshots() == []
    assert result["destroyed"][-1] == live and len(result["destroyed"]) == 2 and result["snapshots"] == 2
    assert all(c[:2] == ["destroy", "-r"] for c in fake.mutating())
    assert path.is_dir() and not any(path.iterdir())  # the empty mountpoint directory is left for the caller


# -- convert ------------------------------------------------------------------------------
def test_convert_plain_directory(agent_settings, fake):
    iid = uuid.uuid4()
    path = agent_settings.instances_dir / str(iid)
    path.mkdir()
    write(path, "server.properties", "motd=x\n")
    write(path, "world/region/r.0.0.mca", "data")
    os.symlink("/etc/passwd", path / "link")  # copied as a link, never followed
    snaps = ZfsSnapshots(agent_settings, iid)
    with pytest.raises(AgentError):
        snaps.create("x")
    result = snaps.convert()
    name = f"{PARENT}/mcpanel-{iid}"
    assert result == {"converted": True, "dataset": name}
    tmp = path.with_name(f"{iid}.mcpanel-convert")
    assert fake.mutating() == [
        ["create", "-o", f"mountpoint={tmp}", "-o", f"mcpanel:instance={iid}", name],
        ["set", f"mountpoint={path}", name],
    ]
    assert snaps.dataset() == name
    assert (path / "server.properties").read_text() == "motd=x\n"
    assert (path / "world/region/r.0.0.mca").read_text() == "data"
    assert os.readlink(path / "link") == "/etc/passwd"
    assert sorted(p.name for p in agent_settings.instances_dir.iterdir()) == [str(iid)]  # no leftovers
    assert snaps.create()["name"]
    assert snaps.convert() == {"converted": False, "reason": "already a dataset"}


def test_convert_failure_puts_the_directory_back(agent_settings, fake):
    iid = uuid.uuid4()
    path = agent_settings.instances_dir / str(iid)
    path.mkdir()
    write(path, "keep.txt", "precious")
    fake.fail(f"set mountpoint={path}", "cannot mount: permission denied")
    with pytest.raises(AgentError):
        ZfsSnapshots(agent_settings, iid).convert()
    assert (path / "keep.txt").read_text() == "precious"
    assert fake.datasets() == [PARENT]
    assert sorted(p.name for p in agent_settings.instances_dir.iterdir()) == [str(iid)]


def test_convert_refuses_leftovers(agent_settings, fake):
    iid = uuid.uuid4()
    (agent_settings.instances_dir / str(iid)).mkdir()
    (agent_settings.instances_dir / f"{iid}.mcpanel-convert").mkdir()
    with pytest.raises(AgentError) as err:
        ZfsSnapshots(agent_settings, iid).convert()
    assert err.value.code == "convert_conflict" and fake.mutating() == []


# -- in-place swaps (restic restore / clone into a dataset) -------------------------------
def test_swap_contents(tmp_path):
    target = tmp_path / "t"
    write(target, "old.txt", "old")
    write(target, "world/a", "old-world")
    staging = target / ".mcpanel-restore-1"
    write(staging, "world/a", "new-world")
    write(staging, "new.txt", "new")
    swap_contents(target, staging)
    assert sorted(p.name for p in target.iterdir()) == ["new.txt", "world"]
    assert (target / "world/a").read_text() == "new-world"


def test_swap_contents_failure_moves_everything_back(tmp_path, monkeypatch):
    target = tmp_path / "t"
    write(target, "a.txt", "old-a")
    write(target, "b.txt", "old-b")
    staging = target / ".mcpanel-restore-1"
    write(staging, "a.txt", "new-a")
    write(staging, "b.txt", "new-b")
    real_rename = Path.rename
    calls = {"n": 0}

    def flaky(self, dst):
        calls["n"] += 1
        if calls["n"] == 4:  # after both old entries moved aside and one new one moved in
            raise OSError("disk on fire")
        return real_rename(self, dst)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError):
        swap_contents(target, staging)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert (target / "a.txt").read_text() == "old-a" and (target / "b.txt").read_text() == "old-b"
    assert sorted(p.name for p in staging.iterdir()) == ["a.txt", "b.txt"]
    assert sorted(p.name for p in target.iterdir()) == [".mcpanel-restore-1", "a.txt", "b.txt"]


def test_clone_into_a_dataset_keeps_it_a_dataset(api, auth, agent_settings, fake, monkeypatch):
    from mcagent import docker_ops
    from mcagent.main import registry

    monkeypatch.setattr(docker_ops, "status", lambda iid: {"running": False})
    src_id, dst_id = uuid.uuid4(), uuid.uuid4()
    src = agent_settings.instances_dir / str(src_id)
    ensure_instance_dir(agent_settings, src)
    write(src, "server.properties", "motd=src\n")
    write(src, "world/level.dat", "w")
    r = api.post(
        f"/v1/instances/{src_id}/clone",
        json={"target_instance_id": str(dst_id), "exclude_worlds": True},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    op = registry.get(r.json()["operation"]["id"])
    deadline = time.monotonic() + 10
    while op.status in ("pending", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert op.status == "succeeded", op.as_dict()
    dst = agent_settings.instances_dir / str(dst_id)
    assert ZfsSnapshots(agent_settings, dst_id).dataset() == f"{PARENT}/mcpanel-{dst_id}"
    assert sorted(p.name for p in dst.iterdir()) == ["server.properties"]


def test_snapshot_api_uses_the_zfs_provider(api, auth, agent_settings, fake, inst):
    iid, _path, snaps = inst
    name = snaps.create("api")["name"]
    r = api.get(f"/v1/instances/{iid}/snapshots", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["capability"]["provider"] == "zfs" and body["capability"]["instance_ready"] is True
    assert [s["name"] for s in body["snapshots"]] == [name]
    assert api.delete(f"/v1/instances/{iid}/snapshots/{name}", headers=auth).status_code == 200
    assert snaps.list() == []


def test_delete_instance_destroys_its_datasets(api, auth, agent_settings, fake, inst, monkeypatch):
    from mcagent import docker_ops
    from mcagent.main import registry

    iid, path, snaps = inst
    monkeypatch.setattr(docker_ops, "remove_container", lambda *a, **k: False)
    write(path, "a.txt", "x")
    snaps.create("one")
    r = api.request("DELETE", f"/v1/instances/{iid}/data", json={"confirm": str(iid)}, headers=auth)
    assert r.status_code == 200, r.text
    op = registry.get(r.json()["operation"]["id"])
    deadline = time.monotonic() + 10
    while op.status in ("pending", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert op.status == "succeeded", op.as_dict()
    assert op.result["snapshots_deleted"] == 1
    assert fake.datasets() == [PARENT] and not path.exists()


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic binary not installed")
def test_restic_restore_into_a_dataset_keeps_it_a_dataset(api, auth, agent_settings, fake, inst, monkeypatch):
    from mcagent import docker_ops
    from mcagent.backups import Restic
    from mcagent.main import registry

    iid, path, _snaps = inst
    monkeypatch.setattr(docker_ops, "status", lambda i: {"running": False})
    write(path, "world/level.dat", "ORIGINAL")
    write(path, "server.properties", "motd=x\n")
    snap = Restic(agent_settings, iid).backup(path, [f"instance:{iid}"], [])["snapshot_id"]
    write(path, "world/level.dat", "BROKEN")
    write(path, "junk.txt", "added after the backup")
    r = api.post(f"/v1/instances/{iid}/backups/restore", json={"snapshot_id": snap}, headers=auth)
    assert r.status_code == 200, r.text
    op = registry.get(r.json()["operation"]["id"])
    deadline = time.monotonic() + 60
    while op.status in ("pending", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert op.status == "succeeded", op.as_dict()
    assert (path / "world/level.dat").read_text() == "ORIGINAL" and not (path / "junk.txt").exists()
    assert sorted(p.name for p in path.iterdir()) == ["server.properties", "world"]  # no staging leftovers
    assert ZfsSnapshots(agent_settings, iid).dataset() == f"{PARENT}/mcpanel-{iid}"  # still a dataset
