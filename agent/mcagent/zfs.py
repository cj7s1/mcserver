"""Native ZFS snapshots for instance directories (same interface as snapshots.BtrfsSnapshots).

Design:

* Each instance directory is its **own dataset**, mounted at the instance path. On a ZFS data
  root, new instances get ``<parent>/mcpanel-<instance-id>`` with an explicit ``mountpoint``;
  ``<parent>`` is the dataset that holds the instances directory (read from
  /proc/self/mountinfo, never from a request). An existing plain directory is converted while
  the server is stopped: the dataset is created at a temporary mountpoint next to it, the files
  are copied in, the directory is moved aside and the dataset is remounted at the instance path.
  A folder that already is a dataset (made by hand on the host) is used as it is.
* Snapshots are ``<dataset>@<YYYYMMDDTHHMMSSZ[-slug]>``. Label and metadata are ZFS user
  properties (``mcpanel:label``, ``mcpanel:kind``, ``mcpanel:consistent``) set atomically with
  the snapshot; the time is ZFS's own ``creation`` property.
* **Rollback never destroys a snapshot.** ``zfs rollback`` can only return to the newest
  snapshot; anything older needs ``-r``, which destroys every newer snapshot, including the
  safety snapshot the panel takes right before a rollback. So the chosen snapshot is cloned
  instead, the current dataset is unmounted and renamed to ``<dataset>-old-<epoch>`` (a
  *history* dataset: never mounted, it keeps the snapshots newer than the target), and the
  clone takes over the name and the mountpoint and is promoted until it depends on no other
  dataset. Every step is undone in reverse order if a later one fails. Snapshots on history
  datasets are listed with the others and can be rolled back to or deleted; a history dataset
  is destroyed once its last snapshot is deleted. A snapshot that other datasets were cloned
  from cannot be deleted, and the error names the snapshots that depend on it.

Every command is an argv list built from validated values: dataset names come from mountinfo
or ``zfs list`` and must match DATASET_RE, snapshot names must match NAME_RE, instance ids are
UUIDs. Requests can never name a dataset.

Inside a container this needs /dev/zfs, CAP_SYS_ADMIN (mounting) and the data directory
bind-mounted at the same path with **shared** propagation. Without the last one a dataset the
agent mounts would exist only in the agent's mount namespace: Docker would bind the empty
directory underneath into the Minecraft container. ``environment_problem`` checks all of it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from . import storage
from .config import Settings
from .docker_ops import AgentError

# Same naming as btrfs snapshots (timestamp + optional slug of the label).
NAME_RE = re.compile(r"^\d{8}T\d{6}Z(-[a-z0-9-]{1,40})?$")
_COMPONENT = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}"
# A pool or dataset: components start with a letter or digit (never '-', so a name can never
# be read as an option), no '@', '#', '%', spaces or '..' components.
PARENT_RE = re.compile(rf"^{_COMPONENT}(/{_COMPONENT})*$")
# An instance dataset is never a pool root.
DATASET_RE = re.compile(rf"^{_COMPONENT}(/{_COMPONENT})+$")
MAX_DATASET_LEN = 200  # ZFS allows 255 including '@snapshot'
HISTORY_SUFFIX_RE = r"-old-\d{10,12}"
ZFS_DEVICE = "/dev/zfs"
LIST_COLUMNS = "name,creation,used,clones,mcpanel:label,mcpanel:kind,mcpanel:consistent"
_META_PROPS = {"kind": "mcpanel:kind", "consistent": "mcpanel:consistent"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
CAP_SYS_ADMIN = 21
log = logging.getLogger(__name__)


# -- environment ------------------------------------------------------------------------
def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def has_sys_admin() -> bool:
    """Whether this process has CAP_SYS_ADMIN (needed to mount datasets)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool((int(line.split()[1], 16) >> CAP_SYS_ADMIN) & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


def nested_bind(mount: dict[str, str]) -> str | None:
    """A bind mount of a subdirectory below the data mount (e.g. a separately mounted backups dir).

    With the data directory bind-mounted ``:rshared``, every bind mount the container runtime
    places below it propagates back to the host at each container start, and those copies pile
    up there (verified with Docker 29: 1, 3, 7, ... mounts). Datasets are mounted with root ``/``,
    bind mounts of a directory are not, so they are told apart by the mountinfo root field.
    """
    base = (mount.get("mount_point") or "").rstrip("/")
    if not base:
        return None
    for other in storage.mounts():
        if other["mount_point"].startswith(base + "/") and other.get("root", "/") != "/":
            return other["mount_point"]
    return None


def environment_problem(mount: dict[str, str] | None, *, same_host_path: bool = True) -> str | None:
    """Why ZFS snapshots cannot work for a path on ``mount`` (None if nothing is missing)."""
    mount = mount or {}
    fstype = mount.get("fstype")
    if fstype != "zfs":
        return f"The data directory is on '{fstype}', not ZFS"
    if not shutil.which("zfs"):
        return "zfs tools are not available: zfsutils-linux is not installed in the agent image"
    if not os.path.exists(ZFS_DEVICE):
        return "/dev/zfs is not available to the agent (pass it to the agent container as a device, see docs/UNRAID.md)"
    if not has_sys_admin():
        return "The agent lacks CAP_SYS_ADMIN, which mounting ZFS datasets needs (cap_add: [SYS_ADMIN], see docs/UNRAID.md)"
    if not same_host_path:
        return "ZFS snapshots need the data directory at the same path inside the agent as on the host (AGENT_HOST_DATA_ROOT differs)"
    if in_container():
        if "shared:" not in (mount.get("propagation") or ""):
            return (
                "The data directory is not bind-mounted into the agent with shared propagation (:rshared), so "
                "datasets the agent mounts would be invisible to Docker; see docs/UNRAID.md"
            )
        leak = nested_bind(mount)
        if leak:
            return (
                f"{leak} is a separate bind mount inside the shared data directory: with :rshared, Docker copies "
                "it onto the host at every agent start and the copies pile up. Set MCPANEL_BACKUP_DIR to a "
                "folder outside MCPANEL_DATA_DIR (see docs/UNRAID.md)"
            )
    if not valid_parent(mount.get("source") or ""):
        return f"The ZFS dataset name '{mount.get('source')}' contains characters MCPanel does not handle"
    return None


def valid_parent(name: str) -> bool:
    return len(name) <= MAX_DATASET_LEN and bool(PARENT_RE.fullmatch(name))


def valid_dataset(name: str) -> bool:
    return len(name) <= MAX_DATASET_LEN and bool(DATASET_RE.fullmatch(name))


def _check_dataset(name: str) -> str:
    if not valid_dataset(name):
        raise AgentError(f"Unexpected ZFS dataset name {name!r}", "invalid_dataset", 500)
    return name


def _zfs() -> str:
    binary = shutil.which("zfs")
    if not binary:
        raise AgentError("zfs tools are not available in the agent image", "snapshots_unavailable", 503)
    return binary


def _try(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run([_zfs(), *args], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"zfs {args[0]} timed out after {timeout} s", "zfs_failed", 504) from exc


def _message(proc: subprocess.CompletedProcess[str]) -> str:
    """The line that explains a zfs failure: 'cannot ...' (later lines are often hints or lists)."""
    lines = [line.strip() for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        return f"exit status {proc.returncode}"
    return next((line for line in lines if line.startswith("cannot ")), lines[-1])[:300]


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    proc = _try(args, timeout)
    if proc.returncode != 0:
        raise AgentError(f"zfs {args[0]} failed: {_message(proc)}", "zfs_failed", 500)
    return proc


def _rows(proc: subprocess.CompletedProcess[str]) -> list[list[str]]:
    return [line.split("\t") for line in proc.stdout.splitlines() if line.strip()]


def _exists(name: str) -> bool:
    return _try(["list", "-H", "-o", "name", name], timeout=60).returncode == 0


def _prop(name: str, prop: str) -> str:
    return _run(["list", "-H", "-p", "-o", prop, name], timeout=60).stdout.strip()


def _ensure_mounted(name: str) -> None:
    if _prop(name, "mounted") != "yes":
        _run(["mount", name])


def _ensure_unmounted(name: str) -> None:
    if _prop(name, "mounted") == "yes":
        _run(["unmount", name])


def _clean(value: str) -> str:
    return _CONTROL.sub(" ", value).strip()


def dataset_mounted_at(path: Path) -> str | None:
    """The ZFS dataset mounted exactly at ``path``, if any.

    A bind mount of a directory of some dataset also shows that dataset as its source, but with
    a root other than ``/``: it is not the dataset of ``path``.
    """
    mount = storage.mount_for(path) or {}
    if (
        mount.get("fstype") == "zfs"
        and mount.get("mount_point") == os.path.realpath(path)
        and mount.get("root", "/") == "/"
    ):
        return mount.get("source")
    return None


def _iso(epoch: str) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]


def create_instance_dataset(settings: Settings, path: Path) -> bool:
    """Create ``path`` (an instance directory that does not exist yet) as its own dataset.

    Returns False, creating nothing, when the environment cannot do ZFS (the caller then makes a
    plain directory). Raises when a dataset would be left behind that could later be mounted
    over a plain directory.
    """
    instance_id = UUID(path.name)
    mount = storage.mount_for(path.parent)
    same = settings.host_instances_dir == settings.instances_dir
    if environment_problem(mount, same_host_path=same) is not None:
        return False
    parent = (mount or {})["source"]
    name = f"{parent}/mcpanel-{instance_id}"
    if not valid_dataset(name):
        return False
    if _exists(name):
        raise AgentError(
            f"A ZFS dataset named {name} already exists but is not mounted at the server folder; "
            "rename or destroy it on the host first",
            "dataset_conflict",
            409,
        )
    host_path = settings.host_instances_dir / path.name
    proc = _try(["create", "-o", f"mountpoint={host_path}", "-o", f"mcpanel:instance={instance_id}", name])
    if proc.returncode == 0 and dataset_mounted_at(path) == name:
        return True
    # Not created, or created but not mounted where Docker will look: never leave it behind
    # (the host would mount it over the plain directory the caller creates instead).
    if _exists(name):
        undo = _try(["destroy", name])
        if undo.returncode != 0:
            raise AgentError(
                f"ZFS dataset {name} was created but not mounted, and removing it failed ({_message(undo)}); "
                "destroy it on the host before retrying",
                "zfs_failed",
                500,
            )
    return False


class ZfsSnapshots:
    provider = "zfs"

    def __init__(self, settings: Settings, instance_id: UUID) -> None:
        self.settings = settings
        self.instance_id = instance_id
        self.instance_dir = settings.instances_dir / str(instance_id)
        self.host_instance_dir = settings.host_instances_dir / str(instance_id)

    # -- discovery -----------------------------------------------------------------------
    def _parent_mount(self) -> dict[str, str]:
        return storage.mount_for(self.settings.instances_dir) or {}

    def _environment_problem(self) -> str | None:
        same = self.settings.host_instances_dir == self.settings.instances_dir
        return environment_problem(self._parent_mount(), same_host_path=same)

    def dataset(self) -> str | None:
        """The dataset mounted at the instance directory (validated), or None for a plain folder.

        Anything else mounted at the folder is refused rather than treated as the server's own
        dataset: a bind mount names the dataset it comes from, and rolling back, deleting or
        destroying that one would hit data that is not this server's.
        """
        mount = storage.mount_for(self.instance_dir) or {}
        if mount.get("mount_point") != os.path.realpath(self.instance_dir):
            return None  # a plain folder on the data directory's filesystem
        name = dataset_mounted_at(self.instance_dir)
        if name is None:
            raise AgentError(
                "The server folder is a mount point but not a ZFS dataset of its own (a bind mount?); "
                "MCPanel does not manage snapshots for it",
                "invalid_dataset",
                409,
            )
        _check_dataset(name)
        mountpoint = _prop(name, "mountpoint")
        if mountpoint != str(self.host_instance_dir):
            raise AgentError(
                f"ZFS dataset {name} is mounted at the server folder but its mountpoint is {mountpoint!r} "
                "(a bind mount?); MCPanel only manages a dataset whose mountpoint is the server folder",
                "invalid_dataset",
                409,
            )
        return name

    def _history_re(self, live: str) -> re.Pattern[str]:
        return re.compile(rf"^{re.escape(live)}{HISTORY_SUFFIX_RE}$")

    def _family(self, live: str) -> list[str]:
        """The live dataset followed by its history datasets (oldest first)."""
        parent = live.rsplit("/", 1)[0]
        pattern = self._history_re(live)
        rows = _rows(
            _run(["list", "-H", "-t", "filesystem", "-d", "1", "-o", "name,mcpanel:instance", parent])
        )
        history = sorted(
            row[0]
            for row in rows
            if len(row) == 2
            and pattern.match(row[0])
            and row[1] == str(self.instance_id)
            and valid_dataset(row[0])
        )
        return [live, *history]

    def _snapshot_rows(self, datasets: list[str]) -> list[dict[str, Any]]:
        if not datasets:
            return []  # 'zfs list' without names would list every snapshot on the host
        rows = _rows(_run(["list", "-H", "-p", "-t", "snapshot", "-d", "1", "-o", LIST_COLUMNS, *datasets]))
        out = []
        for row in rows:
            if len(row) != 7:
                continue
            full, creation, used, clones, label, kind, consistent = row
            ds, _, name = full.partition("@")
            if ds not in datasets:
                continue
            out.append(
                {
                    "dataset": ds,
                    "name": name,
                    "ours": bool(NAME_RE.fullmatch(name)),
                    "creation": creation,
                    "used": used,
                    "clones": [c for c in clones.split(",") if c and c != "-"],
                    "label": "" if label == "-" else label,
                    "kind": None if kind == "-" else kind,
                    "consistent": {"true": True, "false": False}.get(consistent),
                }
            )
        return out

    # -- capability ----------------------------------------------------------------------
    def capability(self) -> dict[str, Any]:
        mount = self._parent_mount()
        info: dict[str, Any] = {
            "provider": "zfs",
            "available": False,
            "fstype": mount.get("fstype"),
            "reason": None,
            # legacy name kept for older panels: "the folder is its own subvolume/dataset"
            "instance_is_subvolume": False,
            "instance_ready": False,
            "dataset": None,
        }
        problem = self._environment_problem()
        if problem:
            info["reason"] = problem
            return info
        probe = _try(["list", "-H", "-o", "name", mount["source"]], timeout=60)
        if probe.returncode != 0:
            info["reason"] = f"The zfs command cannot use the ZFS kernel module: {_message(probe)}"
            return info
        info["available"] = True
        version = _try(["version"], timeout=30)
        if version.returncode == 0:
            info["zfs_version"] = " / ".join(version.stdout.split())
        try:
            live = self.dataset()
        except AgentError as exc:
            info["available"] = False
            info["reason"] = str(exc)
            return info
        info["dataset"] = live
        info["instance_is_subvolume"] = info["instance_ready"] = live is not None
        if live is None:
            info["reason"] = (
                "This server's folder is a plain directory; convert it to a ZFS dataset (server stopped) to enable snapshots"
            )
        return info

    def require_ready(self) -> str:
        cap = self.capability()
        if not cap["available"]:
            raise AgentError(cap["reason"] or "Snapshots unavailable", "snapshots_unavailable", 409)
        if not cap["instance_ready"]:
            raise AgentError(cap["reason"], "not_subvolume", 409)
        return cap["dataset"]

    # -- conversion ----------------------------------------------------------------------
    def convert(self) -> dict[str, Any]:
        """Turn the existing instance directory into its own dataset (copy + swap)."""
        if self.dataset():
            return {"converted": False, "reason": "already a dataset"}
        problem = self._environment_problem()
        if problem:
            raise AgentError(problem, "snapshots_unavailable", 409)
        if not self.instance_dir.is_dir():
            raise AgentError("Instance directory does not exist", "not_found", 404)
        parent = self._parent_mount()["source"]
        name = _check_dataset(f"{parent}/mcpanel-{self.instance_id}")
        tmp = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-convert")
        host_tmp = self.settings.host_instances_dir / tmp.name
        if os.path.lexists(tmp) or _exists(name):
            self._clear_interrupted_convert(name, tmp, host_tmp)
        old = self.instance_dir.with_name(f"{self.instance_id}.mcpanel-preconvert-{int(time.time())}")
        moved = False
        try:
            _run(
                ["create", "-o", f"mountpoint={host_tmp}", "-o", f"mcpanel:instance={self.instance_id}", name]
            )
            if dataset_mounted_at(tmp) != name:
                raise AgentError("The new dataset was not mounted where expected", "zfs_failed", 500)
            # cp -a keeps ownership, modes and symlinks (never followed); --reflink=auto uses
            # block cloning where the pool supports it, a plain copy otherwise.
            proc = subprocess.run(
                ["cp", "-a", "--reflink=auto", f"{self.instance_dir}/.", str(tmp)],
                capture_output=True,
                text=True,
                timeout=6 * 3600,
                check=False,
            )
            if proc.returncode != 0:
                raise AgentError(f"Copy failed: {proc.stderr.strip()[-300:]}", "convert_failed", 500)
            self.instance_dir.rename(old)
            moved = True
            _run(["set", f"mountpoint={self.host_instance_dir}", name])  # remounts at the instance path
            if dataset_mounted_at(self.instance_dir) != name:
                raise AgentError("The dataset was not mounted at the server folder", "zfs_failed", 500)
        except BaseException:
            if _exists(name):
                _try(["destroy", name])  # unmounts it wherever it is
            if moved:
                with contextlib.suppress(OSError):
                    self.instance_dir.rmdir()  # the (now empty) mountpoint directory
                if not self.instance_dir.exists():
                    old.rename(self.instance_dir)  # put the original back
            with contextlib.suppress(OSError):
                tmp.rmdir()
            raise
        with contextlib.suppress(OSError):
            tmp.rmdir()
        result: dict[str, Any] = {"converted": True, "dataset": name}
        shutil.rmtree(old, ignore_errors=True)
        if os.path.lexists(old):
            log.warning("The folder before the conversion was not removed completely: %s", old)
            result["warnings"] = [
                "The server folder as it was before the conversion could not be removed completely; "
                f"the rest is in {old}. It still uses disk space; delete it on the host"
            ]
            result["leftovers"] = [str(old)]
        return result

    def _clear_interrupted_convert(self, name: str, tmp: Path, host_tmp: Path) -> None:
        """Remove what a conversion that was interrupted during its copy left: its dataset (still
        mounted at the ``.mcpanel-convert`` folder, a partial copy: the server folder itself is
        still a plain directory) and that folder. Anything that does not look exactly like that
        is left alone (409 ``convert_conflict``)."""

        def conflict(why: str) -> AgentError:
            return AgentError(
                f"A previous conversion was interrupted and what it left cannot be removed safely ({why}): "
                f"remove the folder {tmp} and the dataset {name} if it exists",
                "convert_conflict",
                409,
            )

        if _exists(name):
            if _prop(name, "mcpanel:instance") != str(self.instance_id):
                raise conflict(f"the dataset {name} is not marked as this server's")
            if _prop(name, "mountpoint") != str(host_tmp):
                raise conflict(f"the dataset {name} is not mounted at {host_tmp}")
            proc = _try(["destroy", name])  # without -r: refused if anything was made of it
            if proc.returncode != 0:
                raise conflict(_message(proc))
            log.warning("Destroyed the dataset %s, left by an interrupted conversion", name)
        if os.path.lexists(tmp):
            from .snapshots import _discard, is_mount_point

            if is_mount_point(tmp):
                raise conflict(f"something is still mounted at {tmp}")

            problem = _discard(tmp)
            if problem:
                raise conflict(problem)
            log.warning("Removed %s, left by an interrupted conversion", tmp)

    # -- snapshots -----------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        live = self.dataset()
        if live is None:
            return []
        out = []
        for row in self._snapshot_rows(self._family(live)):
            if not row["ours"]:
                continue  # snapshots made by other tools are left alone
            item: dict[str, Any] = {
                "name": row["name"],
                "label": row["label"],
                "created_at": _iso(row["creation"]),
            }
            if row["kind"]:
                item["kind"] = row["kind"]
            if row["consistent"] is not None:
                item["consistent"] = row["consistent"]
            if row["used"].isdigit():
                item["used_bytes"] = int(row["used"])
            if row["dataset"] != live:
                item["retained"] = True  # kept on a history dataset from before a rollback
            out.append(item)
        out.sort(key=lambda s: s["name"], reverse=True)
        return out

    def _find(self, name: str) -> tuple[str, list[str], dict[str, Any]]:
        """(live dataset, family, row) for one of our snapshots."""
        if not NAME_RE.fullmatch(name):
            raise AgentError("Invalid snapshot name", "invalid_snapshot", 400)
        live = self.dataset()
        if live is None:
            raise AgentError("Snapshot not found", "snapshot_not_found", 404)
        family = self._family(live)
        for row in self._snapshot_rows(family):
            if row["name"] == name:
                return live, family, row
        raise AgentError("Snapshot not found", "snapshot_not_found", 404)

    def _snapshot(self, dataset: str, name: str, label: str, meta: dict[str, Any] | None) -> None:
        props = {"mcpanel:instance": str(self.instance_id)}
        if _clean(label):
            props["mcpanel:label"] = _clean(label)[:120]
        for key, value in (meta or {}).items():
            if key in _META_PROPS:
                props[_META_PROPS[key]] = (
                    ("true" if value else "false") if isinstance(value, bool) else _clean(str(value))[:64]
                )
        args = ["snapshot"]
        for key, value in props.items():
            args += ["-o", f"{key}={value}"]
        _run([*args, f"{dataset}@{name}"])

    def create(self, label: str = "", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        live = self.require_ready()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        slug = _slugify(label)
        name = f"{stamp}-{slug}" if slug else stamp
        if any(row["name"] == name for row in self._snapshot_rows(self._family(live))):
            raise AgentError(
                "A snapshot with this timestamp already exists; retry in a second", "snapshot_exists", 409
            )
        self._snapshot(live, name, label, meta)
        return {
            "name": name,
            "label": label[:120],
            "created_at": datetime.now(UTC).isoformat(),
            **(meta or {}),
        }

    def _dependents_error(self, name: str, clones: list[str], family: list[str]) -> AgentError:
        ours = [c for c in clones if c in family]
        foreign = [c for c in clones if c not in family]
        if foreign:
            return AgentError(
                f"Snapshot {name} cannot be deleted: other ZFS datasets were cloned from it ({', '.join(foreign)}); "
                "remove them on the host first",
                "snapshot_has_dependents",
                409,
            )
        names = sorted({row["name"] for row in self._snapshot_rows(ours) if row["ours"]}, reverse=True)
        return AgentError(
            f"Snapshot {name} cannot be deleted yet: the server state from before a rollback is based on it"
            + (f" (snapshots {', '.join(names)}). Delete those first." if names else "."),
            "snapshot_has_dependents",
            409,
        )

    def delete(self, name: str) -> None:
        live, family, row = self._find(name)
        if row["clones"]:
            raise self._dependents_error(name, row["clones"], family)
        _run(["destroy", f"{row['dataset']}@{name}"])
        dataset = row["dataset"]
        if dataset == live:
            return
        try:
            if not self._snapshot_rows([dataset]):
                # A history dataset without snapshots holds nothing anyone can roll back to.
                _run(["destroy", dataset])
        except AgentError as exc:
            # The snapshot is gone, which is what was asked; the empty dataset goes with the server.
            log.warning("Could not remove the empty history dataset %s: %s", dataset, exc)

    # -- rollback ------------------------------------------------------------------------
    def restore(self, name: str) -> dict[str, Any]:
        self.require_ready()
        live, family, row = self._find(name)
        stamp = int(time.time())
        history = f"{live}-old-{stamp}"
        clone = f"{live}-new-{stamp}"
        for candidate in (history, clone):
            _check_dataset(candidate)
            if _exists(candidate):
                raise AgentError("Another rollback ran this second; retry", "rollback_conflict", 409)
        warnings: list[str] = []
        # The state being replaced must stay reachable from the list: the panel takes a safety
        # snapshot first, this covers callers that did not (or wrote something after it).
        if not self._state_captured(live):
            auto = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-before-rollback"
            if not any(r["name"] == auto for r in self._snapshot_rows(family)):
                self._snapshot(
                    live, auto, f"before rollback to {name}", {"kind": "pre_restore", "consistent": True}
                )

        mountpoint = str(self.host_instance_dir)
        undo: list[tuple[str, Any]] = []

        def step(action: Any, revert: Any, what: str) -> None:
            action()
            undo.append((what, revert))

        try:
            step(
                lambda: _run(
                    [
                        "clone",
                        "-o",
                        "canmount=noauto",
                        "-o",
                        f"mountpoint={mountpoint}",
                        "-o",
                        f"mcpanel:instance={self.instance_id}",
                        f"{row['dataset']}@{name}",
                        clone,
                    ]
                ),
                lambda: _run(["destroy", clone]),
                "remove the clone",
            )
            try:
                step(
                    lambda: _ensure_unmounted(live),
                    lambda: _ensure_mounted(live),
                    "remount the current dataset",
                )
            except AgentError as exc:
                raise AgentError(
                    f"The server folder is busy and cannot be unmounted ({exc!s})", "dataset_busy", 409
                ) from exc
            step(
                lambda: _run(["set", "mountpoint=none", live]),
                lambda: _run(["set", f"mountpoint={mountpoint}", live]),
                "restore its mountpoint",
            )
            step(
                lambda: _run(["rename", live, history]),
                lambda: _run(["rename", history, live]),
                "rename it back",
            )
            step(
                lambda: _run(["rename", clone, live]),
                lambda: _run(["rename", live, clone]),
                "rename the clone back",
            )
            step(
                lambda: _run(["set", "canmount=on", live]),
                lambda: _run(["set", "canmount=noauto", live]),
                "reset canmount",
            )
            step(lambda: _ensure_mounted(live), lambda: _ensure_unmounted(live), "unmount the clone")
            if dataset_mounted_at(self.instance_dir) != live:
                raise AgentError(
                    "The rolled-back dataset did not appear at the server folder", "zfs_failed", 500
                )
        except BaseException as exc:
            failed = []
            for what, revert in reversed(undo):
                try:
                    revert()
                except Exception:  # keep undoing the remaining steps
                    failed.append(what)
            if failed:
                raise AgentError(
                    f"Rollback failed ({exc}) and could not be undone automatically ({', '.join(failed)}). "
                    f"The previous files are in ZFS dataset {history if _exists(history) else live}; "
                    "check 'zfs list' on the host before starting the server",
                    "rollback_failed",
                    500,
                ) from exc
            raise
        # The rollback is done; the rest only tidies up, never undoes it and never raises.
        try:
            _run(["set", f"mcpanel:instance={self.instance_id}", history])
        except AgentError as exc:
            warnings.append(f"Could not tag the history dataset {history}: {exc!s}")
        origin = "its origin"
        for _ in range(64):
            try:
                origin = _prop(live, "origin")
                if origin in ("", "-"):
                    break
                _run(["promote", live])
            except AgentError as exc:
                warnings.append(f"Could not promote {live}; it still depends on {origin}: {exc!s}")
                break
        warnings += self._tidy_history(live)
        return {"restored": name, "dataset": live, "history_dataset": history, "warnings": warnings}

    def _state_captured(self, live: str) -> bool:
        """True if the files of ``live`` are exactly its newest MCPanel snapshot.

        Judged against our own snapshots only: a newer snapshot by another tool (sanoid,
        zfs-auto-snapshot, ...) is not listed in the panel, so it does not count.
        """
        ours = [row for row in self._snapshot_rows([live]) if row["ours"]]
        if not ours:
            return False
        newest = max(ours, key=lambda r: (int(r["creation"]) if r["creation"].isdigit() else 0, r["name"]))
        try:
            return _prop(live, f"written@{newest['name']}") == "0"
        except AgentError:
            return False  # unknown: take a snapshot to be safe

    def _tidy_history(self, live: str) -> list[str]:
        """Destroy history datasets that have no snapshots and no changes of their own.

        After a promote a history dataset can be left with no snapshots at all, its state being
        identical to the snapshot it now hangs off ('written' = 0): nothing would be lost.
        Problems are returned as warnings: the rollback itself is already done.
        """
        warnings = []
        try:
            family = self._family(live)
            with_snaps = {row["dataset"] for row in self._snapshot_rows(family)}
        except AgentError as exc:
            return [f"Could not check the history datasets: {exc!s}"]
        for dataset in family[1:]:
            if dataset in with_snaps:
                continue
            try:
                if _prop(dataset, "written") == "0":
                    _run(["destroy", dataset])
                else:
                    warnings.append(f"History dataset {dataset} has changes but no snapshots; kept")
            except AgentError as exc:
                warnings.append(f"Could not remove the empty history dataset {dataset}: {exc!s}")
        return warnings

    # -- instance deletion ---------------------------------------------------------------
    def destroy_all(self) -> dict[str, Any]:
        """Destroy the instance dataset, its history datasets and all their snapshots."""
        live = self.dataset()
        if live is None:
            return {"destroyed": [], "snapshots": 0, "warnings": []}
        remaining = self._family(live)
        count = sum(1 for row in self._snapshot_rows(remaining) if row["ours"])
        destroyed: list[str] = []
        while remaining:
            rows = self._snapshot_rows(remaining)
            blocked = {row["dataset"] for row in rows if row["clones"]}
            ready = [ds for ds in remaining if ds not in blocked]
            if not ready:
                clones = sorted({c for row in rows for c in row["clones"]})
                raise AgentError(
                    f"Cannot delete ZFS datasets {', '.join(remaining)}: datasets outside MCPanel were cloned from "
                    f"their snapshots ({', '.join(clones)})",
                    "dataset_has_dependents",
                    409,
                )
            for ds in ready:
                _run(["destroy", "-r", ds])  # its own snapshots; never -R (foreign clones)
                destroyed.append(ds)
                remaining.remove(ds)
        return {"destroyed": destroyed, "snapshots": count, "warnings": []}
