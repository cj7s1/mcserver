"""Extract never writes the files the panel protects (feature ``extract_protected``).

A real-container gate (logs 71/72) extracted a zip with ``config/rconpw`` (Factorio) or
``.mcpanel/api-token`` (Satisfactory) plus another file into the server root with overwrite on, and
the extract replaced the secret file. The panel now names the protected paths; the agent skips every
entry that lands on one (or inside a protected folder), after its own normalisation and strip.
"""

from __future__ import annotations

import io
import tarfile
import uuid
import zipfile

import pytest

from mcagent import docker_ops, main
from mcagent.archive import extract
from mcagent.docker_ops import AgentError
from mcagent.safefs import SafeRoot

from .test_security_audit import _wait_op

FACTORIO = ["config/rconpw"]
SATISFACTORY = [".mcpanel", "saved/ServerSettings.7777.sav"]


@pytest.fixture
def root(tmp_path):
    inst = tmp_path / "inst"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "rconpw").write_text("panel-secret\n")
    (inst / ".mcpanel").mkdir()
    (inst / ".mcpanel" / "api-token").write_text("panel-token\n")
    (inst / "saved").mkdir()
    (inst / "saved" / "ServerSettings.7777.sav").write_bytes(b"CLAIM")
    return SafeRoot(inst), inst


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _unchanged(inst) -> None:
    assert (inst / "config" / "rconpw").read_text() == "panel-secret\n"
    assert (inst / ".mcpanel" / "api-token").read_text() == "panel-token\n"
    assert (inst / "saved" / "ServerSettings.7777.sav").read_bytes() == b"CLAIM"


def test_the_gate_archives_do_not_replace_the_secret_files(root):
    sr, inst = root
    # the gate's zips: the secret file plus another file, no common folder, overwrite on
    _zip(inst / "f.zip", {"config/rconpw": "attacker\n", "readme.txt": "x"})
    result = extract(sr, "f.zip", overwrite=True, protected=FACTORIO)
    assert (inst / "readme.txt").read_text() == "x"  # the rest is extracted
    assert result["protected_count"] == 1 and result["protected"] == ["config/rconpw"]
    assert result["skipped_count"] == 0  # (not counted as an unsafe entry)
    _zip(
        inst / "s.zip", {".mcpanel/api-token": "attacker\n", ".mcpanel/claim-verified": "1", "other.txt": "y"}
    )
    result = extract(sr, "s.zip", overwrite=True, protected=SATISFACTORY)
    assert result["protected_count"] == 2 and (inst / "other.txt").read_text() == "y"
    assert not (inst / ".mcpanel" / "claim-verified").exists()  # the whole panel folder is protected
    _unchanged(inst)


@pytest.mark.parametrize(
    "name",
    [
        "./config/rconpw",
        "config//rconpw",
        "/config/rconpw",
        "config\\rconpw",
        "CONFIG/RconPW",
        "config/./rconpw",
        "./.mcpanel/api-token",
        ".MCPANEL/api-token",
        "saved/serversettings.7777.sav",
        ".mcpanel/new-file",
        "config/rconpw/inside",  # would make the secret file a folder
    ],
)
def test_path_tricks_land_on_the_protected_path_and_are_skipped(root, name):
    sr, inst = root
    _zip(inst / "t.zip", {name: "attacker\n", "x.txt": "x"})
    result = extract(sr, "t.zip", strip="none", overwrite=True, protected=FACTORIO + SATISFACTORY)
    assert result["protected_count"] == 1, result
    assert not (inst / "config" / "rconpw").is_dir()
    _unchanged(inst)


def test_a_stripped_common_folder_cannot_bring_an_entry_onto_a_secret(root):
    sr, inst = root
    _zip(
        inst / "p.zip",
        {"x/.mcpanel/api-token": "attacker\n", "x/config/rconpw": "attacker\n", "x/ok.txt": "ok"},
    )
    result = extract(sr, "p.zip", overwrite=True, protected=FACTORIO + SATISFACTORY)
    assert result["stripped_prefix"] == "x" and result["protected_count"] == 2
    assert (inst / "ok.txt").read_text() == "ok"
    _unchanged(inst)


