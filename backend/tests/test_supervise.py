"""The supervisor of the single panel container (backend/container/supervise.py) and its health check.

The numbered tests are the acceptance tests the supervisor was written against. Most drive the
supervisor's logic with fake children and a fake clock; the ones at the end start real
processes (a shell, the real entrypoint with stub commands). The same behaviour inside a real
container (tini, UID 10001, no capabilities, real Redis and Celery) is checked by the CI
Compose job.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import itertools
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONTAINER = REPO / "backend" / "container"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"mcpanel_container_{name}", CONTAINER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sup = _load("supervise")
healthcheck = _load("healthcheck")


KILLED = -signal.SIGKILL


class FakeProcs:
    """Children that live only in memory, with a clock the test moves."""

    def __init__(self):
        self.now = 1000.0
        self.next_pid = 100
        self.spawned: list[tuple[str, tuple[str, ...], dict]] = []
        self.signals: list[tuple[str, int, bool]] = []
        self.alive: dict[int, str] = {}
        self.exits: list[tuple[int, int, bytes]] = []
        self.pending: list[tuple[float, int, int, bytes]] = []
        self.fail_spawn: set[str] = set()
        self.dies_at_once: set[str] = set()
        self.term_delay: dict[str, float | None] = {}  # seconds a child needs after SIGTERM; None: ignores it
        self.ping_output = b""

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    def spawn(self, name, argv, env, capture=False):
        if name in self.fail_spawn:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        pid = self.next_pid
        self.next_pid += 1
        self.spawned.append((name, tuple(argv), dict(env)))
        self.alive[pid] = name
        if name in self.dies_at_once:
            self.pending.append((self.now, pid, 1, b""))
        if capture:
            self.pending.append((self.now + 1.0, pid, 0 if self.ping_output else 69, self.ping_output))
        return pid

    def signal(self, pid, sig, group=False):
        name = self.alive.get(pid)
        self.signals.append((name or "?", sig, group))
        if name is None:
            return
        if sig == signal.SIGKILL:
            self.pending.append((self.now, pid, -signal.SIGKILL, b""))
        elif sig == signal.SIGTERM:
            delay = self.term_delay.get(name, 0.0)
            if delay is not None:
                self.pending.append((self.now + delay, pid, 0, b""))

    def die(self, name: str, code: int = KILLED) -> None:
        self.pending.append((self.now, self.pid(name), code, b""))

    def pid(self, name: str) -> int:
        return next(pid for pid, child in self.alive.items() if child == name)

    def reap(self):
        done = []
        for item in sorted(self.pending):
            when, pid, code, output = item
            if when <= self.now and pid in self.alive:
                del self.alive[pid]
                done.append((pid, code, output))
        self.pending = [item for item in self.pending if item[1] in self.alive]
        return done

    def pump(self, timeout):
        self.now += timeout

    def started(self, name: str) -> int:
        return sum(1 for child, _argv, _env in self.spawned if child == name)

    def sent(self, name: str, sig: int) -> int:
        return sum(1 for child, s, _group in self.signals if child == name and s == sig)


def plan(redis: bool = True):
    ready = {"redis": True}
    specs = []
    after = ()
    if redis:
        after = ("redis",)
        specs.append(
            sup.ChildSpec("redis", ("redis-server",), grace=5, stage=2, env={"PATH": "/bin"}, ready=lambda: ready["redis"])
        )
    specs += [
        sup.ChildSpec("web", ("web",), grace=15, stage=0, after=after),
        sup.ChildSpec("worker", ("worker",), grace=110, stage=0, after=after, finish_before_restart=True),
        sup.ChildSpec("periodic", ("periodic",), grace=30, stage=1, after=after),
        sup.ChildSpec("beat", ("beat",), grace=10, stage=0, after=after),
    ]
    return specs, ready


def make(tmp_path=None, redis=True, probe=None):
    procs = FakeProcs()
    specs, ready = plan(redis)
    lines: list[str] = []
    status_path = str(tmp_path / "supervisor.json") if tmp_path is not None else None
    supervisor = sup.Supervisor(specs, procs, status_path=status_path, probe=probe, environ={"SECRET": "s"}, log=lines.append)
    return supervisor, procs, ready, lines


def drive(supervisor, procs, seconds: float, step: float = 0.25) -> None:
    end = procs.now + seconds
    while procs.now < end and not supervisor.done:
        procs.now += step
        supervisor.step()


def states(supervisor) -> dict[str, str]:
    return {name: child.state for name, child in supervisor.children.items()}


# --- 1. never exits because a child failed ---------------------------------------------------
def test_1_a_failing_child_is_retried_forever_with_capped_backoff_and_the_rest_keeps_running(tmp_path):
    supervisor, procs, _ready, lines = make(tmp_path)
    procs.dies_at_once.add("beat")
    starts: list[float] = []
    for _ in range(4 * 600):  # ten minutes
        before = procs.started("beat")
        procs.now += 0.25
        supervisor.step()
        if procs.started("beat") > before:
            starts.append(procs.now)
    assert not supervisor.done
    gaps = [round(b - a) for a, b in itertools.pairwise(starts)]
    assert gaps[:6] == [1, 2, 4, 8, 16, 30] and set(gaps[6:]) == {30}, gaps
    # the others were started once and never signalled
    for name in ("redis", "web", "worker", "periodic"):
        assert procs.started(name) == 1 and supervisor.children[name].state == "running"
        assert not [s for s in procs.signals if s[0] == name]
    status = json.loads((tmp_path / "supervisor.json").read_text())
    assert status["children"]["beat"]["state"] == "backoff" and status["children"]["beat"]["last_exit"] == 1
    assert status["children"]["web"]["state"] == "running"
    assert any("beat exited (exit code 1)" in line for line in lines)


def test_1_a_child_that_cannot_be_started_is_retried_too():
    supervisor, procs, _ready, lines = make()
    procs.fail_spawn.add("worker")
    drive(supervisor, procs, 120)
    assert not supervisor.done and supervisor.children["worker"].state == "backoff"
    assert supervisor.children["web"].state == "running"
    assert any("worker could not be started" in line for line in lines)
    procs.fail_spawn.clear()
    drive(supervisor, procs, 31)
    assert supervisor.children["worker"].state == "running"


def test_1_backoff_starts_over_after_a_stable_run():
    supervisor, procs, _ready, lines = make()
    drive(supervisor, procs, 1)
    for _ in range(3):
        procs.die("web")
        drive(supervisor, procs, 10)
    assert any("web exited (killed by SIGKILL)" in line and "again in 4 s" in line for line in lines)
    drive(supervisor, procs, sup.STABLE_AFTER + 1)
    procs.die("web")
    drive(supervisor, procs, 0.5)
    assert "again in 1 s" in [line for line in lines if "web exited" in line][-1]


def test_1_an_internal_error_does_not_end_supervision(monkeypatch):
    supervisor, _procs, _ready, lines = make()
    calls = {"n": 0}
    real_step = supervisor.step

    def step():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        if calls["n"] == 6:
            supervisor.stop_requested = True
        real_step()

    monkeypatch.setattr(supervisor, "step", step)
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    assert supervisor.run() == 0
    assert any("internal error (supervision goes on)" in line and "boom" in line for line in lines)


# --- 2. the status file -----------------------------------------------------------------------
def test_2_status_write_never_raises_and_recovers(tmp_path):
    supervisor, procs, _ready, lines = make(tmp_path / "gone")  # the directory does not exist
    drive(supervisor, procs, 30)
    assert states(supervisor)["web"] == "running" and not supervisor.done
    assert sum("cannot write the status file" in line for line in lines) == 1  # said once, not every 5 s
    (tmp_path / "gone").mkdir()
    drive(supervisor, procs, 6)
    assert json.loads((tmp_path / "gone" / "supervisor.json").read_text())["phase"] == "running"
    assert any("can be written again" in line for line in lines)


def test_2_status_lives_in_a_private_directory(tmp_path):
    run_dir = tmp_path / "run"
    assert sup.private_directory(str(run_dir)) == str(run_dir)
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    run_dir.chmod(0o755)
    assert sup.private_directory(str(run_dir)) and stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    link = tmp_path / "link"
    link.symlink_to(run_dir)
    assert sup.private_directory(str(link)) is None  # a link somebody else may have planted
    assert sup.RUN_DIR == "/run/mcpanel" and not sup.RUN_DIR.startswith("/tmp")
    supervisor, procs, _ready, _lines = make(run_dir)
    drive(supervisor, procs, 1)
    assert stat.S_IMODE((run_dir / "supervisor.json").stat().st_mode) == 0o600


def test_2_status_is_rewritten_as_a_heartbeat(tmp_path):
    supervisor, procs, _ready, _lines = make(tmp_path)
    drive(supervisor, procs, 2)
    first = json.loads((tmp_path / "supervisor.json").read_text())["time"]
    drive(supervisor, procs, sup.HEARTBEAT + 1)
    assert json.loads((tmp_path / "supervisor.json").read_text())["time"] > first


# --- 3. every log line names its child -----------------------------------------------------------
def test_3_lines_get_the_child_name_in_front():
    out: list[bytes] = []
    lines = sup.Lines("periodic", out.append)
    lines.feed(b"first\nsec")
    lines.feed(b"ond\r\n\nthird without end")
    assert out == [b"[periodic] first\n", b"[periodic] second\n", b"[periodic] \n"]
    lines.flush()
    assert out[-1] == b"[periodic] third without end\n"
    lines.feed(b"x" * (sup.Lines.LIMIT + 10))  # a line without end is passed on in pieces, not kept forever
    assert out[-1].startswith(b"[periodic] xxx") and lines.rest == b""


def test_3_real_children_are_prefixed_on_stdout_and_stderr():
    out: list[bytes] = []
    procs = sup.Processes(write=out.append)
    pid = procs.spawn("worker", ("sh", "-c", "echo one; echo two >&2; printf three"), dict(os.environ))
    deadline = time.monotonic() + 10
    done = []
    while not done and time.monotonic() < deadline:
        procs.pump(0.1)
        done = procs.reap()
    assert done == [(pid, 0, b"")]
    assert sorted(out) == [b"[worker] one\n", b"[worker] three\n", b"[worker] two\n"]


@pytest.mark.skipif(shutil.which("yes") is None, reason="needs coreutils yes")
def test_a_child_that_never_stops_writing_does_not_starve_the_loop():
    """`yes` writes faster than the supervisor reads: one round must still end, so the other
    children are supervised, the status file is written and a stop signal is seen."""
    written = [0]

    def count(data: bytes) -> None:
        written[0] += len(data)

    procs = sup.Processes(write=count)
    pid = procs.spawn("flood", ("yes", "a line of output, over and over"), dict(os.environ))
    rounds: list[tuple[float, int]] = []  # (seconds, bytes passed on) per round

    def loop() -> None:
        for _ in range(5):
            began, before = time.monotonic(), written[0]
            procs.pump(0.05)
            rounds.append((time.monotonic() - began, written[0] - before))

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    thread.join(20)
    stuck = thread.is_alive()
    procs.signal(pid, signal.SIGKILL, group=True)  # (also ends a loop that is stuck in its read)
    thread.join(20)
    deadline = time.monotonic() + 10
    done: list = []
    while not done and time.monotonic() < deadline:
        procs.pump(0.05)
        done = procs.reap()
    assert not stuck, "pump() never came back while the child was writing"
    assert len(rounds) == 5 and max(seconds for seconds, _size in rounds) < 5
    # every round passed output on: a bounded piece of it (what was read, plus the name in front of each line)
    per_round = sup.READ_CHUNK * sup.READ_CHUNKS_PER_ROUND
    assert sum(size for _seconds, size in rounds) > 0
    assert max(size for _seconds, size in rounds) <= 2 * per_round
    assert done and done[0][0] == pid and not procs.fds


def test_output_of_the_ping_command_is_captured_whole_even_when_it_ends_before_it_is_reaped():
    procs = sup.Processes(write=lambda _data: None)
    pid = procs.spawn(
        "ping", ("sh", "-c", """echo '{"celery@h": {"ok": "pong"}}'; echo noise >&2"""), dict(os.environ), capture=True
    )
    other = procs.spawn("web", ("sh", "-c", "sleep 0.3"), dict(os.environ))  # must not take over the first pipe
    time.sleep(0.2)
    for _ in range(5):  # sees the end of file first ...
        procs.pump(0.05)
    done: list = []
    deadline = time.monotonic() + 10
    while len(done) < 2 and time.monotonic() < deadline:  # ... and reaps afterwards
        procs.pump(0.05)
        done += procs.reap()
    assert sorted(done) == sorted([(pid, 0, b'{"celery@h": {"ok": "pong"}}\n'), (other, 0, b"")])
    assert sup.parse_pongs(dict((p, out) for p, _code, out in done)[pid]) == {"celery@h"}
    assert not procs.sinks and not procs.fds and not procs.popen


# --- 4. stop: own grace per child, one deadline --------------------------------------------------
def test_4_stop_order_is_web_beat_worker_then_periodic_then_redis():
    supervisor, procs, _ready, lines = make()
    drive(supervisor, procs, 1)
    procs.term_delay.update({"worker": 20.0, "web": 2.0})
    supervisor.stop_requested = True
    drive(supervisor, procs, 5)
    assert procs.sent("web", signal.SIGTERM) == procs.sent("beat", signal.SIGTERM) == procs.sent("worker", signal.SIGTERM) == 1
    assert procs.sent("periodic", signal.SIGTERM) == 0 and procs.sent("redis", signal.SIGTERM) == 0
    assert states(supervisor)["periodic"] == "running"  # still there to deliver what the job worker queues
    drive(supervisor, procs, 16)
    assert procs.sent("periodic", signal.SIGTERM) == 1
    drive(supervisor, procs, 2)
    assert supervisor.done and procs.sent("redis", signal.SIGTERM) == 1
    order = [name for name, sig, _group in procs.signals if sig == signal.SIGTERM]
    assert order.index("worker") < order.index("periodic") < order.index("redis")
    assert not [s for s in procs.signals if s[1] == signal.SIGKILL]  # nobody had to be killed
    assert any("stop requested" in line and "worker (110 s)" in line for line in lines)


def test_4_each_child_has_its_own_grace_under_one_deadline_below_two_minutes():
    supervisor, procs, _ready, _lines = make()
    drive(supervisor, procs, 1)
    for name in supervisor.children:
        procs.term_delay[name] = None  # nobody reacts to SIGTERM
    began = procs.now
    supervisor.stop_requested = True
    killed: dict[str, float] = {}
    while not supervisor.done and procs.now - began < 200:
        procs.now += 0.25
        supervisor.step()
        for name, sig, group in procs.signals:
            if sig == signal.SIGKILL and name not in killed:
                assert group, "the whole process group is killed"
                killed[name] = round(procs.now - began)
    assert killed == {"beat": 10, "web": 15, "worker": 110, "periodic": 113, "redis": 116}
    assert supervisor.done and procs.now - began <= sup.STOP_DEADLINE + 1 < 120
    assert sup.STOP_DEADLINE < 120


def test_4_sigterm_goes_to_the_main_process_only():
    """Celery's pool processes must not get SIGTERM themselves: they finish their task first."""
    supervisor, procs, _ready, _lines = make()
    drive(supervisor, procs, 1)
    supervisor.stop_requested = True
    drive(supervisor, procs, 5)
    assert all(not group for _name, sig, group in procs.signals if sig == signal.SIGTERM)


