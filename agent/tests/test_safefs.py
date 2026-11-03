import os

import pytest

from mcagent.safefs import PathError, SafeRoot, split_path


@pytest.fixture
def root(tmp_path):
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / "world").mkdir()
    (inst / "server.properties").write_text("motd=hello\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret")
    return SafeRoot(inst), inst, outside


def test_split_path_rejects_parent_refs():
    with pytest.raises(PathError):
        split_path("../etc/passwd")
    with pytest.raises(PathError):
        split_path("world/../../x")
    assert split_path("/world//region/") == ["world", "region"]
    assert split_path("") == []


def test_split_path_rejects_nul():
    with pytest.raises(PathError):
        split_path("a\x00b")


def test_list_and_read(root):
    sr, _inst, _ = root
    names = [e.name for e in sr.list("")]
    assert names == ["world", "server.properties"]
    assert sr.read_bytes("server.properties", 1024) == b"motd=hello\n"


def test_symlink_file_not_followed(root):
    sr, inst, outside = root
    os.symlink(outside / "secret.txt", inst / "link.txt")
    entry = next(e for e in sr.list("") if e.name == "link.txt")
    assert entry.type == "symlink"
    with pytest.raises(PathError) as exc:
        sr.read_bytes("link.txt", 1024)
    assert exc.value.code == "symlink"


def test_symlink_dir_not_traversed(root):
    sr, inst, outside = root
    os.symlink(outside, inst / "escape")
    with pytest.raises(PathError):
        sr.list("escape")
    with pytest.raises(PathError):
        sr.read_bytes("escape/secret.txt", 1024)
    with pytest.raises(PathError):
        sr.write_bytes("escape/new.txt", b"x")
    assert not (outside / "new.txt").exists()


def test_write_replaces_symlink_not_target(root):
    sr, inst, outside = root
    os.symlink(outside / "secret.txt", inst / "victim.txt")
    sr.write_bytes("victim.txt", b"replaced")
    assert (outside / "secret.txt").read_text() == "host secret"
    assert not (inst / "victim.txt").is_symlink()
    assert (inst / "victim.txt").read_text() == "replaced"


def test_delete_recursive_does_not_follow_links(root):
    sr, inst, outside = root
    (inst / "world" / "region").mkdir()
    os.symlink(outside, inst / "world" / "region" / "link")
    sr.delete("world", recursive=True)
    assert not (inst / "world").exists()
    assert (outside / "secret.txt").exists()


def test_mkdir_rename_delete(root):
    sr, inst, _ = root
    sr.mkdir("plugins/config", parents=True)
    assert (inst / "plugins" / "config").is_dir()
    sr.write_bytes("plugins/a.jar", b"jar")
    sr.rename("plugins/a.jar", "plugins/b.jar")
    assert (inst / "plugins" / "b.jar").exists()
    with pytest.raises(PathError):
        sr.rename("plugins", "plugins/config/inner")
    with pytest.raises(PathError):
        sr.delete("plugins")  # not empty, not recursive
    sr.delete("plugins", recursive=True)
    assert not (inst / "plugins").exists()


def test_root_operations_refused(root):
    sr, _inst, _ = root
    with pytest.raises(PathError):
        sr.delete("")
    with pytest.raises(PathError):
        sr.write_bytes("/", b"x")


def test_write_size_limit_aborts_cleanly(root):
    sr, inst, _ = root
    with pytest.raises(PathError):
        sr.write_stream("big.bin", iter([b"a" * 10, b"b" * 10]), max_bytes=15)
    assert not (inst / "big.bin").exists()
    assert not [p for p in inst.iterdir() if p.name.startswith(".mcpanel-tmp")]


def test_no_overwrite(root):
    sr, _inst, _ = root
    with pytest.raises(PathError):
        sr.write_bytes("server.properties", b"x", overwrite=False)


def test_search(root):
    sr, _inst, _ = root
    results = sr.search("", "hello")
    assert results and results[0]["path"] == "server.properties"
