"""Housekeeping at agent start (``maintenance``): the data root is closed to other users (audit
live-ops#7) and what interrupted operations left is removed, or kept and reported where it may
be the only copy of a server's files (agent-files#5, agent-backups#6). Real temporary folders."""

from __future__ import annotations

import os
import stat
import time
import uuid
from pathlib import Path

import pytest

from mcagent import maintenance


# -- live-ops#7 -------------------------------------------------------------------------------------
def test_the_data_root_and_the_instances_folder_become_0750(agent_settings):
    server = agent_settings.instances_dir / str(uuid.uuid4())
    server.mkdir()
    os.chmod(agent_settings.data_root, 0o755)
    os.chmod(agent_settings.instances_dir, 0o775)
    os.chmod(server, 0o777)
    assert maintenance.restrict_data_root(agent_settings) == []
    assert stat.S_IMODE(agent_settings.data_root.stat().st_mode) == 0o750
    assert stat.S_IMODE(agent_settings.instances_dir.stat().st_mode) == 0o750
    assert stat.S_IMODE(server.stat().st_mode) == 0o777  # server folders keep their modes


def test_as_root_the_two_folders_are_given_to_root(agent_settings, monkeypatch):
    changed: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda p, u, g, follow_symlinks=True: changed.append((str(p), u, g)))
    maintenance.restrict_data_root(agent_settings)
    assert changed == [(str(agent_settings.data_root), 0, 0), (str(agent_settings.instances_dir), 0, 0)]


def test_a_data_root_that_cannot_be_changed_is_reported_not_fatal(agent_settings, monkeypatch):
    def refuse(_path, _mode):
        raise PermissionError(1, "Operation not permitted")

    os.chmod(agent_settings.data_root, 0o755)
    monkeypatch.setattr(os, "chmod", refuse)
    problems = maintenance.restrict_data_root(agent_settings)
    assert len(problems) == 2 and "Operation not permitted" in problems[0]


def test_the_start_up_hook_restricts_the_data_root(agent_settings, monkeypatch):
    import asyncio

    from mcagent import docker_ops, main

    def no_docker(*_a, **_k):
        raise ConnectionError("Docker is not reachable")

    monkeypatch.setattr(main, "get_settings", lambda: agent_settings)
    for name in ("_blocked_networks", "_remembered_networks", "_attached_networks"):
        monkeypatch.setattr(main, name, getattr(main, name))
    monkeypatch.setattr(docker_ops, "ensure_network", no_docker)
    monkeypatch.setattr(docker_ops, "client", no_docker)
    os.chmod(agent_settings.data_root, 0o755)
    try:
        asyncio.run(main._startup())
    finally:
        main._background_stop.set()
    assert stat.S_IMODE(agent_settings.data_root.stat().st_mode) == 0o750


# -- leftovers --------------------------------------------------------------------------------------
def _old(path: Path, seconds: float = 3600) -> Path:
    """Back-date ``path`` (and everything in it): it was there before the agent started."""
    then = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in dirnames + filenames:
            os.utime(os.path.join(dirpath, name), (then, then), follow_symlinks=False)
    os.utime(path, (then, then), follow_symlinks=False)
    return path


def _make(path: Path, files: tuple[str, ...] = ("f",)) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel in files:
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(rel)
    return path


@pytest.fixture
def server(agent_settings):
    iid = uuid.uuid4()
    root = _make(agent_settings.instances_dir / str(iid), ("server.properties", "world/level.dat"))
    return iid, root


HEX = "0123456789ab"


def test_temporary_files_inside_server_folders_are_removed(agent_settings, server):
    _iid, root = server
    gone = [
        _old(_make(root / f".mcpanel-tmp-{HEX}")),  # an upload that was cut off
        _old(_make(root / "plugins" / f".mcpanel-tmp-{HEX}")),
        _old(_make(root / f".mcpanel-tmp-clone-{HEX}", ("c/x",))),
        _old(_make(root / ".mcpanel-world-new-abcdef")),
    ]
    _old(root / "world")
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert sorted(report["removed"]) == sorted(str(p) for p in gone)
    assert not any(p.exists() for p in gone)
    assert (root / "world" / "level.dat").exists() and (root / "server.properties").exists()


