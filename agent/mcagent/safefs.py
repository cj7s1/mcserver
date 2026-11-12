"""Race-free confined filesystem access for instance directories.

Every path is resolved component-by-component from a directory file descriptor
using ``O_NOFOLLOW``, so neither ``..`` nor symlinks (including symlinks created
concurrently by a running server or a malicious mod) can escape the instance
root. Symlinks inside an instance are listed but never followed.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import secrets
import shutil
import stat as statmod
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

log = logging.getLogger(__name__)

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_PATH_LEN = 4096
MAX_COMPONENT_LEN = 255


class PathError(Exception):
    """Raised for invalid, escaping or otherwise disallowed paths."""

    def __init__(self, message: str, code: str = "invalid_path", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Entry:
    name: str
    path: str
    type: str  # file | directory | symlink | other
    size: int
    modified: float
    mode: str
    # identity of the listed file (used as a cache key, not part of the API output)
    mtime_ns: int = 0
    ctime_ns: int = 0
    ino: int = 0

    def as_dict(self) -> dict:
        out = {
            "name": display_name(self.name),
            "path": display_name(self.path),
            "type": self.type,
            "size": self.size,
            "modified": self.modified,
            "mode": self.mode,
        }
        if not name_is_utf8(self.path):
            # A name with bytes that are not UTF-8 (old Latin-1 file names): shown with U+FFFD in
            # their place, which is not the real name, so the entry cannot be opened, renamed or
            # deleted by that name. One such file no longer breaks the whole listing.
            out["name_valid"] = False
        return out


def name_is_utf8(name: str) -> bool:
    """False for a name Python decoded with surrogate escapes (bytes that are not UTF-8)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def display_name(name: str) -> str:
    """``name`` with undecodable bytes replaced by U+FFFD (always encodable as UTF-8)."""
    if name_is_utf8(name):
        return name
    return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


_RENAME_NOREPLACE = 1  # linux/fs.h


def _libc_renameat2() -> Any:
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):  # not glibc >= 2.28 / musl >= 1.2.4: the stat check is used
        return None
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    return fn


_renameat2 = _libc_renameat2()


