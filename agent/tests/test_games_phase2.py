"""Multi-game phase 2 + 3 on the agent: the generic pieces and the Factorio profile.

* profile data: exact image tags, the environment allow-list with value patterns, ports;
* Factorio's container (runtime uid:gid as the user, no capabilities, /factorio, no Docker
  health check, the game label) and the mounting of every VOLUME an image declares;
* the agent-side readiness probes (``tcp``, ``log``) as ``describe()`` reports them;
* the RCON driver with its password in a file of the server folder (real RCON peer), and the
  save that waits for the game's "save finished" log line (save and stop);
* the control secret hidden in every console text; the profile's backup excludes; /v1/info.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from mcagent import docker_ops, drivers, lastrun, main, profiles
from mcagent.docker_ops import AgentError, build_create_kwargs, image_volume_plan, validate_image
from mcagent.drivers import RconCommands, RconDriver, RconStep
from mcagent.profiles import Health, Mount, factorio
from mcagent.profiles import minecraft as mc_profile
from mcagent.schemas import InstanceSpec

from .test_games import _testgame
from .test_graceful_stop import RconPeer

IID = uuid.UUID("0123abcd-0000-4000-8000-0000000fac70")
IMAGE = "factoriotools/factorio:2.1.24-rootless"
SECRET = "Fz-rcon-secret-0123456789"


@pytest.fixture
def settings(agent_settings):
    agent_settings.runtime_uid, agent_settings.runtime_gid = 99, 100
    (agent_settings.instances_dir / str(IID)).mkdir(parents=True)
    return agent_settings


def fz_spec(**over) -> InstanceSpec:
    values = {
        "name": "Factory",
        "image": IMAGE,
        "memory_limit_mb": 2048,
        "rcon_password": "x" * 32,
        "game": "factorio",
        "ports": [{"container_port": 34197, "host_port": 34197, "protocol": "udp"}],
        "env": {
            "DLC_SPACE_AGE": "false",
            "GENERATE_NEW_SAVE": "true",
            "LOAD_LATEST_SAVE": "false",
            "SAVE_NAME": "world",
        },
        "stop_timeout": 120,
    }
    values.update(over)
    return InstanceSpec(**values)


# --- profile data ------------------------------------------------------------------------------------
def test_factorio_profile_is_registered():
    assert profiles.get("factorio") is factorio.PROFILE
    assert profiles.for_spec(fz_spec()) is factorio.PROFILE
    assert factorio.PROFILE.control == "rcon"


@pytest.mark.parametrize(
    "image",
    ["factoriotools/factorio:2.1.24-rootless", "docker.io/factoriotools/factorio:2.0.77-rootless"],
)
def test_factorio_runs_exact_rootless_tags(settings, image):
    assert validate_image(settings, image, factorio.PROFILE) == image


@pytest.mark.parametrize(
    "image",
    [
        "factoriotools/factorio:2.1.24",
        "factoriotools/factorio:stable-rootless",
        "factoriotools/factorio:latest",
    ],
)
def test_factorio_refuses_moving_or_root_tags(settings, image):
    with pytest.raises(AgentError) as exc:
        validate_image(settings, image, factorio.PROFILE)
    assert exc.value.code == "invalid_image"


def test_factorio_refuses_other_repositories(settings):
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, fz_spec(image="itzg/minecraft-server:stable-java21"), "h")
    assert exc.value.code == "image_not_allowed"


@pytest.mark.parametrize(
    "env",
    [
        {"UPDATE_MODS_ON_START": "true"},
        {"USERNAME": "me"},
        {"TOKEN": "t"},
        {"CONFIG": "/tmp"},
        {"SERVER_SETTINGS_FILE": "/etc/passwd"},
        {"PRESET": "death-world"},
        {"DLC_SPACE_AGE": "space-age quality"},
        {"SAVE_NAME": "/factorio/../etc/x"},
        {"SAVE_NAME": "a b"},
        {"GENERATE_NEW_SAVE": "yes"},
        {"BIND": "0.0.0.0"},
        {"PUID": "0"},
    ],
)
def test_factorio_environment_is_an_allow_list(settings, env):
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, fz_spec(env=env), "h")
    assert exc.value.code == "env_not_allowed"


def test_injected_ports_replace_what_a_spec_says(settings):
    kwargs = build_create_kwargs(settings, IID, fz_spec(env={"PORT": "1234"}), "h")
    assert kwargs["environment"]["PORT"] == "34197"


def test_factorio_accepts_what_the_panel_sends(settings):
    env = {
        "DLC_SPACE_AGE": "true",
        "GENERATE_NEW_SAVE": "true",
        "LOAD_LATEST_SAVE": "false",
        "SAVE_NAME": "world_2",
        "TZ": "UTC",
    }
    kwargs = build_create_kwargs(settings, IID, fz_spec(env=env), "h")
    assert {k: kwargs["environment"][k] for k in env} == env


@pytest.mark.parametrize(
    ("port", "protocol"), [(34197, "tcp"), (27015, "tcp"), (27015, "udp"), (25565, "tcp")]
)
def test_factorio_publishes_only_its_game_port(settings, port, protocol):
    spec = fz_spec(ports=[{"container_port": port, "host_port": 34200, "protocol": protocol}])
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, spec, "h")
    assert exc.value.code == "port_not_allowed"


# --- the container -----------------------------------------------------------------------------------------
def test_factorio_container(settings):
    kwargs = build_create_kwargs(settings, IID, fz_spec(), "abc123", image_volumes=["/factorio"])
    host = str(settings.host_instances_dir / str(IID))
    assert kwargs["user"] == "99:100"
    assert kwargs["cap_add"] == [] and kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["volumes"] == {host: {"bind": "/factorio", "mode": "rw"}}
    assert kwargs["healthcheck"] == {"test": ["NONE"]}
    assert "tmpfs" not in kwargs
    assert kwargs["labels"]["io.mcpanel.game"] == "factorio" and "io.mcpanel.variant" not in kwargs["labels"]
    assert kwargs["ports"] == {"34197/udp": [("0.0.0.0", 34197)]}
    assert kwargs["environment"]["PORT"] == "34197" and kwargs["environment"]["RCON_PORT"] == "27015"
    assert kwargs["environment"]["SAVE_NAME"] == "world"
    assert "RCON_PASSWORD" not in kwargs["environment"]
    assert kwargs["hostname"] == f"factorio-{IID.hex[:8]}"
    assert kwargs["stdin_open"] is False and kwargs["tty"] is False
    assert kwargs["stop_signal"] == "SIGTERM"
    assert kwargs["image"] == IMAGE


def test_every_declared_volume_gets_a_mount(settings):
    profile = _testgame(mount_image_volumes=True, volume_tmpfs=("/tmp/cache",))
    declared = ["/srv/game", "/srv/game/logs", "/srv/game/saves", "/var/lib/thing", "/tmp/cache", "/x/"]
    binds, tmpfs = image_volume_plan(profile, declared)
    # /srv/game and /srv/game/saves are profile mounts; logs is below the "." mount
    assert binds == {"/srv/game/logs": "logs", "/var/lib/thing": ".volumes/var_lib_thing", "/x": ".volumes/x"}
    assert tmpfs == ["/tmp/cache"]
    assert image_volume_plan(profile, None) == ({}, [])
    assert image_volume_plan(_testgame(), declared) == ({}, [])  # mount_image_volumes off


def test_image_volumes_in_the_create_arguments(settings, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    profile = profiles.register(_testgame(mount_image_volumes=True, volume_tmpfs=("/tmp/cache",)))
    spec = InstanceSpec(
        name="T",
        image="example/testgame:1",
        memory_limit_mb=1024,
        rcon_password="x" * 16,
        game=profile.game,
        ports=[{"container_port": 7777, "host_port": 7777, "protocol": "udp"}],
    )
    kwargs = build_create_kwargs(settings, IID, spec, "h", image_volumes=["/var/lib/thing", "/tmp/cache"])
    host = settings.host_instances_dir / str(IID)
    assert kwargs["volumes"][str(host / ".volumes/var_lib_thing")] == {"bind": "/var/lib/thing", "mode": "rw"}
    assert kwargs["tmpfs"] == {"/tmp/cache": docker_ops.IMAGE_VOLUME_TMPFS_OPTIONS}


def test_a_declared_volume_path_with_dotdot_is_refused():
    with pytest.raises(AgentError):
        image_volume_plan(factorio.PROFILE, ["/factorio/../etc"])


class _Images:
    def __init__(self, volumes):
        self.volumes = volumes
        self.asked: list[str] = []

    def get(self, name):
        self.asked.append(name)
        return SimpleNamespace(attrs={"Config": {"Volumes": self.volumes}})


class _CreatingClient:
    def __init__(self, volumes):
        self.images = _Images(volumes)
        self.created: dict = {}
        self.api = SimpleNamespace(api_version="1.47", create_container=self._create)
        self.containers = SimpleNamespace(get=lambda cid: SimpleNamespace(id=cid))

    def _create(self, **kwargs):
        self.created = kwargs
        return {"Id": "new"}


def test_create_container_inspects_the_image_and_prepares_the_folders(settings, monkeypatch):
    fake = _CreatingClient({"/factorio": {}, "/factorio/saves": {}, "/opt/cache": {}})
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    docker_ops.create_container(settings, IID, fz_spec(), "h")
    assert fake.images.asked == [IMAGE]
    folder = settings.instances_dir / str(IID)
    assert (folder / "saves").is_dir() and (folder / ".volumes" / "opt_cache").is_dir()
    binds = fake.created["host_config"]["Binds"]
    host = settings.host_instances_dir / str(IID)
    assert f"{host}:/factorio:rw" in binds
    assert f"{host / 'saves'}:/factorio/saves:rw" in binds
    assert f"{host / '.volumes/opt_cache'}:/opt/cache:rw" in binds


def test_create_container_refuses_a_volume_target_that_is_a_file(settings, monkeypatch):
    fake = _CreatingClient({"/factorio/saves": {}})
    (settings.instances_dir / str(IID) / "saves").write_text("not a folder")
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    with pytest.raises(AgentError) as exc:
        docker_ops.create_container(settings, IID, fz_spec(), "h")
    assert exc.value.status == 409 and fake.created == {}


def test_minecraft_create_never_inspects_the_image(settings, monkeypatch):
    fake = _CreatingClient({"/data": {}})
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    spec = InstanceSpec(
        name="S", image="itzg/minecraft-server:stable-java21", memory_limit_mb=2048, rcon_password="x" * 16
    )
    docker_ops.create_container(settings, IID, spec, "h")
    assert fake.images.asked == []
    assert mc_profile.PROFILE.mount_image_volumes is False


# --- agent-side readiness probes -----------------------------------------------------------------------------
class _Container:
    def __init__(
        self, labels=None, running=True, started="2026-11-10T20:00:00.5Z", logs=b"", ip="172.30.0.9"
    ):
        self.id = "c-" + uuid.uuid4().hex[:6]
        self.name = docker_ops.container_name(IID)
        self.labels = {"io.mcpanel.managed": "true", "io.mcpanel.instance": str(IID), **(labels or {})}
        self.running = running
        self.started = started
        self.log_text = logs
        self.log_calls: list[dict] = []
        self.ip = ip
        self.ports: dict = {}
        self.network_mode = "mcpanel_instances"

    @property
    def status(self):
        return "running" if self.running else "exited"

    @property
    def attrs(self):
        return {
            "Config": {"Image": IMAGE, "Env": []},
            "HostConfig": {"NetworkMode": self.network_mode},
            "State": {
                "Status": self.status,
                "Running": self.running,
                "StartedAt": self.started,
                "ExitCode": 0,
            },
            "NetworkSettings": {"Networks": {"n": {"IPAddress": self.ip}}, "Ports": self.ports},
        }

    def logs(self, **kwargs):
        self.log_calls.append(kwargs)
        text = self.log_text() if callable(self.log_text) else self.log_text
        return text if kwargs.get("stream") is None else iter([text])


def _ts(text):
    return docker_ops._timestamp(text).timestamp()


def _recent(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.5Z", time.gmtime(time.time() - seconds_ago))


def test_log_probe(settings):
    c = _Container(labels={"io.mcpanel.game": "factorio"}, logs=b"loading\n  1.0 Info Changing state\n")
    c.started = _recent(5)
    start = _ts(c.started)
    d = docker_ops.describe(c)
    assert d["health"] == "starting" and "not seen yet" in d["health_output"]
    assert c.log_calls[0]["since"] == pytest.approx(start - docker_ops.HEALTH_LOG_OVERLAP)
    # after the start period without the line: unhealthy (state.py: "Unresponsive")
    health, _ = docker_ops.agent_health(c, factorio.PROFILE, d, now=start + 601)
    assert health == "unhealthy"
    c.log_text = b"x\r  1.04 Info RemoteCommandProcessor.cpp:119: Starting RCON interface at IP ADDR:({0.0.0.0:27015})\n"
    assert docker_ops.describe(c)["health"] == "healthy"
    calls = len(c.log_calls)
    c.log_text = b""  # sticky for the run: the log is not read again
    assert docker_ops.describe(c)["health"] == "healthy" and len(c.log_calls) == calls
    c.started = _recent(1)  # a new run starts over
    assert docker_ops.describe(c)["health"] == "starting"
    c.running = False
    assert docker_ops.describe(c)["health"] is None


def test_minecraft_health_is_dockers(settings):
    c = _Container()
    assert docker_ops.describe(c)["health"] is None and c.log_calls == []


def test_tcp_probe(settings, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    profiles.register(_testgame(health=Health("tcp", port=port, start_period_s=60)))
    c = _Container(labels={"io.mcpanel.game": "testgame"}, ip="127.0.0.1")
    now = time.time()
    c.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 5))
    try:
        assert docker_ops.describe(c)["health"] == "healthy"
    finally:
        server.close()
    # cached for a few seconds, then checked again
    assert docker_ops.describe(c)["health"] == "healthy"
    key = (c.id, c.started)
    docker_ops._health_state[key]["at"] -= docker_ops.HEALTH_TCP_CACHE_SECONDS + 1
    assert docker_ops.describe(c)["health"] == "starting"
    # published mode: the published host port on 127.0.0.1
    server = socket.create_server(("127.0.0.1", 0))
    try:
        settings.rcon_mode = "published"
        c.ports = {f"{port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(server.getsockname()[1])}]}
        assert docker_ops._probe_tcp(c, port, settings) is True
    finally:
        server.close()


def test_register_refuses_unfit_probes(monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    for health in (Health("tcp"), Health("log"), Health("log", pattern="(unclosed"), Health("driver")):
        with pytest.raises(ValueError):
            profiles.register(_testgame(health=health))
    profiles.register(_testgame(health=Health("log", pattern="ready")))


def test_agent_side_probes_get_no_docker_healthcheck(settings, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    profiles.register(_testgame(health=Health("tcp", port=7777)))
    spec = InstanceSpec(
        name="T",
        image="example/testgame:1",
        memory_limit_mb=1024,
        rcon_password="x" * 16,
        game="testgame",
        ports=[{"container_port": 7777, "host_port": 7777, "protocol": "udp"}],
    )
    assert build_create_kwargs(settings, IID, spec, "h")["healthcheck"] == {"test": ["NONE"]}


# --- waiting for a log line -----------------------------------------------------------------------------------
def test_wait_for_log(monkeypatch):
    monkeypatch.setattr(docker_ops, "LOG_WAIT_POLL", 0.01)
    c = _Container()
    lines = [b"Saving game as /factorio/saves/world.zip\n"]
    c.log_text = lambda: b"".join(lines)
    threading.Timer(
        0.05, lambda: lines.append(b"  2.0 Info AppManagerStates.cpp:2131: Saving finished\n")
    ).start()
    assert docker_ops.wait_for_log(c, factorio.SAVE_DONE_PATTERN, 123.5, 2.0) is True
    assert c.log_calls[-1]["since"] == 123.5
    started = time.monotonic()
    c.log_text = b"nothing\n"
    assert docker_ops.wait_for_log(c, factorio.SAVE_DONE_PATTERN, 0.0, 0.1) is False
    assert time.monotonic() - started < 1.0


# --- the RCON driver with its password in a file ---------------------------------------------------------------
def _write_secret(settings, text=SECRET):
    folder = settings.instances_dir / str(IID) / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rconpw").write_text(text + "\n")


def test_factorio_driver_from_its_options():
    d = drivers.for_profile(factorio.PROFILE)
    assert isinstance(d, RconDriver)
    assert (d.port, d.secret_env, d.secret_file) == (27015, None, "config/rconpw")
    assert (d.commands.save_before_stop, d.commands.stop) == ("/server-save", None)
    assert d.commands.save_done_log == "Saving finished"
    assert d.can_save and not d.can_pause_saving
    with pytest.raises(ValueError):
        RconDriver(port=27015, commands=RconCommands())
    with pytest.raises(ValueError):
        RconDriver(port=27015, secret_file="../x", commands=RconCommands())
    plain = RconDriver.from_options({"port": 1, "secret_file": "pw", "commands": {"save_done_log": "done"}})
    assert plain.commands.save_done_log == "done" and plain.secret_env is None


def test_secret_file_is_read_from_the_server_folder(settings):
    d = drivers.for_profile(factorio.PROFILE)
    c = _Container(labels={"io.mcpanel.game": "factorio"})
    with pytest.raises(AgentError) as exc:
        d.endpoint(settings, c)
    assert exc.value.code == "rcon_unavailable"
    _write_secret(settings)
    assert d.endpoint(settings, c) == ("172.30.0.9", 27015, SECRET)


def test_secret_file_symlink_is_not_followed(settings, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("stolen-password")
    folder = settings.instances_dir / str(IID) / "config"
    folder.mkdir(parents=True)
    os.symlink(outside, folder / "rconpw")
    c = _Container(labels={"io.mcpanel.game": "factorio"})
    assert drivers.for_profile(factorio.PROFILE).secret(settings, c) is None


def test_factorio_command_over_real_rcon(api, auth, agent_settings, monkeypatch):
    (agent_settings.instances_dir / str(IID)).mkdir(parents=True)
    _write_secret(agent_settings)
    peer = RconPeer()
    passwords: list[str] = []
    original = RconPeer._read

    def remember(conn):
        req_id, ptype, payload = original(conn)
        if ptype == 3:
            passwords.append(payload.decode())
        return req_id, ptype, payload

    monkeypatch.setattr(RconPeer, "_read", staticmethod(remember))
    c = _Container(labels={"io.mcpanel.game": "factorio"}, ip="127.0.0.1")
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    driver = RconDriver(port=peer.port, secret_file="config/rconpw", commands=factorio.COMMANDS)
    monkeypatch.setattr(drivers, "for_container", lambda _c: driver)
    try:
        r = api.post(f"/v1/instances/{IID}/command", json={"command": "/players online"}, headers=auth)
    finally:
        peer.close()
    assert r.status_code == 200 and r.json() == {"output": "done"}
    assert peer.commands == ["/players online"]  # the leading slash is kept
    assert passwords == [SECRET]


# --- the save that waits for "Saving finished" ------------------------------------------------------------------
def test_save_waits_for_the_log_line():
    driver = drivers.for_profile(factorio.PROFILE)
    sent, waits, reports = [], [], []
    driver.save(
        lambda cmd, t: sent.append((cmd, t)),
        lambda i, m: reports.append(m),
        lambda p, s, t: waits.append((p, t)) or True,
    )
    assert sent == [("/server-save", 120)] and waits == [("Saving finished", 120)]
    assert reports == ["Saving the map (/server-save)"]
    with pytest.raises(AgentError) as exc:
        driver.save(lambda *_a: None, lambda *_a: None, lambda *_a: False)
    assert exc.value.code == "save_unconfirmed"
    driver.save(lambda *_a: None, lambda *_a: None)  # no waiter (an older caller): only sent


def test_backup_save_of_factorio_waits_for_the_log(agent_settings, monkeypatch):
    sent: list[str] = []
    c = _Container(labels={"io.mcpanel.game": "factorio"})
    c.log_text = b"Saving finished\n"
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": True, "game": "factorio"})
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    monkeypatch.setattr(main, "_rcon", lambda _s, _i, cmd, _t: sent.append(cmd))
    monkeypatch.setattr(main, "SAVE_SETTLE_SECONDS", 0)
    op = SimpleNamespace(update=lambda *a, **k: None)
    with main._world_saving_paused(agent_settings, IID, "require", op, "backup", 0.02) as state:
        pass
    assert sent == ["/server-save"] and state.consistent
    assert c.log_calls and "since" in c.log_calls[-1]
    c.log_text = b"Saving game as x\n"
    monkeypatch.setattr(docker_ops, "wait_for_log", lambda *_a: False)
    with pytest.raises(AgentError) as exc:
        with main._world_saving_paused(agent_settings, IID, "require", op, "backup", 0.02):
            pass
    assert exc.value.code == "consistency_failed"


@pytest.fixture
def stopping(settings, monkeypatch):
    from .test_graceful_stop import FakeContainer, FakeDocker

    log: list = []
    container = FakeContainer(log)
    container.labels["io.mcpanel.game"] = "factorio"
    _write_secret(settings)
    container.labels["io.mcpanel.instance"] = str(IID)
    sent: list = []

    def fake_rcon(host, port, password, command, *, timeout, connect_timeout):
        sent.append((port, password, command))
        log.append(("rcon", command))
        return "Saving the map"

    monkeypatch.setattr(docker_ops, "client", lambda: FakeDocker(container))
    monkeypatch.setattr(docker_ops, "run_command_sync", fake_rcon)
    container.stop_may_return.set()
    return SimpleNamespace(container=container, log=log, sent=sent, settings=settings)


def test_stop_saves_and_waits_for_saving_finished(stopping, monkeypatch):
    waited: list = []

    def wait(c, pattern, since, timeout):
        waited.append((pattern, timeout))
        stopping.log.append(("waited",))
        return True

    monkeypatch.setattr(docker_ops, "wait_for_log", wait)
    result = docker_ops.graceful_stop(stopping.settings, stopping.container, 120)
    assert stopping.sent == [(27015, SECRET, "/server-save")]
    assert stopping.log == [("rcon", "/server-save"), ("waited",), ("docker stop", 120)]
    assert waited == [("Saving finished", docker_ops.flush_timeout(120))]
    assert result.flushed is True and result.warnings == [] and result.stop_sent_via_rcon is False


def test_stop_without_the_save_line_is_a_warning(stopping, monkeypatch):
    monkeypatch.setattr(docker_ops, "wait_for_log", lambda *_a: False)
    result = docker_ops.graceful_stop(stopping.settings, stopping.container, 60)
    assert ("docker stop", 60) in stopping.log
    assert result.flushed is False and "did not report" in result.warnings[0]


# --- the secret in console output --------------------------------------------------------------------------------
XTRACE = (
    b"+ exec /opt/factorio/bin/x64/factorio --port 34197 --rcon-port 27015 --rcon-password "
    + SECRET.encode()
    + b" --start-server world\n"
    b"++ cat /factorio/config/rconpw\n"
    b'   0.000 Program arguments: "--rcon-password" <private>\n'
)


def test_redactor(settings):
    _write_secret(settings)
    c = _Container(labels={"io.mcpanel.game": "factorio"})
    redact = docker_ops.log_redactor(settings, c)
    out = redact(XTRACE.decode() + f"echo {SECRET}\n")
    assert SECRET not in out
    assert "--rcon-password ******** --start-server world" in out
    assert f"echo {docker_ops.REDACTED}" in out
    assert docker_ops.log_redactor(settings, _Container()) is None  # Minecraft: nothing to hide
    # without the file the pattern still hides the command-line argument
    (settings.instances_dir / str(IID) / "config" / "rconpw").unlink()
    assert SECRET not in docker_ops.log_redactor(settings, c)(XTRACE.decode())


def test_console_history_and_logs_are_redacted(settings, monkeypatch):
    _write_secret(settings)
    c = _Container(labels={"io.mcpanel.game": "factorio"}, logs=XTRACE)
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: c)
    monkeypatch.setattr(docker_ops, "require_container", lambda _iid: c)
    lines = docker_ops.console_history(IID, 100, settings=settings)
    assert lines and not any(SECRET in line for line in lines)
    assert SECRET not in docker_ops.logs(IID, 100, settings=settings)


def test_last_run_record_is_redacted(settings, monkeypatch):
    _write_secret(settings)
    c = _Container(labels={"io.mcpanel.game": "factorio"}, logs=XTRACE, running=False)
    c.started = "2026-11-10T20:00:00Z"
    assert docker_ops.save_last_run_record(settings, IID, c) is True
    record = lastrun.load(settings, IID)
    assert record is not None and record["game"] == "factorio"
    assert record["log"]["lines"] and not any(SECRET in line for line in record["log"]["lines"])
    assert lastrun.summary(settings, IID)["game"] == "factorio"


def test_minecraft_last_run_record_has_no_game(settings):
    c = _Container(logs=b"Done\n", running=False)
    docker_ops.save_last_run_record(settings, IID, c)
    assert "game" not in lastrun.load(settings, IID)


class _StreamContainer(_Container):
    def __init__(self, chunks, **kw):
        super().__init__(**kw)
        self.chunks = chunks
        self.calls = 0

    def reload(self):
        pass

    def logs(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            from .test_security_audit import _FakeLogStream

            return _FakeLogStream(self.chunks)
        self.running = False
        from .test_security_audit import _FakeLogStream

        return _FakeLogStream([])


def test_log_stream_is_redacted(api, auth, agent_settings, monkeypatch):
    from .test_security_audit import _collect_lines

    (agent_settings.instances_dir / str(IID)).mkdir(parents=True)
    _write_secret(agent_settings)
    c = _StreamContainer([XTRACE[:40], XTRACE[40:], b"done\n"], labels={"io.mcpanel.game": "factorio"})
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: c)
    monkeypatch.setattr(docker_ops, "previous_run_lines", lambda *_a: [])
    monkeypatch.setattr(docker_ops, "count_log_lines", lambda *_a: 0)
    lines = _collect_lines(api, auth, IID, until="done")
    assert any("--rcon-password ********" in line for line in lines)
    assert not any(SECRET in line for line in lines)


# --- backups: the profile's excludes ------------------------------------------------------------------------------
def test_backup_excludes_of_the_game(agent_settings, monkeypatch):
    source = agent_settings.instances_dir / str(IID)
    monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None: {"exists": True, "game": "factorio"})
    assert main._profile_backup_excludes(agent_settings, IID) == [str(source / "temp")]
    # a stopped server: the game of its last run
    monkeypatch.setattr(
        docker_ops, "status", lambda _iid, _s=None: {"exists": False, "last_run": {"game": "factorio"}}
    )
    assert main._profile_backup_excludes(agent_settings, IID) == [str(source / "temp")]
    monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None: {"exists": True})
    assert main._profile_backup_excludes(agent_settings, IID) == []  # Minecraft
    monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None: {"exists": False})
    assert main._profile_backup_excludes(agent_settings, IID) == []

    def broken(*_a):
        raise RuntimeError("docker down")

    monkeypatch.setattr(docker_ops, "status", broken)
    assert main._profile_backup_excludes(agent_settings, IID) == []


def test_backup_request_passes_the_merged_excludes(api, auth, agent_settings, monkeypatch):
    source = agent_settings.instances_dir / str(IID)
    source.mkdir(parents=True)
    seen: dict = {}

    class FakeRestic:
        def __init__(self, *_a):
            pass

        def backup(self, src, tags, excludes, progress):
            seen["excludes"] = list(excludes)
            return {"snapshot_id": "abc"}

    monkeypatch.setattr(main, "Restic", FakeRestic)
    monkeypatch.setattr(
        docker_ops, "status", lambda _iid, _s=None: {"exists": False, "last_run": {"game": "factorio"}}
    )
    r = api.post(
        f"/v1/instances/{IID}/backups", json={"backup_id": "b1", "excludes": ["*.tmp"]}, headers=auth
    )
    assert r.status_code == 200
    op_id = r.json()["operation"]["id"]
    for _ in range(200):
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.01)
    assert op["status"] == "succeeded", op
    assert seen["excludes"] == ["*.tmp", str(source / "temp")]


# --- GET /v1/info ---------------------------------------------------------------------------------------------------
def test_info_describes_factorio(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    monkeypatch.setattr(main.storage, "capabilities", lambda _settings: {})
    monkeypatch.setattr(main, "restic_available", lambda _settings: None)
    games = api.get("/v1/info", headers=auth).json()["games"]
    assert games["factorio"] == {
        "variants": [],
        "images": ["factoriotools/factorio", "docker.io/factoriotools/factorio"],
        "control": "rcon",
        "ports": [[34197, "udp"]],
        "never_publish": [[27015, None]],
        "health": "log",
        "install_retry_exits": [],
    }
    monkeypatch.setenv("AGENT_GAME_FACTORIO_IMAGES", "registry.local/factorio")
    assert api.get("/v1/info", headers=auth).json()["games"]["factorio"]["images"] == [
        "registry.local/factorio"
    ]


def test_profile_mappings_are_read_only():
    assert isinstance(factorio.PROFILE.env_values, MappingProxyType)
    assert isinstance(factorio.PROFILE.control_options, MappingProxyType)
    assert Path(factorio.SECRET_FILE).parts == ("config", "rconpw")
    assert factorio.PROFILE.mounts == (Mount(".", "/factorio"),)
    assert RconStep("/server-save", 120, "Saving the map (/server-save)") in factorio.COMMANDS.save