def test_the_old_world_of_a_failed_world_replace_is_kept_and_reported(agent_settings, server):
    _iid, root = server
    old_world = _old(_make(root / ".mcpanel-world-old-abcdef", ("overworld/level.dat",)))
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert report["kept"] == [str(old_world)] and old_world.exists()


def test_the_halves_of_an_interrupted_swap_are_kept(agent_settings, server):
    """ZFS restore or clone stopped after the old entries were moved aside, before the new ones
    were moved in: the aside folder and the staging folder hold the only copies."""
    _iid, root = server
    aside = _old(_make(root / f".mcpanel-tmp-old-{HEX}", ("world/level.dat",)))
    staged = _old(_make(root / f".mcpanel-tmp-restore-{HEX}", ("r/world/level.dat",)))
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert sorted(report["kept"]) == sorted([str(aside), str(staged)])
    assert aside.exists() and staged.exists() and report["removed"] == []


def test_entries_newer_than_the_agent_are_left_alone(agent_settings, server):
    _iid, root = server
    fresh = _make(root / f".mcpanel-tmp-{HEX}")  # an upload of this process
    started = time.time() - 60
    report = maintenance.sweep_leftovers(agent_settings, started)
    assert fresh.exists() and report == {"removed": [], "kept": [], "failed": []}


@pytest.mark.parametrize(
    "names",
    [
        # one half of a swap, the server folder in place: what an interruption before the swap left
        ("clone-" + HEX, "restore-1731000000", "convert"),  # "convert": blocked "Enable snapshots"
        ("old-1731000000", "preconvert-1731000000"),
        ("rollback-abcd1234", "restore-1731000001"),
        # the previous folder of one restore and the new one of another: not the halves of one swap
        ("old-1731000000", "restore-1731000001"),
    ],
)
def test_copies_next_to_the_server_folders(agent_settings, server, names):
    iid, _root = server
    d = agent_settings.instances_dir
    removed = [_old(_make(d / f"{iid}.mcpanel-{name}")) for name in names]
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert sorted(report["removed"]) == sorted(str(p) for p in removed)
    assert sorted(p.name for p in d.iterdir()) == [str(iid)]


@pytest.mark.parametrize("live", ["missing", "empty", "new world"])
@pytest.mark.parametrize(
    "halves",
    [
        ("i/.mcpanel-convert", "t/-preconvert-1731000000"),  # btrfs "Enable snapshots"
        ("i/.mcpanel-convert", "i/.mcpanel-preconvert-1731000000"),  # ZFS "Enable snapshots"
        ("i/.mcpanel-restore-1731000000", "i/.mcpanel-old-1731000000"),  # restore into a subvolume
        ("s/restore-{iid}-1731000000", "t/pre-restore-{iid}-1731000000"),  # restore, plain folder
        ("i/.mcpanel-rollback-abcd1234", "i/.mcpanel-old-1731000005"),  # btrfs rollback
    ],
)
def test_both_halves_of_an_interrupted_swap_are_kept_whatever_the_server_folder_holds(
    agent_settings, halves, live
):
    """Interrupted between the two renames of a swap: the previous files are in one folder, the
    new ones in the other. A start since then may have made a new world in the server folder
    (review r3 exp_sweep: both copies were deleted at the next agent start)."""
    iid = uuid.uuid4()
    where = {
        "i": agent_settings.instances_dir,
        "s": agent_settings.staging_dir,
        "t": agent_settings.trash_dir,
    }

    def path(spec: str) -> Path:
        folder, name = spec.split("/", 1)
        name = name.format(iid=iid)
        return where[folder] / (f"{iid}{name}" if name.startswith((".", "-")) else name)

    kept = [_old(_make(path(h), ("world/level.dat", "server.properties"))) for h in halves]
    if live != "missing":
        root = _make(
            agent_settings.instances_dir / str(iid), ("world/level.dat",) if live == "new world" else ()
        )
        _old(root, 60)
    for _ in range(2):  # and again at the next start
        report = maintenance.sweep_leftovers(agent_settings, time.time())
        assert sorted(report["kept"]) == sorted(str(p) for p in kept)
        assert report["removed"] == [] and all((p / "world" / "level.dat").exists() for p in kept)


