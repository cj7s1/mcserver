"""World reset: what counts as a world, the move-aside with rollback, the error contract and the HTTP API.

Everything runs on a real temporary filesystem. ``reset`` promises that it raises only
``AgentError`` and that the code says whether the world was touched; the tests of the
"nothing was changed" codes compare the whole server folder before and after.
"""

from __future__ import annotations

import gzip
import os
import threading
import time
import uuid
from pathlib import Path

import docker.errors
import pytest
import requests.exceptions

from mcagent import docker_ops, world
from mcagent.docker_ops import AgentError
from mcagent.operations import BLOCKS_LIFECYCLE, OperationRegistry
from mcagent.safefs import PathError, SafeRoot

LEVEL_DAT = gzip.compress(b"\x0a\x00\x00\x00")  # a gzip-compressed (empty) NBT compound
# After these codes nothing was changed (the panel's WORLD_RESET_UNTOUCHED; must_stop comes from the route).
UNTOUCHED = {
    "invalid_level_name",
    "invalid_layout",
    "invalid_keep",
    "not_a_directory",
    "not_a_world",
    "world_changed",
    "world_reset_failed",
}


@pytest.fixture
def inst(tmp_path):
    d = tmp_path / "inst"
    d.mkdir()
    (d / "server.properties").write_text("motd=x\n")
    (d / "mods").mkdir()
    (d / "mods" / "a.jar").write_text("j")
    return d


@pytest.fixture
def sr(inst):
    return SafeRoot(inst)


def mk(inst, name="world", marker="old"):
    (inst / name / "region").mkdir(parents=True)
    (inst / name / "level.dat").write_bytes(LEVEL_DAT)
    (inst / name / "region" / "r.0.0.mca").write_text(marker)
    (inst / name / "playerdata").mkdir()
    (inst / name / "playerdata" / "p.dat").write_text("p")


def leftovers(inst):
    return [p.name for p in inst.iterdir() if p.name.startswith(world.STAGING_PREFIX)]