def test_4_a_child_in_backoff_is_not_started_again_while_stopping():
    supervisor, procs, _ready, _lines = make()
    drive(supervisor, procs, 1)
    procs.die("beat")
    drive(supervisor, procs, 0.25)
    supervisor.stop_requested = True
    drive(supervisor, procs, 5)
    assert supervisor.done and procs.started("beat") == 1


# --- 5. / 6. / 7. the commands of the children ------------------------------------------------------
def test_5_6_7_the_panel_children():
    env = {
        "PATH": "/opt/venv/bin:/usr/bin",
        "TZ": "Europe/Berlin",
        "MCPANEL_SECRET_KEY": "secret",
        "POSTGRES_PASSWORD": "secret",
        "MCPANEL_AGENT_TOKEN": "secret",
        "MCPANEL_WORKER_CONCURRENCY": "8",
        "MCPANEL_REDIS_URL": "unix:///run/mcpanel/redis.sock?db=0",
    }
    specs, probe = sup.panel_plan(env, "/run/mcpanel")
    by_name = {spec.name: spec for spec in specs}
    assert list(by_name) == ["redis", "web", "worker", "periodic", "beat"]
    # 5. the periodic worker always has 2 slots (and the entrypoint must not add another value)
    periodic = by_name["periodic"].argv
    assert periodic[periodic.index("-c") + 1] == "2" and periodic[periodic.index("-Q") + 1] == "periodic"
    assert "-c" not in by_name["worker"].argv and "--concurrency" not in by_name["worker"].argv
    # 6. the scheduler keeps its schedule in memory
    beat = by_name["beat"].argv
    assert beat[beat.index("-S") + 1] == "celery.beat:Scheduler" and "--schedule" not in beat
    # 7. the web server is started through the entrypoint's own code path
    assert by_name["web"].argv == ("mcpanel-entrypoint", "serve")
    # tick noise: periodic worker and scheduler log at WARNING
    assert periodic[periodic.index("-l") + 1] == "warning" and beat[beat.index("-l") + 1] == "warning"
    # stop: the job worker gets 110 s; Redis goes last
    assert by_name["worker"].grace == 110 and by_name["worker"].stage < by_name["periodic"].stage < by_name["redis"].stage
    # only the job worker is waited for without a time limit when Redis came back
    assert [name for name, spec in by_name.items() if spec.finish_before_restart] == ["worker"]
    assert all(spec.after == ("redis",) for spec in specs if spec.name != "redis")
    # the ping asks both workers
    assert set(probe.nodes.values()) == {"worker", "periodic"}
    assert all(node in probe.argv[probe.argv.index("-d") + 1] for node in probe.nodes)


