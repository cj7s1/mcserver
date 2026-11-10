"""restic locks, signals and dumps (audit agent-backups#4, #7, #8), against a stand-in restic.

restic 0.19 never removes a stale lock by itself: the agent unlocks every repository at start-up
(``--remove-all``: nothing of it runs yet), unlocks stale locks and retries once when a command
finds its repository locked (exit 11), lets backup, forget and check wait for a lock, and stops
restic with SIGINT first (restic then removes its own lock). The stand-in is
``tests/fake_restic.py``.
"""

from __future__ import annotations

import json
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from mcagent import backups, main
from mcagent.backups import Restic
from mcagent.docker_ops import AgentError

from . import fake_restic

SNAP_ID = "0123456789abcdef" * 4


@pytest.fixture
def restic_env(agent_settings, tmp_path):
    agent_settings.restic_binary = str(fake_restic.install(tmp_path / "bin"))
    home = agent_settings.state_dir
    iid = uuid.uuid4()
    r = Restic(agent_settings, iid)
    r.repo.mkdir(parents=True)
    (r.repo / "config").write_text("x")  # initialised
    yield agent_settings, home, r
    with backups._startup_lock:
        backups._startup_pending.clear()


def _snapshots_json(root: str) -> str:
    return json.dumps(
        [{"id": SNAP_ID, "short_id": SNAP_ID[:8], "paths": [root], "time": "2026-11-09T00:00:00Z"}]
    )


# -- exit 11: unlock stale locks, try once more --------------------------------------------------
def test_a_locked_repository_is_unlocked_and_the_command_tried_again(restic_env):
    _settings, home, r = restic_env
    fake_restic.set_plan(
        home, {"forget": [{"code": 11, "stderr": "repository is already locked"}, {"code": 0}]}
    )
    assert r.forget(["abcd1234"], prune=True) == {"removed": ["abcd1234"]}
    assert fake_restic.commands(home) == [
        ["forget", "--retry-lock", "2m", "abcd1234", "--prune"],
        ["unlock"],  # stale locks only: a live restic keeps its lock
        ["forget", "--retry-lock", "2m", "abcd1234", "--prune"],
    ]


def test_a_lock_that_stays_is_409_repo_locked_after_one_unlock(restic_env):
    _settings, home, r = restic_env
    fake_restic.set_plan(home, {"check": [{"code": 11, "stderr": "repository is already locked by PID 7"}]})
    with pytest.raises(AgentError) as err:
        r.check()
    assert err.value.code == "repo_locked" and err.value.status == 409
    assert fake_restic.commands(home) == [
        ["check", "--retry-lock", "2m"],
        ["unlock"],
        ["check", "--retry-lock", "2m"],
    ]


def test_a_check_that_finds_errors_is_a_result_not_an_error(restic_env):
    _settings, home, r = restic_env
    fake_restic.set_plan(home, {"check": [{"code": 1, "stderr": "Fatal: repository contains errors"}]})
    assert r.check() == {"ok": False, "output": ["Fatal: repository contains errors"]}


def test_backup_waits_for_a_lock_and_is_retried_after_an_unlock(restic_env, tmp_path):
    _settings, home, r = restic_env
    summary = json.dumps({"message_type": "summary", "snapshot_id": SNAP_ID}) + "\n"
    fake_restic.set_plan(home, {"backup": [{"code": 11}, {"code": 0, "stdout": summary}]})
    result = r.backup(tmp_path, ["t"], ["logs"])
    assert result["snapshot_id"] == SNAP_ID
    runs = fake_restic.commands(home)
    assert [c[0] for c in runs] == ["backup", "unlock", "backup"]
    backup = runs[0]
    assert backup[backup.index("--retry-lock") + 1] == "2m"
    # the agent's own temporary files and imported archives never go into a backup
    excludes = [backup[i + 1] for i, a in enumerate(backup) if a == "--exclude"]
    assert excludes == ["logs", ".mcpanel-tmp-*", ".mcpanel-import-*", ".mcpanel-world-*"]


