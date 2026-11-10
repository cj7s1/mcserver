"""A stand-in ``restic`` for the tests of lock handling, signals and dumps (restic is not installed
on every test machine, and a real one cannot be made to leave a stale lock on purpose).

Installed by :func:`install` as an executable script; the agent runs it with its own environment
(``HOME`` is the agent's state folder), so the stand-in keeps its files in ``$HOME/fake-restic``:

* ``plan.json``: ``{"<subcommand>": [behaviour, ...]}``. Each run of a subcommand takes the first
  behaviour of its list (the last one stays and is used for every later run). A behaviour is
  ``{"code": 0, "stdout": "", "stderr": "", "sleep": 0, "sigint": "exit" | "ignore",
  "sigterm": "exit" | "ignore"}``; without an entry a subcommand prints nothing and exits 0;
* ``log.jsonl``: one line when a run starts (``{"argv": [...], "repo": ..., "pid": ..., "at": <time>}``),
  one when it ends normally (``{"ended": [...argv], "repo": ..., "pid": ..., "at": <time>}``) and one
  per signal received (``{"signal": "SIGINT", "argv": [...]}``), written as they happen;
  :func:`runs` pairs them.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

SCRIPT = """#!{python}
import sys
sys.path.insert(0, {tests!r})
import fake_restic
raise SystemExit(fake_restic.main(sys.argv[1:]))
"""


def install(folder: Path) -> Path:
    """Write the executable stand-in into ``folder``; returns its path (for ``restic_binary``)."""
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / "restic"
    binary.write_text(SCRIPT.format(python=sys.executable, tests=str(Path(__file__).parent)))
    binary.chmod(0o755)
    return binary


def state_dir(home: Path) -> Path:
    return home / "fake-restic"


def set_plan(home: Path, plan: dict) -> None:
    d = state_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(json.dumps(plan))


def calls(home: Path) -> list[dict]:
    path = state_dir(home) / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def commands(home: Path) -> list[list[str]]:
    """The argv of every run, in order."""
    return [c["argv"] for c in calls(home) if "argv" in c and "signal" not in c]


def runs(home: Path) -> list[dict]:
    """One entry per run, in start order: ``{"argv", "repo", "start", "end"}`` (``end`` None when
    it did not end normally: still running, or stopped by a signal)."""
    out: list[dict] = []
    by_pid: dict[int, dict] = {}
    for c in calls(home):
        if "argv" in c and "signal" not in c:
            run = {"argv": c["argv"], "repo": c["repo"], "start": c["at"], "end": None}
            out.append(run)
            by_pid[c["pid"]] = run
        elif "ended" in c and c["pid"] in by_pid:
            by_pid[c["pid"]]["end"] = c["at"]
    return out


def _log(d: Path, entry: dict) -> None:
    with open(d / "log.jsonl", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(entry) + "\n")


def _take(d: Path, cmd: str) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "plan.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = d / "plan.json"
        plan = json.loads(path.read_text()) if path.exists() else {}
        steps = plan.get(cmd) or [{}]
        step = steps[0]
        if len(steps) > 1:
            plan[cmd] = steps[1:]
            path.write_text(json.dumps(plan))
        return step


def main(argv: list[str]) -> int:
    d = state_dir(Path(os.environ["HOME"]))
    d.mkdir(parents=True, exist_ok=True)
    cmd = argv[0] if argv else ""
    if cmd == "version":
        print("restic 0.19.1 (fake)")
        return 0
    repo = os.environ.get("RESTIC_REPOSITORY")
    _log(d, {"argv": argv, "repo": repo, "pid": os.getpid(), "at": time.time()})
    step = _take(d, cmd)

    def handler(signum: int, _frame: object) -> None:
        name = signal.Signals(signum).name
        _log(d, {"signal": name, "argv": argv})
        if step.get(name.lower(), "exit") == "exit":
            os._exit(130 if signum == signal.SIGINT else 143)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    out = step.get("stdout", "")
    if out:
        sys.stdout.buffer.write(out.encode("utf-8", "surrogateescape"))
        sys.stdout.flush()
    deadline = time.monotonic() + float(step.get("sleep", 0))
    while time.monotonic() < deadline:
        time.sleep(0.02)
    if step.get("stderr"):
        sys.stderr.write(step["stderr"])
        sys.stderr.flush()
    _log(d, {"ended": argv, "repo": repo, "pid": os.getpid(), "at": time.time()})
    return int(step.get("code", 0))