def test_redis_gets_a_socket_only_no_persistence_and_none_of_the_secrets():
    env = {"PATH": "/usr/bin", "TZ": "UTC", "MCPANEL_SECRET_KEY": "secret", "POSTGRES_PASSWORD": "pw", "LANG": "C.UTF-8"}
    env["MCPANEL_REDIS_URL"] = "unix:///run/mcpanel/redis.sock?db=0"
    redis = sup.panel_plan(env, "/run/mcpanel")[0][0]
    argv = redis.argv
    assert argv[argv.index("--port") + 1] == "0"  # no TCP listener
    assert argv[argv.index("--unixsocket") + 1] == "/run/mcpanel/redis.sock"
    assert argv[argv.index("--unixsocketperm") + 1] == "700"
    assert argv[argv.index("--save") + 1] == "" and argv[argv.index("--appendonly") + 1] == "no"
    assert "--bind" not in argv
    assert redis.env == {"PATH": "/usr/bin", "TZ": "UTC", "LANG": "C.UTF-8"}
    # the one file Redis would load at its start has a fixed name in the socket's directory
    assert argv[argv.index("--dir") + 1] == "/run/mcpanel" and argv[argv.index("--dbfilename") + 1] == "dump.rdb"
    assert redis.before_start is not None


def test_what_redis_would_load_is_removed_before_every_start(tmp_path):
    """The web process can write into the run directory, and cache values are pickles: a dump
    file there must never be loaded when the Redis child is started again."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {"PATH": "/usr/bin", "MCPANEL_REDIS_URL": f"unix://{run_dir}/redis.sock?db=0"}
    redis = sup.panel_plan(env, str(run_dir))[0][0]
    assert redis.argv[redis.argv.index("--dir") + 1] == str(run_dir)
    assert redis.before_start() is None  # nothing there: nothing to say

    secret = tmp_path / "elsewhere.txt"
    secret.write_text("keep")
    (run_dir / "dump.rdb").write_bytes(b"REDIS0011 planted")
    (run_dir / "temp-123.rdb").write_bytes(b"x")
    (run_dir / "appendonlydir").mkdir()
    (run_dir / "appendonlydir" / "appendonly.aof.1.base.rdb").write_bytes(b"x")
    (run_dir / "appendonly.aof").symlink_to(secret)  # a link is removed, what it points to is not
    (run_dir / "supervisor.json").write_text("{}")
    (run_dir / "redis.sock").write_text("")
    note = redis.before_start()
    assert sorted(p.name for p in run_dir.iterdir()) == ["redis.sock", "supervisor.json"]
    assert secret.read_text() == "keep"
    assert "dump.rdb" in note and "temp-123.rdb" in note and str(run_dir) in note
    # a dump "file" that is a directory goes, too
    (run_dir / "dump.rdb").mkdir()
    (run_dir / "dump.rdb" / "x").write_text("")
    redis.before_start()
    assert not (run_dir / "dump.rdb").exists()
    assert sup.remove_redis_data(str(tmp_path / "missing")) == []


def test_the_preparation_runs_before_every_start_and_a_failure_keeps_the_child_down():
    calls: list[str] = []
    outcome: dict[str, object] = {"raise": None, "note": "removed dump.rdb from /run/mcpanel"}

    def prepare():
        calls.append("prepare")
        if outcome["raise"]:
            raise outcome["raise"]
        return outcome["note"]

    procs = FakeProcs()
    lines: list[str] = []
    specs = [
        sup.ChildSpec("redis", ("redis-server",), grace=5, stage=2, before_start=prepare),
        sup.ChildSpec("web", ("web",), grace=15, stage=0, after=("redis",)),
    ]
    supervisor = sup.Supervisor(specs, procs, environ={}, log=lines.append)
    drive(supervisor, procs, 2)
    assert calls == ["prepare"] and procs.started("redis") == 1
    assert any("before the start of redis: removed dump.rdb" in line for line in lines)
    # the Redis child dies: the file a process may have planted meanwhile is removed before the restart
    procs.die("redis")
    drive(supervisor, procs, 3)
    assert calls == ["prepare", "prepare"] and procs.started("redis") == 2
    # something that cannot be removed: Redis is not started next to it, and the supervisor goes on
    outcome["raise"] = PermissionError(13, "Permission denied", "/run/mcpanel/dump.rdb")
    procs.die("redis")
    drive(supervisor, procs, 20)
    assert procs.started("redis") == 2 and states(supervisor)["redis"] == "backoff" and not supervisor.done
    assert any("redis is not started: its preparation failed" in line and "dump.rdb" in line for line in lines)
    outcome["raise"] = None
    drive(supervisor, procs, 31)
    assert procs.started("redis") == 3 and states(supervisor)["redis"] == "running"


def test_an_external_redis_url_means_no_redis_child():
    for url in ("redis://redis:6379/0", "rediss://example.com:6380/1"):
        specs, _probe = sup.panel_plan({"PATH": "/usr/bin", "MCPANEL_REDIS_URL": url}, "/run/mcpanel")
        assert [spec.name for spec in specs] == ["web", "worker", "periodic", "beat"]
        assert all(spec.after == () for spec in specs)
    assert sup.embedded_redis_socket({"MCPANEL_REDIS_URL": "unix:///a/b.sock?db=0"}) == "/a/b.sock"
    assert sup.embedded_redis_socket({}) is None


# --- Redis first, Redis last, and what happens when it dies ------------------------------------------
def test_nothing_starts_before_redis_answers():
    supervisor, procs, ready, lines = make()
    ready["redis"] = False
    drive(supervisor, procs, 10)
    assert [name for name, _argv, _env in procs.spawned] == ["redis"]
    assert states(supervisor)["redis"] == "starting" and states(supervisor)["web"] == "waiting"
    assert supervisor.phase == "starting"
    ready["redis"] = True
    drive(supervisor, procs, 1)
    assert [name for name, _argv, _env in procs.spawned] == ["redis", "web", "worker", "periodic", "beat"]
    assert supervisor.phase == "running" and any("redis is ready" in line for line in lines)
    # redis got its own small environment, the others the supervisor's
    assert procs.spawned[0][2] == {"PATH": "/bin"} and procs.spawned[1][2] == {"SECRET": "s"}


def test_redis_that_never_answers_is_killed_and_started_again():
    supervisor, procs, ready, lines = make()
    ready["redis"] = False
    drive(supervisor, procs, sup.READY_TIMEOUT + 3)
    assert procs.sent("redis", signal.SIGKILL) == 1 and procs.started("redis") == 2
    assert any("redis is not ready after" in line for line in lines)
    assert procs.started("web") == 0


def test_when_redis_dies_it_is_restarted_and_then_the_others():
    supervisor, procs, ready, lines = make()
    drive(supervisor, procs, 5)
    procs.term_delay["worker"] = 3.0  # finishes its task first
    ready["redis"] = False
    procs.die("redis")
    drive(supervisor, procs, 0.5)
    assert states(supervisor)["redis"] == "backoff" and states(supervisor)["web"] == "running"
    drive(supervisor, procs, 2)  # redis is back but does not answer yet: the others are left alone
    assert states(supervisor)["redis"] == "starting" and procs.sent("web", signal.SIGTERM) == 0
    ready["redis"] = True
    drive(supervisor, procs, 0.5)
    for name in ("web", "worker", "periodic", "beat"):
        assert procs.sent(name, signal.SIGTERM) == 1, name
    assert states(supervisor)["worker"] == "stopping"
    drive(supervisor, procs, 5)
    assert all(state == "running" for state in states(supervisor).values())
    assert all(procs.started(name) == 2 for name in supervisor.children)
    assert any("restarting worker: redis was started again" in line for line in lines)
    # a deliberate restart is not a failure: no backoff was added
    assert supervisor.children["worker"].failures == 0


def test_a_busy_job_worker_finishes_its_job_when_redis_came_back_and_is_never_killed_for_it():
    """A restore that needs hours must not be cut off 110 s after the Redis child was restarted."""
    supervisor, procs, ready, lines = make()
    drive(supervisor, procs, 5)
    procs.term_delay["worker"] = 3 * 3600.0  # Celery's warm shutdown: it exits when its job is done
    ready["redis"] = False
    procs.die("redis")
    drive(supervisor, procs, 3)
    ready["redis"] = True
    drive(supervisor, procs, 1)
    assert procs.sent("worker", signal.SIGTERM) == 1 and states(supervisor)["worker"] == "stopping"
    assert any("restarting worker: redis was started again (it finishes what it is doing first" in line for line in lines)
    drive(supervisor, procs, 3 * 3600 - 10, step=1.0)
    assert procs.sent("worker", signal.SIGKILL) == 0 and procs.sent("worker", signal.SIGTERM) == 1
    assert states(supervisor)["worker"] == "stopping" and procs.started("worker") == 1
    # the others were restarted right away, with their own grace periods
    assert all(procs.started(name) == 2 and states(supervisor)[name] == "running" for name in ("web", "periodic", "beat"))
    drive(supervisor, procs, 20)  # the job is done: the worker exits by itself and is started again
    assert procs.started("worker") == 2 and states(supervisor)["worker"] == "running"
    assert procs.sent("worker", signal.SIGKILL) == 0 and supervisor.children["worker"].failures == 0


def test_a_container_stop_while_the_worker_finishes_its_job_gives_it_its_grace_period_once():
    supervisor, procs, ready, _lines = make()
    drive(supervisor, procs, 5)
    procs.term_delay["worker"] = None  # still busy, whatever happens
    ready["redis"] = False
    procs.die("redis")
    drive(supervisor, procs, 3)
    ready["redis"] = True
    drive(supervisor, procs, 600)
    assert states(supervisor)["worker"] == "stopping" and procs.sent("worker", signal.SIGKILL) == 0
    began = procs.now
    supervisor.stop_requested = True
    drive(supervisor, procs, 109)
    # no second SIGTERM (Celery would stop at once and lose the job that has 110 s to finish)
    assert procs.sent("worker", signal.SIGTERM) == 1 and procs.sent("worker", signal.SIGKILL) == 0
    drive(supervisor, procs, 10)
    assert procs.sent("worker", signal.SIGKILL) == 1 and supervisor.done
    assert procs.now - began <= sup.STOP_DEADLINE + 1


def test_a_child_that_crashed_while_redis_was_down_waits_for_it():
    supervisor, procs, ready, _lines = make()
    drive(supervisor, procs, 5)
    ready["redis"] = False
    procs.die("redis")
    procs.die("beat", 1)
    drive(supervisor, procs, 10)
    assert states(supervisor)["beat"] == "backoff" and procs.started("beat") == 1
    ready["redis"] = True
    drive(supervisor, procs, 3)
    assert states(supervisor)["beat"] == "running" and procs.started("beat") == 2


# --- worker ping --------------------------------------------------------------------------------------
def ping_probe():
    return sup.Probe(("celery", "inspect", "ping"), {"celery@h": "worker", "periodic@h": "periodic"})


def test_both_workers_are_pinged_and_the_answers_are_in_the_status(tmp_path):
    supervisor, procs, _ready, lines = make(tmp_path, probe=ping_probe())
    procs.ping_output = b'{"celery@h": {"ok": "pong"}, "periodic@h": {"ok": "pong"}}\n'
    drive(supervisor, procs, sup.PING_WARMUP + 3)
    status = json.loads((tmp_path / "supervisor.json").read_text())
    assert set(status["workers"]) == {"celery@h", "periodic@h"} and all(status["workers"].values())
    assert status["worker_children"] == {"celery@h": "worker", "periodic@h": "periodic"} and status["ping"] == "ok"
    first = dict(supervisor.pongs)
    drive(supervisor, procs, sup.PING_INTERVAL + 2)
    assert supervisor.pongs["celery@h"] > first["celery@h"]
    assert procs.started("ping") == 2  # one per interval, not one per round
    assert not any("worker ping" in line for line in lines)  # silent while all is well


def test_a_deaf_worker_keeps_its_old_answer_time_and_is_pinged_sooner():
    supervisor, procs, _ready, lines = make(probe=ping_probe())
    procs.ping_output = b'{"celery@h": {"ok": "pong"}, "periodic@h": {"ok": "pong"}}'
    drive(supervisor, procs, sup.PING_WARMUP + 3)
    before = dict(supervisor.pongs)
    procs.ping_output = b'{"periodic@h": {"ok": "pong"}}'
    drive(supervisor, procs, sup.PING_INTERVAL + 3)
    assert supervisor.pongs["celery@h"] == before["celery@h"] and supervisor.pongs["periodic@h"] > before["periodic@h"]
    assert any("worker ping: no answer from celery@h" in line for line in lines)
    count = procs.started("ping")
    drive(supervisor, procs, sup.PING_RETRY + 2)
    assert procs.started("ping") == count + 1


def test_no_ping_while_a_worker_is_down_and_a_hanging_ping_is_killed():
    supervisor, procs, _ready, _lines = make(probe=ping_probe())
    procs.dies_at_once.add("worker")
    drive(supervisor, procs, 60)
    assert procs.started("ping") == 0
    assert sup.parse_pongs(b"") == set() and sup.parse_pongs(b"Error: No nodes replied\n") == set()
    assert sup.parse_pongs(b'warning\n{"a@b": {"ok": "pong"}, "c@d": {"error": "x"}}\n') == {"a@b"}


# --- 8. health check -------------------------------------------------------------------------------------
NOW = 1_800_000_000.0


def good_status(now: float = NOW) -> dict:
    children = {
        name: {"state": "running", "pid": 10 + i, "since": now - 3600, "restarts": 0, "last_exit": None}
        for i, name in enumerate(("redis", "web", "worker", "periodic", "beat"))
    }
    return {
        "time": now - 2,
        "pid": 7,
        "phase": "running",
        "children": children,
        "workers": {"celery@h": now - 30, "periodic@h": now - 30},
        "worker_children": {"celery@h": "worker", "periodic@h": "periodic"},
        "ping": "ok",
    }


def good_health() -> dict:
    return {"status": "ok", "checks": {"database": "ok", "cache": "ok"}, "tick_age_seconds": 4.2}


def problems(status=None, health=None, status_error=None, health_error=None):
    return healthcheck.problems_all(status, status_error, health, health_error, NOW)


def test_8_healthy_when_everything_is_fine():
    assert problems(good_status(), good_health()) == []


def test_8_a_child_that_is_down_is_named():
    status = good_status()
    status["children"]["beat"].update(state="backoff", last_exit=1, restarts=7)
    found = problems(status, good_health())
    assert found == ["beat: backoff (last exit 1, 7 restarts)"]


def test_8_a_worker_that_no_longer_answers_is_named():
    status = good_status()
    status["workers"]["celery@h"] = NOW - 200
    assert problems(status, good_health()) == ["worker: no answer to a ping for 200 s (celery@h)"]
    status["workers"]["celery@h"] = NOW - 119
    assert problems(status, good_health()) == []
    # no allowance for "just started": healthy means both workers did answer
    status["workers"]["periodic@h"] = None
    status["children"]["periodic"]["since"] = NOW - 5
    assert problems(status, good_health()) == ["periodic: has not answered a ping yet (periodic@h)"]


def test_8_the_age_of_the_reconcile_tick_counts():
    health = good_health()
    health["tick_age_seconds"] = 181.0
    found = problems(good_status(), health)
    assert len(found) == 1 and found[0].startswith("beat/periodic: the last reconcile tick started 181 s ago")
    health["tick_age_seconds"] = 150.0  # above the 120 s the no-overlap lock of a dead run can last
    assert problems(good_status(), health) == []
    assert healthcheck.TICK_MAX_AGE > 120
    health["tick_age_seconds"] = None  # e.g. right after a start: not healthy before the first tick ran
    assert problems(good_status(), health)[0].startswith("beat/periodic: no reconcile tick yet")


def test_8_web_database_and_cache_failures_are_named():
    assert problems(good_status(), None, health_error="connection refused") == ["web: /api/health failed (connection refused)"]
    health = good_health()
    health.update(status="degraded", checks={"database": "error", "cache": "ok"})
    assert problems(good_status(), health) == ["database: error"]


def test_8_a_missing_or_stale_status_is_unhealthy():
    assert problems(None, good_health(), status_error="no status file (/run/mcpanel/supervisor.json)") == [
        "supervisor: no status file (/run/mcpanel/supervisor.json)"
    ]
    status = good_status()
    status["time"] = NOW - 60
    assert problems(status, good_health())[0].startswith("supervisor: its status is 60 s old")
    status = good_status()
    status["phase"] = "stopping"
    assert "supervisor: stopping" in problems(status, good_health())
    assert problems({"time": NOW, "phase": "running", "children": "garbage"}, good_health()) == [
        "supervisor: no children in its status"
    ]


def test_8_the_health_check_reads_what_the_supervisor_writes(tmp_path, monkeypatch):
    supervisor, procs, _ready, _lines = make(tmp_path, probe=ping_probe())
    procs.ping_output = b'{"celery@h": {"ok": "pong"}, "periodic@h": {"ok": "pong"}}'
    drive(supervisor, procs, 20)
    status, error = healthcheck.read_status(str(tmp_path / "supervisor.json"))
    assert error is None
    now = procs.time()
    assert healthcheck.problems_all(status, None, good_health(), None, now) == []
    procs.die("periodic", 1)
    drive(supervisor, procs, 0.5)
    status, _error = healthcheck.read_status(str(tmp_path / "supervisor.json"))
    assert healthcheck.problems_all(status, None, good_health(), None, procs.time()) == [
        "periodic: backoff (last exit 1, 0 restarts)"
    ]
    assert healthcheck.read_status(str(tmp_path / "missing.json"))[1].startswith("no status file")


def test_8_web_mode_still_only_asks_api_health(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(healthcheck, "STATUS_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(healthcheck, "fetch_health", lambda: (good_health(), None))
    assert healthcheck.main([]) == 0
    monkeypatch.setattr(healthcheck, "fetch_health", lambda: (None, "HTTP 503"))
    assert healthcheck.main([]) == 1 and "unhealthy: HTTP 503" in capsys.readouterr().err
    # `all` (what the Compose file passes) needs the supervisor's status even if the file is gone
    monkeypatch.setattr(healthcheck, "fetch_health", lambda: (good_health(), None))
    assert healthcheck.main(["all"]) == 1 and "supervisor: no status file" in capsys.readouterr().err


# --- 9. start-up guard ---------------------------------------------------------------------------------------
ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t000014AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
lo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0
"""


