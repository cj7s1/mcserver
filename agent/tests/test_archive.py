import io
import os
import tarfile
import zipfile

import pytest

from mcagent.archive import extract
from mcagent.docker_ops import AgentError
from mcagent.safefs import SafeRoot


@pytest.fixture
def root(tmp_path):
    inst = tmp_path / "inst"
    inst.mkdir()
    return SafeRoot(inst), inst, tmp_path


def _zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_zip_extract_with_auto_strip(root):
    sr, inst, _ = root
    _zip(
        inst / "upload.zip",
        {
            "myserver/server.properties": "motd=x\n",
            "myserver/world/level.dat": b"\x00\x01",
            "myserver/mods/": "",
        },
    )
    result = extract(sr, "upload.zip")
    assert result["stripped_prefix"] == "myserver"
    assert (inst / "server.properties").read_text() == "motd=x\n"
    assert (inst / "world" / "level.dat").read_bytes() == b"\x00\x01"
    assert (inst / "mods").is_dir()


def test_zip_slip_and_absolute_paths_are_skipped(root):
    sr, inst, tmp = root
    _zip(inst / "evil.zip", {"../escape.txt": "x", "/etc/passwd_copy": "y", "ok.txt": "z"})
    result = extract(sr, "evil.zip", strip="none")
    assert (inst / "ok.txt").exists()
    assert not (tmp / "escape.txt").exists()
    assert result["skipped_count"] == 1  # '..' path; leading '/' is treated as relative
    assert (inst / "etc" / "passwd_copy").exists()


def test_tar_symlinks_and_devices_not_created(root):
    sr, inst, tmp = root
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = str(tmp)
        tf.addfile(link)
        data = b"hello"
        info = tarfile.TarInfo("dir/file.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        fifo = tarfile.TarInfo("fifo")
        fifo.type = tarfile.FIFOTYPE
        tf.addfile(fifo)
    (inst / "a.tar.gz").write_bytes(buf.getvalue())
    result = extract(sr, "a.tar.gz", strip="none")
    assert not os.path.lexists(inst / "link")
    assert not os.path.lexists(inst / "fifo")
    assert (inst / "dir" / "file.txt").read_bytes() == b"hello"
    assert result["skipped_count"] == 2


def test_existing_files_kept_unless_overwrite(root):
    sr, inst, _ = root
    (inst / "server.properties").write_text("original")
    _zip(inst / "u.zip", {"server.properties": "new"})
    r1 = extract(sr, "u.zip", strip="none")
    assert (inst / "server.properties").read_text() == "original"
    assert r1["existing_count"] == 1
    extract(sr, "u.zip", strip="none", overwrite=True)
    assert (inst / "server.properties").read_text() == "new"


def test_zip_bomb_ratio_rejected(root):
    sr, inst, _ = root
    _zip(inst / "bomb.zip", {"big.bin": b"\x00" * (8 * 1024 * 1024)})
    with pytest.raises(AgentError) as exc:
        extract(sr, "bomb.zip", strip="none")
    assert exc.value.code == "archive_bomb"
    assert not (inst / "big.bin").exists()


def test_destination_subdirectory(root):
    sr, inst, _ = root
    _zip(inst / "w.zip", {"world/level.dat": "x"})
    extract(sr, "w.zip", "imports", strip="none")
    assert (inst / "imports" / "world" / "level.dat").exists()


def test_unsupported_and_corrupt(root):
    sr, inst, _ = root
    (inst / "x.bin").write_bytes(b"not an archive at all")
    with pytest.raises(AgentError) as exc:
        extract(sr, "x.bin")
    assert exc.value.code == "unsupported_archive"
    (inst / "c.zip").write_bytes(b"PK\x03\x04garbage")
    with pytest.raises(AgentError):
        extract(sr, "c.zip")


def test_zip_symlink_entry_skipped(root):
    sr, inst, tmp = root
    with zipfile.ZipFile(inst / "l.zip", "w") as zf:
        info = zipfile.ZipInfo("evil-link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, str(tmp))
        zf.writestr("fine.txt", "ok")
    result = extract(sr, "l.zip", strip="none")
    assert not os.path.lexists(inst / "evil-link")
    assert result["skipped_count"] == 1


def test_stream_zip_roundtrip_skips_symlinks(root):
    from mcagent.archive import stream_zip

    sr, inst, tmp = root
    (inst / "world" / "region").mkdir(parents=True)
    (inst / "world" / "level.dat").write_bytes(b"L" * 5000)
    (inst / "world" / "region" / "r.0.0.mca").write_bytes(b"\x02" * 70000)
    os.symlink(tmp, inst / "world" / "escape")
    data = b"".join(stream_zip(sr, "world"))
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(zf.namelist()) == ["world/level.dat", "world/region/r.0.0.mca"]
    assert zf.read("world/region/r.0.0.mca") == b"\x02" * 70000
    # the exported zip can be imported back with the safe extractor
    (inst / "export.zip").write_bytes(data)
    extract(sr, "export.zip", "copy", strip="none")
    assert (inst / "copy" / "world" / "level.dat").read_bytes() == b"L" * 5000


def test_tar_member_of_unknown_type_counts_towards_the_size_limit(tmp_path, monkeypatch):
    """B-8 (security review 2026-11-11): tarfile decompresses the data of a member of an unknown type
    (it skips it by its declared size), so that size counts towards MAX_TOTAL_BYTES."""
    from mcagent import archive

    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1024 * 1024)
    data = bytes(8 * 1024 * 1024)
    with tarfile.open(tmp_path / "import.tar.gz", "w:gz") as tf:
        info = tarfile.TarInfo("blob")
        info.type = b"Z"  # not a type tarfile knows: its data is skipped by size when it is read
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(AgentError) as err:
        extract(SafeRoot(tmp_path), "import.tar.gz", "out")
    assert err.value.code == "archive_too_large"
