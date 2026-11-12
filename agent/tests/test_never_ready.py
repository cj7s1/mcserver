"""E-1 / E-2 of the security review (2026-11-11): a server that IS up must never be taken for "never ready"
and stopped without its save; a long-running server must not read "Unresponsive" after an agent restart.

Adopted from the reviewers' PoC (audit-2026-11-09/security-review/e-bugs/poc/agent/test_e_never_ready.py).
The fake container answers ``logs(since=, tail=)`` the way Docker does: only lines stamped after ``since``,
and of those only the last ``tail``; with ``stream=True`` it yields Docker's timestamped lines.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import pytest

from mcagent import docker_ops, drivers
from mcagent.profiles import palworld, rust, valheim, zomboid

IID = uuid.UUID("7c2e4a6b-8d0f-4e1a-9b3c-5d7e9f1a2b3c")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _iso_nano(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


class Run:
    """A running container with a time-stamped log, like Docker's json-file log."""

    def __init__(self, game: str, started: float, lines: list[tuple[float, str]], *, streams: bool = False):
        self.id = f"c-{game}"
        self.name = f"mcpanel-{game}"
        self.status = "running"
        self.labels = {"io.mcpanel.instance": str(IID), "io.mcpanel.game": game}
        self.lines = lines
        self.streams = streams
        self.log_calls: list[dict] = []
        self.chunks_read = 0
        self.attrs = {
            "Config": {"Env": [], "Image": "x"},
            "State": {"Status": "running", "Running": True, "StartedAt": _iso(started), "ExitCode": 0},
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": "10.1.2.9"}}, "Ports": {}},
            "HostConfig": {},
        }
        self.stopped_with = None

    def reload(self):
        pass

    def logs(self, since=None, tail="all", stream=False, timestamps=False, **_kw):
        self.log_calls.append({"since": since, "tail": tail, "stream": stream})
        out = [(ts, text) for ts, text in self.lines if since is None or ts >= since]
        if tail != "all":
            out = out[-int(tail) :]
        if not (stream and self.streams):
            return ("\n".join(text for _ts, text in out) + "\n").encode()

        def gen():
            for ts, text in out:
                self.chunks_read += 1
                yield (f"{_iso_nano(ts)} {text}\n" if timestamps else f"{text}\n").encode()

        return gen()

    def stop(self, timeout):
        self.stopped_with = timeout
        self.status = "exited"
        self.attrs["State"].update({"Status": "exited", "Running": False, "ExitCode": 137})


@pytest.fixture
def driver_stops(monkeypatch):
    """The driver's own stop (save first, then the usual timeout), recorded instead of run."""
    calls = []

    def fake(self, settings, c, timeout, result):
        calls.append(timeout)
        c.stop(timeout)
        return True

    for cls in {
        type(drivers.for_profile(p))
        for p in (zomboid.PROFILE, palworld.PROFILE, rust.PROFILE, valheim.PROFILE)
    }:
        monkeypatch.setattr(cls, "stop", fake)
    monkeypatch.setattr(docker_ops, "fix_ownership", lambda *a, **k: None)
    return calls


READY = {
    "project-zomboid": "LOG  : Network      f:0 st:395,090,316> *** SERVER STARTED ****",
    "valheim": "11/11/2026 09:26:22: Game server connected",
    "palworld": "Running Palworld dedicated server on :8211",
}


def _played_run(game: str, started: float, chatter: int, *, streams: bool = False) -> Run:
    """Ready 60 s after its start, then `chatter` more log lines (players joining, saves, mods ...)."""
    lines = [(started + 1, "starting"), (started + 60, READY[game])]
    lines += [(started + 61 + i * 0.5, f"chatter {i}") for i in range(chatter)]
    return Run(game, started, lines, streams=streams)


@pytest.mark.parametrize("streams", [False, True])
@pytest.mark.parametrize("game", ["project-zomboid", "valheim", "palworld"])
def test_e1_first_look_after_5000_lines_then_stop_saves(agent_settings, driver_stops, game, streams):
    """The agent's first look at the log comes after more than 5000 lines: the ready line is still found,
    and a stop without the panel's word is the ordinary saving one."""
    now = time.time()
    c = _played_run(game, started=now - 3 * 3600, chatter=6000, streams=streams)
    described = docker_ops.describe(c)
    assert described["health"] == "healthy", described["health_output"]
    result = docker_ops.graceful_stop(agent_settings, c, 120)
    assert driver_stops == [120], "a server that ran for 3 h must get its saving stop"
    assert c.stopped_with == 120 and not result.not_ready