def test_9_only_names_that_answer_from_the_containers_own_networks_count(tmp_path):
    routes = tmp_path / "route"
    routes.write_text(ROUTES)
    networks = sup.connected_networks(str(routes))
    assert networks == [ipaddress.ip_network("172.20.0.0/16")]
    resolved = {"worker": ["172.20.0.5"], "beat": ["192.168.1.77"], "worker-periodic": ["not-an-ip"]}
    # "beat" is some other machine on the LAN that happens to have that name: not ours
    assert sup.old_layout_containers(resolved, networks) == {"worker": "172.20.0.5"}
    assert sup.old_layout_containers({}, networks) == {}


def test_9_the_guard_refuses_to_start_next_to_old_containers(monkeypatch, capsys):
    monkeypatch.setattr(sup, "connected_networks", lambda: [ipaddress.ip_network("172.20.0.0/16")])
    monkeypatch.setattr(sup, "resolve_names", lambda names: {"worker": ["172.20.0.5"], "beat": ["172.20.0.6"]})
    assert sup.guard({}) == sup.EX_CONFIG == 78
    message = capsys.readouterr().err
    assert "beat (172.20.0.6), worker (172.20.0.5)" in message
    assert all(line.startswith("[guard] ") for line in message.splitlines())
    assert "./scripts/upgrade.sh run" in message and "docker compose up -d --remove-orphans" in message
    assert sup.guard({"MCPANEL_SKIP_LAYOUT_GUARD": "true"}) == 0
    monkeypatch.setattr(sup, "resolve_names", lambda names: {})
    assert sup.guard({}) == 0