def _rename_noreplace(src: str, dst: str, src_dir_fd: int, dst_dir_fd: int) -> bool:
    """Rename without replacing an existing destination in one step (``renameat2`` with
    ``RENAME_NOREPLACE``: no window between a check and the rename; B-13). True when renamed;
    False when the system or the file system cannot do it (the caller checks, then renames);
    PathError 409 ``exists`` when the destination exists."""
    if _renameat2 is None:
        return False
    if _renameat2(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise PathError("Destination already exists", "exists", 409)
    if err in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        return False
    raise OSError(err, os.strerror(err), src)


def touches_protected(
    parts: list[str] | tuple[str, ...], protected: Iterable[tuple[str, ...]], *, above: bool
) -> bool:
    """Whether a change of the entry ``parts`` reaches a protected path (``protected``: case-folded
    parts, as :func:`folded` makes them). Case is folded (a case-insensitive file system has one entry
    for both spellings). Always when the entry is a protected path or lies inside one; with ``above``
    also when it is a folder that holds one (a delete or rename moves the protected path with it, a
    file or a rename put in its place replaces it)."""
    target = folded(parts)
    for prot in protected:
        if target[: len(prot)] == prot:
            return True
        if above and len(target) < len(prot) and prot[: len(target)] == target:
            return True
    return False


def folded(parts: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p.casefold() for p in parts)


def split_path(rel: str) -> list[str]:
    """Normalise a user-supplied relative path into safe components.

    ``""``, ``"/"`` and ``"."`` all refer to the root. ``..`` is rejected rather than
    resolved so that behaviour is predictable.
    """
    if rel is None:
        rel = ""
    if "\x00" in rel:
        raise PathError("Path contains a NUL byte")
    if len(rel) > MAX_PATH_LEN:
        raise PathError("Path is too long")
    parts: list[str] = []
    for part in rel.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PathError("Parent directory references ('..') are not allowed")
        if len(part) > MAX_COMPONENT_LEN:
            raise PathError("Path component is too long")
        parts.append(part)
    return parts


def validate_name(name: str) -> str:
    parts = split_path(name)
    if len(parts) != 1 or parts[0] != name:
        raise PathError("Invalid file name")
    return name


def _type_of(st: os.stat_result) -> str:
    if statmod.S_ISDIR(st.st_mode):
        return "directory"
    if statmod.S_ISREG(st.st_mode):
        return "file"
    if statmod.S_ISLNK(st.st_mode):
        return "symlink"
    return "other"


class SafeRoot:
    def __init__(self, root: Path, uid: int | None = None, gid: int | None = None) -> None:
        self.root = Path(root)
        self.uid = uid
        self.gid = gid

    # -- fd helpers ---------------------------------------------------------------
    def _open_root(self) -> int:
        try:
            return os.open(self.root, DIR_FLAGS)
        except FileNotFoundError as exc:
            raise PathError("Instance directory does not exist", "not_found", 404) from exc

    @contextmanager
    def _dir_fd(self, parts: list[str]) -> Iterator[int]:
        fd = self._open_root()
        try:
            for part in parts:
                try:
                    nfd = os.open(part, DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError as exc:
                    raise PathError(f"'{part}' does not exist", "not_found", 404) from exc
                except NotADirectoryError as exc:
                    raise PathError(f"'{part}' is not a directory", "not_a_directory") from exc
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise PathError(
                            f"'{part}' is a symbolic link; links are not followed", "symlink"
                        ) from exc
                    raise
                os.close(fd)
                fd = nfd
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def dir_fd(self, rel: str) -> Iterator[int]:
        """A file descriptor of the directory ``rel`` (no link followed on the way); closed on exit."""
        with self._dir_fd(split_path(rel)) as fd:
            yield fd

    @contextmanager
    def _parent(self, rel: str, *, allow_root: bool = False) -> Iterator[tuple[int, str | None, list[str]]]:
        parts = split_path(rel)
        if not parts:
            if not allow_root:
                raise PathError("This operation is not allowed on the root directory")
            with self._dir_fd([]) as fd:
                yield fd, None, parts
            return
        with self._dir_fd(parts[:-1]) as fd:
            yield fd, parts[-1], parts

    def _chown(self, name: str, dir_fd: int) -> None:
        if self.uid is None or self.gid is None:
            return
        try:
            os.chown(name, self.uid, self.gid, dir_fd=dir_fd, follow_symlinks=False)
        except PermissionError:
            pass  # running unprivileged (development); ownership stays with agent user

    # -- queries ------------------------------------------------------------------
    def stat(self, rel: str) -> Entry:
        with self._parent(rel, allow_root=True) as (fd, name, parts):
            try:
                st = os.stat(name or ".", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise PathError("Does not exist", "not_found", 404) from exc
            return Entry(
                name=name or "",
                path="/".join(parts),
                type=_type_of(st),
                size=st.st_size,
                modified=st.st_mtime,
                mode=statmod.filemode(st.st_mode),
                mtime_ns=st.st_mtime_ns,
                ctime_ns=st.st_ctime_ns,
                ino=st.st_ino,
            )

    def list(self, rel: str) -> list[Entry]:
        parts = split_path(rel)
        entries: list[Entry] = []
        with self._dir_fd(parts) as fd:
            with os.scandir(fd) as it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    entries.append(
                        Entry(
                            name=de.name,
                            path="/".join([*parts, de.name]),
                            type=_type_of(st),
                            size=st.st_size,
                            modified=st.st_mtime,
                            mode=statmod.filemode(st.st_mode),
                            mtime_ns=st.st_mtime_ns,
                            ctime_ns=st.st_ctime_ns,
                            ino=st.st_ino,
                        )
                    )
        entries.sort(key=lambda e: (e.type != "directory", e.name.lower()))
        return entries

    @contextmanager
    def open_read(self, rel: str) -> Iterator[tuple[BinaryIO, int]]:
        """Open a regular file for reading.

        ``O_NONBLOCK`` makes opening a FIFO (which a mod can create with ``mkfifo``) return
        at once instead of waiting for a writer; anything but a regular file (FIFO, socket,
        device) is refused by ``fstat`` before a single byte is read.
        """
        with self._parent(rel) as (fd, name, _parts):
            try:
                ffd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=fd
                )
            except FileNotFoundError as exc:
                raise PathError("File does not exist", "not_found", 404) from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PathError("Symbolic links are not followed", "symlink") from exc
                if exc.errno in (errno.ENXIO, errno.EOPNOTSUPP, errno.ENODEV):  # socket / device node
                    raise PathError("Not a regular file", "not_a_file") from exc
                raise
            try:
                st = os.fstat(ffd)
                if not statmod.S_ISREG(st.st_mode):
                    raise PathError("Not a regular file", "not_a_file")
                os.set_blocking(ffd, True)  # regular file: back to normal blocking reads
            except BaseException:
                os.close(ffd)
                raise
            fh = os.fdopen(ffd, "rb")
            try:
                yield fh, st.st_size
            finally:
                fh.close()

    def read_bytes(self, rel: str, max_bytes: int) -> bytes:
        with self.open_read(rel) as (fh, size):
            if size > max_bytes:
                raise PathError(f"File is larger than {max_bytes} bytes", "too_large", 413)
            return fh.read(max_bytes + 1)[:max_bytes]

    def exists(self, rel: str) -> bool:
        try:
            self.stat(rel)
            return True
        except PathError:
            return False

    # -- mutations ----------------------------------------------------------------
    def open_writer(self, rel: str, *, overwrite: bool = True, max_bytes: int | None = None) -> AtomicWriter:
        """Open an atomic writer (temp file in the target directory, renamed on commit)."""
        parts = split_path(rel)
        if not parts:
            raise PathError("Cannot write to the root directory")
        with self._dir_fd(parts[:-1]) as fd:
            dir_fd = os.dup(fd)
        name = parts[-1]
        try:
            existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if statmod.S_ISDIR(existing.st_mode):
                raise PathError("A directory with that name exists", "is_directory", 409)
            if not overwrite:
                raise PathError("File already exists", "exists", 409)
        except FileNotFoundError:
            pass
        except BaseException:
            os.close(dir_fd)
            raise
        return AtomicWriter(self, dir_fd, name, max_bytes)

    def write_stream(
        self, rel: str, chunks: Iterator[bytes], *, overwrite: bool = True, max_bytes: int | None = None
    ) -> int:
        writer = self.open_writer(rel, overwrite=overwrite, max_bytes=max_bytes)
        try:
            for chunk in chunks:
                writer.write(chunk)
            return writer.commit()
        except BaseException:
            writer.abort()
            raise

    def write_bytes(self, rel: str, data: bytes, *, overwrite: bool = True) -> int:
        return self.write_stream(rel, iter([data]), overwrite=overwrite)

    def mkdir(self, rel: str, *, parents: bool = False) -> None:
        parts = split_path(rel)
        if not parts:
            raise PathError("Cannot create the root directory")
        if parents:
            # create missing intermediate directories one at a time (each safely)
            for i in range(1, len(parts)):
                if not self.exists("/".join(parts[:i])):
                    self.mkdir("/".join(parts[:i]))
        with self._dir_fd(parts[:-1]) as fd:
            try:
                os.mkdir(parts[-1], 0o755, dir_fd=fd)
            except FileExistsError as exc:
                if parents:
                    return
                raise PathError("Already exists", "exists", 409) from exc
            self._chown(parts[-1], fd)

    def rename(self, src: str, dst: str, *, overwrite: bool = False) -> None:
        src_parts = split_path(src)
        dst_parts = split_path(dst)
        if not src_parts or not dst_parts:
            raise PathError("Cannot rename the root directory")
        if dst_parts[: len(src_parts)] == src_parts:
            raise PathError("Cannot move a directory into itself")
        with self._dir_fd(src_parts[:-1]) as sfd, self._dir_fd(dst_parts[:-1]) as dfd:
            try:
                os.stat(src_parts[-1], dir_fd=sfd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise PathError("Source does not exist", "not_found", 404) from exc
            if not overwrite:
                if _rename_noreplace(src_parts[-1], dst_parts[-1], sfd, dfd):
                    return
                try:
                    os.stat(dst_parts[-1], dir_fd=dfd, follow_symlinks=False)
                    raise PathError("Destination already exists", "exists", 409)
                except FileNotFoundError:
                    pass
            os.rename(src_parts[-1], dst_parts[-1], src_dir_fd=sfd, dst_dir_fd=dfd)

    def delete(self, rel: str, *, recursive: bool = False) -> None:
        with self._parent(rel) as (fd, name, _parts):
            assert name is not None
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise PathError("Does not exist", "not_found", 404) from exc
            if statmod.S_ISDIR(st.st_mode):
                if not recursive:
                    try:
                        os.rmdir(name, dir_fd=fd)
                    except OSError as exc:
                        if exc.errno == errno.ENOTEMPTY:
                            raise PathError("Directory is not empty", "not_empty", 409) from exc
                        raise
                else:
                    # shutil.rmtree with dir_fd uses the fd-based, symlink-attack-resistant implementation
                    shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)

    # -- search -------------------------------------------------------------------
    def walk_files(
        self, rel: str = "", *, max_depth: int = 12, too_deep: list[str] | None = None
    ) -> Iterator[tuple[str, os.stat_result]]:
        """Yield (relative path, stat) for regular files, never following links. Folders below
        ``max_depth`` are left out; ``too_deep`` (when given) gets the path of each of them, so the
        caller can say so."""
        base = split_path(rel)

        def _walk(fd: int, prefix: list[str], depth: int) -> Iterator[tuple[str, os.stat_result]]:
            if depth > max_depth:
                if too_deep is not None:
                    too_deep.append("/".join(prefix))
                return
            with os.scandir(fd) as it:
                items = list(it)
            for de in items:
                try:
                    st = de.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if statmod.S_ISDIR(st.st_mode):
                    try:
                        cfd = os.open(de.name, DIR_FLAGS, dir_fd=fd)
                    except OSError:
                        continue
                    try:
                        yield from _walk(cfd, [*prefix, de.name], depth + 1)
                    finally:
                        os.close(cfd)
                elif statmod.S_ISREG(st.st_mode):
                    yield "/".join([*prefix, de.name]), st

        with self._dir_fd(base) as fd:
            yield from _walk(fd, base, 0)

    def chown_tree(
        self,
        uid: int,
        gid: int,
        *,
        skip: tuple[str, ...] = (),
        max_depth: int = 64,
        too_deep: list[str] | None = None,
    ) -> int:
        """Give the root folder and everything below it the owner ``uid:gid`` (what ``chown -R``
        does), never following a symbolic link (a link itself is changed, not its target). Entries
        that already have it are not touched; ``skip``: paths relative to the root left out with
        everything below them. Returns how many entries were changed. Raises OSError (e.g.
        PermissionError when the agent may not chown)."""
        skipped = {tuple(split_path(s)) for s in skip if split_path(s)}
        changed = 0

        def fix(name: str | None, fd: int, st: os.stat_result) -> None:
            nonlocal changed
            if st.st_uid == uid and st.st_gid == gid:
                return
            if name is None:
                os.fchown(fd, uid, gid)
            else:
                os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)
            changed += 1

        def walk(fd: int, prefix: list[str], depth: int) -> None:
            if depth > max_depth:
                if too_deep is not None:  # left as it is: the caller says so
                    too_deep.append("/".join(prefix))
                return
            with os.scandir(fd) as it:
                items = list(it)
            for de in items:
                path = (*prefix, de.name)
                if path in skipped:
                    continue
                try:
                    st = de.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                try:
                    fix(de.name, fd, st)
                except FileNotFoundError:
                    continue
                if statmod.S_ISDIR(st.st_mode):
                    try:
                        cfd = os.open(de.name, DIR_FLAGS, dir_fd=fd)
                    except OSError:
                        continue  # replaced by a link or removed meanwhile
                    try:
                        walk(cfd, list(path), depth + 1)
                    finally:
                        os.close(cfd)

        with self._dir_fd([]) as fd:
            fix(None, fd, os.fstat(fd))
            walk(fd, [], 0)
        return changed

    def search(
        self,
        rel: str,
        query: str,
        *,
        max_results: int = 200,
        max_file_bytes: int = 2 * 1024 * 1024,
        extensions: tuple[str, ...] = (
            ".properties",
            ".json",
            ".json5",
            ".toml",
            ".yml",
            ".yaml",
            ".cfg",
            ".conf",
            ".txt",
            ".ini",
            ".snbt",
            ".js",
            ".zs",
            ".mcmeta",
            ".log",
        ),
    ) -> list[dict]:
        if not query:
            return []
        needle = query.lower()
        results: list[dict] = []
        for path, st in self.walk_files(rel):
            if len(results) >= max_results:
                break
            if st.st_size > max_file_bytes or not path.lower().endswith(extensions):
                continue
            if needle in path.lower():
                results.append({"path": display_name(path), "line": None, "text": None})
            try:
                data = self.read_bytes(path, max_file_bytes)
            except (PathError, OSError):
                continue
            text = data.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    results.append({"path": display_name(path), "line": lineno, "text": line.strip()[:300]})
                    if len(results) >= max_results:
                        break
        return results

    def disk_usage(self) -> int:
        total = 0
        too_deep: list[str] = []
        for _path, st in self.walk_files("", max_depth=64, too_deep=too_deep):
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
        if too_deep:
            log.warning(
                "Disk usage of %s leaves out %d folders deeper than 64 levels (first: %r)",
                self.root,
                len(too_deep),
                too_deep[0],
            )
        return total


class AtomicWriter:
    def __init__(self, root: SafeRoot, dir_fd: int, name: str, max_bytes: int | None) -> None:
        self.root = root
        self.dir_fd = dir_fd
        self.name = name
        self.max_bytes = max_bytes
        self.tmp = f".mcpanel-tmp-{secrets.token_hex(6)}"
        fd = os.open(
            self.tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
            dir_fd=dir_fd,
        )
        self.fh = os.fdopen(fd, "wb")
        self.written = 0
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.written += len(chunk)
        if self.max_bytes is not None and self.written > self.max_bytes:
            raise PathError(f"Upload exceeds limit of {self.max_bytes} bytes", "too_large", 413)
        self.fh.write(chunk)

    def commit(self) -> int:
        try:
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()
            self.root._chown(self.tmp, self.dir_fd)
            os.rename(self.tmp, self.name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        except BaseException:
            self.abort()
            raise
        self._close_dir()
        return self.written

    def abort(self) -> None:
        if self.closed:
            return
        try:
            self.fh.close()
        except OSError:
            pass
        try:
            os.unlink(self.tmp, dir_fd=self.dir_fd)
        except FileNotFoundError:
            pass
        self._close_dir()

    def _close_dir(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.dir_fd)