def test_e2_after_an_agent_restart_a_long_running_server_reads_healthy(agent_settings):
    now = time.time()
    c = _played_run("valheim", started=now - 2 * 86400, chatter=6000)
    first = docker_ops.describe(c)
    later = docker_ops.agent_health(c, valheim.PROFILE, docker_ops.describe(c), now=now + 3600)
    assert first["health"] == "healthy" and later[0] == "healthy"
    # the first look of a run reads its whole log since StartedAt, not a tail
    assert c.log_calls[0]["tail"] == "all" and c.log_calls[0]["stream"] is True


def test_e1b_the_panels_word_does_not_kill_a_server_that_ran_for_days(agent_settings, driver_stops):
    """Agent restart + a Start job that waits + Stop: the panel sends not_ready, but the run was ready."""
    now = time.time()
    c = _played_run("project-zomboid", started=now - 2 * 86400, chatter=6000)
    result = docker_ops.graceful_stop(agent_settings, c, 120, not_ready=True)
    assert driver_stops == [120] and not result.not_ready


def test_e1c_a_probe_that_never_answers_does_not_make_stops_kills(agent_settings, driver_stops, monkeypatch):
    now = time.time()
    c = Run("rust", now - 6 * 3600, [(now - 6 * 3600 + 1, "booting")])
    monkeypatch.setattr(docker_ops, "_probe_a2s", lambda *a: False)
    result = docker_ops.graceful_stop(agent_settings, c, 180)
    assert driver_stops == [180] and not result.not_ready


def test_e1d_a_full_table_drops_only_marks_of_containers_that_are_gone(monkeypatch):
    ids = {f"c{i}" for i in range(docker_ops._READY_RUNS_MAX)} | {"newest"}
    monkeypatch.setattr(docker_ops, "_existing_container_ids", lambda: set(ids) - {"c7"})
    for i in range(docker_ops._READY_RUNS_MAX):
        docker_ops._mark_ready((f"c{i}", "t"), float(i))
    docker_ops._mark_ready(("newest", "t"), 1e9)
    assert ("c0", "t") in docker_ops._ready_runs and ("newest", "t") in docker_ops._ready_runs
    assert ("c7", "t") not in docker_ops._ready_runs


def test_a_full_table_keeps_everything_when_docker_does_not_answer(monkeypatch):
    monkeypatch.setattr(docker_ops, "_READY_RUNS_MAX", 3)
    monkeypatch.setattr(docker_ops, "_existing_container_ids", lambda: None)
    for i in range(5):
        docker_ops._mark_ready((f"c{i}", "t"), float(i))
    assert len(docker_ops._ready_runs) == 5


def test_one_mark_per_container_its_latest_run():
    docker_ops._mark_ready(("c1", "run-1"), 1.0)
    docker_ops._mark_ready(("c1", "run-2"), 2.0)
    assert docker_ops._ready_runs == {("c1", "run-2"): 2.0}


# --- ready marks on disk ---------------------------------------------------------------------------------
def test_ready_marks_survive_an_agent_restart(agent_settings, driver_stops, monkeypatch):
    now = time.time()
    monkeypatch.setattr(docker_ops, "_existing_container_ids", lambda: {"c-project-zomboid"})
    docker_ops.load_ready_marks(agent_settings)
    c = _played_run("project-zomboid", started=now - 3600, chatter=10)
    assert docker_ops.describe(c)["health"] == "healthy"
    stored = json.loads((agent_settings.state_dir / docker_ops.READY_MARKS_FILE).read_text())
    assert [entry[:2] for entry in stored] == [["c-project-zomboid", c.attrs["State"]["StartedAt"]]]

    # the agent restarts: memory is empty, the log no longer shows the line (rotated away)
    monkeypatch.setattr(docker_ops, "_ready_runs", {})
    monkeypatch.setattr(docker_ops, "_health_state", {})
    monkeypatch.setattr(docker_ops, "_ready_file", None)
    assert docker_ops.load_ready_marks(agent_settings) == 1
    c.lines = [(now - 10, "chatter after rotation")]
    c.log_calls.clear()
    assert docker_ops.describe(c)["health"] == "healthy"
    assert c.log_calls == []  # a run marked ready needs no new look at its log
    result = docker_ops.graceful_stop(agent_settings, c, 120, not_ready=True)
    assert driver_stops == [120] and not result.not_ready