def test_9_a_guard_that_cannot_check_does_not_block_the_start(monkeypatch, capsys):
    def broken():
        raise OSError("no /proc")

    monkeypatch.setattr(sup, "connected_networks", broken)
    monkeypatch.setattr(sup, "resolve_names", lambda names: {"worker": ["172.20.0.5"]})
    assert sup.guard({}) == 0 and "starting anyway" in capsys.readouterr().err
    assert sup.OLD_SERVICES == ("worker", "worker-periodic", "beat")


# --- real processes ---------------------------------------------------------------------------------------------
def _wait(condition, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_real_children_kill_9_restart_and_ordered_stop(tmp_path, monkeypatch):
    """Real processes: a killed child comes back alone, its orphans are killed, the stop is ordered."""
    monkeypatch.setattr(sup, "BACKOFF_FIRST", 0.2)
    out: list[bytes] = []
    procs = sup.Processes(write=out.append)
    marker = tmp_path / "orphan.pid"
    specs = [
        sup.ChildSpec("a", ("sh", "-c", f"sleep 300 & echo $! > {marker}; echo a up; wait"), grace=5, stage=0),
        sup.ChildSpec(
            "b", ("sh", "-c", "trap 'echo b bye; exit 0' TERM; echo b up; while :; do sleep 0.1; done"), grace=5, stage=1
        ),
    ]
    lines: list[str] = []
    supervisor = sup.Supervisor(specs, procs, status_path=str(tmp_path / "s.json"), environ=dict(os.environ), log=lines.append)

    def spin(condition, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            procs.pump(0.05)
            supervisor.step()
        return condition()

    assert spin(lambda: out.count(b"[a] a up\n") == 1 and b"[b] b up\n" in out and marker.exists())
    pid_a, pid_b = supervisor.children["a"].pid, supervisor.children["b"].pid
    orphan = int(marker.read_text())
    marker.unlink()
    os.kill(pid_a, signal.SIGKILL)
    assert spin(lambda: out.count(b"[a] a up\n") == 2 and marker.exists())
    assert supervisor.children["a"].pid != pid_a and supervisor.children["b"].pid == pid_b  # b was not touched
    assert any("a exited (killed by SIGKILL)" in line for line in lines)
    # the background process the killed child left behind is gone as well (its process group was killed)
    assert _wait(lambda: not _alive(orphan))
    supervisor.stop_requested = True
    assert spin(lambda: supervisor.done)
    assert b"[b] b bye\n" in out
    stopped = [line for line in lines if " stopped (" in line]
    assert stopped[0].startswith("a stopped") and stopped[1].startswith("b stopped (exit code 0)")
    assert json.loads((tmp_path / "s.json").read_text())["phase"] == "stopping"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie that nobody reaped yet is not running any more
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


STUB_PYTHON = """#!/bin/sh
# the supervisor runs with the real interpreter; migrations and the database wait are stubs
case "$1" in
    */supervise.py) exec "{python}" "$@" ;;
    manage.py) echo "manage $*" >> "{log}"; echo "stub: applying migrations"; exit 0 ;;
    *) cat > /dev/null 2>&1 || true; exit 0 ;;
esac
"""
STUB_PRINT = '#!/bin/sh\necho "{name} $*"\nexec sleep 300\n'
STUB_REDIS = """#!{python}
import os, signal, socket, sys
path = sys.argv[sys.argv.index("--unixsocket") + 1]
open("{log}", "a").write("redis argv " + " ".join(sys.argv[1:]) + "\\nredis env " + ",".join(sorted(os.environ)) + "\\n")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
server = socket.socket(socket.AF_UNIX)
server.bind(path)
server.listen(8)
print("redis stub ready", flush=True)
while True:
    conn, _ = server.accept()
    conn.recv(64)
    conn.sendall(b"+PONG\\r\\n")
    conn.close()
"""


def _stub_bin(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    scripts = {
        "python": STUB_PYTHON.format(python=sys.executable, log=log),
        "uvicorn": STUB_PRINT.format(name="uvicorn"),
        "celery": '#!/bin/sh\necho "celery $*"\ncase "$4" in inspect) exit 69 ;; esac\nexec sleep 300\n',
        "redis-server": STUB_REDIS.format(python=sys.executable, log=log),
        "mcpanel-entrypoint": f'#!/bin/sh\nexec sh "{CONTAINER / "entrypoint.sh"}" "$@"\n',
    }
    for name, body in scripts.items():
        script = bin_dir / name
        script.write_text(body)
        script.chmod(0o755)
    return bin_dir, log


def _run_all(tmp_path: Path, wanted: list[str], **env: str) -> tuple[str, int, str]:
    """Run `entrypoint.sh all` with stub commands until every wanted text was printed, then stop it."""
    bin_dir, log = _stub_bin(tmp_path)
    run_dir = tmp_path / "run"
    full_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "MCPANEL_SUPERVISE": str(CONTAINER / "supervise.py"),
        "MCPANEL_RUN_DIR": str(run_dir),
        "MCPANEL_SKIP_LAYOUT_GUARD": "true",
        "MCPANEL_SECRET_KEY": "not-for-redis",
        **env,
    }
    output = tmp_path / "out.log"
    with output.open("wb") as fh:
        proc = subprocess.Popen(
            ["sh", str(CONTAINER / "entrypoint.sh"), "all"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=full_env,
            stdin=subprocess.DEVNULL,
        )
        try:
            assert _wait(lambda: all(text in output.read_text() for text in wanted), 30), output.read_text()
            assert json.loads((run_dir / "supervisor.json").read_text())["children"]["web"]["state"] == "running"
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                code = proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
    return output.read_text(), code, log.read_text() if log.exists() else ""


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_7_entrypoint_all_runs_the_four_commands_under_the_supervisor(tmp_path):
    out, code, calls = _run_all(
        tmp_path,
        ["[web] uvicorn", "[worker] celery", "[periodic] celery", "[beat] celery"],
        MCPANEL_REDIS_URL="redis://redis.invalid:6379/0",
        MCPANEL_BEHIND_TLS_PROXY="true",
        MCPANEL_WORKER_CONCURRENCY="4",
    )
    assert code == 0, out
    lines = out.splitlines()
    # 7. the same uvicorn command as `web` mode, proxy flags included (loopback only by default)
    assert (
        "[web] uvicorn mcpanel.asgi:application --host 0.0.0.0 --port 8000 --lifespan off --no-server-header"
        " --proxy-headers --forwarded-allow-ips 127.0.0.1,::1"
    ) in lines
    assert any(
        line.startswith("[web] mcpanel: MCPANEL_BEHIND_TLS_PROXY=true but MCPANEL_FORWARDED_ALLOW_IPS is empty") for line in lines
    )
    # 5. MCPANEL_WORKER_CONCURRENCY reaches the job worker and never the periodic worker
    assert "[worker] celery -A mcpanel worker -l info -n celery@%h --concurrency 4" in lines
    assert "[periodic] celery -A mcpanel worker -Q periodic -c 2 -n periodic@%h -l warning" in lines
    # 6. in-memory schedule, no pickle file
    assert "[beat] celery -A mcpanel beat -S celery.beat:Scheduler -l warning" in lines
    # migrations ran once, before anything was started, with their own name in front
    assert calls.count("manage manage.py migrate --noinput") == 1
    assert "[migrate] stub: applying migrations" in lines
    assert lines.index("[migrate] stub: applying migrations") < next(i for i, line in enumerate(lines) if "started web" in line)
    assert "redis" not in calls and not any(line.startswith("[redis]") for line in lines)
    # 3. every line names who wrote it
    names = ("[migrate] ", "[supervisor] ", "[web] ", "[worker] ", "[periodic] ", "[beat] ")
    assert all(line.startswith(names) for line in lines), out
    assert any("stop requested" in line for line in lines) and lines[-1].endswith("all children are gone; exiting")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_entrypoint_all_starts_redis_first_on_a_socket_without_the_secrets(tmp_path):
    socket_path = tmp_path / "run" / "redis.sock"
    out, code, calls = _run_all(
        tmp_path,
        ["[web] uvicorn", "[beat] celery"],
        MCPANEL_REDIS_URL=f"unix://{socket_path}?db=0",
        MCPANEL_CELERY_BROKER_URL=f"redis+socket://{socket_path}",
    )
    assert code == 0, out
    lines = out.splitlines()
    ready = next(i for i, line in enumerate(lines) if line.endswith("redis is ready"))
    first_other = next(i for i, line in enumerate(lines) if "started web" in line)
    assert ready < first_other
    assert f"redis argv --port 0 --unixsocket {socket_path} --unixsocketperm 700 --save  --appendonly no" in calls
    env_line = next(line for line in calls.splitlines() if line.startswith("redis env "))
    assert "MCPANEL_SECRET_KEY" not in env_line and "MCPANEL_REDIS_URL" not in env_line and "PATH" in env_line
    # stop order: redis is the last one to go
    stopped = [line.split("] ", 1)[1].split(" ", 2)[2] for line in lines if " stopped (" in line]
    assert stopped[-1].startswith("redis stopped") and len(stopped) == 5


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_9_entrypoint_all_stops_when_the_guard_refuses(tmp_path):
    bin_dir, log = _stub_bin(tmp_path)
    (bin_dir / "python").write_text(
        '#!/bin/sh\ncase "$2" in guard) echo refused >&2; exit 78 ;; esac\necho "python $*" >> "' + str(log) + '"\n'
    )
    result = subprocess.run(
        ["sh", str(CONTAINER / "entrypoint.sh"), "all"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 78 and "refused" in result.stderr
    assert not log.exists()  # no migration, no supervisor


def test_step_runs_one_command_with_its_name_in_front_and_returns_its_exit_code(capfd):
    assert sup.step("migrate", ["sh", "-c", "echo applying; echo careful >&2; exit 3"]) == 3
    assert sorted(capfd.readouterr().out.splitlines()) == ["[migrate] applying", "[migrate] careful"]
    assert sup.step("migrate", ["sh", "-c", "kill -9 $$"]) == 137
    assert sup.step("migrate", ["/nonexistent/command"]) == 127
    assert "[migrate] cannot run /nonexistent/command" in capfd.readouterr().out
    assert sup.main(["step", "only-a-name"]) == 64


def test_status_command_prints_the_children(tmp_path, monkeypatch, capsys):
    supervisor, procs, _ready, _lines = make(tmp_path, probe=ping_probe())
    procs.ping_output = b'{"celery@h": {"ok": "pong"}, "periodic@h": {"ok": "pong"}}'
    drive(supervisor, procs, 20)
    status = json.loads((tmp_path / "supervisor.json").read_text())
    shift = time.time() - status["time"]
    status["time"] += shift
    for child in status["children"].values():
        child["since"] += shift
    status["workers"] = {node: answered + shift for node, answered in status["workers"].items()}
    (tmp_path / "supervisor.json").write_text(json.dumps(status))
    assert sup.status({"MCPANEL_RUN_DIR": str(tmp_path)}) == 0
    printed = capsys.readouterr().out
    assert "phase running" in printed and "periodic  running" in printed and "ping celery@h: answered" in printed
    status["children"]["beat"]["state"] = "backoff"
    (tmp_path / "supervisor.json").write_text(json.dumps(status))
    assert sup.status({"MCPANEL_RUN_DIR": str(tmp_path)}) == 1
    assert sup.status({"MCPANEL_RUN_DIR": str(tmp_path / "nope")}) == 1