def snapshot(base: Path) -> dict[str, tuple]:
    """Every entry below ``base`` with its type and content (links are not followed)."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            rel = str(path.relative_to(base))
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir",)
            elif path.is_file():
                out[rel] = ("file", path.read_bytes())
            else:
                out[rel] = ("other",)
    return out


def refused(inst, code, level_name, layout, **kwargs) -> AgentError:
    """``reset`` must refuse with ``code`` and leave the server folder exactly as it was."""
    before = snapshot(inst)
    with pytest.raises(AgentError) as exc:
        world.reset(SafeRoot(inst), level_name, layout, **kwargs)
    assert exc.value.code == code and code in UNTOUCHED
    assert snapshot(inst) == before
    return exc.value


def fail_on_call(monkeypatch, method: str, numbers, error):
    """Make the n-th call(s) of a SafeRoot method raise ``error``; the other calls are real."""
    real = getattr(SafeRoot, method)
    calls = {"n": 0}

    def wrapper(self, *args, **kwargs):
        calls["n"] += 1
        if numbers(calls["n"]) if callable(numbers) else calls["n"] in numbers:
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SafeRoot, method, wrapper)
    return calls


# --- classify / keepable -------------------------------------------------------------------
def test_keepable_names_cover_reset_keep():
    names = {
        name for roles in world.RESET_KEEP.values() for entries in roles.values() for name, _t in entries
    }
    assert names == world.KEEPABLE_NAMES
    assert not world.KEEPABLE_NAMES & world.WORLD_MARKERS


def test_classify(sr, inst, tmp_path):
    assert world.classify(sr, "nope") == "missing"
    assert world.classify(sr, "server.properties") == "not_a_directory"
    assert world.classify(sr, "server.properties/world") == "missing"  # the parent is a file
    os.symlink(tmp_path, inst / "link")
    assert world.classify(sr, "link") == "not_a_directory"
    os.mkfifo(inst / "pipe")
    assert world.classify(sr, "pipe") == "not_a_directory"
    (inst / "empty").mkdir()
    assert world.classify(sr, "empty") == "empty"
    assert world.classify(sr, "mods") == "unknown"
    mk(inst)
    assert world.classify(sr, "world") == "world"
    (inst / "world" / "datapacks").mkdir()
    (inst / "world" / "notes.txt").write_text("x")
    assert world.classify(sr, "world") == "world"  # a marker decides, whatever else is in it
    (inst / "after" / "datapacks").mkdir(parents=True)
    (inst / "after" / "serverconfig").mkdir()
    (inst / "after" / "paper-world.yml").write_text("a: 1")
    assert world.classify(sr, "after") == "pending"  # what a reset with carry-overs leaves
    (inst / "after" / "notes.txt").write_text("mine")
    assert world.classify(sr, "after") == "unknown"
    assert world.classify(SafeRoot(tmp_path / "no-server-folder"), "world") == "missing"


def test_keepable_and_classify(sr, inst, tmp_path):
    mk(inst)
    (inst / "world" / "datapacks").mkdir()  # empty: not keepable
    (inst / "world" / "serverconfig").mkdir()
    (inst / "world" / "serverconfig" / "a.toml").write_text("x")
    os.symlink(tmp_path, inst / "world" / "paper-world.yml")  # a link is never kept
    assert world.keepable(sr, "overworld", "world") == {"config": ["serverconfig"]}
    assert world.classify(sr, "world") == "world" and world.classify(sr, "nope") == "missing"
    assert world.classify(sr, "server.properties") == "not_a_directory"


def test_keepable_needs_the_right_type_and_role(sr, inst):
    for name in ("world", "world_nether", "world_the_end"):
        mk(inst, name)
        (inst / name / "paper-world.yml").write_text("a: 1")
        (inst / name / "datapacks").mkdir()
        (inst / name / "datapacks" / "p.zip").write_text("dp")
    assert world.keepable(sr, "overworld", "world") == {
        "datapacks": ["datapacks"],
        "config": ["paper-world.yml"],
    }
    assert world.keepable(sr, "nether", "world_nether") == {
        "config": ["paper-world.yml"]
    }  # no datapacks there
    assert world.keepable(sr, "end", "world_the_end") == {"config": ["paper-world.yml"]}
    (inst / "world" / "paper-world.yml").unlink()
    (inst / "world" / "paper-world.yml").mkdir()  # a folder where a file is expected
    (inst / "world" / "serverconfig").write_text("a file where a folder is expected")
    assert world.keepable(sr, "overworld", "world") == {"datapacks": ["datapacks"]}
    assert world.keepable(sr, "overworld", "nope") == {}


def test_parent_symlink_kind_is_none(sr, inst, tmp_path):
    """world._kind() hides a linked parent as 'missing' (what the reset relies on)."""
    os.symlink(tmp_path, inst / "link")
    assert world._kind(sr, "link/x") is None and world._kind(sr, "link") == "symlink"
    with pytest.raises(PathError):
        sr.list("link")


# --- reset: what is deleted -------------------------------------------------------------------
def test_vanilla_deletes_only_the_world(sr, inst):
    mk(inst)
    (inst / "world" / "DIM-1").mkdir()
    (inst / "world" / "dimensions" / "mod" / "x").mkdir(parents=True)
    (inst / "world_nether").mkdir()  # stale sibling on a vanilla-layout server: not touched
    steps = []
    out = world.reset(sr, "world", "vanilla", expect=["world"], progress=lambda p, m: steps.append((p, m)))
    assert out == {
        "level_name": "world",
        "layout": "vanilla",
        "deleted": ["world"],
        "kept": [],
        "skipped": [],
        "leftover": None,
        "warnings": [],
    }
    assert not (inst / "world").exists() and (inst / "world_nether").exists()
    assert (inst / "mods" / "a.jar").exists() and (inst / "server.properties").read_text() == "motd=x\n"
    assert not leftovers(inst)
    assert steps == [(0.05, "Moving the world aside"), (0.3, "Deleting the old world")]


def test_bukkit_deletes_three_folders(sr, inst):
    for n in ("bruh", "bruh_nether", "bruh_the_end"):
        mk(inst, n)
    out = world.reset(sr, "bruh", "bukkit", expect=["bruh_the_end", "bruh", "bruh_nether"])  # any order
    assert out["deleted"] == ["bruh", "bruh_nether", "bruh_the_end"]
    assert not any((inst / n).exists() for n in ("bruh", "bruh_nether", "bruh_the_end")) and not leftovers(
        inst
    )
    assert snapshot(inst) == {
        "server.properties": ("file", b"motd=x\n"),
        "mods": ("dir",),
        "mods/a.jar": ("file", b"j"),
    }


def test_nested_level_name(sr, inst):
    (inst / "worlds").mkdir()
    mk(inst, "worlds/survival")
    mk(inst, "worlds/survival_nether")
    mk(inst, "worlds/creative")
    out = world.reset(sr, "worlds/survival", "bukkit", expect=["worlds/survival", "worlds/survival_nether"])
    assert (
        out["deleted"] == ["worlds/survival", "worlds/survival_nether"]
        and out["level_name"] == "worlds/survival"
    )
    assert (inst / "worlds" / "creative" / "level.dat").exists() and not (
        inst / "worlds" / "survival"
    ).exists()
    assert not (inst / "worlds" / "survival_nether").exists() and not leftovers(inst)


def test_nested_level_name_with_keep(sr, inst):
    (inst / "worlds").mkdir()
    mk(inst, "worlds/survival")
    (inst / "worlds" / "survival" / "datapacks").mkdir()
    (inst / "worlds" / "survival" / "datapacks" / "t.zip").write_text("dp")
    mk(inst, "worlds/survival_the_end")
    (inst / "worlds" / "survival_the_end" / "paper-world.yml").write_text("e: 1")
    out = world.reset(
        sr,
        "worlds/survival",
        "bukkit",
        expect=["worlds/survival", "worlds/survival_the_end"],
        keep=["datapacks", "config"],
    )
    assert out["kept"] == ["worlds/survival/datapacks", "worlds/survival_the_end/paper-world.yml"]
    assert snapshot(inst / "worlds") == {
        "survival": ("dir",),
        "survival/datapacks": ("dir",),
        "survival/datapacks/t.zip": ("file", b"dp"),
        "survival_the_end": ("dir",),
        "survival_the_end/paper-world.yml": ("file", b"e: 1"),
    }
    assert not leftovers(inst)


def test_links_inside_the_world_are_not_followed(sr, inst, tmp_path):
    mk(inst)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("safe")
    os.symlink(outside, inst / "world" / "link")
    world.reset(sr, "world", "vanilla", expect=["world"])
    assert (outside / "f").read_text() == "safe" and not (inst / "world").exists()


def test_read_only_subfolder_and_fifo_are_deleted(sr, inst):
    mk(inst)
    os.mkfifo(inst / "world" / "pipe")
    ro = inst / "world" / "ro"
    ro.mkdir()
    (ro / "f").write_text("x")
    ro.chmod(0o555)
    try:
        out = world.reset(sr, "world", "vanilla", expect=["world"])
    finally:
        for p in inst.rglob("ro"):
            p.chmod(0o755)
    assert out["deleted"] == ["world"] and not (inst / "world").exists()
    if out["leftover"]:  # not root: the read-only folder could not be emptied
        assert leftovers(inst) == [out["leftover"]] and len(out["warnings"]) == 1
    else:
        assert out["warnings"] == [] and not leftovers(inst)


# --- reset: only folders that look like a world -----------------------------------------------
def test_a_folder_that_is_not_a_world_is_refused(inst):
    """level-name pointing at some other folder (a live map, a plugin's data) must not delete it."""
    (inst / "bluemap" / "web").mkdir(parents=True)
    (inst / "bluemap" / "web" / "index.html").write_text("not a world")
    exc = refused(inst, "not_a_world", "bluemap", "vanilla", expect=["bluemap"])
    assert exc.status == 409 and "Nothing was changed" in str(exc)
    assert (inst / "bluemap" / "web" / "index.html").exists() and not leftovers(inst)


def test_one_folder_that_is_not_a_world_refuses_everything(inst):
    mk(inst, "bruh")
    mk(inst, "bruh_nether")
    (inst / "bruh_the_end").mkdir()
    (inst / "bruh_the_end" / "notes.txt").write_text("mine")
    refused(inst, "not_a_world", "bruh", "bukkit", expect=["bruh", "bruh_nether"])
    assert (inst / "bruh" / "level.dat").exists() and (inst / "bruh_nether" / "level.dat").exists()
    assert not leftovers(inst)


@pytest.mark.parametrize("marker", sorted(world.WORLD_MARKERS))
def test_any_marker_makes_a_world(sr, inst, marker):
    (inst / "world").mkdir()
    target = inst / "world" / marker
    if "." in marker:
        target.write_text("x")
    else:
        target.mkdir()
    out = world.reset(sr, "world", "vanilla", expect=["world"])
    assert out["deleted"] == ["world"] and not (inst / "world").exists()


def test_empty_and_pending_folders_are_skipped(sr, inst):
    (inst / "world" / "datapacks").mkdir(parents=True)
    (inst / "world" / "datapacks" / "a.zip").write_text("dp")  # state after an earlier reset: no world yet
    (inst / "world_nether").mkdir()  # empty
    before = snapshot(inst)
    out = world.reset(sr, "world", "bukkit", expect=[], keep=["datapacks"])
    assert out["deleted"] == [] and sorted(out["skipped"]) == ["world", "world_nether"]
    assert (inst / "world" / "datapacks" / "a.zip").exists() and (inst / "world_nether").exists()
    assert snapshot(inst) == before and not leftovers(inst)


def test_skipped_folders_next_to_a_world(sr, inst):
    mk(inst, "world_nether")
    (inst / "world" / "datapacks").mkdir(parents=True)
    (inst / "world" / "datapacks" / "a.zip").write_text("dp")
    (inst / "world_the_end").mkdir()
    out = world.reset(sr, "world", "bukkit", expect=["world_nether"])
    assert out["deleted"] == ["world_nether"] and out["skipped"] == ["world", "world_the_end"]
    assert (inst / "world" / "datapacks" / "a.zip").exists() and (inst / "world_the_end").is_dir()
    assert not (inst / "world_nether").exists()


def test_no_world_is_a_noop(sr, inst):
    before = snapshot(inst)
    out = world.reset(sr, "world", "bukkit", expect=[])
    assert out["deleted"] == [] and out["skipped"] == [] and out["leftover"] is None
    assert snapshot(inst) == before and not leftovers(inst)


def test_server_folder_that_does_not_exist(tmp_path):
    missing = tmp_path / "never-installed"
    assert world.reset(SafeRoot(missing), "world", "vanilla", expect=[])["deleted"] == []
    with pytest.raises(AgentError) as exc:
        world.reset(SafeRoot(missing), "world", "vanilla", expect=["world"])
    assert exc.value.code == "world_changed" and not missing.exists()


def test_symlinked_world_folder_is_refused(inst, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "level.dat").write_bytes(LEVEL_DAT)
    os.symlink(real, inst / "world")
    exc = refused(inst, "not_a_directory", "world", "vanilla", expect=["world"])
    assert exc.status == 409
    assert (real / "level.dat").exists() and (inst / "world").is_symlink()


def test_symlinked_parent_is_treated_as_missing(sr, inst, tmp_path):
    """A nested level-name whose parent is a link: nothing is followed, nothing is deleted."""
    outside = tmp_path / "outside"
    (outside / "main").mkdir(parents=True)
    (outside / "main" / "level.dat").write_bytes(LEVEL_DAT)
    os.symlink(outside, inst / "worlds")
    out = world.reset(sr, "worlds/main", "vanilla", expect=[])
    assert out["deleted"] == [] and (outside / "main" / "level.dat").exists()
    refused(inst, "world_changed", "worlds/main", "vanilla", expect=["worlds/main"])
    assert (outside / "main" / "level.dat").exists()


def test_file_named_like_the_world_is_refused(inst):
    (inst / "world").write_text("x")
    refused(inst, "not_a_directory", "world", "vanilla", expect=["world"])
    assert (inst / "world").read_text() == "x"


def test_a_file_where_a_dimension_folder_should_be_refuses_everything(inst):
    mk(inst)
    (inst / "world_the_end").write_text("not a folder")
    refused(inst, "not_a_directory", "world", "bukkit", expect=["world"])
    assert (inst / "world" / "level.dat").exists()


# --- reset: what is carried over ----------------------------------------------------------------
def test_keep_datapacks_and_config(sr, inst):
    mk(inst)
    mk(inst, "world_nether")
    (inst / "world" / "datapacks").mkdir()
    (inst / "world" / "datapacks" / "terra.zip").write_text("dp")
    (inst / "world" / "serverconfig").mkdir()
    (inst / "world" / "serverconfig" / "forge-server.toml").write_text("cfg")
    (inst / "world" / "paper-world.yml").write_text("a: 1")
    (inst / "world_nether" / "paper-world.yml").write_text("b: 2")
    steps = []
    out = world.reset(
        sr,
        "world",
        "bukkit",
        expect=["world", "world_nether"],
        keep=["datapacks", "config"],
        progress=lambda p, m: steps.append(m),
    )
    assert sorted(out["kept"]) == [
        "world/datapacks",
        "world/paper-world.yml",
        "world/serverconfig",
        "world_nether/paper-world.yml",
    ]
    assert sorted(p.name for p in (inst / "world").iterdir()) == [
        "datapacks",
        "paper-world.yml",
        "serverconfig",
    ]
    assert (inst / "world" / "datapacks" / "terra.zip").read_text() == "dp"
    assert (inst / "world" / "serverconfig" / "forge-server.toml").read_text() == "cfg"
    assert [p.name for p in (inst / "world_nether").iterdir()] == ["paper-world.yml"]
    assert (inst / "world_nether" / "paper-world.yml").read_text() == "b: 2"
    assert not (inst / "world" / "level.dat").exists() and not leftovers(inst)
    assert steps[0] == "Moving the world aside" and steps[-1] == "Deleting the old world"
    assert "Carrying over what is kept" in steps


@pytest.mark.parametrize(
    ("keep", "left"),
    [
        ([], None),
        (["datapacks"], ["datapacks"]),
        (["config"], ["paper-world.yml", "serverconfig"]),
        (["config", "datapacks", "config"], ["datapacks", "paper-world.yml", "serverconfig"]),
    ],
)
def test_only_the_chosen_options_are_kept(sr, inst, keep, left):
    mk(inst)
    (inst / "world" / "datapacks").mkdir()
    (inst / "world" / "datapacks" / "terra.zip").write_text("dp")
    (inst / "world" / "serverconfig").mkdir()
    (inst / "world" / "serverconfig" / "c.toml").write_text("cfg")
    (inst / "world" / "paper-world.yml").write_text("a: 1")
    out = world.reset(sr, "world", "vanilla", expect=["world"], keep=keep)
    assert out["deleted"] == ["world"]
    if left is None:
        assert out["kept"] == [] and not (inst / "world").exists()
    else:
        assert sorted(out["kept"]) == [f"world/{name}" for name in left]
        assert sorted(p.name for p in (inst / "world").iterdir()) == left
        assert world.classify(sr, "world") == "pending"
    assert not leftovers(inst)


def test_keep_nothing_when_absent_or_empty_or_link(sr, inst, tmp_path):
    mk(inst)
    (inst / "world" / "datapacks").mkdir()  # empty
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("keep me")
    os.symlink(outside, inst / "world" / "serverconfig")  # a link is never carried over, and never followed
    out = world.reset(sr, "world", "vanilla", expect=["world"], keep=["datapacks", "config"])
    assert out["kept"] == [] and not (inst / "world").exists()
    assert (outside / "f").read_text() == "keep me"


def test_bukkit_with_keep_and_a_second_reset_is_a_noop(sr, inst):
    for n in ("bruh", "bruh_nether", "bruh_the_end"):
        mk(inst, n)
    (inst / "bruh" / "datapacks").mkdir()
    (inst / "bruh" / "datapacks" / "t.zip").write_text("dp")
    (inst / "bruh_nether" / "paper-world.yml").write_text("a: 1")
    expect = ["bruh", "bruh_nether", "bruh_the_end"]
    out = world.reset(sr, "bruh", "bukkit", expect=expect, keep=["datapacks", "config"])
    assert out["deleted"] == ["bruh", "bruh_nether", "bruh_the_end"]
    assert sorted(out["kept"]) == ["bruh/datapacks", "bruh_nether/paper-world.yml"]
    assert [p.name for p in (inst / "bruh").iterdir()] == ["datapacks"] and not (
        inst / "bruh_the_end"
    ).exists()
    assert not leftovers(inst)
    # the same request again: the folders it confirmed are no longer worlds
    refused(inst, "world_changed", "bruh", "bukkit", expect=expect, keep=["datapacks", "config"])
    again = world.reset(sr, "bruh", "bukkit", expect=[], keep=["datapacks", "config"])
    assert again["deleted"] == [] and again["kept"] == []
    assert sorted(again["skipped"]) == ["bruh", "bruh_nether"]
    assert (inst / "bruh" / "datapacks" / "t.zip").read_text() == "dp"
    assert (inst / "bruh_nether" / "paper-world.yml").read_text() == "a: 1"


# --- reset: what the user confirmed --------------------------------------------------------------
@pytest.mark.parametrize(
    ("layout", "expect"),
    [
        ("vanilla", []),
        ("vanilla", ["world", "world_nether"]),
        ("vanilla", ["world_nether"]),
        ("vanilla", ["world", "world"]),
        ("vanilla", ["World"]),
        ("vanilla", ["./world"]),
        ("bukkit", ["world"]),
        ("bukkit", ["world", "world_nether", "world_the_end"]),
    ],
)
def test_expect_mismatch_is_world_changed(inst, layout, expect):
    mk(inst)
    mk(inst, "world_nether")
    exc = refused(inst, "world_changed", "world", layout, expect=expect)
    assert exc.status == 409 and "Nothing was changed" in str(exc) and not leftovers(inst)


def test_a_folder_that_is_not_a_world_wins_over_the_expect_check(inst):
    mk(inst)
    (inst / "world_nether").mkdir()
    (inst / "world_nether" / "notes.txt").write_text("mine")
    refused(inst, "not_a_world", "world", "bukkit", expect=["something-else"])


# --- reset: arguments ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["plugins", "../x", ".hidden", "mods", ".", "/", "a/b/c/d/e", "wor\\ld", "", "x" * 201]
)
def test_bad_level_names(inst, name):
    mk(inst)
    exc = refused(inst, "invalid_level_name", name, "vanilla", expect=["world"])
    assert exc.status == 400 and (inst / "mods" / "a.jar").exists()