def test_loading_drops_marks_of_containers_that_are_gone(agent_settings, monkeypatch):
    path = agent_settings.state_dir / docker_ops.READY_MARKS_FILE
    path.write_text(json.dumps([["alive", "t1", 1.0], ["gone", "t2", 2.0], ["bad"], "junk"]))
    monkeypatch.setattr(docker_ops, "_existing_container_ids", lambda: {"alive"})
    assert docker_ops.load_ready_marks(agent_settings) == 1
    assert docker_ops._ready_runs == {("alive", "t1"): 1.0}
    assert json.loads(path.read_text()) == [["alive", "t1", 1.0]]


def test_loading_keeps_every_mark_when_docker_does_not_answer(agent_settings, monkeypatch):
    path = agent_settings.state_dir / docker_ops.READY_MARKS_FILE
    path.write_text(json.dumps([["a", "t1", 1.0], ["b", "t2", 2.0]]))
    monkeypatch.setattr(docker_ops, "_existing_container_ids", lambda: None)
    assert docker_ops.load_ready_marks(agent_settings) == 2


def test_a_damaged_marks_file_is_not_fatal(agent_settings):
    (agent_settings.state_dir / docker_ops.READY_MARKS_FILE).write_text("{not json")
    assert docker_ops.load_ready_marks(agent_settings) == 0


def test_a_removed_container_loses_its_mark(agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "_existing_container_ids", lambda: {"c1", "c2"})
    docker_ops.load_ready_marks(agent_settings)
    docker_ops._mark_ready(("c1", "t"), 1.0)
    docker_ops._mark_ready(("c2", "t"), 2.0)
    docker_ops.forget_ready("c1")
    stored = json.loads((agent_settings.state_dir / docker_ops.READY_MARKS_FILE).read_text())
    assert stored == [["c2", "t", 2.0]]


# --- the streamed log scan ---------------------------------------------------------------------------------
def test_a_look_cut_by_its_budget_goes_on_from_its_last_line(agent_settings, monkeypatch):
    now = time.time()
    c = _played_run("valheim", started=now - 3600, chatter=0, streams=True)
    c.lines = [(now - 3600 + i, f"downloading {i}") for i in range(100)] + [(now - 1000, READY["valheim"])]
    monkeypatch.setattr(docker_ops, "HEALTH_LOG_SCAN_SECONDS", 0.0)  # every look reads one chunk only
    health, _ = docker_ops.agent_health(c, valheim.PROFILE, docker_ops.describe(c))
    assert health == "unhealthy" and c.chunks_read <= 3
    # later looks start from where the previous one ended, so the line is reached without a full re-read
    for _ in range(200):
        health, _ = docker_ops.agent_health(c, valheim.PROFILE, docker_ops.describe(c))
        if health == "healthy":
            break
    assert health == "healthy"
    assert c.log_calls[-1]["since"] > now - 3600


def test_a_line_split_over_chunks_is_matched(agent_settings):
    now = time.time()
    c = Run("palworld", now - 60, [])

    def logs(**kwargs):
        stamp = _iso_nano(now - 30)
        return iter([f"{stamp} Running Palworld ded".encode(), b"icated server on :8211\n"])

    c.logs = logs
    assert docker_ops.agent_health(c, palworld.PROFILE, docker_ops.describe(c))[0] == "healthy"


def test_the_log_stream_is_closed(agent_settings):
    now = time.time()
    closed = []

    class Stream:
        def __iter__(self):
            yield f"{_iso_nano(now - 5)} {READY['palworld']}\n".encode()
            yield b"never read\n"

        def close(self):
            closed.append(True)

    c = Run("palworld", now - 60, [])
    c.logs = lambda **kw: Stream()
    assert docker_ops.agent_health(c, palworld.PROFILE, docker_ops.describe(c))[0] == "healthy"
    assert closed == [True]