# -- agent start: every repository is unlocked with --remove-all before it is used ----------------
def test_start_up_unlocks_every_repository_before_any_command_uses_it(restic_env, tmp_path):
    """``unlock --remove-all`` removes every lock of a repository, also the lock of a command this
    agent has just started: no command on a repository may start before that repository's
    start-up unlock has ended (the stand-in records when each run starts and ends)."""
    settings, home, r = restic_env
    other = Restic(settings, uuid.uuid4())
    other.repo.mkdir(parents=True)
    (other.repo / "config").write_text("x")
    (settings.backups_dir / "restic" / "not-a-repo").mkdir()  # no config: left alone
    summary = json.dumps({"message_type": "summary", "snapshot_id": SNAP_ID}) + "\n"
    fake_restic.set_plan(
        home,
        {
            "unlock": [{"sleep": 0.6}],
            "snapshots": [{"stdout": _snapshots_json("/srv/x")}],
            "backup": [{"stdout": summary}],
        },
    )
    repos = backups.prepare_startup_unlock(settings)
    assert sorted(repos) == sorted([r.repo, other.repo])
    unlocker = threading.Thread(target=backups.startup_unlock, args=(settings, repos))
    unlocker.start()
    time.sleep(0.1)  # both unlocks are running now
    results: dict[str, object] = {}

    def use(name, fn):
        results[name] = fn()

    users = [
        threading.Thread(target=use, args=("snapshots", lambda: len(r.snapshots()))),  # Restic.run
        threading.Thread(
            target=use, args=("backup", lambda: other.backup(tmp_path, ["t"], [])["snapshot_id"])
        ),
        threading.Thread(target=use, args=("forget", lambda: other.forget([SNAP_ID], prune=False) and "ok")),
    ]
    for t in users:
        t.start()
    for t in users:
        t.join(15)
    unlocker.join(15)
    assert results == {"snapshots": 1, "backup": SNAP_ID, "forget": "ok"}

    runs = fake_restic.runs(home)
    unlocks = {run["repo"]: run for run in runs if run["argv"] == ["unlock", "--remove-all"]}
    assert sorted(unlocks) == sorted([str(r.repo), str(other.repo)])
    assert all(u["end"] is not None for u in unlocks.values())
    used = [run for run in runs if run["argv"][0] != "unlock"]
    assert {run["argv"][0] for run in used} == {"snapshots", "backup", "forget"}
    for run in used:
        assert run["start"] >= unlocks[run["repo"]]["end"], (
            f"{run['argv'][0]} started before the unlock ended"
        )


def test_a_dump_waits_for_the_start_up_unlock_of_its_repository(restic_env, monkeypatch):
    """``dump`` starts restic itself (not through ``run``): it waits too."""
    settings, home, r = restic_env
    fake_restic.set_plan(home, {"unlock": [{"sleep": 0.6}], "dump": [{"stdout": "level data"}]})
    snap = {"id": SNAP_ID, "paths": ["/srv/x"]}
    monkeypatch.setattr(Restic, "snapshot", lambda self, _sid: snap)  # no restic run before the dump
    repos = backups.prepare_startup_unlock(settings)
    unlocker = threading.Thread(target=backups.startup_unlock, args=(settings, repos))
    unlocker.start()
    time.sleep(0.1)
    stream = r.dump(SNAP_ID, ["world", "level.dat"])
    assert stream.read(64) == b"level data"
    stream.finish()
    unlocker.join(15)
    unlock, dump = fake_restic.runs(home)
    assert unlock["argv"] == ["unlock", "--remove-all"] and dump["argv"][0] == "dump"
    assert dump["start"] >= unlock["end"]


def test_a_failed_start_up_unlock_is_logged_and_releases_the_repository(restic_env, caplog):
    settings, home, r = restic_env
    fake_restic.set_plan(home, {"unlock": [{"code": 1, "stderr": "Fatal: unable to open repository"}]})
    repos = backups.prepare_startup_unlock(settings)
    assert backups.startup_unlock(settings, repos) == {str(r.repo): "Fatal: unable to open repository"}
    assert "unable to open repository" in caplog.text
    assert not backups._startup_pending


# -- stopping restic: SIGINT, then SIGTERM, then SIGKILL --------------------------------------------
def _signals(home: Path) -> list[str]:
    return [c["signal"] for c in fake_restic.calls(home) if "signal" in c]


