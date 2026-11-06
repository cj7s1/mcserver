"""Digests of the files in an instance folder, with a persistent cache.

The panel identifies mods and plugins on Modrinth by their SHA-1, so listing the mods
of a large modpack means reading every jar: about 1 GB for a 300-mod pack, which took
45 s on a cold page cache in the dev sandbox (the disk, not the hashing, is the limit;
SHA-1 of the same files from RAM takes 0.6 s). A digest is therefore cached and reused
for as long as the file's name, size, mtime, ctime (ns) and inode are unchanged, and the
cache is kept on disk so it survives agent restarts. ctime cannot be set by a process in
the Minecraft container (unlike mtime), so a jar rewritten there with its old size and
mtime is still noticed.

* Cache files live under the agent's state directory (``Settings.hash_cache_dir``),
  never inside an instance directory, which the Minecraft server and its mods can write.
  One JSON file per (instance, folder); names are derived from the instance UUID and a
  hash of the folder path, so no user input reaches a file name.
* The cache is an optimisation only. A missing, oversized, unreadable or corrupted file
  is treated as empty and rewritten; invalid entries are dropped one by one. Writes are
  atomic (temp file + rename) and best-effort: a failed write never fails a request.
* Bounded: entries for files no longer in the folder are dropped on every pass, a folder
  keeps at most ``max_entries`` entries, and the oldest cache files are removed beyond
  ``max_files``.
* Files are read through :class:`SafeRoot` (fd-based, no symlinks are followed). A file
  that changes while it is read, or was modified within the last couple of seconds
  (timestamps are coarse), is hashed but not cached.
* Hashing a folder is serialised per folder, so concurrent requests never read the same
  files twice. With ``wait`` a request returns what is known after that many seconds
  while one bounded background pass finishes the folder (``HashJobs``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .safefs import Entry, PathError, SafeRoot, split_path

log = logging.getLogger("mcagent.hashcache")

CACHE_VERSION = 1
DIGEST_LENGTHS = {"sha1": 40, "sha512": 128}
MAX_ENTRIES = 20_000  # per folder; a larger folder is still hashed, but not progressively
MAX_FILES = 2_000  # cache files (instance folders) kept; the least recently written go first
MAX_FILE_BYTES = 16 * 1024 * 1024
READ_CHUNK = 1024 * 1024
SAVE_INTERVAL = 1.0  # seconds between intermediate saves during a long pass
RACY_WINDOW_NS = 2_000_000_000  # files modified this recently are not cached
STALE_TEMP_SECONDS = 3600
_HEX = re.compile(r"^[0-9a-f]+$")


class HashingCancelled(Exception):
    """The agent is shutting down."""


@dataclass(frozen=True)
class Signature:
    """What must be unchanged for a cached digest to be reused."""

    size: int
    mtime_ns: int
    ctime_ns: int
    ino: int

    @classmethod
    def of(cls, st: os.stat_result) -> Signature:
        return cls(st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)

    @classmethod
    def of_entry(cls, entry: Entry) -> Signature:
        return cls(entry.size, entry.mtime_ns, entry.ctime_ns, entry.ino)


def _int(value: Any, *, minimum: int | None = None) -> int | None:
    if type(value) is not int:  # bool is an int subclass and is not accepted
        return None
    if minimum is not None and value < minimum:
        return None
    return value


def _valid_name(name: Any) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name.encode("utf-8", "surrogateescape")) <= 255
        and name not in (".", "..")
        and "/" not in name
        and "\x00" not in name
    )


class FolderCache:
    """Cached digests of the files directly inside one folder, keyed by file name."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[Signature, dict[str, str]]] = {}
        self.dirty = False

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, name: str, sig: Signature, algorithm: str) -> str | None:
        hit = self._entries.get(name)
        if hit is None or hit[0] != sig:
            return None
        return hit[1].get(algorithm)

    def put(self, name: str, sig: Signature, algorithm: str, digest: str) -> None:
        hit = self._entries.get(name)
        if hit is not None and hit[0] == sig:
            if hit[1].get(algorithm) == digest:
                return
            digests = {**hit[1], algorithm: digest}  # same file, another algorithm
        else:
            digests = {algorithm: digest}
        self._entries[name] = (sig, digests)
        self.dirty = True

    def retain(self, names: set[str]) -> None:
        """Forget files that are no longer in the folder."""
        gone = [n for n in self._entries if n not in names]
        for n in gone:
            del self._entries[n]
        if gone:
            self.dirty = True

    def to_json(self, max_entries: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, (sig, digests) in self._entries.items():
            if len(out) >= max_entries:
                break
            out[name] = {
                "size": sig.size,
                "mtime_ns": sig.mtime_ns,
                "ctime_ns": sig.ctime_ns,
                "ino": sig.ino,
                **digests,
            }
        return out

    @classmethod
    def from_json(cls, data: Any, max_entries: int) -> FolderCache:
        fc = cls()
        if not isinstance(data, dict):
            return fc
        for name, value in data.items():
            if len(fc._entries) >= max_entries:
                fc.dirty = True
                break
            if not _valid_name(name) or not isinstance(value, dict):
                fc.dirty = True
                continue
            size = _int(value.get("size"), minimum=0)
            mtime_ns = _int(value.get("mtime_ns"))
            ctime_ns = _int(value.get("ctime_ns"))
            ino = _int(value.get("ino"), minimum=0)
            digests = {
                alg: value[alg]
                for alg, length in DIGEST_LENGTHS.items()
                if isinstance(value.get(alg), str) and len(value[alg]) == length and _HEX.match(value[alg])
            }
            if size is None or mtime_ns is None or ctime_ns is None or ino is None or not digests:
                fc.dirty = True
                continue
            fc._entries[name] = (Signature(size, mtime_ns, ctime_ns, ino), digests)
        return fc


class HashCache:
    """On-disk store of :class:`FolderCache` objects (one JSON file per instance folder)."""

    def __init__(
        self,
        directory: Path,
        *,
        max_entries: int = MAX_ENTRIES,
        max_files: int = MAX_FILES,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self.max_entries = max_entries
        self.max_files = max_files
        self.max_bytes = max_bytes

    def path_for(self, instance_id: UUID, folder: str) -> Path:
        tag = hashlib.sha256(folder.encode("utf-8", "surrogateescape")).hexdigest()[:24]
        return self.directory / f"{UUID(str(instance_id))}-{tag}.json"

    def load(self, instance_id: UUID, folder: str) -> FolderCache:
        path = self.path_for(instance_id, folder)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return FolderCache()
        except OSError as exc:
            log.warning("Ignoring unreadable hash cache %s: %s", path.name, exc)
            return FolderCache()
        try:
            with os.fdopen(fd, "rb") as fh:
                raw = fh.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise ValueError("cache file is too large")
            doc = json.loads(raw)
            if not isinstance(doc, dict) or doc.get("version") != CACHE_VERSION:
                raise ValueError("unknown cache format")
            if doc.get("instance") != str(instance_id) or doc.get("folder") != folder:
                raise ValueError("cache file belongs to another folder")
            return FolderCache.from_json(doc.get("entries"), self.max_entries)
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            log.warning("Ignoring corrupted hash cache %s: %s", path.name, exc)
            fc = FolderCache()
            fc.dirty = True  # rewrite it on the next save
            return fc

    def save(self, instance_id: UUID, folder: str, fc: FolderCache, *, durable: bool = True) -> None:
        """Atomically replace the folder's cache file. Never raises (the cache is optional)."""
        path = self.path_for(instance_id, folder)
        doc = {
            "version": CACHE_VERSION,
            "instance": str(instance_id),
            "folder": folder,
            "entries": fc.to_json(self.max_entries),
        }
        data = json.dumps(doc, separators=(",", ":")).encode()
        if len(data) > self.max_bytes:
            log.warning("Not caching digests for %s: the cache would exceed %d bytes", folder, self.max_bytes)
            return
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            existed = path.exists()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    if durable:
                        os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            fc.dirty = False
            if not existed:
                self._prune(keep=path)
        except OSError as exc:
            log.warning("Could not write hash cache %s: %s", path.name, exc)

    def forget(self, instance_id: UUID) -> None:
        """Remove every cache file of an instance (its data was deleted)."""
        prefix = f"{UUID(str(instance_id))}-"
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        for name in names:
            if name.startswith(prefix) and name.endswith(".json"):
                with contextlib.suppress(OSError):
                    os.unlink(self.directory / name)

    def _prune(self, keep: Path) -> None:
        files: list[tuple[float, Path]] = []
        now = time.time()
        with os.scandir(self.directory) as it:
            for de in it:
                if not de.is_file(follow_symlinks=False):
                    continue
                try:
                    mtime = de.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if de.name.endswith(".tmp"):
                    if now - mtime > STALE_TEMP_SECONDS:  # left behind by a crash
                        with contextlib.suppress(OSError):
                            os.unlink(de.path)
                elif de.name.endswith(".json") and de.path != str(keep):
                    files.append((mtime, Path(de.path)))
        excess = len(files) + 1 - self.max_files
        for _mtime, old in sorted(files)[: max(excess, 0)]:
            with contextlib.suppress(OSError):
                os.unlink(old)


# =====================================================================================
# Hashing
# =====================================================================================
_STRIPES = [threading.Lock() for _ in range(64)]


def _folder_lock(key: tuple[str, str]) -> threading.Lock:
    return _STRIPES[hash(key) % len(_STRIPES)]


def _hash_file(
    root: SafeRoot, path: str, algorithm: str, stop: threading.Event | None
) -> tuple[str, os.stat_result, bool] | None:
    """Digest of one file, its stat and whether it may be cached. None if it vanished."""
    try:
        with root.open_read(path) as (fh, _size):
            before = os.fstat(fh.fileno())
            h = hashlib.new(algorithm)
            while chunk := fh.read(READ_CHUNK):
                if stop is not None and stop.is_set():
                    raise HashingCancelled
                h.update(chunk)
            after = os.fstat(fh.fileno())
    except PathError as exc:
        # removed or replaced (e.g. by a symlink) after listing: skip just this entry
        if exc.code in ("not_found", "symlink", "not_a_file"):
            return None
        raise
    unchanged = Signature.of(before) == Signature.of(after)
    # ctime is bumped by every change (mtime can be set to anything): a change within the same
    # coarse timestamp tick would go unnoticed, so files changed this recently are not cached
    settled = abs(time.time_ns() - after.st_ctime_ns) >= RACY_WINDOW_NS
    return h.hexdigest(), before, unchanged and settled


def _folder(rel: str) -> str:
    return "/".join(split_path(rel))


def hash_folder(
    root: SafeRoot,
    cache: HashCache,
    instance_id: UUID,
    rel: str,
    algorithm: str,
    *,
    stop: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Every regular file directly inside ``rel`` with its digest; only uncached files are read."""
    folder = _folder(rel)
    with _folder_lock((str(instance_id), folder)):
        entries = [e for e in root.list(folder) if e.type == "file"]
        fc = cache.load(instance_id, folder)
        fc.retain({e.name for e in entries})
        out: list[dict[str, Any]] = []
        last_save = time.monotonic()
        for entry in entries:
            item = entry.as_dict()
            digest = fc.get(entry.name, Signature.of_entry(entry), algorithm)
            if digest is None:
                result = _hash_file(root, entry.path, algorithm, stop)
                if result is None:
                    continue
                digest, st, cacheable = result
                item.update(size=st.st_size, modified=st.st_mtime)  # what was actually read
                if cacheable and len(fc) < cache.max_entries:
                    fc.put(entry.name, Signature.of(st), algorithm, digest)
                if fc.dirty and time.monotonic() - last_save >= SAVE_INTERVAL:
                    cache.save(instance_id, folder, fc, durable=False)  # progress for concurrent readers
                    last_save = time.monotonic()
            out.append({**item, "hash": digest})
        if fc.dirty:
            cache.save(instance_id, folder, fc)
        return out


def cached_listing(
    root: SafeRoot, cache: HashCache, instance_id: UUID, rel: str, algorithm: str
) -> tuple[list[dict[str, Any]], int]:
    """The folder's regular files with cached digests (``None`` where unknown); reads no file."""
    folder = _folder(rel)
    entries = [e for e in root.list(folder) if e.type == "file"]
    fc = cache.load(instance_id, folder)
    out = []
    pending = 0
    for e in entries:
        digest = fc.get(e.name, Signature.of_entry(e), algorithm)
        pending += digest is None
        out.append({**e.as_dict(), "hash": digest})
    return out, pending


class HashJobs:
    """At most one background pass per (instance, folder, algorithm), ``workers`` at a time."""

    def __init__(self, workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcagent-hash")
        self._jobs: dict[tuple[str, str, str], Future[list[dict[str, Any]]]] = {}
        self._lock = threading.RLock()
        self.stop = threading.Event()

    def ensure(
        self, key: tuple[str, str, str], fn: Callable[[], list[dict[str, Any]]]
    ) -> Future[list[dict[str, Any]]]:
        with self._lock:
            fut = self._jobs.get(key)
            if fut is None or fut.done():
                fut = self._executor.submit(fn)
                self._jobs[key] = fut
                fut.add_done_callback(lambda f, k=key: self._done(k, f))
            return fut

    def _done(self, key: tuple[str, str, str], fut: Future[list[dict[str, Any]]]) -> None:
        with self._lock:
            if self._jobs.get(key) is fut:
                del self._jobs[key]
        exc = None if fut.cancelled() else fut.exception()
        if exc is not None and not isinstance(exc, PathError | HashingCancelled):
            log.warning("Background hashing of %s failed: %s", key[1], exc)

    def running(self) -> int:
        with self._lock:
            return len(self._jobs)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.running():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def shutdown(self) -> None:
        self.stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)


def hash_directory(
    root: SafeRoot,
    cache: HashCache,
    jobs: HashJobs,
    instance_id: UUID,
    rel: str,
    algorithm: str,
    *,
    wait: float | None = None,
) -> dict[str, Any]:
    """Digests of the folder's files: all of them, or (with ``wait``) what is known by then.

    With ``wait`` the result may be partial (``complete`` false, ``hash`` null for files
    not read yet) while a background pass keeps hashing into the cache, so the next call
    returns more. Without it, the call returns when every file is hashed.
    """
    if algorithm not in DIGEST_LENGTHS:
        raise ValueError(f"unsupported algorithm {algorithm}")
    folder = _folder(rel)
    if wait is not None:
        files, pending = cached_listing(root, cache, instance_id, folder, algorithm)
        if pending == 0:
            return {"files": files, "complete": True, "pending": 0}
        if len(files) <= cache.max_entries:  # otherwise progress could never be cached
            fut = jobs.ensure(
                (str(instance_id), folder, algorithm),
                lambda: hash_folder(root, cache, instance_id, folder, algorithm, stop=jobs.stop),
            )
            try:
                return {"files": fut.result(timeout=wait), "complete": True, "pending": 0}
            except FutureTimeout:
                files, pending = cached_listing(root, cache, instance_id, folder, algorithm)
                return {"files": files, "complete": pending == 0, "pending": pending}
            except CancelledError as exc:  # the executor was shut down
                raise HashingCancelled from exc
    return {
        "files": hash_folder(root, cache, instance_id, folder, algorithm, stop=jobs.stop),
        "complete": True,
        "pending": 0,
    }