def test_unknown_layout(inst):
    mk(inst)
    assert refused(inst, "invalid_layout", "world", "spigot", expect=["world"]).status == 400


@pytest.mark.parametrize(
    "keep", [["playerdata"], ["datapacks", "region"], [["datapacks"]], [None], "datapacks"]
)
def test_unknown_keep(inst, keep):
    mk(inst)
    exc = refused(inst, "invalid_keep", "world", "vanilla", expect=["world"], keep=keep)
    assert exc.status == 400 and (inst / "world" / "level.dat").exists()


# --- reset: failures before anything is moved ------------------------------------------------------
def test_mkdir_failure_is_world_reset_failed(inst, monkeypatch):
    mk(inst)
    fail_on_call(monkeypatch, "mkdir", lambda n: True, OSError(30, "Read-only file system"))
    exc = refused(inst, "world_reset_failed", "world", "vanilla", expect=["world"])
    assert exc.status == 500 and "Nothing was changed" in str(exc)
    assert (inst / "world" / "level.dat").exists()


def test_mkdir_that_fails_after_creating_the_folder_leaves_nothing_behind(inst, monkeypatch):
    mk(inst)
    real = SafeRoot.mkdir

    def half(self, rel, **kwargs):
        real(self, rel, **kwargs)
        raise OSError(95, "Operation not supported")  # e.g. the chown of the new folder

    monkeypatch.setattr(SafeRoot, "mkdir", half)
    refused(inst, "world_reset_failed", "world", "vanilla", expect=["world"])
    assert not leftovers(inst)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "Permission denied"),
        PathError("'world' does not exist", "not_found", 404),
        RuntimeError("bug"),
    ],
    ids=["oserror", "patherror", "unexpected"],
)
def test_failure_while_checking_is_world_reset_failed(inst, monkeypatch, error):
    mk(inst)
    before = snapshot(inst)
    monkeypatch.setattr(SafeRoot, "list", lambda self, rel: (_ for _ in ()).throw(error))
    with pytest.raises(AgentError) as exc:
        world.reset(SafeRoot(inst), "world", "vanilla", expect=["world"])
    assert exc.value.code == "world_reset_failed" and "Nothing was changed" in str(exc.value)
    assert snapshot(inst) == before