def test_a_restic_that_runs_too_long_gets_sigint_first(restic_env):
    _settings, home, r = restic_env
    fake_restic.set_plan(home, {"stats": [{"sleep": 30}]})
    started = time.monotonic()
    with pytest.raises(AgentError) as err:
        r._exec(["stats"], timeout=0.5)
    assert err.value.code == "restic_timeout" and time.monotonic() - started < 10
    assert _signals(home) == ["SIGINT"]  # restic removes its lock on SIGINT


def test_terminate_escalates_to_sigterm_and_sigkill(restic_env):
    _settings, home, r = restic_env
    fake_restic.set_plan(home, {"stats": [{"sleep": 30, "sigint": "ignore", "sigterm": "ignore"}]})
    proc = subprocess.Popen([r.binary, "stats"], env=r._env(), stdout=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while not fake_restic.commands(home) and time.monotonic() < deadline:
        time.sleep(0.02)
    code = backups.terminate(proc, int_grace=0.3, term_grace=0.3)
    assert code == -signal.SIGKILL
    assert _signals(home) == ["SIGINT", "SIGTERM"]


def test_agent_shutdown_stops_running_restic_children(restic_env):
    _settings, home, r = restic_env
    fake_restic.set_plan(home, {"backup": [{"sleep": 30}]})
    box: dict = {}

    def run() -> None:
        try:
            r.run(["backup", "--json", "/x"], timeout=60)
        except AgentError as exc:
            box["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    deadline = time.monotonic() + 10
    while backups.running_children() == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert backups.stop_children(grace=2) == 1
    t.join(10)
    assert not t.is_alive() and box["error"].code == "restic_failed"
    assert _signals(home) == ["SIGINT"] and backups.running_children() == 0


def test_the_shutdown_hook_stops_restic_children(monkeypatch, agent_settings):
    import asyncio

    stopped: list[float] = []
    monkeypatch.setattr(backups, "stop_children", lambda grace: stopped.append(grace) or 0)
    monkeypatch.setattr(main, "get_settings", lambda: agent_settings)
    asyncio.run(main._shutdown())
    assert stopped == [main.SHUTDOWN_RESTIC_GRACE]


# -- agent-backups#7: names with [ ] * ? are restored as themselves ------------------------------------
@pytest.mark.parametrize(
    ("name", "pattern"),
    [
        ("[1.20.1] SomeMod.jar", r"\[1.20.1\] SomeMod.jar"),
        ("foo[.txt", r"foo\[.txt"),
        ("what?*.txt", r"what\?\*.txt"),
        ("back\\slash", r"back\\slash"),
        ("plain.jar", "plain.jar"),
    ],
)
def test_glob_escape(name, pattern):
    assert backups.glob_escape(name) == pattern


def test_restore_path_passes_an_escaped_include_pattern(restic_env, tmp_path):
    _settings, home, r = restic_env
    root = "/srv/data/instances/x"
    fake_restic.set_plan(home, {"snapshots": [{"stdout": _snapshots_json(root)}]})
    staging = tmp_path / "staging"

    real_exec = r._exec

    def exec_and_create(args, **kwargs):
        proc = real_exec(args, **kwargs)
        if args[0] == "restore":  # what restic does when the pattern matches
            (staging / "[1.20.1] SomeMod.jar").write_bytes(b"jar")
        return proc

    r._exec = exec_and_create  # type: ignore[method-assign]
    restored = r.restore_path(SNAP_ID[:8], ["mods", "[1.20.1] SomeMod.jar"], staging)
    assert restored.read_bytes() == b"jar"
    restore = next(c for c in fake_restic.commands(home) if c[0] == "restore")
    assert restore[restore.index("--include") + 1] == r"/\[1.20.1\] SomeMod.jar"
    assert restore[1] == f"{SNAP_ID}:{root}/mods"


# -- agent-backups#8: a failed dump is an error, never a complete-looking file ------------------------
@pytest.fixture
def dump_env(restic_env, api, auth):
    settings, home, r = restic_env
    root = str(settings.instances_dir / str(r.instance_id))

    def plan(dump: list[dict], kind: str = "file") -> None:
        node = {"struct_type": "node", "path": f"{root}/world/level.dat", "type": kind, "name": "level.dat"}
        snap = {"struct_type": "snapshot", "id": SNAP_ID, "paths": [root]}
        fake_restic.set_plan(
            home,
            {
                "snapshots": [{"stdout": _snapshots_json(root)}],
                "ls": [{"stdout": json.dumps(snap) + "\n" + json.dumps(node) + "\n"}],
                "dump": dump,
            },
        )

    url = f"/v1/instances/{r.instance_id}/backups/{SNAP_ID[:8]}/download?path=world/level.dat"
    return home, plan, lambda: api.get(url, headers=auth)


def test_download_from_a_backup_streams_the_file(dump_env):
    _home, plan, get = dump_env
    plan([{"stdout": "level data"}])
    r = get()
    assert r.status_code == 200 and r.content == b"level data"


def test_download_from_a_backup_of_an_empty_file(dump_env):
    _home, plan, get = dump_env
    plan([{"stdout": ""}])
    r = get()
    assert r.status_code == 200 and r.content == b""


def test_download_that_fails_at_once_is_an_error_answer(dump_env):
    _home, plan, get = dump_env
    plan([{"code": 1, "stderr": "Fatal: cannot read file: input/output error\n"}])
    r = get()
    assert r.status_code == 500 and r.json()["error"]["code"] == "restic_failed"
    assert "input/output error" in r.json()["error"]["message"]


def test_download_of_a_locked_repository_unlocks_and_tries_again(dump_env):
    home, plan, get = dump_env
    plan([{"code": 11, "stderr": "repository is already locked"}, {"stdout": "level data"}])
    r = get()
    assert r.status_code == 200 and r.content == b"level data"
    assert [c[0] for c in fake_restic.commands(home)].count("unlock") == 1


def test_download_that_fails_part_way_does_not_end_as_a_complete_download(dump_env):
    _home, plan, get = dump_env
    plan([{"stdout": "half of the f", "code": 1, "stderr": "Fatal: read error\n"}])
    # the chunked body is aborted, not ended (Starlette: "response already started")
    with pytest.raises(RuntimeError) as err:
        get()
    assert isinstance(err.value.__cause__, AgentError) and err.value.__cause__.code == "restic_failed"


def test_a_folder_is_not_downloaded_as_a_tar_under_its_name(dump_env):
    home, plan, get = dump_env
    plan([{"stdout": "tar data"}], kind="dir")
    r = get()
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_file"
    assert "dump" not in [c[0] for c in fake_restic.commands(home)]


def test_dump_stops_restic_when_the_reader_goes_away(restic_env):
    _settings, home, r = restic_env
    root = "/srv/x"
    fake_restic.set_plan(
        home,
        {
            "snapshots": [{"stdout": _snapshots_json(root)}],
            "dump": [{"stdout": "x" * 1000, "sleep": 30}],
        },
    )
    stream = r.dump(SNAP_ID[:8], ["big.bin"])
    assert stream.read(10) == b"x" * 10
    started = time.monotonic()
    stream.abort()  # the client disconnected
    assert time.monotonic() - started < 10 and stream.proc.poll() is not None
    assert _signals(home) == ["SIGINT"] and backups.running_children() == 0


def test_a_backup_download_the_client_gives_up_on_stops_restic_at_once(restic_env):
    """review r3 #25: the answer closes its generator when it ends, also on a disconnect (before,
    restic kept running, with its lock, until the garbage collector got to the generator)."""
    import asyncio

    from starlette.requests import ClientDisconnect

    settings, home, r = restic_env
    root = str(settings.instances_dir / str(r.instance_id))
    node = {"struct_type": "node", "path": f"{root}/big.bin", "type": "file", "name": "big.bin"}
    snap = {"struct_type": "snapshot", "id": SNAP_ID, "paths": [root]}
    fake_restic.set_plan(
        home,
        {
            "snapshots": [{"stdout": _snapshots_json(root)}],
            "ls": [{"stdout": json.dumps(snap) + "\n" + json.dumps(node) + "\n"}],
            "dump": [{"stdout": "x" * (3 * main.DOWNLOAD_CHUNK), "sleep": 30}],
        },
    )

    async def scenario():
        response = await main.backup_download(r.instance_id, SNAP_ID[:8], path="big.bin", settings=settings)
        sent = 0

        async def send(message):
            nonlocal sent
            if message["type"] == "http.response.body" and message.get("body"):
                sent += 1
                if sent > 1:
                    raise OSError("connection reset by peer")

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        return response, backups.running_children()

    response, running = asyncio.run(scenario())
    assert running == 0 and _signals(home) == ["SIGINT"]
    del response
