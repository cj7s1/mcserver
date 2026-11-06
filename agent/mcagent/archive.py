"""Safe extraction of uploaded archives (zip / tar / tar.gz / tar.xz) inside an instance.

Used to import existing servers, worlds and manually downloaded server packs. Every
member goes through the same confined :class:`SafeRoot` writer as uploads, so:

* absolute paths, ``..`` components and NUL bytes are rejected,
* symlinks, hard links, devices and FIFOs inside the archive are skipped (never created),
* total uncompressed size, file count and per-file compression ratio are bounded
  (zip-bomb protection) -- and the archive's own index is bounded too: the zip central
  directory is measured before :mod:`zipfile` loads it, tar headers are counted while they
  are read (never all listed first) and oversized extended headers are refused,
* existing files are only replaced when ``overwrite`` is set.
"""

from __future__ import annotations

import os
import queue
import stat
import struct
import tarfile
import threading
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
# zipfile reads the whole central directory into memory and builds a ZipInfo per entry
MAX_ZIP_DIRECTORY_BYTES = 128 * 1024 * 1024
# pax / GNU long-name headers are read into memory by tarfile (and can be a gzip bomb)
MAX_TAR_EXT_HEADER_BYTES = 1024 * 1024
MAX_TAR_EXT_HEADERS_TOTAL = 64 * 1024 * 1024
REPORT_LIMIT = 200  # skipped/existing paths listed in the result (all are counted)
# zip download streaming: chunks buffered between the zip thread and the HTTP response
ZIP_QUEUE_CHUNKS = 16
ZIP_PUT_POLL_SECONDS = 0.5
ZIP_STALL_SECONDS = 15 * 60  # a consumer that takes nothing for this long is gone