# --- reset: failures while moving, and the rollback -------------------------------------------------
def test_failure_while_moving_aside_puts_everything_back(inst, monkeypatch):
    for n in ("world", "world_nether", "world_the_end"):
        mk(inst, n, marker=n)
    fail_on_call(monkeypatch, "rename", {3}, OSError(5, "Input/output error"))
    exc = refused(
        inst, "world_reset_failed", "world", "bukkit", expect=["world", "world_nether", "world_the_end"]
    )
    assert exc.status == 500 and "Nothing was changed" in str(exc)
    for n in ("world", "world_nether", "world_the_end"):
        assert (inst / n / "region" / "r.0.0.mca").read_text() == n
    assert not leftovers(inst)


def test_failure_while_carrying_over_puts_everything_back(inst, monkeypatch):
    mk(inst)
    (inst / "world" / "datapacks").mkdir()
    (inst / "world" / "datapacks" / "a.zip").write_text("dp")
    (inst / "world" / "serverconfig").mkdir()
    (inst / "world" / "serverconfig" / "c.toml").write_text("c")
    # 1 = the world goes aside, 2 = datapacks comes back, 3 = serverconfig
    fail_on_call(monkeypatch, "rename", {3}, OSError(28, "No space left on device"))
    refused(inst, "world_reset_failed", "world", "vanilla", expect=["world"], keep=["datapacks", "config"])
    assert (inst / "world" / "level.dat").exists() and (
        inst / "world" / "datapacks" / "a.zip"
    ).read_text() == "dp"
    assert (inst / "world" / "serverconfig" / "c.toml").read_text() == "c" and not leftovers(inst)


