"""B-7 of the security review (2026-11-11): a folder that holds a protected path cannot be deleted, renamed
away or replaced through the file API, and the compare folds case (a case-insensitive file system has one
entry for both spellings).

Adopted from the reviewers' tests (audit-2026-11-09/security-review/b-agent-fs/tests/test_b_file_api_guards.py;
its B-1 test of mounted sub-folders belongs to round 2 and is not here yet).
"""

from __future__ import annotations

import uuid

import pytest

from mcagent.claimmarker import MARKER, GuardedRoot, is_managed
from mcagent.profiles import cs2
from mcagent.safefs import PathError


def _guarded(path, protected):
    return GuardedRoot(path, None, None, settings=None, instance_id=uuid.uuid4(), protected=protected)


def _cs2_folder(tmp_path):
    cfg = tmp_path / "game" / "csgo" / "cfg"
    cfg.mkdir(parents=True)
    (cfg / "server.cfg").write_text("// generated\n")
    return cfg


def test_folder_that_holds_a_protected_file_cannot_be_deleted(tmp_path):
    cfg = _cs2_folder(tmp_path)
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    for rel in ("game/csgo/cfg", "game", "GAME/csgo"):
        with pytest.raises(PathError) as err:
            root.delete(rel, recursive=True)
        assert err.value.code == "managed_file"
    assert (cfg / "server.cfg").exists()


def test_folder_that_holds_a_protected_file_cannot_be_renamed_away_or_replaced(tmp_path):
    cfg = _cs2_folder(tmp_path)
    other = tmp_path / "upload"
    other.mkdir()
    (other / "server.cfg").write_text("// not generated\n")
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    with pytest.raises(PathError) as err:
        root.rename("game/csgo/cfg", "game/csgo/cfg.old")
    assert err.value.code == "managed_file"
    with pytest.raises(PathError):
        root.rename("upload", "game/csgo/cfg", overwrite=True)
    with pytest.raises(PathError):
        root.rename("upload/server.cfg", "game/csgo/cfg/SERVER.CFG", overwrite=True)
    assert (cfg / "server.cfg").read_text() == "// generated\n"


def test_a_file_cannot_take_the_place_of_a_folder_that_holds_one(tmp_path):
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    with pytest.raises(PathError) as err:
        root.open_writer("game/csgo")
    assert err.value.code == "managed_file"
    with pytest.raises(PathError):
        root.open_writer("Pre.sh")


def test_what_lies_beside_a_protected_path_stays_writable(tmp_path):
    cfg = _cs2_folder(tmp_path)
    root = _guarded(tmp_path, cs2.PROFILE.protected_paths)
    root.mkdir("game/csgo/maps", parents=True)  # a folder above a protected path may be made
    writer = root.open_writer("game/csgo/cfg/other.cfg")
    writer.write(b"x")
    writer.commit()
    root.rename("game/csgo/cfg/other.cfg", "game/csgo/cfg/other2.cfg")
    root.delete("game/csgo/cfg/other2.cfg")
    assert (cfg / "server.cfg").exists() and not (cfg / "other2.cfg").exists()


def test_the_claim_marker_is_matched_without_case():
    assert is_managed(MARKER) and is_managed(MARKER.upper()) and is_managed("./" + MARKER.title())
    assert not is_managed(MARKER + ".bak")