def test_a_previous_copy_of_a_server_whose_folder_is_missing_or_empty_is_kept(agent_settings):
    d = agent_settings.instances_dir
    missing, empty = uuid.uuid4(), uuid.uuid4()
    (d / str(empty)).mkdir()  # made by a start after the interruption
    kept = [
        _old(_make(d / f"{missing}.mcpanel-old-1731000000")),
        _old(_make(d / f"{missing}.mcpanel-restore-1731000000")),
        _old(_make(d / f"{empty}.mcpanel-old-1731000000")),
        _old(_make(agent_settings.trash_dir / f"pre-restore-{missing}-1731000000")),
        _old(_make(agent_settings.staging_dir / f"restore-{missing}-1731000000")),
        _old(_make(agent_settings.trash_dir / f"{empty}-preconvert-1731000000")),
    ]
    clone = _old(_make(d / f"{missing}.mcpanel-clone-{HEX}"))  # a partial copy: never the only one
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert sorted(report["kept"]) == sorted(str(p) for p in kept)
    assert all(p.exists() for p in kept) and report["removed"] == [str(clone)]


def test_staging_and_trash_are_emptied(agent_settings, server):
    iid, _root = server
    deleted_server = uuid.uuid4()
    items = [
        _old(_make(agent_settings.staging_dir / f"clone-{uuid.uuid4()}")),
        _old(_make(agent_settings.staging_dir / f"restore-{iid}-1731000000")),
        _old(_make(agent_settings.trash_dir / f"{deleted_server}-1731000000", ("world/level.dat",))),
        _old(_make(agent_settings.trash_dir / f"pre-restore-{iid}-1731000001")),  # another restore
        _old(_make(agent_settings.trash_dir / f"{iid}-preconvert-1731000000")),
    ]
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert sorted(report["removed"]) == sorted(str(p) for p in items)
    assert list(agent_settings.staging_dir.iterdir()) == [] and list(agent_settings.trash_dir.iterdir()) == []


def test_a_server_with_a_running_operation_is_skipped(agent_settings, server):
    iid, root = server
    tmp = _old(_make(root / f".mcpanel-tmp-{HEX}"))
    clone = _old(_make(agent_settings.instances_dir / f"{iid}.mcpanel-clone-{HEX}"))
    report = maintenance.sweep_leftovers(agent_settings, time.time(), busy=lambda u: u == iid)
    assert tmp.exists() and clone.exists() and report["removed"] == []


def test_links_are_never_followed(agent_settings, server, tmp_path):
    _iid, root = server
    outside = _make(tmp_path / "outside", (f".mcpanel-tmp-{HEX}",))
    os.symlink(outside, root / "linked")
    _old(outside)
    maintenance.sweep_leftovers(agent_settings, time.time())
    assert (outside / f".mcpanel-tmp-{HEX}").exists()


def test_a_mounted_dataset_is_never_removed(agent_settings, server, monkeypatch):
    from mcagent import snapshots

    iid, _root = server
    convert = _old(_make(agent_settings.instances_dir / f"{iid}.mcpanel-convert"))
    monkeypatch.setattr(snapshots, "is_mount_point", lambda p: Path(p) == convert)
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert convert.exists() and report["kept"] == [str(convert)]