def test_a_destination_inside_a_protected_folder_writes_nothing_there(root):
    sr, inst = root
    _zip(inst / "d.zip", {"api-token": "attacker\n", "claim-verified": "1"})
    result = extract(sr, "d.zip", ".mcpanel", overwrite=True, protected=SATISFACTORY)
    assert result["protected_count"] == 2 and sorted((inst / ".mcpanel").iterdir()) == [
        inst / ".mcpanel" / "api-token"
    ]
    _zip(inst / "c.zip", {"rconpw": "attacker\n"})
    assert extract(sr, "c.zip", "Config", overwrite=True, protected=FACTORIO)["protected_count"] == 1
    _unchanged(inst)


def test_a_file_entry_cannot_take_the_place_of_a_protected_folder(tmp_path):
    inst = tmp_path / "fresh"
    inst.mkdir()
    sr = SafeRoot(inst)  # before the first apply: no .mcpanel/ and no config/ yet
    _zip(inst / "a.zip", {".mcpanel": "a file", "config": "a file", "keep/me.txt": "k"})
    result = extract(sr, "a.zip", strip="none", protected=FACTORIO + SATISFACTORY)
    assert result["protected_count"] == 2
    assert not (inst / ".mcpanel").exists() and not (inst / "config").exists()
    assert (inst / "keep" / "me.txt").read_text() == "k"
    # a folder entry above a protected file is harmless and created
    _zip(inst / "b.zip", {"config/": "", "config/server-settings.json": "{}"})
    assert extract(sr, "b.zip", strip="none", protected=FACTORIO)["protected_count"] == 0
    assert (inst / "config" / "server-settings.json").read_text() == "{}"


def test_tar_archives_are_checked_the_same_way(root):
    sr, inst = root
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("srv/config/rconpw", b"attacker\n"), ("srv/mods.txt", b"m")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    (inst / "a.tar.gz").write_bytes(buf.getvalue())
    result = extract(sr, "a.tar.gz", overwrite=True, protected=FACTORIO)
    assert result["protected_count"] == 1 and (inst / "mods.txt").read_bytes() == b"m"
    _unchanged(inst)


def test_without_protected_paths_nothing_changes(root):
    """Minecraft (the panel sends no list): an extract writes what it always wrote."""
    sr, inst = root
    _zip(inst / "m.zip", {"config/rconpw": "mine\n", "server.properties": "motd=x\n"})
    result = extract(sr, "m.zip", overwrite=True)
    assert (inst / "config" / "rconpw").read_text() == "mine\n"
    assert result["protected_count"] == 0 and result["protected"] == []


@pytest.mark.parametrize("bad", ["/etc", "../x", "", ".", "a/../b", "x\x00y"])
def test_invalid_protected_paths_are_refused(root, bad):
    sr, inst = root
    _zip(inst / "z.zip", {"a.txt": "a"})
    with pytest.raises(AgentError) as exc:
        extract(sr, "z.zip", protected=[bad])
    assert exc.value.code == "invalid_path"
    assert not (inst / "a.txt").exists()


# -- the endpoint ---------------------------------------------------------------------------------------
@pytest.fixture
def server(agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": False})
    iid = uuid.uuid4()
    inst = agent_settings.instances_dir / str(iid)
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "rconpw").write_text("panel-secret\n")
    return iid, inst


def test_the_endpoint_skips_protected_entries_and_reports_them(api, auth, server):
    iid, inst = server
    _zip(inst / ".mcpanel-import-0123abcd.zip", {"config/rconpw": "attacker\n", "other.txt": "o"})
    r = api.post(
        f"/v1/instances/{iid}/files/extract",
        json={
            "path": ".mcpanel-import-0123abcd.zip",
            "overwrite": True,
            "delete_archive": True,
            "protected": FACTORIO,
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    assert op["result"]["protected_count"] == 1 and op["result"]["protected"] == ["config/rconpw"]
    assert (inst / "config" / "rconpw").read_text() == "panel-secret\n" and (
        inst / "other.txt"
    ).read_text() == "o"
    assert "attacker" not in str(op)


def test_the_endpoint_refuses_an_invalid_list_before_it_starts(api, auth, server):
    iid, inst = server
    _zip(inst / "u.zip", {"a.txt": "a"})
    r = api.post(
        f"/v1/instances/{iid}/files/extract", json={"path": "u.zip", "protected": ["../x"]}, headers=auth
    )
    assert r.status_code == 400 and not (inst / "a.txt").exists()


def test_the_feature_is_announced():
    assert "extract_protected" in main.FEATURES
