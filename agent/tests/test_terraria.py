"""Terraria on the agent: the three runtime profiles (vanilla, tModLoader, TShock), the console
primitives (exec of a fixed argv, stdin through the attach API) with their sanitising, and the
``console`` and ``tshock-rest`` drivers against fakes (a fake Docker API, a real local HTTP server
speaking TShock's REST answers)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from mcagent import docker_ops, drivers, main, profiles
from mcagent.docker_ops import AgentError, build_create_kwargs, validate_image
from mcagent.drivers import ConsoleDriver, TShockDriver, console_io
from mcagent.drivers.console import ConsoleChannel
from mcagent.drivers.tshock import TShockChannel, plain
from mcagent.profiles import terraria
from mcagent.schemas import InstanceSpec

IID = uuid.UUID("0123abcd-0000-4000-8000-00000007e77a")
TOKEN = "a3f1" * 12  # the panel's REST token in config.json


@pytest.fixture
def settings(agent_settings):
    agent_settings.runtime_uid, agent_settings.runtime_gid = 99, 100
    (agent_settings.instances_dir / str(IID)).mkdir(parents=True)
    return agent_settings


def tr_spec(variant: str = "vanilla", **over) -> InstanceSpec:
    image = {
        "vanilla": "passivelemon/terraria-docker:terraria-1.4.5.8",
        "tmodloader": "passivelemon/terraria-docker:tmodloader-2026.09.3.1",
        "tshock": "ryshe/terraria:tshock-1.4.5.8-6.2.1",
    }[variant]
    values = {
        "name": "Terra",
        "image": image,
        "memory_limit_mb": 1536,
        "rcon_password": "x" * 32,
        "game": "terraria",
        "variant": variant,
        "ports": [{"container_port": 7777, "host_port": 7777, "protocol": "tcp"}],
        "env": {"MODPACK": "mcpanel"} if variant == "tmodloader" else {},
        "stop_timeout": 60,
    }
    values.update(over)
    return InstanceSpec(**values)


# --- profiles ----------------------------------------------------------------------------------------------
def test_one_profile_per_variant():
    assert profiles.get("terraria", "vanilla") is terraria.VANILLA
    assert profiles.get("terraria", "tmodloader") is terraria.TMODLOADER
    assert profiles.get("terraria", "tshock") is terraria.TSHOCK
    assert profiles.get("terraria") is None  # a Terraria spec always names its variant
    with pytest.raises(AgentError) as exc:
        profiles.for_spec(tr_spec(variant="tshock").model_copy(update={"variant": "calamity"}))
    assert exc.value.code == "unknown_game"
    assert isinstance(drivers.for_profile(terraria.VANILLA), ConsoleDriver)
    assert isinstance(drivers.for_profile(terraria.TSHOCK), TShockDriver)


@pytest.mark.parametrize(
    ("profile", "good", "bad"),
    [
        (
            terraria.VANILLA,
            ["passivelemon/terraria-docker:terraria-1.4.5.8", "docker.io/passivelemon/terraria-docker:terraria-1.4.4.9"],
            ["passivelemon/terraria-docker:latest", "passivelemon/terraria-docker:terraria-latest",
             "passivelemon/terraria-docker:tmodloader-2026.09.3.1", "ryshe/terraria:terraria-1.4.5.8"],
        ),
        (
            terraria.TMODLOADER,
            ["passivelemon/terraria-docker:tmodloader-2026.09.3.1"],
            ["passivelemon/terraria-docker:tmodloader-latest", "passivelemon/terraria-docker:tmodloader-latest-pre",
             "passivelemon/terraria-docker:terraria-1.4.5.8"],
        ),
        (
            terraria.TSHOCK,
            ["ryshe/terraria:tshock-1.4.5.8-6.2.1", "docker.io/ryshe/terraria:tshock-1.4.4.9-5.2.4"],
            ["ryshe/terraria:latest", "ryshe/terraria:tshock-latest", "passivelemon/terraria-docker:tshock-1.4.5.8-6.2.1"],
        ),
    ],
)  # fmt: skip
def test_exact_tags_of_each_variant(settings, profile, good, bad):
    for image in good:
        assert validate_image(settings, image, profile) == image
    for image in bad:
        with pytest.raises(AgentError):
            validate_image(settings, image, profile)


def test_images_per_variant_from_the_environment(settings, monkeypatch):
    monkeypatch.setenv("AGENT_GAME_TERRARIA_IMAGES", "mirror.local/terraria")
    monkeypatch.setenv("AGENT_GAME_TERRARIA_TSHOCK_IMAGES", "mirror.local/tshock")
    assert profiles.allowed_images(settings, terraria.VANILLA) == ["mirror.local/terraria"]
    assert profiles.allowed_images(settings, terraria.TSHOCK) == ["mirror.local/tshock"]


@pytest.mark.parametrize(
    ("uid", "gid", "expected"), [(99, 100, (99, 99)), (1000, 1000, (1000, 1000)), (100, 100, (100, 4999))]
)
def test_puid_pgid_avoid_a_group_the_image_has(settings, uid, gid, expected):
    settings.runtime_uid, settings.runtime_gid = uid, gid
    assert terraria.terraria_ids(settings) == expected
    env = build_create_kwargs(settings, IID, tr_spec(), "h")["environment"]
    assert (env["PUID"], env["PGID"], env["SERVERCONFIG"]) == (str(expected[0]), str(expected[1]), "1")


@pytest.mark.parametrize(
    "key", ["WORLDNAME", "PASSWORD", "MOTD", "AUTOCREATE", "SEED", "BANLIST", "MODPACK", "JAVA_OPTS"]
)
def test_vanilla_environment_is_an_allow_list(settings, key):
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, tr_spec(env={key: "x"}), "h")
    assert exc.value.code == "env_not_allowed"


def test_tmodloader_modpack_value_is_checked(settings):
    build_create_kwargs(
        settings, IID, tr_spec("tmodloader", env={"MODPACK": "mcpanel", "TZ": "Europe/Berlin"}), "h"
    )
    for bad in ("../x", "a b", "pack;rm"):
        with pytest.raises(AgentError):
            build_create_kwargs(settings, IID, tr_spec("tmodloader", env={"MODPACK": bad}), "h")


@pytest.mark.parametrize("key", ["WORLD_FILENAME", "CONFIGPATH", "LOGPATH", "DOTNET_ROOT"])
def test_tshock_environment_is_an_allow_list(settings, key):
    with pytest.raises(AgentError):
        build_create_kwargs(settings, IID, tr_spec("tshock", env={key: "/x"}), "h")
    # what the agent injects always wins over a spec's value
    env = build_create_kwargs(settings, IID, tr_spec("tshock", env={"HOME": "/root"}), "h")["environment"]
    assert env["HOME"] == "/tmp"


def test_vanilla_container(settings):
    kwargs = build_create_kwargs(settings, IID, tr_spec(), "h")
    host = str(settings.host_instances_dir / str(IID))
    assert "user" not in kwargs  # the image starts as root and drops to PUID/PGID
    assert kwargs["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"]
    assert kwargs["cap_drop"] == ["ALL"] and kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["volumes"] == {host: {"bind": "/opt/terraria/config", "mode": "rw"}}
    assert kwargs["healthcheck"] == {"test": ["NONE"]}
    assert kwargs["stop_signal"] == "SIGTERM" and kwargs["stdin_open"] is False and kwargs["tty"] is False
    assert (
        kwargs["labels"]["io.mcpanel.game"] == "terraria"
        and kwargs["labels"]["io.mcpanel.variant"] == "vanilla"
    )
    assert kwargs["ports"] == {"7777/tcp": [("0.0.0.0", 7777)]}
    assert not {"entrypoint", "command", "working_dir"} & set(kwargs)


def test_tshock_container_runs_unprivileged_with_its_own_process(settings):
    kwargs = build_create_kwargs(
        settings,
        IID,
        tr_spec("tshock"),
        "h",
        image_volumes=[
            "/config",
            "/root/.local/share/Terraria/Worlds",
            "/tshock/ServerPlugins",
            "/tshock/logs",
        ],
    )
    host = str(settings.host_instances_dir / str(IID))
    assert kwargs["user"] == "99:100" and kwargs["cap_add"] == []
    assert kwargs["entrypoint"] == ["/tshock/TShock.Server"]
    assert kwargs["command"] == [
        "-configpath", "/config", "-logpath", "/config/logs", "-additionalplugins", "/plugins",
        "-config", "/config/serverconfig.txt",
    ]  # fmt: skip
    assert kwargs["working_dir"] == "/tmp"
    assert (
        kwargs["environment"]["HOME"] == "/tmp"
        and kwargs["environment"]["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] == "/tmp"
    )
    assert kwargs["volumes"] == {host: {"bind": "/config", "mode": "rw"}}
    assert set(kwargs["tmpfs"]) == {
        "/root/.local/share/Terraria/Worlds",
        "/tshock/ServerPlugins",
        "/tshock/logs",
    }
    assert kwargs["stdin_open"] is True and kwargs["tty"] is False and kwargs["stop_signal"] == "SIGINT"


@pytest.mark.parametrize("port", [7878, 25575, 7777])
def test_only_the_game_port_is_published(settings, port):
    spec = tr_spec(
        "tshock",
        ports=[{"container_port": port, "host_port": 28901, "protocol": "tcp" if port != 7777 else "udp"}],
    )
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, spec, "h")
    assert exc.value.code == "port_not_allowed"


def test_create_container_passes_the_profile_process(settings, monkeypatch):
    created: dict = {}

    class _Client:
        images = SimpleNamespace(
            get=lambda _n: SimpleNamespace(attrs={"Config": {"Volumes": {"/config": {}, "/tshock/logs": {}}}})
        )
        containers = SimpleNamespace(get=lambda cid: SimpleNamespace(id=cid))

        def __init__(self):
            self.api = SimpleNamespace(api_version="1.47", create_container=self._create)

        def _create(self, **kwargs):
            created.update(kwargs)
            return {"Id": "new"}

    monkeypatch.setattr(docker_ops, "client", lambda: _Client())
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    docker_ops.create_container(settings, IID, tr_spec("tshock"), "h")
    assert created["entrypoint"] == ["/tshock/TShock.Server"]
    assert created["command"][:2] == ["-configpath", "/config"]
    assert created["working_dir"] == "/tmp" and created["user"] == "99:100"


def test_register_refuses_a_bad_process():
    import dataclasses

    for change in (
        {"entrypoint": ("TShock.Server",)},
        {"entrypoint": ["/x"]},
        {"working_dir": "relative"},
        {"working_dir": "/a/../b"},
    ):
        with pytest.raises(ValueError):
            profiles.register(dataclasses.replace(terraria.TSHOCK, game="tshock-bad", **change))


# --- console line sanitising ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "say hello",
        "kick Some Player",
        "say a;b c",
        "say Grüße 你好",
        "say #{pane_id} $(id) `id`",
        "playing",
        "save",
        "exit",
    ],
)
def test_lines_that_are_typed_literally(line):
    assert console_io.sanitise_line(f"  {line} ", guard="tmux") == line


@pytest.mark.parametrize(
    "line",
    [
        "", "   ", "say a\nexit", "say a\rb", "say \x1b[31m", "say \x00", "say \u200b", "say \u2028x",
        "say \ud800", "x" * 257,
    ],
)  # fmt: skip
def test_lines_that_are_refused(line):
    with pytest.raises(AgentError) as exc:
        console_io.sanitise_line(line)
    assert exc.value.code == "console_line_refused" and exc.value.status == 400


@pytest.mark.parametrize(
    "line",
    ["C-c", "c-d", "M-x", "^C", "S-Tab", "Escape", "enter", "Up", "F1", "KPEnter", "0x03", "MouseDown1Pane",
     "-R", "-t other", "say done;", "x;"],
)  # fmt: skip
def test_tmux_guard_refuses_what_tmux_would_not_type(line):
    with pytest.raises(AgentError):
        console_io.sanitise_line(line, guard="tmux")
    if not line.startswith("-") and not line.endswith(";"):
        assert console_io.sanitise_line(line) == line  # without the guard (stdin) it is just text


def test_exec_template_rules():
    assert console_io.validate_exec_template(["inject", "{line}"]) == ("inject", "{line}")
    for bad in (
        [],
        ["{line}"],
        ["inject"],
        ["inject", "{line}", "{line}"],
        ["sh", "-c", "echo {line}"],
        ["-x", "{line}"],
    ):
        with pytest.raises(ValueError):
            console_io.validate_exec_template(bad)
    assert console_io.exec_argv(("inject", "{line}"), "say $(rm -rf /)") == ["inject", "say $(rm -rf /)"]


# --- the exec and attach primitives ----------------------------------------------------------------------------
class FakeApi:
    def __init__(self, exit_code=0, output=b""):
        self.exit_code = exit_code
        self.output = output
        self.created: list[dict] = []
        self.sent: list[bytes] = []
        self.closed = 0

    def exec_create(self, container, cmd, **kw):
        self.created.append({"container": container, "cmd": cmd, **kw})
        return {"Id": "e1"}

    def exec_start(self, exec_id, **kw):
        return self.output

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.exit_code}

    def attach_socket(self, container, params=None):
        assert params == {"stdin": 1, "stdout": 0, "stderr": 0, "stream": 1}
        api = self

        class _Sock:
            def sendall(self, data):
                api.sent.append(data)

            def close(self):
                api.closed += 1

        return _Sock()


class FakeContainer:
    def __init__(self, *, env=None, stdin=False, logs=b"", labels=None, variant="vanilla", ip="172.30.0.7"):
        self.id = "c-" + uuid.uuid4().hex[:6]
        self.name = docker_ops.container_name(IID)
        self.labels = {
            "io.mcpanel.managed": "true",
            "io.mcpanel.instance": str(IID),
            "io.mcpanel.game": "terraria",
            "io.mcpanel.variant": variant,
            **(labels or {}),
        }
        self.env = env if env is not None else ["PUID=99", "PGID=99", "SERVERCONFIG=1"]
        self.stdin = stdin
        self.log_text = logs
        self.ip = ip
        self.status = "running"

    @property
    def attrs(self):
        return {
            "Config": {"Env": self.env, "OpenStdin": self.stdin},
            "HostConfig": {"NetworkMode": "mcpanel_instances"},
            "State": {"Running": True, "Status": "running", "StartedAt": "2026-11-10T20:00:00Z"},
            "NetworkSettings": {"Networks": {"n": {"IPAddress": self.ip}}, "Ports": {}},
        }

    def logs(self, **kwargs):
        text = self.log_text() if callable(self.log_text) else self.log_text
        return text

    def reload(self):
        pass


@pytest.fixture
def fake_api(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(console_io, "_docker_api", lambda: api)
    return api


def test_exec_runs_the_fixed_argv_as_the_server_user(fake_api):
    driver = drivers.for_profile(terraria.VANILLA)
    c = FakeContainer()
    assert driver.write(c, " say hi; there ") == "say hi; there"
    (call,) = fake_api.created
    assert call["cmd"] == ["inject", "say hi; there"]
    assert (
        call["user"] == "99:99"
        and call["privileged"] is False
        and call["tty"] is False
        and call["stdin"] is False
    )


def test_exec_failure_is_reported(fake_api):
    fake_api.exit_code, fake_api.output = 1, b"unknown command: Enter\n"
    with pytest.raises(AgentError) as exc:
        drivers.for_profile(terraria.VANILLA).write(FakeContainer(), "say x")
    assert exc.value.code == "console_failed" and "unknown command" in str(exc.value)


def test_exec_needs_the_server_user(fake_api):
    with pytest.raises(AgentError) as exc:
        drivers.for_profile(terraria.VANILLA).write(FakeContainer(env=["PATH=/bin"]), "say x")
    assert exc.value.code == "console_unavailable" and fake_api.created == []


def test_refused_line_never_reaches_docker(fake_api):
    with pytest.raises(AgentError):
        drivers.for_profile(terraria.VANILLA).write(FakeContainer(), "C-c")
    assert fake_api.created == []


def test_stdin_write(fake_api):
    console = ConsoleDriver(transport="stdin")
    console.write(FakeContainer(stdin=True), "say via stdin")
    assert fake_api.sent == [b"say via stdin\n"] and fake_api.closed >= 1
    with pytest.raises(AgentError) as exc:
        console.write(FakeContainer(stdin=False), "say x")
    assert exc.value.code == "console_unavailable"


def test_console_lock_serialises_one_server():
    with console_io.console_lock("srv"):
        with pytest.raises(AgentError) as exc:
            with console_io.console_lock("srv", wait=0.05):
                pass
        with console_io.console_lock("other", wait=0.05):
            pass
    assert exc.value.code == "console_busy"


def test_command_answers_what_the_server_printed(fake_api, settings, monkeypatch):
    monkeypatch.setattr(console_io, "OUTPUT_QUIET", 0.05)
    c = FakeContainer(logs=b": No players connected.\r\n: Saving world data: 3%\r\n")
    out = drivers.for_profile(terraria.VANILLA).command_sync(settings, c, "playing", 1.0)
    assert out == ": No players connected."
    assert fake_api.created[0]["cmd"] == ["inject", "playing"]


def test_output_lines_drop_progress_and_blanks():
    assert console_io.output_lines("a\r: Saving world data: 3%\r\n\r\nb\n") == ["a", "b"]


# --- saving --------------------------------------------------------------------------------------------------------
def test_vanilla_save_waits_for_its_log_line():
    driver = drivers.for_profile(terraria.VANILLA)
    sent, waits = [], []
    driver.save(lambda cmd, t: sent.append(cmd), lambda *_a: None, lambda p, s, t: waits.append(p) or True)
    assert sent == ["save"] and waits == [terraria.SAVE_DONE_LOG]
    with pytest.raises(AgentError) as exc:
        driver.save(lambda *_a: None, lambda *_a: None, lambda *_a: False)
    assert exc.value.code == "save_unconfirmed"


def test_tmodloader_save_waits_for_the_twld_file(settings, fake_api, monkeypatch):
    monkeypatch.setattr("mcagent.drivers.console.SAVE_FILE_SETTLE", 0.05)
    driver = drivers.for_profile(terraria.TMODLOADER)
    folder = settings.instances_dir / str(IID) / "Worlds"
    folder.mkdir()
    twld = folder / "world.twld"
    twld.write_bytes(b"old")
    os.utime(twld, (time.time() - 100, time.time() - 100))
    c = FakeContainer(env=["PUID=99", "PGID=99"], variant="tmodloader")
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    channel = driver.channel(settings, IID)
    assert isinstance(channel, ConsoleChannel)

    def write_later():
        time.sleep(0.2)
        twld.write_bytes(b"new")

    threading.Thread(target=write_later).start()
    reports: list[str] = []
    driver.save(
        channel,
        lambda _i, m: reports.append(m),
        lambda *_a: pytest.fail("tModLoader prints no line at the end of a save"),
    )
    assert (
        "waits until Worlds/*.twld is written" in reports[0]
        and reports[1] == "The world was written (Worlds/*.twld)"
    )
    assert fake_api.created[0]["cmd"] == ["inject", "save"]
    monkeypatch.setattr(driver, "save_timeout", 0.3)
    with pytest.raises(AgentError) as exc:
        driver.save(channel, lambda *_a: None)
    assert exc.value.code == "save_unconfirmed"


def test_backup_save_goes_through_the_console(agent_settings, fake_api, monkeypatch):
    c = FakeContainer(logs=b": Backing up world file\n")
    monkeypatch.setattr(
        docker_ops, "status", lambda _iid: {"running": True, "game": "terraria", "variant": "vanilla"}
    )
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    monkeypatch.setattr(main, "_rcon", lambda *_a: pytest.fail("a console server has no RCON"))
    monkeypatch.setattr(main, "SAVE_SETTLE_SECONDS", 0)
    op = SimpleNamespace(update=lambda *a, **k: None)
    with main._world_saving_paused(agent_settings, IID, "require", op, "backup", 0.02) as state:
        pass
    assert state.consistent and fake_api.created[0]["cmd"] == ["inject", "save"]


# --- TShock REST -----------------------------------------------------------------------------------------------------
class _Rest(BaseHTTPRequestHandler):
    calls: list = []
    users: list = []
    up = True

    def log_message(self, *_a):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        type(self).calls.append((url.path, q))
        body: dict
        if url.path == "/v2/server/status":
            body = {"status": "200", "playercount": 1, "maxplayers": 8, "serverversion": "v1.4.5.8",
                    "tshockversion": "6.2.1.0", "world": "w", "players": [{"nickname": "Ann"}]}  # fmt: skip
        elif q.get("token") != TOKEN:
            body = {"status": "401", "error": "Not authorized. The specified API endpoint requires a token."}
        elif url.path == "/v3/server/rawcmd":
            body = {"status": "200", "response": [f"ran [c/AAAAFF:{q['cmd']}]"]}
        elif url.path == "/v2/world/save":
            body = {"status": "200", "response": "World saved"}
        elif url.path == "/v2/server/broadcast":
            body = {"status": "200", "response": "The message was broadcasted successfully"}
        elif url.path == "/v2/users/list":
            body = {
                "status": "200",
                "users": [{"name": n, "id": i, "group": "owner"} for i, n in enumerate(type(self).users)],
            }
        elif url.path == "/v2/users/create":
            type(self).users.append(q["user"])
            body = {"status": "200", "response": "User was successfully created"}
        elif url.path == "/v2/users/update":
            body = {"status": "200", "response": "Password updated"}
        else:
            body = {"status": "404", "error": "Specified API endpoint doesn't exist."}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def rest(settings, monkeypatch):
    _Rest.calls, _Rest.users = [], []
    server = HTTPServer(("127.0.0.1", 0), _Rest)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    folder = settings.instances_dir / str(IID)
    (folder / "config.json").write_text(
        json.dumps(
            {
                "Settings": {
                    "RestApiEnabled": True,
                    "ApplicationRestTokens": {
                        "short": {"Username": "mcpanel"},
                        TOKEN: {"Username": "mcpanel", "UserGroupName": "superadmin"},
                    },
                }
            }
        )
    )
    monkeypatch.setattr(TShockDriver, "address", lambda self, s, c: ("127.0.0.1", server.server_address[1]))
    try:
        yield SimpleNamespace(settings=settings, folder=folder, calls=_Rest.calls)
    finally:
        server.shutdown()
        server.server_close()


def test_tshock_token_comes_from_config_json(rest):
    driver = drivers.for_profile(terraria.TSHOCK)
    assert driver.secret(rest.settings, FakeContainer(variant="tshock")) == TOKEN
    (rest.folder / "config.json").write_text("{not json")
    assert driver.secret(rest.settings, FakeContainer(variant="tshock")) is None


def test_tshock_token_file_symlink_is_not_followed(rest, tmp_path):
    outside = tmp_path / "elsewhere.json"
    outside.write_text((rest.folder / "config.json").read_text())
    (rest.folder / "config.json").unlink()
    (rest.folder / "config.json").symlink_to(outside)
    assert drivers.for_profile(terraria.TSHOCK).secret(rest.settings, FakeContainer(variant="tshock")) is None


def test_tshock_command_through_rest(rest):
    driver = drivers.for_profile(terraria.TSHOCK)
    out = driver.command_sync(rest.settings, FakeContainer(variant="tshock"), "playing", 10)
    assert out == "ran /playing"
    assert rest.calls[-1] == ("/v3/server/rawcmd", {"cmd": "/playing", "token": TOKEN})
    assert plain("[c/AAAAFF:6.2.1.0] x") == "6.2.1.0 x"


def test_tshock_save_broadcast_status(rest, monkeypatch):
    driver = drivers.for_profile(terraria.TSHOCK)
    c = FakeContainer(variant="tshock")
    assert driver.save_now(rest.settings, c) == "rest"
    driver.broadcast(rest.settings, c, "Restart in\n5 minutes")
    assert ("/v2/server/broadcast", {"msg": "Restart in 5 minutes", "token": TOKEN}) in rest.calls
    status = driver.status(rest.settings, c)
    assert status["online"] == 1 and status["max"] == 8 and status["sample"] == ["Ann"]
    assert status["version"] == "TShock 6.2.1.0 (Terraria 1.4.5.8)"
    assert "token" not in rest.calls[-1][1]  # the status needs no token
    # the saving pause's channel: "save" is the synchronous REST save
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    channel = driver.channel(rest.settings, IID)
    assert isinstance(channel, TShockChannel)
    driver.save(channel, lambda *_a: None)
    assert rest.calls[-1][0] == "/v2/world/save"


def test_tshock_query_target_for_ping(rest):
    driver = drivers.for_profile(terraria.TSHOCK)
    target = driver.query_endpoint(rest.settings, FakeContainer(variant="tshock"))
    import asyncio

    host, _port = target
    assert host == "127.0.0.1"
    assert asyncio.run(target.query(5.0))["sample"] == ["Ann"]


def test_tshock_owner_account(rest):
    driver = drivers.for_profile(terraria.TSHOCK)
    c = FakeContainer(variant="tshock")
    first = driver.game_action(
        rest.settings, c, "owner_account", {"user": "owner", "password": "Pw-123456"}, 10
    )
    assert first == {"account": "owner", "group": "owner", "created": True}
    again = driver.game_action(
        rest.settings, c, "owner_account", {"user": "owner", "password": "Pw-654321"}, 10
    )
    assert again["created"] is False and rest.calls[-1][0] == "/v2/users/update"
    for params in ({"user": "a b", "password": "Pw-123456"}, {"user": "owner", "password": "abc"}):
        with pytest.raises(AgentError):
            driver.game_action(rest.settings, c, "owner_account", params, 10)
    with pytest.raises(AgentError) as exc:
        driver.game_action(rest.settings, c, "claim", {}, 10)
    assert exc.value.code == "action_unsupported"


def test_tshock_falls_back_to_the_console(settings, fake_api, monkeypatch):
    monkeypatch.setattr(console_io, "OUTPUT_QUIET", 0.05)
    driver = drivers.for_profile(terraria.TSHOCK)  # no config.json: no token
    c = FakeContainer(
        variant="tshock",
        stdin=True,
        logs=b"Server executed: /playing.\nThere are currently no players online.\n",
    )
    out = driver.command_sync(settings, c, "/playing", 1.0)
    assert fake_api.sent == [b"playing\n"] and "no players online" in out
    monkeypatch.setattr(
        docker_ops, "wait_for_log", lambda c, pattern, since, timeout: pattern == "Backing up world file"
    )
    assert driver.save_now(settings, c, 1.0) == "console"


def test_tshock_errors_never_show_the_token(rest, monkeypatch):
    driver = drivers.for_profile(terraria.TSHOCK)
    c = FakeContainer(variant="tshock")
    with pytest.raises(AgentError) as exc:
        driver.call(rest.settings, c, "/v9/nothing")
    assert exc.value.code == "rest_failed" and TOKEN not in str(exc.value)
    monkeypatch.setattr(TShockDriver, "address", lambda self, s, c: ("127.0.0.1", 9))  # nothing listens
    with pytest.raises(AgentError) as exc:
        driver.call(rest.settings, c, "/v2/world/save")
    assert exc.value.code == "rest_unavailable" and TOKEN not in str(exc.value)


def test_tshock_setup_code_and_token_are_redacted(rest):
    c = FakeContainer(variant="tshock")
    redact = docker_ops.log_redactor(rest.settings, c)
    out = redact(f": To setup the server, join the game and type /setup 1234567\ntoken {TOKEN}\n")
    assert "1234567" not in out and TOKEN not in out and "/setup ********" in out


# --- /v1/info --------------------------------------------------------------------------------------------------------
def test_info_lists_the_variants(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    monkeypatch.setattr(main.storage, "capabilities", lambda _settings: {})
    monkeypatch.setattr(main, "restic_available", lambda _settings: None)
    entry = api.get("/v1/info", headers=auth).json()["games"]["terraria"]
    assert entry["variants"] == ["vanilla", "tmodloader", "tshock"]
    assert "ryshe/terraria" in entry["images"] and "passivelemon/terraria-docker" in entry["images"]
    assert [7878, None] in entry["never_publish"]
    assert entry["variant_profiles"]["tshock"]["control"] == "tshock-rest"
    assert entry["variant_profiles"]["vanilla"]["clean_exit_codes"] == [0, 143]


def test_command_endpoint_refuses_a_bad_line(api, auth, monkeypatch, fake_api):
    c = FakeContainer()
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    r = api.post(f"/v1/instances/{IID}/command", headers=auth, json={"command": "C-c"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "console_line_refused"
    assert fake_api.created == []
