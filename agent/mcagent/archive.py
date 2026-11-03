"""Safe extraction of uploaded archives (zip / tar / tar.gz / tar.xz) inside an instance.

Used to import existing servers, worlds and manually downloaded server packs. Every
member goes through the same confined :class:`SafeRoot` writer as uploads, so:

* absolute paths, ``..`` components and NUL bytes are rejected,
* symlinks, hard links, devices and FIFOs inside the archive are skipped (never created),
* total uncompressed size, file count and per-file compression ratio are bounded
  (zip-bomb protection),
* existing files are only replaced when ``overwrite`` is set.
"""

from __future__ import annotations

import stat
import tarfile
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import IO

from .docker_ops import AgentError
from .safefs import PathError, SafeRoot, split_path

MAX_TOTAL_BYTES = 64 * 1024 * 1024 * 1024  # 64 GiB uncompressed
MAX_FILES = 500_000
MAX_RATIO = 200  # uncompressed/compressed per member (zip only)
CHUNK = 1024 * 1024


@dataclass
class ExtractStats:
    files: int = 0
    directories: int = 0
    bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files": self.files,
            "directories": self.directories,
            "bytes": self.bytes,
            "skipped": self.skipped[:200],
            "skipped_count": len(self.skipped),
            "existing_count": len(self.existing),
            "existing": self.existing[:200],
        }


@dataclass
class Member:
    name: str
    kind: str  # file | dir | other
    size: int
    compressed: int | None
    open: Callable[[], IO[bytes]] | None


def _zip_members(zf: zipfile.ZipFile) -> Iterator[Member]:
    for info in zf.infolist():
        mode = (info.external_attr >> 16) & 0xFFFF
        if info.is_dir():
            kind = "dir"
        elif stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            kind = "other"  # symlink, device, fifo... (type bits present and not a regular file)
        else:
            kind = "file"
        yield Member(
            info.filename,
            kind,
            info.file_size,
            info.compress_size,
            (lambda i=info: zf.open(i)) if kind == "file" else None,
        )


def _tar_members(tf: tarfile.TarFile) -> Iterator[Member]:
    for info in tf:
        if info.isdir():
            yield Member(info.name, "dir", 0, None, None)
        elif info.isreg():
            yield Member(info.name, "file", info.size, None, lambda i=info: tf.extractfile(i))  # type: ignore[misc]
        else:
            yield Member(info.name, "other", 0, None, None)


def _common_root(names: list[str]) -> str | None:
    """If every entry lives under one top-level directory, return it (for strip=auto)."""
    roots = set()
    for n in names:
        try:
            parts = split_path(n)
        except PathError:
            continue
        if not parts:
            continue
        if len(parts) == 1 and not n.endswith("/"):
            return None  # a file at top level
        roots.add(parts[0])
        if len(roots) > 1:
            return None
    return roots.pop() if len(roots) == 1 else None


def _open_archive(fh: IO[bytes]) -> tuple[str, object]:
    head = fh.read(8)
    fh.seek(0)
    if head.startswith(b"PK"):
        return "zip", zipfile.ZipFile(fh)
    try:
        return "tar", tarfile.open(fileobj=fh, mode="r:*")
    except tarfile.TarError as exc:
        raise AgentError(
            "Unsupported archive format (use .zip, .tar, .tar.gz or .tar.xz)", "unsupported_archive", 415
        ) from exc