@dataclass
class ExtractStats:
    files: int = 0
    directories: int = 0
    bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    skipped_count: int = 0
    existing_count: int = 0

    def skip(self, note: str) -> None:
        self.skipped_count += 1
        if len(self.skipped) < REPORT_LIMIT:
            self.skipped.append(note[:600])

    def exists(self, target: str) -> None:
        self.existing_count += 1
        if len(self.existing) < REPORT_LIMIT:
            self.existing.append(target)

    def as_dict(self) -> dict:
        return {
            "files": self.files,
            "directories": self.directories,
            "bytes": self.bytes,
            "skipped": self.skipped,
            "skipped_count": self.skipped_count,
            "existing_count": self.existing_count,
            "existing": self.existing,
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


class _CommonRoot:
    """Incrementally finds the one top-level directory every entry lives under (strip=auto)."""

    def __init__(self) -> None:
        self.root: str | None = None
        self.possible = True

    def add(self, name: str, kind: str) -> None:
        if not self.possible:
            return
        try:
            parts = split_path(name)
        except PathError:
            return
        if not parts:
            return
        if len(parts) == 1 and kind != "dir" and not name.endswith("/"):
            self.possible = False  # a file at top level
        elif self.root is None:
            self.root = parts[0]
        elif parts[0] != self.root:
            self.possible = False

    def result(self) -> str | None:
        return self.root if self.possible else None


def _common_root(names: list[str]) -> str | None:
    """If every entry lives under one top-level directory, return it (for strip=auto)."""
    acc = _CommonRoot()
    for n in names:
        acc.add(n, "dir" if n.endswith("/") else "file")
    return acc.result()


_EXT_HEADER_TYPES = (
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
    tarfile.XHDTYPE,
    tarfile.XGLTYPE,
    tarfile.SOLARIS_XHDTYPE,
)


class _BoundedTarInfo(tarfile.TarInfo):
    """TarInfo that refuses headers whose processing would read unbounded data into memory.

    tarfile reads pax and GNU long-name headers whole (a compressed archive can declare a
    multi-GiB header that decompresses from a few MB) and keeps every member's TarInfo.
    ``_proc_member`` is tarfile's documented hook for subclasses; it runs before any of
    that data is read.
    """

    def _proc_member(self, tarfile_obj):  # type: ignore[no-untyped-def]
        if self.type in _EXT_HEADER_TYPES:
            if self.size > MAX_TAR_EXT_HEADER_BYTES:
                raise AgentError("The archive has an oversized tar header", "archive_too_large", 413)
            total = getattr(tarfile_obj, "_mcpanel_ext_bytes", 0) + self.size
            if total > MAX_TAR_EXT_HEADERS_TOTAL:
                raise AgentError("The archive's tar headers are too large", "archive_too_large", 413)
            tarfile_obj._mcpanel_ext_bytes = total
        elif self.type == tarfile.GNUTYPE_SPARSE:
            # old GNU sparse maps are an unbounded chain of extension blocks
            raise AgentError("Sparse files in tar archives are not supported", "unsupported_archive", 415)
        return super()._proc_member(tarfile_obj)

    def _proc_gnusparse_10(self, next_member, pax_headers, tarfile_obj):  # type: ignore[no-untyped-def]
        # the pax 1.0 sparse map is read from the data area with no size limit
        raise AgentError("Sparse files in tar archives are not supported", "unsupported_archive", 415)


_CD_HEADER = struct.Struct("<4s42x")  # central directory file header: 46 bytes
_CD_SIGNATURE = b"PK\x01\x02"


def _check_zip_directory(fh: IO[bytes]) -> None:
    """Bound the zip central directory (bytes and entries) before zipfile loads it.

    ``zipfile.ZipFile`` reads the whole central directory into memory and creates one
    ZipInfo per entry before any of our checks could run. The directory is located the
    way zipfile locates it and walked record by record without keeping anything. If it
    cannot be located, nothing is decided here and zipfile reports the archive as corrupt.
    """
    try:
        endrec = zipfile._EndRecData(fh)  # type: ignore[attr-defined]
    except (AttributeError, OSError, zipfile.BadZipFile, struct.error):
        endrec = None
    if not endrec:
        fh.seek(0)
        return
    size_cd = endrec[zipfile._ECD_SIZE]  # type: ignore[attr-defined]
    offset_cd = endrec[zipfile._ECD_OFFSET]  # type: ignore[attr-defined]
    if size_cd > MAX_ZIP_DIRECTORY_BYTES:
        raise AgentError("The archive's file index is too large", "archive_too_large", 413)
    start = endrec[zipfile._ECD_LOCATION] - size_cd  # type: ignore[attr-defined]  # = offset_cd + concat
    if start < 0 or offset_cd < 0:
        fh.seek(0)
        return
    fh.seek(start)
    pos = count = 0
    while pos < size_cd:
        head = fh.read(_CD_HEADER.size)
        if len(head) != _CD_HEADER.size or head[:4] != _CD_SIGNATURE:
            break  # zipfile will report the corruption
        name_len, extra_len, comment_len = struct.unpack_from("<3H", head, 28)
        skip = name_len + extra_len + comment_len
        fh.seek(skip, 1)
        pos += _CD_HEADER.size + skip
        count += 1
        if count > MAX_FILES:
            raise AgentError(f"Archive has more than {MAX_FILES} entries", "archive_too_large", 413)
    fh.seek(0)


def _open_archive(fh: IO[bytes]) -> tuple[str, object]:
    head = fh.read(8)
    fh.seek(0)
    if head.startswith(b"PK"):
        _check_zip_directory(fh)
        return "zip", zipfile.ZipFile(fh)
    try:
        return "tar", tarfile.open(fileobj=fh, mode="r:*", tarinfo=_BoundedTarInfo)
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
    check: Callable[[int, int], None] | None = None,
) -> dict:
    """Extract ``archive`` into ``destination`` (both relative to ``root``).

    ``check(file_count, total_bytes)`` runs after the member list is read and before
    anything is written (e.g. to refuse an archive that does not fit on the disk).
    """
    dest_parts = split_path(destination)
    with root.open_read(archive) as (fh, _size):
        try:
            kind, arc = _open_archive(fh)
        except (zipfile.BadZipFile, EOFError) as exc:
            raise AgentError(f"The archive is corrupt or truncated: {exc}", "archive_corrupt", 400) from exc
        try:
            return _extract_members(
                root,
                kind,
                arc,
                archive,
                dest_parts,
                strip=strip,
                overwrite=overwrite,
                progress=progress,
                check=check,
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
    check: Callable[[int, int], None] | None = None,
) -> dict:
    stats = ExtractStats()
    if strip not in ("auto", "0", "none"):
        raise AgentError("strip must be 'auto' or 'none'", "invalid_strip", 400)

    def members() -> Iterator[Member]:
        return _zip_members(arc) if kind == "zip" else _tar_members(arc)  # type: ignore[arg-type]

    try:
        # First pass: limits and the common prefix, enforced while the entries are read
        # (nothing is collected; a tar keeps at most MAX_FILES headers in its member cache).
        count = files = total = 0
        common = _CommonRoot()
        for m in members():
            count += 1
            if count > MAX_FILES:
                raise AgentError(f"Archive has more than {MAX_FILES} entries", "archive_too_large", 413)
            if m.kind == "file":
                files += 1
                total += m.size
                if total > MAX_TOTAL_BYTES:
                    raise AgentError("Archive expands to more than 64 GiB", "archive_too_large", 413)
            common.add(m.name, m.kind)
        if check is not None:
            check(files, total)  # before anything is written
        strip_prefix = common.result() if strip == "auto" else None
        archive_rel = "/".join(split_path(archive))
        done = 0
        for m in members():
            try:
                parts = split_path(m.name)
            except PathError:
                stats.skip(f"{m.name[:300]} (unsafe path)")
                continue
            if strip_prefix and parts and parts[0] == strip_prefix:
                parts = parts[1:]
            if not parts:
                continue
            target = "/".join([*dest_parts, *parts])
            if target == archive_rel:
                stats.skip(f"{m.name} (would overwrite the archive)")
                continue
            if m.kind == "other":
                stats.skip(f"{m.name} (links and special files are not extracted)")
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
                stats.exists(target)
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