# -- age and busy checks (review r3 finding 10) ---------------------------------------------------
def test_leftovers_newer_than_the_agent_are_neither_removed_nor_reported(agent_settings, server):
    """Next to the server folders, in staging/ and in trash/: anything made after the agent
    started may belong to an operation of this process."""
    iid, _root = server
    missing = uuid.uuid4()
    d = agent_settings.instances_dir
    fresh = [
        _make(d / f"{iid}.mcpanel-clone-{HEX}"),
        _make(d / f"{iid}.mcpanel-old-1731000000"),
        _make(d / f"{missing}.mcpanel-old-1731000000"),  # would be reported as kept
        _make(agent_settings.staging_dir / f"clone-{iid}"),
        _make(agent_settings.staging_dir / f"restore-{missing}-1731000000"),
        _make(agent_settings.trash_dir / f"{uuid.uuid4()}-1731000000"),
        _make(agent_settings.trash_dir / f"pre-restore-{missing}-1731000000"),
    ]
    report = maintenance.sweep_leftovers(agent_settings, time.time() - 60)
    assert report == {"removed": [], "kept": [], "failed": []}
    assert all(p.exists() for p in fresh)


@pytest.mark.parametrize("where", ["sibling", "staging", "trash"])
def test_a_leftover_touched_while_the_sweep_runs_is_left_alone(agent_settings, server, where):
    """The age is checked again right before the delete: an operation may have taken the folder
    over since it was listed."""
    iid, _root = server
    path = {
        "sibling": agent_settings.instances_dir / f"{iid}.mcpanel-clone-{HEX}",
        "staging": agent_settings.staging_dir / f"clone-{iid}",
        "trash": agent_settings.trash_dir / f"{iid}-1731000000",
    }[where]
    _old(_make(path))

    def busy(_uid):
        os.utime(path)  # now: written by an operation that started meanwhile
        return False

    report = maintenance.sweep_leftovers(agent_settings, time.time(), busy=busy)
    assert path.exists() and report["removed"] == []


def test_leftovers_of_a_busy_server_are_neither_removed_nor_reported(agent_settings):
    """A server with a running operation: nothing of it is touched, nothing reported (its
    folders may be that operation's)."""
    iid = uuid.uuid4()  # no server folder: its previous copies would be reported as kept
    leftovers = [
        _old(_make(agent_settings.instances_dir / f"{iid}.mcpanel-old-1731000000")),
        _old(_make(agent_settings.instances_dir / f"{iid}.mcpanel-clone-{HEX}")),
        _old(_make(agent_settings.staging_dir / f"restore-{iid}-1731000000")),
        _old(_make(agent_settings.staging_dir / f"clone-{iid}")),
        _old(_make(agent_settings.trash_dir / f"pre-restore-{iid}-1731000001")),
        _old(_make(agent_settings.trash_dir / f"{iid}-1731000000")),
    ]
    report = maintenance.sweep_leftovers(agent_settings, time.time(), busy=lambda u: u == iid)
    assert report == {"removed": [], "kept": [], "failed": []}
    assert all(p.exists() for p in leftovers)


@pytest.mark.parametrize("where", ["sibling", "staging", "trash"])
def test_an_operation_that_starts_during_the_sweep_is_respected(agent_settings, server, where):
    """``busy`` is asked again right before each delete."""
    iid, _root = server
    path = {
        "sibling": agent_settings.instances_dir / f"{iid}.mcpanel-clone-{HEX}",
        "staging": agent_settings.staging_dir / f"clone-{iid}",
        "trash": agent_settings.trash_dir / f"{iid}-1731000000",
    }[where]
    _old(_make(path))
    import sys

    def busy(uid):  # idle when listed, busy at the delete
        return uid == iid and sys._getframe(1).f_code.co_name == "_discard"

    report = maintenance.sweep_leftovers(agent_settings, time.time(), busy=busy)
    assert path.exists() and report["removed"] == []