def test_failure_creating_the_new_world_folder_puts_everything_back(inst, monkeypatch):
    mk(inst)
    (inst / "world" / "datapacks").mkdir()
    (inst / "world" / "datapacks" / "a.zip").write_text("dp")
    fail_on_call(monkeypatch, "mkdir", {2}, OSError(28, "No space left on device"))  # 1 = the aside folder
    refused(inst, "world_reset_failed", "world", "vanilla", expect=["world"], keep=["datapacks"])
    assert not leftovers(inst)


def test_unexpected_error_while_moving_is_rolled_back_and_typed(inst, monkeypatch):
    for n in ("world", "world_nether"):
        mk(inst, n, marker=n)
    fail_on_call(monkeypatch, "rename", {2}, RuntimeError("bug"))
    refused(inst, "world_reset_failed", "world", "bukkit", expect=["world", "world_nether"])
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "world" and not leftovers(inst)


def test_interrupt_while_moving_puts_the_world_back(sr, inst, monkeypatch):
    for n in ("world", "world_nether"):
        mk(inst, n, marker=n)
    before = snapshot(inst)
    fail_on_call(monkeypatch, "rename", {2}, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        world.reset(sr, "world", "bukkit", expect=["world", "world_nether"])
    assert snapshot(inst) == before


def test_rollback_failure_reports_where_the_world_is(sr, inst, monkeypatch):
    mk(inst)
    mk(inst, "world_nether")
    fail_on_call(monkeypatch, "rename", lambda n: n >= 2, OSError(5, "Input/output error"))
    with pytest.raises(AgentError) as exc:
        world.reset(sr, "world", "bukkit", expect=["world", "world_nether"])
    assert exc.value.code == "world_rollback_failed" and exc.value.code not in UNTOUCHED
    (left,) = leftovers(inst)
    assert left in str(exc.value) and "Nothing was changed" not in str(exc.value)
    assert (inst / left / "overworld" / "level.dat").exists() and (
        inst / "world_nether" / "level.dat"
    ).exists()
    assert not (inst / "world").exists()


def test_unexpected_error_while_putting_back_is_a_rollback_failure(sr, inst, monkeypatch):
    for n in ("world", "world_nether", "world_the_end"):
        mk(inst, n, marker=n)
    real = SafeRoot.rename
    calls = {"n": 0}

    def flaky(self, src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:  # the end cannot be moved aside ...
            raise OSError(5, "Input/output error")
        if calls["n"] == 4:  # ... and the nether cannot be put back
            raise RuntimeError("bug")
        return real(self, src, dst, **kwargs)

    monkeypatch.setattr(SafeRoot, "rename", flaky)
    with pytest.raises(AgentError) as exc:
        world.reset(sr, "world", "bukkit", expect=["world", "world_nether", "world_the_end"])
    assert exc.value.code == "world_rollback_failed"
    (left,) = leftovers(inst)
    assert left in str(exc.value) and "world_nether" in str(exc.value)
    assert (inst / left / "nether" / "region" / "r.0.0.mca").read_text() == "world_nether"
    # the rest of the undo still ran: the overworld is back, the end never left
    assert (inst / "world" / "region" / "r.0.0.mca").read_text() == "world"
    assert (inst / "world_the_end" / "region" / "r.0.0.mca").read_text() == "world_the_end"


def test_a_move_that_fails_after_it_happened_is_not_reported_as_unchanged(sr, inst, monkeypatch):
    """Nothing is recorded for a rename that raised, so nothing is undone: the aside folder gives it away."""
    mk(inst)
    real = SafeRoot.rename

    def lying(self, src, dst, **kwargs):
        real(self, src, dst, **kwargs)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(SafeRoot, "rename", lying)
    with pytest.raises(AgentError) as exc:
        world.reset(sr, "world", "vanilla", expect=["world"])
    assert exc.value.code == "world_rollback_failed"
    (left,) = leftovers(inst)
    assert left in str(exc.value) and (inst / left / "overworld" / "level.dat").exists()


def test_rollback_that_cannot_be_verified_is_a_rollback_failure(sr, inst, monkeypatch):
    """Everything is put back, but the aside folder cannot be read to make sure: not "nothing was changed"."""
    mk(inst)
    mk(inst, "world_nether")
    before = snapshot(inst)
    fail_on_call(monkeypatch, "rename", {2}, OSError(5, "Input/output error"))
    real = SafeRoot.list

    def unreadable_aside(self, rel):
        if rel.startswith(world.STAGING_PREFIX):
            raise OSError(5, "Input/output error")
        return real(self, rel)

    monkeypatch.setattr(SafeRoot, "list", unreadable_aside)
    with pytest.raises(AgentError) as exc:
        world.reset(sr, "world", "bukkit", expect=["world", "world_nether"])
    assert exc.value.code == "world_rollback_failed"
    (left,) = leftovers(inst)
    assert left in str(exc.value) and not any((inst / left).iterdir())
    assert {k: v for k, v in snapshot(inst).items() if k != left} == before


def test_new_world_folder_that_cannot_be_removed_again_is_a_rollback_failure(sr, inst, monkeypatch):
    mk(inst)
    (inst / "world" / "datapacks").mkdir()
    (inst / "world" / "datapacks" / "a.zip").write_text("dp")
    real = SafeRoot.mkdir
    calls = {"n": 0}

    def half(self, rel, **kwargs):
        calls["n"] += 1
        real(self, rel, **kwargs)
        if calls["n"] == 2:  # the new world folder is made, then the call fails
            raise OSError(95, "Operation not supported")

    monkeypatch.setattr(SafeRoot, "mkdir", half)
    with pytest.raises(AgentError) as exc:
        world.reset(sr, "world", "vanilla", expect=["world"], keep=["datapacks"])
    assert exc.value.code == "world_rollback_failed"
    (left,) = leftovers(inst)
    assert (inst / left / "overworld" / "level.dat").exists()
    assert (inst / left / "overworld" / "datapacks" / "a.zip").read_text() == "dp"


# --- reset: after the point of no return -------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        OSError(13, "Permission denied"),
        PathError("Directory is not empty", "not_empty", 409),
        RecursionError("maximum recursion depth exceeded"),
    ],
    ids=["oserror", "patherror", "unexpected"],
)
def test_any_delete_error_is_a_warning_and_the_world_is_gone(sr, inst, monkeypatch, error):
    mk(inst)
    real = SafeRoot.delete

    def stubborn(self, rel, *, recursive=False):
        if recursive:
            raise error
        return real(self, rel, recursive=recursive)

    monkeypatch.setattr(SafeRoot, "delete", stubborn)
    out = world.reset(sr, "world", "vanilla", expect=["world"])
    assert out["deleted"] == ["world"] and not (inst / "world").exists()
    assert len(out["warnings"]) == 1 and out["leftover"] in out["warnings"][0]
    assert leftovers(inst) == [out["leftover"]]
    assert (inst / out["leftover"] / "overworld" / "level.dat").exists()
    # the leftover is what the world info reports (the panel's existing notice finds it)
    assert world.info(sr, "world", "vanilla")["leftovers"] == [out["leftover"]]