def extract(
    root: SafeRoot,
    archive: str,
    destination: str = "",
    *,
    strip: str = "auto",
    overwrite: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    dest_parts = split_path(destination)
    with root.open_read(archive) as (fh, _size):
        try:
            kind, arc = _open_archive(fh)
        except (zipfile.BadZipFile, EOFError) as exc:
            raise AgentError(f"The archive is corrupt or truncated: {exc}", "archive_corrupt", 400) from exc
        try:
            return _extract_members(
                root, kind, arc, archive, dest_parts, strip=strip, overwrite=overwrite, progress=progress
            )
        finally:
            arc.close()  # type: ignore[attr-defined]


def _extract_members(
    root: SafeRoot,
    kind: str,
    arc: object,
    archive: str,
    dest_parts: list[str],
    *,
    strip: str,
    overwrite: bool,
    progress: Callable[[float, str], None] | None,
) -> dict:
    stats = ExtractStats()
    try:
        members = list(_zip_members(arc) if kind == "zip" else _tar_members(arc))  # type: ignore[arg-type]
        if len(members) > MAX_FILES:
            raise AgentError(f"Archive has more than {MAX_FILES} entries", "archive_too_large", 413)
        total = sum(m.size for m in members if m.kind == "file")
        if total > MAX_TOTAL_BYTES:
            raise AgentError("Archive expands to more than 64 GiB", "archive_too_large", 413)
        strip_prefix: str | None = None
        if strip == "auto":
            strip_prefix = _common_root([m.name for m in members])
        elif strip not in ("0", "none"):
            raise AgentError("strip must be 'auto' or 'none'", "invalid_strip", 400)
        archive_rel = "/".join(split_path(archive))
        done = 0
        for m in members:
            try:
                parts = split_path(m.name)
            except PathError:
                stats.skipped.append(f"{m.name} (unsafe path)")
                continue
            if strip_prefix and parts and parts[0] == strip_prefix:
                parts = parts[1:]
            if not parts:
                continue
            target = "/".join([*dest_parts, *parts])
            if target == archive_rel:
                stats.skipped.append(f"{m.name} (would overwrite the archive)")
                continue
            if m.kind == "other":
                stats.skipped.append(f"{m.name} (links and special files are not extracted)")
                continue
            if m.kind == "dir":
                root.mkdir(target, parents=True)
                stats.directories += 1
                continue
            if m.compressed and m.size > 1024 * 1024 and m.size / max(m.compressed, 1) > MAX_RATIO:
                raise AgentError(
                    f"Suspicious compression ratio for {m.name}; refusing to extract", "archive_bomb", 413
                )
            parent = "/".join(target.split("/")[:-1])
            if parent:
                root.mkdir(parent, parents=True)
            if root.exists(target) and not overwrite:
                stats.existing.append(target)
                continue
            assert m.open is not None
            src = m.open()
            writer = root.open_writer(target, overwrite=True, max_bytes=m.size + CHUNK)
            try:
                written = 0
                while chunk := src.read(CHUNK):
                    written += len(chunk)
                    if written > m.size + CHUNK:
                        raise AgentError(f"{m.name} is larger than declared", "archive_corrupt", 400)
                    writer.write(chunk)
                writer.commit()
            except BaseException:
                writer.abort()
                raise
            finally:
                src.close()
            stats.files += 1
            stats.bytes += m.size
            done += m.size
            if progress and total:
                progress(done / total, f"Extracted {stats.files} files")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        raise AgentError(f"The archive is corrupt or truncated: {exc}", "archive_corrupt", 400) from exc
    result = stats.as_dict()
    result["stripped_prefix"] = strip_prefix
    return result


class _PipeWriter:
    """Unseekable file-like sink that hands written bytes to a queue (for streaming zips)."""

    def __init__(self, q) -> None:  # type: ignore[no-untyped-def]
        self.q = q
        self.pos = 0

    def write(self, data: bytes) -> int:
        if data:
            self.q.put(bytes(data))
            self.pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass


def stream_zip(root: SafeRoot, rel: str, *, max_bytes: int = MAX_TOTAL_BYTES) -> Iterator[bytes]:
    """Yield a zip of a directory inside the instance, built on the fly.

    Only regular files are included (symlinks are never followed); paths in the archive
    are relative to the chosen directory's parent so the folder name is preserved.
    """
    import queue
    import threading

    parts = split_path(rel)
    prefix = parts[-1] if parts else "server"
    files = list(root.walk_files("/".join(parts), max_depth=64))
    if sum(st.st_size for _p, st in files) > max_bytes:
        raise AgentError("Folder is too large to download as a zip", "archive_too_large", 413)
    q: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=64)

    def produce() -> None:
        try:
            with zipfile.ZipFile(
                _PipeWriter(q), "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
            ) as zf:
                base = len(parts)
                for path, st in files:
                    arcname = "/".join([prefix, *split_path(path)[base:]])
                    try:
                        with root.open_read(path) as (fh, _size):
                            info = zipfile.ZipInfo(arcname, date_time=time.localtime(st.st_mtime)[:6])
                            info.compress_type = zipfile.ZIP_DEFLATED
                            info.external_attr = (stat.S_IFREG | 0o644) << 16
                            with zf.open(info, "w", force_zip64=st.st_size > 2**31) as out:
                                while chunk := fh.read(CHUNK):
                                    out.write(chunk)
                    except PathError:
                        continue  # vanished or replaced by a symlink meanwhile: skip
            q.put(None)
        except BaseException as exc:
            q.put(exc)

    threading.Thread(target=produce, daemon=True, name="zip-stream").start()
    while True:
        item = q.get()
        if item is None:
            return
        if isinstance(item, BaseException):
            raise item
        yield item