class _ConsumerGone(Exception):
    """The HTTP response stopped reading the zip (client disconnected or stalled)."""


class _PipeWriter:
    """Unseekable file-like sink that hands written bytes to a bounded queue (streaming zips).

    ``write`` never blocks for good: it gives up once the consumer has gone away
    (``cancelled``) or has taken nothing for ``ZIP_STALL_SECONDS``.
    """

    def __init__(self, q: queue.Queue, cancelled: threading.Event) -> None:  # type: ignore[type-arg]
        self.q = q
        self.cancelled = cancelled
        self.pos = 0

    def put(self, item: object) -> None:
        waited = 0.0
        while True:
            if self.cancelled.is_set():
                raise _ConsumerGone
            try:
                self.q.put(item, timeout=ZIP_PUT_POLL_SECONDS)
                return
            except queue.Full:
                waited += ZIP_PUT_POLL_SECONDS
                if waited >= ZIP_STALL_SECONDS:
                    raise _ConsumerGone from None

    def write(self, data: bytes) -> int:
        if data:
            self.put(bytes(data))
            self.pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass


MAX_ZIP_FOLDERS = 8


def stream_zip(root: SafeRoot, rel: str | list[str], *, max_bytes: int = MAX_TOTAL_BYTES) -> Iterator[bytes]:
    """A zip of one or more directories inside the instance, built on the fly.

    The folders are walked and their size checked right away (so a too-large folder is an
    error response, not a broken download); the returned iterator then streams the zip
    from a producer thread through a small bounded queue. Closing the iterator (the
    client went away) stops the producer, which also stops by itself if nothing is read
    for ``ZIP_STALL_SECONDS``.

    Only regular files are included (symlinks are never followed); paths in the archive
    are relative to each chosen directory's parent so the folder names are preserved
    (``world/…``, ``world_nether/…``). Several folders must have distinct names.
    """
    rels = [rel] if isinstance(rel, str) else list(rel)
    if not rels or len(rels) > MAX_ZIP_FOLDERS:
        raise AgentError(f"Choose between 1 and {MAX_ZIP_FOLDERS} folders", "invalid_path", 400)
    folders: list[tuple[list[str], str]] = []
    for item in rels:
        parts = split_path(item)
        if not parts and len(rels) > 1:
            raise AgentError("The server root cannot be combined with other folders", "invalid_path", 400)
        prefix = parts[-1] if parts else "server"
        if any(prefix == other for _p, other in folders):
            raise AgentError(f"Two folders are both named '{prefix}'", "invalid_path", 400)
        folders.append((parts, prefix))
    files: list[tuple[str, str, os.stat_result]] = []  # (path, name in the zip, stat)
    for parts, prefix in folders:
        for path, st in root.walk_files("/".join(parts), max_depth=64):
            files.append((path, "/".join([prefix, *split_path(path)[len(parts) :]]), st))
    if sum(st.st_size for _p, _a, st in files) > max_bytes:
        raise AgentError("Folder is too large to download as a zip", "archive_too_large", 413)
    return _zip_stream(root, files)


def _zip_stream(root: SafeRoot, files: list[tuple[str, str, os.stat_result]]) -> Iterator[bytes]:
    q: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=ZIP_QUEUE_CHUNKS)
    cancelled = threading.Event()
    sink = _PipeWriter(q, cancelled)

    def produce() -> None:
        try:
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
                for path, arcname, st in files:
                    if cancelled.is_set():
                        raise _ConsumerGone
                    try:
                        with root.open_read(path) as (fh, _size):
                            info = zipfile.ZipInfo(arcname, date_time=time.localtime(st.st_mtime)[:6])
                            info.compress_type = zipfile.ZIP_DEFLATED
                            info.external_attr = (stat.S_IFREG | 0o644) << 16
                            with zf.open(info, "w", force_zip64=st.st_size > 2**31) as out:
                                while chunk := fh.read(CHUNK):
                                    out.write(chunk)
                    except PathError:
                        continue  # vanished or replaced by a symlink/FIFO meanwhile: skip
            sink.put(None)
        except _ConsumerGone:
            return
        except BaseException as exc:
            try:
                sink.put(exc)
            except _ConsumerGone:
                return

    thread = threading.Thread(target=produce, daemon=True, name="zip-stream")
    thread.start()
    try:
        while True:
            item = q.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        cancelled.set()
        # free what is buffered right away and unblock a producer waiting to put
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break
