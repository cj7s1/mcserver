"""A fake ``zfs`` command for the ZFS snapshot tests (there is no ZFS kernel module in CI).

Installed by ``tests/test_snapshots_zfs.py`` as an executable named ``zfs`` first on PATH. It
records every argv to ``$FAKE_ZFS_STATE/calls.jsonl`` and emulates the subset of zfs(8) the
agent uses, with real files: a mounted dataset's files live at its mountpoint, an unmounted
one's in ``$FAKE_ZFS_STATE/data/``, snapshots are copies in ``$FAKE_ZFS_STATE/snaps/``.
"Mounting" moves the directory into place, which is what the agent's view (patched
``storage.mount_for``) reads. Semantics follow OpenZFS: snapshots with clones cannot be
destroyed, promote moves the snapshots up to the origin, rename keeps snapshots.

Failure injection: ``state["fail"]`` holds rules ``{"match": "<prefix of the argv>", "message":
..., "times": 1, "mode": "before"|"nomount"}``. "before" fails without doing anything;
"nomount" (create/clone) creates the dataset but does not mount it, then fails. ``"exact": true``
matches the whole argv instead of a prefix.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

STATE_DIR = Path(os.environ["FAKE_ZFS_STATE"])
STATE_FILE = STATE_DIR / "state.json"


class ZfsFail(Exception):
    pass


def load() -> dict:
    return json.loads(STATE_FILE.read_text())


def save(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_FILE)


def store(name: str) -> Path:
    return STATE_DIR / "data" / quote(name, safe="")


def snapstore(name: str) -> Path:
    return STATE_DIR / "snaps" / quote(name, safe="")


def content_dir(state: dict, ds: str) -> Path:
    d = state["datasets"][ds]
    return Path(d["mountpoint"]) if d["mounted"] else store(ds)


def tree(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            p = Path(root) / name
            rel = str(p.relative_to(path))
            if p.is_symlink():
                out[rel] = "link:" + os.readlink(p)
            elif p.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = p.read_bytes().hex()
    return out


def parse_opts(args: list[str], flags: str = "", valued: str = "o") -> tuple[dict, list, list[str]]:
    """Returns ({flag: True | [values]}, [-o k=v], positionals)."""
    opts: dict = {}
    props: list = []
    pos: list[str] = []
    it = iter(args)
    for a in it:
        if a.startswith("-") and len(a) > 1 and not pos:
            for i, ch in enumerate(a[1:]):
                if ch in valued:
                    val = a[i + 2 :] or next(it)
                    if ch == "o" and "=" in val and valued == "o":
                        props.append(val.split("=", 1))
                    else:
                        opts.setdefault(ch, []).append(val)
                    break
                if ch not in flags:
                    raise ZfsFail(f"invalid option '{ch}'")
                opts[ch] = True
        else:
            pos.append(a)
    return opts, props, pos


def need_ds(state: dict, name: str) -> dict:
    if name not in state["datasets"]:
        raise ZfsFail(f"cannot open '{name}': dataset does not exist")
    return state["datasets"][name]


def check_name(name: str) -> None:
    if not name or name.startswith("-") or " " in name or ".." in name.split("/"):
        raise ZfsFail(f"cannot open '{name}': invalid character in name")


def do_mount(state: dict, ds: str) -> None:
    d = state["datasets"][ds]
    if d["mounted"]:
        raise ZfsFail(f"cannot mount '{ds}': filesystem already mounted")
    if d["mountpoint"] in ("none", "legacy", "-"):
        raise ZfsFail(f"cannot mount '{ds}': no mountpoint set")
    if d.get("canmount") == "off":
        raise ZfsFail(f"cannot mount '{ds}': 'canmount' property is set to 'off'")
    target = Path(d["mountpoint"])
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise ZfsFail(f"cannot mount '{target}': directory is not empty")
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    store(ds).rename(target)
    d["mounted"] = True


def do_unmount(state: dict, ds: str) -> None:
    d = state["datasets"][ds]
    if not d["mounted"]:
        raise ZfsFail(f"cannot unmount '{ds}': not currently mounted")
    target = Path(d["mountpoint"])
    store(ds).parent.mkdir(parents=True, exist_ok=True)
    target.rename(store(ds))
    target.mkdir()  # the empty mountpoint directory stays behind
    d["mounted"] = False


def snaps_of(state: dict, ds: str) -> list[str]:
    return sorted(
        (s for s in state["snapshots"] if s.split("@")[0] == ds),
        key=lambda s: state["snapshots"][s]["creation"],
    )


def clones_of(state: dict, snap: str) -> list[str]:
    return sorted(n for n, d in state["datasets"].items() if d.get("origin") == snap)


def written(state: dict, ds: str) -> str:
    current = tree(content_dir(state, ds))
    snaps = snaps_of(state, ds)
    if snaps:
        base = tree(snapstore(snaps[-1]))
    elif state["datasets"][ds].get("origin", "-") != "-":
        base = tree(snapstore(state["datasets"][ds]["origin"]))
    else:
        base = {}
    return "0" if current == base else "4096"


def value(state: dict, name: str, col: str) -> str:
    if "@" in name:
        s = state["snapshots"][name]
        if col == "name":
            return name
        if col == "creation":
            return str(s["creation"])
        if col == "used":
            return "8192"
        if col == "clones":
            return ",".join(clones_of(state, name))
        return s["props"].get(col, "-")
    d = state["datasets"][name]
    if col == "name":
        return name
    if col == "mounted":
        return "yes" if d["mounted"] else "no"
    if col == "written":
        return written(state, name)
    if col.startswith("written@"):
        snap = col.split("@", 1)[1]
        full = snap if "@" in snap else f"{name}@{snap}"
        if full not in state["snapshots"]:
            raise ZfsFail(f"cannot get property '{col}': snapshot does not exist")
        return "0" if tree(content_dir(state, name)) == tree(snapstore(full)) else "4096"
    if col in ("mountpoint", "origin", "canmount", "creation"):
        return str(d.get(col, "-"))
    return d["props"].get(col, "-")


def cmd_list(state: dict, args: list[str]) -> None:
    opts, _props, names = parse_opts(args, flags="Hpr", valued="otds")
    cols = opts.get("o", ["name"])[0].split(",")
    types = (opts.get("t", ["filesystem"])[0]).split(",")
    depth = int(opts.get("d", ["0"])[0])
    rows = []
    for n in names or sorted(state["datasets"]):
        if "@" in n:
            if n not in state["snapshots"]:
                raise ZfsFail(f"cannot open '{n}': dataset does not exist")
            rows.append(n)
            continue
        need_ds(state, n)
        if "filesystem" in types:
            rows.append(n)
            if depth >= 1:
                rows += sorted(
                    c for c in state["datasets"] if c.startswith(n + "/") and "/" not in c[len(n) + 1 :]
                )
        if "snapshot" in types and depth >= 1:
            rows += snaps_of(state, n)
    for r in rows:
        print("\t".join(value(state, r, c) for c in cols))


def inherited_mountpoint(state: dict, name: str) -> str:
    parent, leaf = name.rsplit("/", 1)
    pm = state["datasets"][parent]["mountpoint"]
    return "none" if pm in ("none", "legacy") else f"{pm}/{leaf}"


def new_dataset(state: dict, name: str, props: list, origin: str = "-") -> dict:
    check_name(name)
    if name in state["datasets"]:
        raise ZfsFail(f"cannot create '{name}': dataset already exists")
    if "/" not in name or name.rsplit("/", 1)[0] not in state["datasets"]:
        raise ZfsFail(f"cannot create '{name}': parent does not exist")
    d = {"mountpoint": None, "mounted": False, "canmount": "on", "origin": origin, "props": {}}
    for k, v in props:
        if k in ("mountpoint", "canmount"):
            d[k] = v
        elif ":" in k:
            d["props"][k] = v
        else:
            raise ZfsFail(f"invalid property '{k}'")
    d["mountpoint"] = d["mountpoint"] or inherited_mountpoint(state, name)
    state["datasets"][name] = d
    return d


def cmd_create(state: dict, args: list[str], rule: dict | None) -> None:
    _opts, props, pos = parse_opts(args)
    (name,) = pos
    d = new_dataset(state, name, props)
    store(name).mkdir(parents=True)
    if rule and rule.get("mode") == "nomount":
        raise ZfsFail("filesystem successfully created, but not mounted")
    if d["canmount"] == "on" and d["mountpoint"] != "none":
        do_mount(state, name)


def cmd_clone(state: dict, args: list[str]) -> None:
    _opts, props, pos = parse_opts(args)
    snap, name = pos
    if snap not in state["snapshots"]:
        raise ZfsFail(f"cannot open '{snap}': dataset does not exist")
    d = new_dataset(state, name, props, origin=snap)
    shutil.copytree(snapstore(snap), store(name), symlinks=True)
    if d["canmount"] == "on" and d["mountpoint"] != "none":
        do_mount(state, name)


def cmd_snapshot(state: dict, args: list[str]) -> None:
    _opts, props, pos = parse_opts(args)
    (full,) = pos
    ds, _, short = full.partition("@")
    check_name(short)
    need_ds(state, ds)
    if full in state["snapshots"]:
        raise ZfsFail(f"cannot create snapshot '{full}': dataset already exists")
    state["clock"] += 1
    shutil.copytree(content_dir(state, ds), snapstore(full), symlinks=True)
    state["snapshots"][full] = {"creation": state["clock"], "props": {k: v for k, v in props}}


def destroy_snapshot(state: dict, full: str) -> None:
    if clones_of(state, full):
        raise ZfsFail(
            f"cannot destroy '{full}': snapshot has dependent clones\nuse '-R' to destroy the following datasets:"
        )
    shutil.rmtree(snapstore(full))
    del state["snapshots"][full]


def cmd_destroy(state: dict, args: list[str]) -> None:
    opts, _props, pos = parse_opts(args, flags="r")
    (name,) = pos
    if "@" in name:
        if name not in state["snapshots"]:
            raise ZfsFail("could not find any snapshots to destroy; check snapshot names.")
        destroy_snapshot(state, name)
        return
    d = need_ds(state, name)
    if d.get("external"):
        raise ZfsFail("test bug: refusing to destroy the parent dataset")
    children = [c for c in state["datasets"] if c.startswith(name + "/")]
    snaps = snaps_of(state, name)
    if (children or snaps) and not opts.get("r"):
        raise ZfsFail(
            f"cannot destroy '{name}': filesystem has children\nuse '-r' to destroy the following datasets:"
        )
    for s in snaps:
        if clones_of(state, s):
            raise ZfsFail(f"cannot destroy '{s}': snapshot has dependent clones")
    for c in sorted(children, reverse=True):
        cmd_destroy(state, ["-r", c])
    for s in snaps:
        destroy_snapshot(state, s)
    if d["mounted"]:
        do_unmount(state, name)
    shutil.rmtree(store(name))
    del state["datasets"][name]


def cmd_promote(state: dict, args: list[str]) -> None:
    (name,) = args
    d = need_ds(state, name)
    origin = d.get("origin", "-")
    if origin == "-":
        raise ZfsFail(f"cannot promote '{name}': not a cloned filesystem")
    odata, _, _ = origin.partition("@")
    limit = state["snapshots"][origin]["creation"]
    moving = [s for s in snaps_of(state, odata) if state["snapshots"][s]["creation"] <= limit]
    mine = {s.split("@")[1] for s in snaps_of(state, name)}
    for s in moving:
        if s.split("@")[1] in mine:
            raise ZfsFail(f"cannot promote '{name}': snapshot name conflict")
    renamed = {}
    for s in moving:
        new = f"{name}@{s.split('@')[1]}"
        snapstore(s).rename(snapstore(new))
        state["snapshots"][new] = state["snapshots"].pop(s)
        renamed[s] = new
    for other in state["datasets"].values():
        if other.get("origin") in renamed:
            other["origin"] = renamed[other["origin"]]
    d["origin"] = state["datasets"][odata].get("origin", "-")
    state["datasets"][odata]["origin"] = renamed[origin]


def cmd_rename(state: dict, args: list[str]) -> None:
    old, new = args
    check_name(new)
    d = need_ds(state, old)
    if new in state["datasets"]:
        raise ZfsFail(f"cannot rename to '{new}': dataset already exists")
    if new.rsplit("/", 1)[0] not in state["datasets"]:
        raise ZfsFail(f"cannot rename to '{new}': parent does not exist")
    if not d["mounted"]:
        store(old).rename(store(new))
    state["datasets"][new] = state["datasets"].pop(old)
    for s in list(state["snapshots"]):
        if s.split("@")[0] == old:
            ns = f"{new}@{s.split('@')[1]}"
            snapstore(s).rename(snapstore(ns))
            state["snapshots"][ns] = state["snapshots"].pop(s)
    for other in state["datasets"].values():
        if other.get("origin", "-").split("@")[0] == old:
            other["origin"] = new + "@" + other["origin"].split("@")[1]


def cmd_set(state: dict, args: list[str]) -> None:
    *assignments, name = args
    d = need_ds(state, name)
    for a in assignments:
        k, v = a.split("=", 1)
        if k == "mountpoint":
            was = d["mounted"]
            if was:
                do_unmount(state, name)
            d["mountpoint"] = v
            if v not in ("none", "legacy") and d.get("canmount") == "on":
                do_mount(state, name)
        elif k == "canmount":
            d["canmount"] = v
        elif ":" in k:
            d["props"][k] = v
        else:
            raise ZfsFail(f"cannot set property for '{name}': invalid property '{k}'")


def main(argv: list[str]) -> int:
    with (STATE_DIR / "calls.jsonl").open("a") as fh:
        fh.write(json.dumps(argv) + "\n")
    state = load()
    joined = " ".join(argv)
    rule = None
    for r in state.get("fail", []):
        hit = joined == r["match"] if r.get("exact") else joined.startswith(r["match"])
        if r.get("times", 1) > 0 and hit:
            r["times"] = r.get("times", 1) - 1
            rule = r
            break
    try:
        if rule and rule.get("mode", "before") == "before":
            raise ZfsFail(rule.get("message", "injected failure"))
        if not argv:
            raise ZfsFail("missing command")
        cmd, args = argv[0], argv[1:]
        if cmd == "version":
            print("zfs-2.3.9-0+deb13u1\nzfs-kmod-2.3.9-1")
        elif cmd == "list":
            cmd_list(state, args)
        elif cmd == "create":
            cmd_create(state, args, rule)
        elif cmd == "clone":
            cmd_clone(state, args)
        elif cmd == "snapshot":
            cmd_snapshot(state, args)
        elif cmd == "destroy":
            cmd_destroy(state, args)
        elif cmd == "promote":
            cmd_promote(state, args)
        elif cmd == "rename":
            cmd_rename(state, args)
        elif cmd == "set":
            cmd_set(state, args)
        elif cmd == "mount":
            do_mount(state, need_ds(state, args[0]) and args[0])
        elif cmd in ("unmount", "umount"):
            do_unmount(state, need_ds(state, args[0]) and args[0])
        else:
            raise ZfsFail(f"unrecognized command '{cmd}'")
    except ZfsFail as exc:
        save(state)
        print(str(exc), file=sys.stderr)
        return 1
    save(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
