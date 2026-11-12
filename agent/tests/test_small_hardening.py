"""B-13 of the security review (2026-11-11): small hardening items of the agent.

* a rename without ``overwrite`` never replaces an entry that appears between a check and the rename
  (``renameat2`` with ``RENAME_NOREPLACE``);
* folders deeper than 64 levels are not left out silently (zip export, chown);
* RCON continuation packets must carry the command's id;
* the restic password file is created 0600 (never readable by others in between).
"""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from mcagent import archive, config, rcon, safefs
from mcagent.errors import AgentError
from mcagent.safefs import PathError, SafeRoot


# --- rename without replacing ---------------------------------------------------------------------------
def test_rename_does_not_replace_an_existing_entry(tmp_path):
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    with pytest.raises(PathError) as err:
        SafeRoot(tmp_path).rename("a", "b")
    assert err.value.code == "exists" and (tmp_path / "b").read_text() == "b"
    SafeRoot(tmp_path).rename("a", "c")
    assert (tmp_path / "c").read_text() == "a" and not (tmp_path / "a").exists()


def test_rename_is_one_step_when_the_system_can(tmp_path, monkeypatch):
    if safefs._renameat2 is None:
        pytest.skip("no renameat2 in this libc")
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")

    def no_stat(*_a, **_k):  # the check-then-rename path must not be taken
        raise AssertionError("os.stat used for the destination")

    real_stat = os.stat
    monkeypatch.setattr(
        safefs.os, "stat", lambda p, *a, **k: real_stat(p, *a, **k) if p == "a" else no_stat()
    )
    with pytest.raises(PathError) as err:
        SafeRoot(tmp_path).rename("a", "b")
    assert err.value.code == "exists"


def test_rename_falls_back_without_renameat2(tmp_path, monkeypatch):
    monkeypatch.setattr(safefs, "_renameat2", None)
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    with pytest.raises(PathError):
        SafeRoot(tmp_path).rename("a", "b")
    SafeRoot(tmp_path).rename("a", "c")
    assert (tmp_path / "c").exists()


# --- depth ----------------------------------------------------------------------------------------------
def _deep(tmp_path, levels: int):
    path = tmp_path
    for i in range(levels):
        path = path / f"d{i}"
    path.mkdir(parents=True)
    (path / "f.txt").write_text("deep")
    return path


def test_a_zip_with_folders_too_deep_is_refused(tmp_path):
    _deep(tmp_path, 70)
    with pytest.raises(AgentError) as err:
        archive.stream_zip(SafeRoot(tmp_path), "")
    assert err.value.code == "archive_too_large" and "64 levels" in str(err.value)
    shallow = tmp_path / "ok"
    shallow.mkdir()
    (shallow / "a.txt").write_text("a")
    assert b"".join(archive.stream_zip(SafeRoot(tmp_path), "ok"))[:2] == b"PK"


def test_chown_tree_reports_what_it_left_out(tmp_path):
    _deep(tmp_path, 70)
    too_deep: list[str] = []
    SafeRoot(tmp_path).chown_tree(os.getuid(), os.getgid(), too_deep=too_deep)
    assert len(too_deep) == 1 and too_deep[0].count("/") == 64


# --- RCON -----------------------------------------------------------------------------------------------
def test_an_rcon_continuation_with_another_id_is_refused(monkeypatch):
    client = rcon.RconClient("127.0.0.1", 1, "pw", timeout=1.0)
    packets = []

    async def send(ptype, payload):
        return 41

    async def read(timeout):
        return packets.pop(0)

    monkeypatch.setattr(client, "_send", send)
    monkeypatch.setattr(client, "_read_packet", read)
    packets[:] = [(41, 0, "x" * rcon.FRAGMENT_THRESHOLD), (99, 0, "forged")]
    with pytest.raises(rcon.RconError):
        asyncio.run(client.command("list", timeout=5))
    packets[:] = [(41, 0, "x" * rcon.FRAGMENT_THRESHOLD), (41, 0, "end")]
    assert asyncio.run(client.command("list", timeout=5)).endswith("end")


# --- the restic password file ---------------------------------------------------------------------------
def test_the_generated_restic_password_file_is_private_from_the_start(tmp_path, monkeypatch):
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    modes = []
    real_open = os.open

    def recording_open(path, flags, mode=0o777, *a, **k):
        if ".restic-password." in str(path) and str(path).endswith(".tmp"):
            modes.append(mode)
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(config.os, "open", recording_open)
    settings = config.Settings(token="t" * 32, data_root=tmp_path / "data", restic_password="")
    pw = settings.resolve_restic_password()
    path = settings.state_dir / "restic-password"
    assert path.read_text() == pw and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert modes == [0o600]