# -- the delete never reaches a folder made after the check (review r3 finding 9) -----------------
def test_a_leftover_is_moved_away_before_it_is_deleted(agent_settings, server, monkeypatch):
    """A clone that starts while the sweep deletes ``staging/clone-<id>`` creates that fixed name
    again: the delete must not take the new clone's files with it."""
    from mcagent import snapshots

    iid, _root = server
    fixed = _old(_make(agent_settings.staging_dir / f"clone-{iid}", ("old/f",)))
    real = snapshots._discard

    def slow_delete(path):
        _make(fixed, ("copy/level.dat",))  # the new clone copies into the fixed name meanwhile
        return real(path)

    monkeypatch.setattr(snapshots, "_discard", slow_delete)
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert report["removed"] == [str(fixed)]
    assert (fixed / "copy" / "level.dat").exists() and not (fixed / "old").exists()
    assert sorted(p.name for p in agent_settings.staging_dir.iterdir()) == [fixed.name]


def test_a_cut_off_delete_is_finished_by_the_next_sweep(agent_settings, server, monkeypatch):
    from mcagent import snapshots

    iid, _root = server
    sibling = _old(_make(agent_settings.instances_dir / f"{iid}.mcpanel-clone-{HEX}"))
    staged = _old(_make(agent_settings.staging_dir / f"clone-{iid}"))
    trashed = _old(_make(agent_settings.trash_dir / "not-a-server"))
    monkeypatch.setattr(snapshots, "_discard", lambda _p: "agent stopped")
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert len(report["failed"]) == 3 and not any(p.exists() for p in (sibling, staged, trashed))
    renamed = sorted(
        p.name
        for p in [
            *agent_settings.instances_dir.iterdir(),
            *agent_settings.staging_dir.iterdir(),
            *agent_settings.trash_dir.iterdir(),
        ]
        if "discard" in p.name
    )
    assert len(renamed) == 3
    assert sum(n.startswith(f"{iid}.mcpanel-discard-") for n in renamed) == 1
    assert sum(n.startswith(f"{iid}-discard-") for n in renamed) == 1
    assert sum(n.startswith("discard-") for n in renamed) == 1

    monkeypatch.undo()
    for folder in (agent_settings.instances_dir, agent_settings.staging_dir, agent_settings.trash_dir):
        for p in folder.iterdir():
            if "discard" in p.name:
                _old(p)
    # a busy server's renamed leftovers wait too
    report = maintenance.sweep_leftovers(agent_settings, time.time(), busy=lambda u: u == iid)
    assert [Path(r).name for r in report["removed"]] == [n for n in renamed if n.startswith("discard-")]
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert len(report["removed"]) == 2 and report["kept"] == []
    names = {p.name for f in (agent_settings.staging_dir, agent_settings.trash_dir) for p in f.iterdir()}
    assert names == set() and sorted(p.name for p in agent_settings.instances_dir.iterdir()) == [str(iid)]


def test_a_folder_replaced_between_the_check_and_the_move_is_put_back(agent_settings, server, monkeypatch):
    from mcagent import snapshots

    iid, _root = server
    path = _old(_make(agent_settings.staging_dir / f"clone-{iid}", ("old",)))

    def replaced(p):
        if Path(p) == path:  # runs between the age check and the move
            import shutil

            shutil.rmtree(path)
            _make(path, ("new",))
        return False

    monkeypatch.setattr(snapshots, "is_mount_point", replaced)
    report = maintenance.sweep_leftovers(agent_settings, time.time())
    assert report["removed"] == [] and report["kept"] == [str(path)]
    assert (path / "new").exists()
    assert [p.name for p in agent_settings.staging_dir.iterdir()] == [path.name]


def test_a_server_folder_swapped_for_a_link_after_the_listing_is_not_followed(
    agent_settings, server, tmp_path, monkeypatch
):
    _iid, root = server
    outside = _make(tmp_path / "outside", (f".mcpanel-tmp-{HEX}/f",))
    _old(outside)
    real = maintenance._sweep_server_folder

    def swapped(path, started_at, report):
        if path == root:  # replaced by a link between the listing and the walk
            import shutil

            shutil.rmtree(root)
            os.symlink(outside, root)
        return real(path, started_at, report)

    monkeypatch.setattr(maintenance, "_sweep_server_folder", swapped)
    maintenance.sweep_leftovers(agent_settings, time.time())
    assert (outside / f".mcpanel-tmp-{HEX}" / "f").exists()