# --- operations ---------------------------------------------------------------------------------
def test_world_reset_blocks_power_actions():
    assert "world_reset" in BLOCKS_LIFECYCLE
    reg = OperationRegistry()
    iid = uuid.uuid4()
    release = threading.Event()
    op = reg.start("world_reset", [iid], lambda _op: release.wait(5) and {})
    try:
        with pytest.raises(AgentError) as exc:
            reg.assert_lifecycle_allowed(iid)
        assert exc.value.code == "instance_busy" and exc.value.status == 409
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while op.status in ("pending", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    reg.assert_lifecycle_allowed(iid)


# --- HTTP API -------------------------------------------------------------------------------------
STOPPED = {"exists": True, "status": "exited", "running": False, "restarting": False}
LIVE = [
    pytest.param({"exists": True, "status": "running", "running": True, "restarting": False}, id="running"),
    pytest.param(
        {"exists": True, "status": "restarting", "running": True, "restarting": True}, id="restarting"
    ),
    pytest.param(
        {"exists": True, "status": "running", "running": False, "restarting": True}, id="restarting-flag"
    ),
    pytest.param(
        {"exists": True, "status": "restarting", "running": False, "restarting": False},
        id="restarting-status",
    ),
    pytest.param({"exists": True, "status": "paused", "running": True, "restarting": False}, id="paused"),
]


def _wait(api, auth, op_id, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            return op
        assert time.monotonic() < deadline
        time.sleep(0.05)


def _instance(agent_settings):
    iid = str(uuid.uuid4())
    d = agent_settings.instances_dir / iid
    d.mkdir(parents=True)
    (d / "server.properties").write_text("level-name=world\n")
    return iid, d


def _body(iid, **overrides):
    return {"confirm": iid, "level_name": "world", "layout": "vanilla", "expect": ["world"], **overrides}


def _reset(api, auth, iid, **overrides):
    return api.post(f"/v1/instances/{iid}/world/reset", json=_body(iid, **overrides), headers=auth)


@pytest.fixture
def container(monkeypatch):
    """The container state Docker reports; a test can change it."""
    state = dict(STOPPED)
    monkeypatch.setattr(docker_ops, "status", lambda _iid: dict(state))
    return state


def test_api_reset_deletes_the_world(api, auth, agent_settings, container):
    iid, d = _instance(agent_settings)
    for n in ("world", "world_nether", "world_the_end"):
        mk(d, n)
    (d / "world" / "datapacks").mkdir()
    (d / "world" / "datapacks" / "t.zip").write_text("dp")
    info = api.get(
        f"/v1/instances/{iid}/world",
        params={"level_name": "world", "layout": "bukkit", "sizes": "false"},
        headers=auth,
    ).json()
    assert [f["state"] for f in info["folders"]] == ["world", "world", "world"]
    assert info["folders"][0]["keepable"] == {"datapacks": ["datapacks"]}
    expect = [f["path"] for f in info["folders"] if f["state"] == "world"]

    resp = _reset(api, auth, iid, layout="bukkit", expect=expect, keep=["datapacks", "config"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["operation"]["kind"] == "world_reset"
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    assert op["result"] == {
        "level_name": "world",
        "layout": "bukkit",
        "deleted": ["world", "world_nether", "world_the_end"],
        "kept": ["world/datapacks"],
        "skipped": [],
        "leftover": None,
        "warnings": [],
    }
    assert any("Deleting the old world" in line for line in op["log"])
    assert sorted(p.name for p in d.iterdir()) == ["server.properties", "world"]
    assert (d / "world" / "datapacks" / "t.zip").read_text() == "dp"
    after = api.get(
        f"/v1/instances/{iid}/world", params={"level_name": "world", "layout": "bukkit"}, headers=auth
    ).json()
    assert [f["state"] for f in after["folders"]] == ["pending", "missing", "missing"]
    assert after["leftovers"] == []


def test_api_reset_without_a_container(api, auth, agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"exists": False})
    iid, d = _instance(agent_settings)
    mk(d)
    op = _wait(api, auth, _reset(api, auth, iid).json()["operation"]["id"])
    assert op["status"] == "succeeded" and not (d / "world").exists()


def test_api_confirm_mismatch(api, auth, agent_settings, no_docker):
    iid, d = _instance(agent_settings)
    mk(d)
    before = snapshot(d)
    resp = _reset(api, auth, iid, confirm=str(uuid.uuid4()))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "confirmation_mismatch"
    assert snapshot(d) == before
    assert api.get("/v1/operations", params={"instance_id": iid}, headers=auth).json()["operations"] == []


@pytest.mark.parametrize("name", ["../x", "plugins", ".hidden", "a/b/c/d/e"])
def test_api_bad_level_name_is_refused_before_an_operation_starts(api, auth, agent_settings, no_docker, name):
    iid, d = _instance(agent_settings)
    mk(d)
    resp = _reset(api, auth, iid, level_name=name)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_level_name"
    assert (d / "world" / "level.dat").exists()
    assert api.get("/v1/operations", params={"instance_id": iid}, headers=auth).json()["operations"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"confirm": None},
        {"confirm": "not-a-uuid"},
        {"level_name": None},
        {"level_name": ""},
        {"level_name": "x" * 201},
        {"layout": None},
        {"layout": "spigot"},
        {"expect": None},
        {"expect": []},
        {"expect": ["a", "b", "c", "d"]},
        {"expect": "world"},
        {"keep": ["playerdata"]},
        {"keep": ["datapacks", "config", "datapacks"]},
    ],
    ids=lambda change: "-".join(f"{k}={str(v)[:12]}" for k, v in change.items()),
)
def test_api_request_validation(api, auth, agent_settings, no_docker, change):
    iid, d = _instance(agent_settings)
    mk(d)
    body = {k: v for k, v in {**_body(iid), **change}.items() if v is not None}
    resp = api.post(f"/v1/instances/{iid}/world/reset", json=body, headers=auth)
    assert resp.status_code == 422
    assert (d / "world" / "level.dat").exists()


@pytest.mark.parametrize("live", LIVE)
def test_api_must_stop_before_the_operation(api, auth, agent_settings, container, live):
    iid, d = _instance(agent_settings)
    mk(d)
    before = snapshot(d)
    container.update(live)
    resp = _reset(api, auth, iid)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "must_stop"
    assert snapshot(d) == before
    assert api.get("/v1/operations", params={"instance_id": iid}, headers=auth).json()["operations"] == []
    container.clear()
    container.update(STOPPED)
    op = _wait(api, auth, _reset(api, auth, iid).json()["operation"]["id"])
    assert op["status"] == "succeeded" and not (d / "world").exists()


@pytest.mark.parametrize("live", LIVE)
def test_api_must_stop_inside_the_operation(api, auth, agent_settings, monkeypatch, live):
    """A start that slips in between the endpoint's check and the operation must not lose its world."""
    iid, d = _instance(agent_settings)
    mk(d)
    before = snapshot(d)
    answers = iter([dict(STOPPED), live])
    monkeypatch.setattr(docker_ops, "status", lambda _iid: next(answers))
    resp = _reset(api, auth, iid)
    assert resp.status_code == 200
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "failed" and op["error_code"] == "must_stop"
    assert snapshot(d) == before


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (docker.errors.DockerException("socket gone"), 503, "docker_unavailable"),
        (docker.errors.APIError("500 Server Error", explanation="daemon is busy"), 502, "docker_error"),
        (requests.exceptions.ConnectionError("refused"), 503, "docker_unavailable"),
        (AgentError("not managed by MCPanel", "foreign_container", 409), 409, "foreign_container"),
    ],
    ids=["unavailable", "api-error", "connection", "foreign-container"],
)
def test_api_docker_error_before_the_operation(api, auth, agent_settings, monkeypatch, error, status, code):
    iid, d = _instance(agent_settings)
    mk(d)
    before = snapshot(d)
    monkeypatch.setattr(docker_ops, "status", lambda _iid: (_ for _ in ()).throw(error))
    resp = _reset(api, auth, iid)
    assert resp.status_code == status and resp.json()["error"]["code"] == code
    assert snapshot(d) == before
    assert api.get("/v1/operations", params={"instance_id": iid}, headers=auth).json()["operations"] == []


@pytest.mark.parametrize(
    "error",
    [
        docker.errors.DockerException("socket gone"),
        docker.errors.APIError("500 Server Error", explanation="daemon is busy"),
        requests.exceptions.ConnectionError("refused"),
        AgentError("not managed by MCPanel", "foreign_container", 409),
        RuntimeError("bug"),
    ],
    ids=["unavailable", "api-error", "connection", "foreign-container", "unexpected"],
)
def test_api_docker_error_inside_the_operation(api, auth, agent_settings, monkeypatch, error):
    iid, d = _instance(agent_settings)
    mk(d)
    before = snapshot(d)
    calls = {"n": 0}

    def status(_iid):
        calls["n"] += 1
        if calls["n"] > 1:
            raise error
        return dict(STOPPED)

    monkeypatch.setattr(docker_ops, "status", status)
    resp = _reset(api, auth, iid)
    assert resp.status_code == 200
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "failed" and op["error_code"] == "world_reset_failed"
    assert "Nothing was changed" in op["error"] and str(error) in op["error"]
    assert snapshot(d) == before and calls["n"] == 2


@pytest.mark.parametrize(
    ("setup", "body", "code"),
    [
        ("world", {"expect": ["world", "world_nether"]}, "world_changed"),
        ("none", {}, "world_changed"),
        ("other", {"level_name": "bluemap", "expect": ["bluemap"]}, "not_a_world"),
        ("file", {}, "not_a_directory"),
    ],
)
def test_api_refused_reset_reports_its_code(api, auth, agent_settings, container, setup, body, code):
    iid, d = _instance(agent_settings)
    if setup == "world":
        mk(d)
    elif setup == "other":
        (d / "bluemap" / "web").mkdir(parents=True)
        (d / "bluemap" / "web" / "index.html").write_text("not a world")
    elif setup == "file":
        (d / "world").write_text("x")
    before = snapshot(d)
    resp = _reset(api, auth, iid, **body)
    assert resp.status_code == 200
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "failed" and op["error_code"] == code and code in UNTOUCHED
    assert snapshot(d) == before


def test_api_failure_after_the_world_is_gone_is_not_an_untouched_code(
    api, auth, agent_settings, container, monkeypatch
):
    iid, d = _instance(agent_settings)
    mk(d)
    mk(d, "world_nether")
    fail_on_call(monkeypatch, "rename", lambda n: n >= 2, OSError(5, "Input/output error"))
    resp = _reset(api, auth, iid, layout="bukkit", expect=["world", "world_nether"])
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "failed" and op["error_code"] == "world_rollback_failed"
    (left,) = leftovers(d)
    assert left in op["error"] and (d / left / "overworld" / "level.dat").exists()


def test_api_power_actions_are_refused_while_the_reset_runs(
    api, auth, agent_settings, container, monkeypatch
):
    iid, d = _instance(agent_settings)
    mk(d)
    entered, release = threading.Event(), threading.Event()
    real = world.reset

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a power action reached Docker during a world reset")

    monkeypatch.setattr(world, "reset", slow)
    for name in ("start", "stop", "remove_container"):
        monkeypatch.setattr(docker_ops, name, forbidden)
    resp = _reset(api, auth, iid)
    assert resp.status_code == 200
    try:
        assert entered.wait(5)
        for action in ("start", "stop", "restart"):
            r = api.post(f"/v1/instances/{iid}/{action}", headers=auth)
            assert r.status_code == 409 and r.json()["error"]["code"] == "instance_busy", action
        r = api.delete(f"/v1/instances/{iid}/container", headers=auth)
        assert r.status_code == 409 and r.json()["error"]["code"] == "instance_busy"
        again = _reset(api, auth, iid)  # one operation per server
        assert again.status_code == 409 and again.json()["error"]["code"] == "instance_busy"
        status = api.get(f"/v1/instances/{iid}/status", headers=auth).json()
        assert status["operation"]["kind"] == "world_reset" and status["operation"]["status"] == "running"
        assert (d / "world" / "level.dat").exists()
    finally:
        release.set()
    op = _wait(api, auth, resp.json()["operation"]["id"])
    assert op["status"] == "succeeded" and not (d / "world").exists()
    assert api.get(f"/v1/instances/{iid}/status", headers=auth).json()["operation"] is None
