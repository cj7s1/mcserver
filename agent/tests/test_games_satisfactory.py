"""Satisfactory on the agent: the profile, the generic ``http-api`` driver base, the
``satisfactory-api`` driver against a fake HTTPS/UDP server built from the spike's recorded answers
(tests/sf_fake.py), the readiness kinds ``udp-query`` and ``driver``, the ``game/{action}``
endpoint, the status query of ``/ping``, the saving channel, the publish guard and the restore that
keeps the re-downloadable game files.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import uuid
from types import MappingProxyType, SimpleNamespace

import pytest

from mcagent import docker_ops, drivers, main, profiles
from mcagent.docker_ops import AgentError, build_create_kwargs, validate_image
from mcagent.drivers import GameApiError, HttpApiDriver, QueryTarget, SatisfactoryDriver, http_api
from mcagent.drivers import satisfactory as sfd
from mcagent.profiles import Health, satisfactory
from mcagent.schemas import InstanceSpec

from .sf_fake import FakeSatisfactory, level_of
from .test_games import _testgame

IID = uuid.UUID("0123abcd-0000-4000-8000-00000005a715")
IMAGE = "wolveix/satisfactory-server:v1.9.11"
ADMIN = "Admin-secret-0123456789"


@pytest.fixture
def settings(agent_settings):
    agent_settings.runtime_uid, agent_settings.runtime_gid = 99, 100
    (agent_settings.instances_dir / str(IID)).mkdir(parents=True)
    return agent_settings


def sf_spec(**over) -> InstanceSpec:
    values = {
        "name": "Ficsit",
        "image": IMAGE,
        "memory_limit_mb": 12288,
        "rcon_password": "x" * 32,
        "game": "satisfactory",
        "ports": [
            {"container_port": 7777, "host_port": 29010, "protocol": "tcp"},
            {"container_port": 7777, "host_port": 29010, "protocol": "udp"},
            {"container_port": 29011, "host_port": 29011, "protocol": "tcp"},
        ],
        "env": {"MAXPLAYERS": "8", "SERVERMESSAGINGPORT": "29011", "TZ": "UTC"},
        "stop_timeout": 120,
        "restart_policy": "on-failure",
    }
    values.update(over)
    return InstanceSpec(**values)


# --- the profile ------------------------------------------------------------------------------------
def test_satisfactory_profile_is_registered():
    profile = profiles.get("satisfactory")
    assert profile is satisfactory.PROFILE
    assert profile.control == "satisfactory-api" and profile.health.kind == "udp-query"
    assert profile.install_retry_exits == (5, 8)
    assert profile.default_backup_excludes == ("gamefiles",)
    assert isinstance(drivers.for_profile(profile), SatisfactoryDriver)


@pytest.mark.parametrize(
    "image", [IMAGE, "docker.io/wolveix/satisfactory-server:v1.9.11", "wolveix/satisfactory-server:v1.10.0"]
)
def test_exact_version_tags_only(settings, image):
    assert validate_image(settings, image, satisfactory.PROFILE) == image


@pytest.mark.parametrize(
    "image",
    [
        "wolveix/satisfactory-server:latest",
        "wolveix/satisfactory-server:v1",
        "robtme/satisfactory-server:v1.9.11",
    ],
)
def test_moving_tags_and_other_repositories_are_refused(settings, image):
    with pytest.raises(AgentError):
        validate_image(settings, image, satisfactory.PROFILE)


def test_container_runs_root_mode_with_four_capabilities(settings):
    kw = build_create_kwargs(settings, IID, sf_spec(), "h")
    assert kw["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"] and kw["cap_drop"] == ["ALL"]
    assert "user" not in kw  # root mode: init.sh drops to PUID/PGID with gosu
    assert kw["environment"]["PUID"] == "99" and kw["environment"]["PGID"] == "100"
    assert kw["environment"]["SERVERGAMEPORT"] == "7777"
    assert kw["environment"]["MAXPLAYERS"] == "8"
    assert list(kw["volumes"].values()) == [{"bind": "/config", "mode": "rw"}]
    assert kw["healthcheck"] == {"test": ["NONE"]}  # the agent's UDP query decides
    assert kw["stop_signal"] == "SIGINT" and kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["labels"]["io.mcpanel.game"] == "satisfactory"
    assert kw["ports"] == {
        "7777/tcp": [("0.0.0.0", 29010)],
        "7777/udp": [("0.0.0.0", 29010)],
        "29011/tcp": [("0.0.0.0", 29011)],
    }
    assert kw["restart_policy"] == {"Name": "on-failure", "MaximumRetryCount": 3}


def test_the_messaging_port_is_the_one_the_variable_names(settings):
    with pytest.raises(AgentError) as exc:  # 8888 published while the server listens on 29011
        build_create_kwargs(
            settings,
            IID,
            sf_spec(ports=[{"container_port": 8888, "host_port": 29011, "protocol": "tcp"}]),
            "h",
        )
    assert exc.value.code == "port_not_allowed"
    with pytest.raises(AgentError):  # without the variable no other port at all
        build_create_kwargs(
            settings,
            IID,
            sf_spec(env={}, ports=[{"container_port": 29011, "host_port": 29011, "protocol": "tcp"}]),
            "h",
        )
    with pytest.raises(AgentError):  # the game port's UDP twin only
        build_create_kwargs(
            settings,
            IID,
            sf_spec(ports=[{"container_port": 29011, "host_port": 29011, "protocol": "udp"}]),
            "h",
        )


def test_no_ports_before_the_claim_is_a_valid_container(settings):
    kw = build_create_kwargs(settings, IID, sf_spec(ports=[]), "h")
    assert kw["ports"] == {}


@pytest.mark.parametrize(
    "env",
    [
        {"STEAMBETA": "true"},
        {"STEAMAPPID": "1"},
        {"SKIPUPDATE": "true"},
        {"DEBUG": "true"},
        {"MULTIHOME": "::"},
        {"MAXPLAYERS": "0"},
        {"MAXPLAYERS": "8; rm"},
        {"SERVERMESSAGINGPORT": "88"},
        {"VMOVERRIDE": "true"},
        {"SOMETHING": "x"},
    ],
)
def test_environment_is_an_allow_list(settings, env):
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, sf_spec(env=env, ports=[]), "h")
    assert exc.value.code == "env_not_allowed"


def test_injected_variables_replace_what_a_spec_says(settings):
    env = build_create_kwargs(
        settings, IID, sf_spec(env={"SERVERGAMEPORT": "7778", "PUID": "0"}, ports=[]), "h"
    )["environment"]
    assert (env["SERVERGAMEPORT"], env["PUID"], env["PGID"]) == ("7777", "99", "100")


def test_profile_mappings_are_read_only():
    assert isinstance(satisfactory.PROFILE.env_values, MappingProxyType)
    assert isinstance(satisfactory.PROFILE.env_ports, MappingProxyType)
    assert isinstance(satisfactory.PROFILE.control_options, MappingProxyType)


def test_register_checks_the_new_probe_kinds(monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    for bad in (
        _testgame(health=Health("udp-query", port=7777, payload=b"x")),  # no pattern
        _testgame(health=Health("udp-query", port=7777, pattern="x")),  # no payload
        _testgame(health=Health("udp-query", payload=b"x", pattern="x")),  # no port
        _testgame(health=Health("udp-query", port=7777, payload=b"x", pattern="(")),
        _testgame(health=Health("driver")),  # the rcon driver has no readiness check
        _testgame(env_ports={"PORT_VAR": "tcp"}),  # a port variable without a value pattern
    ):
        with pytest.raises(ValueError):
            profiles.register(bad)
    profiles.register(_testgame(health=Health("udp-query", port=7777, payload=b"x", pattern="y")))


# --- the fake server, and containers pointing at it --------------------------------------------------------
@pytest.fixture(scope="session")
def cert_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("sf-cert")


@pytest.fixture
def fake(cert_dir):
    server = FakeSatisfactory(cert_dir)
    yield server
    server.close()


class Container:
    """A running container whose address is the fake's (127.0.0.1); ``stop`` like docker stop."""

    def __init__(self, port: int, *, running: bool = True) -> None:
        self.id = "sf-" + uuid.uuid4().hex[:8]
        self.name = docker_ops.container_name(IID)
        self.labels = {
            docker_ops.LABEL_MANAGED: "true",
            docker_ops.LABEL_INSTANCE: str(IID),
            "io.mcpanel.game": "satisfactory",
        }
        self.state = "running" if running else "exited"
        self.started = "2026-11-10T20:00:00.5Z"
        self.port = port
        self.log: list = []
        self.exit_code = 0

    @property
    def status(self) -> str:
        return self.state

    @property
    def attrs(self) -> dict:
        return {
            "Config": {"Image": IMAGE, "Env": []},
            "HostConfig": {"NetworkMode": "mcpanel_instances"},
            "State": {
                "Status": self.state,
                "Running": self.state == "running",
                "StartedAt": self.started,
                "ExitCode": self.exit_code,
            },
            "NetworkSettings": {"Networks": {"n": {"IPAddress": "127.0.0.1"}}, "Ports": {}},
        }

    def reload(self) -> None:
        return None

    def stop(self, timeout=None) -> None:
        self.log.append(("docker stop", timeout))
        if self.state == "running":
            self.exit_code = 130
        self.state = "exited"


def driver_for(fake) -> SatisfactoryDriver:
    return SatisfactoryDriver(
        port=fake.port, secret_file=satisfactory.TOKEN_FILE, claim_file=satisfactory.CLAIM_FILE
    )


def write_claim_file(settings, content: bytes = b"claimed settings") -> None:
    """What the game writes when it is claimed: saved/ServerSettings.7777.sav."""
    saved = settings.instances_dir / str(IID) / "saved"
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "ServerSettings.7777.sav").write_bytes(content)


def write_token(settings, token: str) -> None:
    folder = settings.instances_dir / str(IID) / ".mcpanel"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "api-token").write_text(token + "\n")


def claimed(settings, fake, drv, c) -> str:
    token = drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)[
        "token"
    ]
    write_token(settings, token)
    return token


# --- the generic http-api base ------------------------------------------------------------------------------
def test_internal_tls_context_is_not_the_default():
    assert http_api._TLS.verify_mode.name == "CERT_NONE" and http_api._TLS.check_hostname is False
    import ssl

    assert ssl.create_default_context().verify_mode.name == "CERT_REQUIRED"  # nothing global changed


def test_certificate_is_pinned_per_run(settings, fake, monkeypatch):
    monkeypatch.setattr(http_api, "_pins", {})
    drv, c = driver_for(fake), Container(fake.port)
    assert drv.health(settings, c) == "healthy"
    key = (c.id, c.started)
    assert http_api._pins[key]
    http_api._pins[key] = "0" * 64  # another certificate than the one this run started with
    with pytest.raises(GameApiError) as exc:
        drv.health(settings, c)
    assert exc.value.code == "api_tls_changed" and exc.value.status == 409
    c.started = "2026-11-10T21:00:00.5Z"  # a new run (the game may regenerate it at a start)
    assert drv.health(settings, c) == "healthy"


def test_nothing_listening_is_api_unavailable(settings):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    drv = SatisfactoryDriver(port=port, secret_file=satisfactory.TOKEN_FILE)
    with pytest.raises(GameApiError) as exc:
        drv.health(settings, Container(port))
    assert exc.value.code == "api_unavailable" and exc.value.status == 409
    assert drv.ready(settings, Container(port)) is False


def test_a_server_that_never_answers_is_bounded(settings):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        started = time.monotonic()
        with pytest.raises(GameApiError) as exc:
            http_api.http_request("127.0.0.1", port, "POST", "/api/v1", body=b"{}", timeout=0.6)
        assert exc.value.code in ("api_timeout", "api_unavailable")
        assert time.monotonic() - started < 3
    finally:
        listener.close()


def test_answers_are_size_capped(settings, fake, monkeypatch):
    monkeypatch.setattr(http_api, "API_MAX_RESPONSE", 10)
    with pytest.raises(GameApiError) as exc:
        http_api.http_request(
            "127.0.0.1", fake.port, "POST", "/api/v1", body=b'{"function":"HealthCheck"}', max_response=10
        )
    assert "more than 10 bytes" in str(exc.value)


def test_the_published_mode_uses_the_published_port(settings):
    settings.rcon_mode = "published"
    c = Container(1)
    drv = SatisfactoryDriver(port=7777, secret_file=satisfactory.TOKEN_FILE)
    with pytest.raises(GameApiError) as exc:
        drv.address(settings, c)
    assert exc.value.code == "api_unavailable" and "not published" in str(exc.value)


def test_http_api_driver_options_are_checked():
    assert HttpApiDriver.from_options({"port": 8080, "scheme": "http", "secret_file": "a/b"}).scheme == "http"
    for bad in (
        {"port": 0},
        {"port": 1, "scheme": "ftp"},
        {"port": 1, "secret_file": "../x"},
        {"port": 1, "secret_file": "/abs"},
    ):
        with pytest.raises(ValueError):
            HttpApiDriver.from_options(bad)


def test_token_file_is_read_without_following_symlinks(settings, tmp_path):
    drv = SatisfactoryDriver(port=7777, secret_file=satisfactory.TOKEN_FILE)
    assert drv.token_for(settings, IID) is None
    write_token(settings, "tok-123")
    assert drv.token_for(settings, IID) == "tok-123"
    target = tmp_path / "elsewhere"
    target.write_text("stolen")
    link = settings.instances_dir / str(IID) / ".mcpanel" / "api-token"
    link.unlink()
    link.symlink_to(target)
    assert drv.token_for(settings, IID) is None


# --- errors come back as HTTP 200 + errorCode -------------------------------------------------------------
def test_error_codes_in_a_200_answer_are_errors(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    with pytest.raises(GameApiError) as exc:  # HTTP 200, errorCode server_claimed
        drv.call(settings, c, "ClaimServer", {"ServerName": "x", "AdminPassword": "y" * 8}, token=None)
    assert exc.value.api_code == "server_claimed" and exc.value.http_status == 200 and exc.value.status == 409
    with pytest.raises(GameApiError) as exc:  # 403 insufficient_scope
        drv.call(settings, c, "QueryServerState", {})
    assert exc.value.code == "api_forbidden"
    with pytest.raises(GameApiError) as exc:  # 401 invalid token
        drv.call(settings, c, "QueryServerState", {}, token="bm9wZQ.00")
    assert exc.value.code == "api_unauthorized"


# --- claiming: the flow and every failure point ---------------------------------------------------------------
def test_claim_of_a_new_server(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    result = drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)
    assert result["claimed"] == "new" and level_of(result["token"]) == "APIToken"
    assert fake.claimed and fake.server_name == "Ficsit" and fake.admin_password == ADMIN
    assert fake.functions() == [
        "HealthCheck",
        "PasswordlessLogin",
        "ClaimServer",
        "RunCommand",
        "QueryServerState",
    ]
    # the claim used the InitialAdmin token, the token generation the Administrator token
    assert [lvl for f, _d, lvl in fake.requests if f in ("ClaimServer", "RunCommand")] == [
        "InitialAdmin",
        "Administrator",
    ]


def test_claim_again_logs_in_with_the_control_secret(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    first = claimed(settings, fake, drv, c)
    again = drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)
    assert again["claimed"] == "existing" and again["token"] != first
    assert "PasswordLogin" in fake.functions()


def test_claimed_with_another_password(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    fake.claimed, fake.admin_password = True, "someone-else-entirely"
    with pytest.raises(AgentError) as exc:
        drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)
    assert exc.value.code == "claimed_elsewhere" and exc.value.status == 409


def test_claim_waits_for_the_api_and_gives_up(settings, fake, monkeypatch):
    drv, c = driver_for(fake), Container(fake.port)
    fake.api_down = True
    started = time.monotonic()
    with pytest.raises(GameApiError) as exc:
        drv.wait_ready(settings, c, timeout=1.5)
    assert exc.value.code == "api_unavailable" and time.monotonic() - started < 5
    assert not fake.claimed

    def up_later():
        time.sleep(1.0)
        fake.api_down = False

    threading.Thread(target=up_later, daemon=True).start()
    drv.wait_ready(settings, c, timeout=10)


@pytest.mark.parametrize(
    ("function", "answer", "code"),
    [
        ("PasswordlessLogin", (200, {"errorCode": "internal", "errorMessage": "boom"}), "api_error"),
        ("ClaimServer", (500, {"errorCode": "claim_failed", "errorMessage": "no"}), "api_error"),
        ("RunCommand", (403, {"errorCode": "insufficient_scope"}), "api_forbidden"),
        ("QueryServerState", (401, {"errorCode": "invalid_token"}), "api_unauthorized"),
    ],
)
def test_claim_failure_points(settings, fake, function, answer, code):
    drv, c = driver_for(fake), Container(fake.port)
    fake.fail[function] = answer
    with pytest.raises(GameApiError) as exc:
        drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)
    assert exc.value.code == code


def test_claim_without_a_token_in_the_command_output(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    fake.no_token_line = True
    with pytest.raises(GameApiError) as exc:
        drv.game_action(settings, c, "claim", {"server_name": "Ficsit", "admin_password": ADMIN}, 30)
    assert "did not print an API token" in str(exc.value)


@pytest.mark.parametrize(
    "params",
    [
        {"server_name": "Ficsit", "admin_password": "short"},
        {"server_name": "", "admin_password": ADMIN},
        {"server_name": "a\nb", "admin_password": ADMIN},
        {"admin_password": ADMIN},
    ],
)
def test_claim_parameters_are_checked(settings, fake, params):
    with pytest.raises(AgentError) as exc:
        driver_for(fake).game_action(settings, Container(fake.port), "claim", params, 30)
    assert exc.value.code == "invalid_request"
    assert fake.requests == []


# --- client password, new game, saves, sessions, options --------------------------------------------------------
def test_client_password_is_set_and_checked(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    assert drv.game_action(settings, c, "client_password", {"password": "join-me"}, 30) == {"protected": True}
    assert fake.client_password == "join-me"
    assert drv.game_action(settings, c, "client_password", {"password": ""}, 30) == {"protected": False}
    fake.fail["SetClientPassword"] = (204, None)  # pretends, changes nothing
    fake.client_password = ""
    with pytest.raises(GameApiError):
        drv.game_action(settings, c, "client_password", {"password": "x"}, 30)


def test_actions_need_the_token(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    with pytest.raises(GameApiError) as exc:
        drv.game_action(settings, c, "client_password", {"password": "x"}, 30)
    assert exc.value.code == "api_unauthorized" and fake.requests == []


def test_new_game_sends_game_mode_settings_and_waits(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    result = drv.game_action(settings, c, "new_game", {"session_name": "Ficsit world"}, 60)
    assert result["session"] == "Ficsit world" and result["state"]["activeSessionName"] == "Ficsit world"
    create = next(d for f, d, _l in fake.requests if f == "CreateNewGame")["NewGameData"]
    assert create["GameModeSettings"] == {} and create["bSkipOnboarding"] is True
    with pytest.raises(AgentError):
        drv.game_action(settings, c, "new_game", {"session_name": "../x"}, 60)


def test_save_names_the_save_after_the_session(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    assert drv.game_action(settings, c, "save", {}, 30) == {"save_name": None}  # no game yet
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit world"}, 60)
    expected = sfd.save_name_for("Ficsit world")
    assert drv.game_action(settings, c, "save", {}, 30) == {"save_name": expected}
    assert expected.startswith("Ficsit_world_") and expected.endswith("_mcpanel")
    assert sfd.save_name_for("Ficsit_world") == "Ficsit_world_mcpanel"  # plain names keep theirs
    sessions = drv.game_action(settings, c, "sessions", {}, 30)
    assert sessions["current"] == "Ficsit world"
    assert [s["name"] for s in sessions["sessions"][0]["saves"]] == [
        expected,
        "Ficsit world_autosave_0",
    ]
    assert sessions["sessions"][0]["saves"][0]["build"] == 517554


def test_upload_save_is_multipart_and_loads(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    content = bytes(range(256)) * 300 + b"--not-a-boundary--\r\n"
    (settings.instances_dir / str(IID) / ".mcpanel-import-x.sav").write_bytes(content)
    result = drv.game_action(
        settings,
        c,
        "upload_save",
        {"path": ".mcpanel-import-x.sav", "save_name": "imported", "load": True},
        60,
    )
    assert result["session"] == "imported-session" and result["bytes"] == len(content)
    name, body, data = fake.uploads[0]
    assert name == "imported" and body == content
    assert data == {"SaveName": "imported", "LoadSaveGame": True, "EnableAdvancedGameSettings": False}
    for bad in (
        {"path": "../x", "save_name": "a"},
        {"path": "x.sav", "save_name": "a b"},
        {"path": "/etc/passwd", "save_name": "a"},
    ):
        with pytest.raises(AgentError):
            drv.game_action(settings, c, "upload_save", bad, 60)
    with pytest.raises(AgentError) as exc:
        drv.game_action(settings, c, "upload_save", {"path": "missing.sav", "save_name": "a"}, 60)
    assert exc.value.status == 404


def test_server_options_round_trip(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    assert drv.game_action(settings, c, "options", {}, 30)["options"]["FG.AutosaveInterval"] == "300.0"
    after = drv.game_action(settings, c, "apply_options", {"options": {"FG.AutosaveInterval": "600.0"}}, 30)
    assert after["options"]["FG.AutosaveInterval"] == "600.0"
    for bad in (
        {"options": {"Other": "1"}},
        {"options": {"FG.X": 1}},
        {"options": {}},
        {"options": {"FG.X": "a\nb"}},
    ):
        with pytest.raises(AgentError):
            drv.game_action(settings, c, "apply_options", bad, 30)


def test_unknown_actions_are_refused(settings, fake):
    with pytest.raises(AgentError) as exc:
        driver_for(fake).game_action(settings, Container(fake.port), "rm_rf", {}, 30)
    assert exc.value.code == "action_unsupported"
    with pytest.raises(AgentError) as exc:
        drivers.get("rcon", {"port": 1, "secret_env": "X", "commands": {}}).game_action(
            settings, None, "claim", {}, 1
        )
    assert exc.value.code == "action_unsupported"


# --- the UDP query, readiness ------------------------------------------------------------------------------
def test_query_reply_is_parsed(settings, fake):
    fake.server_name = "mcpanel-spike"
    fake.udp_state = 3
    info = sfd.udp_query("127.0.0.1", fake.port)
    assert info["state_name"] == "Playing" and info["net_cl"] == 517554 and info["name"] == "mcpanel-spike"
    assert info["substates"] == [[0, 3], [1, 1], [3, 1], [2, 1]] and info["cookie_ok"]
    # the spike's reply right after the install: 41 bytes, Idle, no name
    reply = (
        struct.pack("<HBBQBIQB", 0xF6D5, 1, 1, 5, 1, 517554, 0, 4)
        + struct.pack("<BHBHBHBH", 0, 3, 1, 1, 3, 1, 2, 1)
        + b"\x00\x00\x01"
    )
    assert len(reply) == 41 and sfd.parse_query_reply(reply)["state_name"] == "Idle"
    for bad in (b"", reply[:20], b"\x00\x00" + reply[2:]):
        with pytest.raises(ValueError):
            sfd.parse_query_reply(bad)


def test_udp_query_readiness_probe(settings, fake, monkeypatch):
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    health = Health("udp-query", port=fake.port, payload=sfd.POLL_PACKET, pattern=sfd.REPLY_PATTERN)
    c = Container(fake.port)
    assert docker_ops._probe_udp(c, health, settings) is True
    fake.udp_answers = False
    assert docker_ops._probe_udp(c, health, settings) is False
    fake.udp_answers = True
    wrong = Health("udp-query", port=fake.port, payload=sfd.POLL_PACKET, pattern=r"\Anope")
    assert docker_ops._probe_udp(c, wrong, settings) is False


def test_describe_reports_the_udp_probe(settings, fake, monkeypatch):
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    monkeypatch.setattr(docker_ops, "_health_state", {})
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    profile = profiles.register(
        _testgame(
            health=Health("udp-query", port=fake.port, payload=sfd.POLL_PACKET, pattern=sfd.REPLY_PATTERN)
        )
    )
    c = Container(fake.port)
    c.labels["io.mcpanel.game"] = "testgame"
    c.started = time.strftime("%Y-%m-%dT%H:%M:%S.5Z", time.gmtime())
    d = docker_ops.describe(c)
    assert d["health"] == "healthy" and "UDP query" in d["health_output"]
    assert profile.health.kind == "udp-query"


def test_driver_readiness_probe_and_its_cache(settings, fake, monkeypatch):
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    monkeypatch.setattr(docker_ops, "_health_state", {})
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    monkeypatch.setitem(
        drivers.FACTORIES,
        "fake-sf",
        lambda _o: SatisfactoryDriver(port=fake.port, secret_file=satisfactory.TOKEN_FILE),
    )
    profiles.register(_testgame(health=Health("driver"), control="fake-sf", control_options={}))
    c = Container(fake.port)
    c.labels["io.mcpanel.game"] = "testgame"
    c.started = time.strftime("%Y-%m-%dT%H:%M:%S.5Z", time.gmtime())
    assert docker_ops.describe(c)["health"] == "healthy"
    calls = len(fake.requests)
    fake.health = "broken"
    assert docker_ops.describe(c)["health"] == "healthy" and len(fake.requests) == calls  # cached for a few s
    monkeypatch.setattr(docker_ops, "HEALTH_TCP_CACHE_SECONDS", 0.0)
    assert docker_ops.describe(c)["health"] == "starting"
    fake.health = "slow"  # up, but below 10 ticks per second
    assert docker_ops.describe(c)["health"] == "healthy"


# --- stop: SaveGame, Shutdown, wait, docker stop -------------------------------------------------------------
def test_stop_saves_then_shuts_down(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit"}, 60)
    fake.requests.clear()

    def exits():
        c.state = "exited"  # the game exits cleanly a moment after Shutdown
        c.exit_code = 0

    fake.on_shutdown = lambda: threading.Timer(0.3, exits).start()
    result = docker_ops.StopResult()
    assert drv.stop(settings, c, 60, result) is True
    assert fake.functions() == ["QueryServerState", "SaveGame", "Shutdown"]
    assert result.flushed is True and result.warnings == []
    assert c.log == [("docker stop", 60)] and c.exit_code == 0  # after the exit: nothing left to signal


def test_stop_falls_back_to_a_signal_when_the_server_does_not_exit(settings, fake, monkeypatch):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    monkeypatch.setattr(sfd, "STOP_POLL", 0.05)
    monkeypatch.setattr(sfd, "MIN_EXIT_WAIT", 0.5)
    result = docker_ops.StopResult()
    drv.stop(settings, c, 1, result)
    assert "nothing to save" in result.warnings[0]
    assert "did not exit within 1 s" in result.warnings[1] and c.log == [("docker stop", 1)]


def test_stop_without_a_token_or_api_is_a_plain_docker_stop(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    result = docker_ops.StopResult()
    assert drv.stop(settings, c, 30, result) is False and "no API token" in result.warnings[0]
    write_token(settings, "bm9wZQ.00")
    fake.fail["Shutdown"] = (500, {"errorCode": "nope"})
    result = docker_ops.StopResult()
    assert drv.stop(settings, c, 30, result) is True
    assert any("Shutdown API call failed" in w for w in result.warnings) and c.log == [("docker stop", 30)]


def test_graceful_stop_goes_through_the_driver(settings, fake, monkeypatch):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    monkeypatch.setattr(drivers, "for_container", lambda _c: drv)
    fake.on_shutdown = lambda: setattr(c, "state", "exited")
    result = docker_ops.graceful_stop(settings, c, 45)
    assert fake.shutdown_called.is_set() and result.exit_code == 0


# --- /ping, the saving channel, game actions, the publish guard ----------------------------------------------
def test_ping_answers_through_the_driver(api, auth, settings, fake, monkeypatch):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit"}, 60)
    fake.players, fake.player_limit, fake.server_name = 2, 8, "Ficsit"
    monkeypatch.setattr(docker_ops, "game_endpoint", lambda s, _i: drv.query_endpoint(s, c))
    body = api.get(f"/v1/instances/{IID}/ping", headers=auth).json()
    assert body["online"] == 2 and body["max"] == 8 and body["sample"] == []
    assert body["motd"] == "Ficsit" and body["version"] == "CL 517554" and body["state"] == "Playing"
    assert body["session"] == "Ficsit"
    target = drv.query_endpoint(settings, c)
    assert isinstance(target, QueryTarget) and tuple(target) == ("127.0.0.1", fake.port)


def test_backup_saves_through_the_api(settings, fake, monkeypatch):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit"}, 60)
    fake.requests.clear()
    monkeypatch.setattr(docker_ops, "status", lambda _iid: {"running": True, "game": "satisfactory"})
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    monkeypatch.setattr(drivers, "for_status", lambda _s: drv)
    monkeypatch.setattr(main, "SAVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(main, "_rcon", lambda *_a: pytest.fail("no RCON for Satisfactory"))
    op = SimpleNamespace(update=lambda *a, **k: None)
    with main._world_saving_paused(settings, IID, "require", op, "backup", 0.02) as state:
        pass
    assert state.consistent and fake.functions() == ["QueryServerState", "SaveGame"]
    fake.fail["SaveGame"] = (200, {"errorCode": "save_game_failed", "errorMessage": "disk full"})
    with pytest.raises(AgentError) as exc:
        with main._world_saving_paused(settings, IID, "require", op, "backup", 0.02):
            pass
    assert exc.value.code == "consistency_failed"


def test_game_action_endpoint(api, auth, settings, fake, monkeypatch):
    c = Container(fake.port)
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    monkeypatch.setitem(drivers.FACTORIES, "satisfactory-api", lambda _o: driver_for(fake))
    r = api.post(
        f"/v1/instances/{IID}/game/claim",
        json={"params": {"server_name": "Ficsit", "admin_password": ADMIN}, "timeout": 30},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["result"]["claimed"] == "new"
    assert api.post(f"/v1/instances/{IID}/game/Bad-Name", json={}, headers=auth).status_code == 422
    r = api.post(f"/v1/instances/{IID}/game/nothing", json={}, headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "action_unsupported"
    r = api.post(f"/v1/instances/{IID}/game/status", json={"timeout": 900}, headers=auth)
    assert r.status_code == 422  # bounded
    assert api.post(f"/v1/instances/{IID}/game/status", json={}).status_code == 401


def test_game_action_needs_a_running_server(api, auth, monkeypatch):
    def not_running(_iid):
        raise AgentError("The server is not running", "not_running", 409)

    monkeypatch.setattr(docker_ops, "running_container", not_running)
    r = api.post(f"/v1/instances/{IID}/game/status", json={}, headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_running"


def test_publish_guard(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    with pytest.raises(AgentError) as exc:  # running and still unclaimed
        drv.publish_guard(settings, IID, c)
    assert exc.value.code == "server_unclaimed"
    with pytest.raises(AgentError):  # no container, never claimed through the panel
        drv.publish_guard(settings, IID, None)
    claimed(settings, fake, drv, c)
    write_claim_file(settings)
    drv.publish_guard(settings, IID, c)
    drv.publish_guard(settings, IID, None)
    c.state = "exited"  # stopped: the claim file and the token decide
    drv.publish_guard(settings, IID, c)


def test_apply_refuses_to_publish_an_unclaimed_server(api, auth, settings, monkeypatch):
    calls: list = []

    class Guard(SatisfactoryDriver):
        def publish_guard(self, s, iid, existing):
            calls.append((iid, existing))
            raise AgentError("not claimed", "server_unclaimed", 409)

    monkeypatch.setitem(drivers.FACTORIES, "satisfactory-api", lambda o: Guard.from_options(dict(o)))
    monkeypatch.setattr(main, "_pull_for_apply", lambda *a, **k: False)
    monkeypatch.setattr(docker_ops, "local_image_id", lambda _i: "sha256:x")
    monkeypatch.setattr(docker_ops, "get_container", lambda _i: None)
    monkeypatch.setattr(docker_ops, "remove_stale_replacement", lambda _i: False)
    created: list = []
    monkeypatch.setattr(
        docker_ops, "create_container", lambda *a, **k: created.append(a) or pytest.fail("created")
    )
    from mcagent import lastrun

    monkeypatch.setattr(lastrun, "summary", lambda *_a: None)
    spec = sf_spec().model_dump(mode="json")
    r = api.put(f"/v1/instances/{IID}/container", json={"spec": spec, "start": True}, headers=auth)
    assert r.status_code == 200
    op = r.json()["operation"]
    for _ in range(100):
        op = api.get(f"/v1/operations/{op['id']}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert op["status"] == "failed" and "not claimed" in str(op.get("error"))
    assert calls == [(IID, None)] and created == []
    # without ports (before the claim) the guard is not asked
    calls.clear()
    monkeypatch.setattr(docker_ops, "create_container", lambda *a, **k: SimpleNamespace(reload=lambda: None))
    monkeypatch.setattr(docker_ops, "describe", lambda _c: {"image_id": "sha256:x", "running": False})
    spec = sf_spec(ports=[]).model_dump(mode="json")
    r = api.put(f"/v1/instances/{IID}/container", json={"spec": spec}, headers=auth)
    op = r.json()["operation"]
    for _ in range(100):
        op = api.get(f"/v1/operations/{op['id']}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert op["status"] == "succeeded" and calls == []


def test_info_describes_satisfactory(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    from mcagent import storage

    monkeypatch.setattr(storage, "capabilities", lambda _s: {})
    monkeypatch.setattr(main, "restic_available", lambda _s: False)
    body = api.get("/v1/info", headers=auth).json()
    assert "game_actions" in body["features"]
    entry = body["games"]["satisfactory"]
    assert entry["control"] == "satisfactory-api" and entry["health"] == "udp-query"
    assert entry["ports"] == [[7777, "tcp"], [7777, "udp"]] and entry["install_retry_exits"] == [5, 8]


# --- restore keeps the game files a backup leaves out ----------------------------------------------------------
def test_restore_keeps_the_excluded_game_files(settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None: {"exists": True, "game": "satisfactory"})
    current = settings.instances_dir / str(IID)
    (current / "gamefiles" / "FactoryGame").mkdir(parents=True)
    (current / "gamefiles" / "FactoryServer.sh").write_text("#!/bin/sh\n")
    restored = settings.staging_dir / "restore-x"
    (restored / "saved" / "server").mkdir(parents=True)
    assert main._carry_over_excluded(settings, IID, current, restored) == []
    assert (restored / "gamefiles" / "FactoryServer.sh").read_text() == "#!/bin/sh\n"
    assert not (current / "gamefiles").exists()
    # a backup that has the folder (taken without the default excludes) keeps its own copy
    (current / "gamefiles").mkdir()
    (current / "gamefiles" / "new").write_text("x")
    assert main._carry_over_excluded(settings, IID, current, restored) == []
    assert not (restored / "gamefiles" / "new").exists()


def test_restore_of_minecraft_carries_nothing(settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None: {"exists": True})
    current = settings.instances_dir / str(IID)
    (current / "gamefiles").mkdir()
    restored = settings.staging_dir / "r"
    restored.mkdir()
    assert main._carry_over_excluded(settings, IID, current, restored) == []
    assert (current / "gamefiles").exists() and not (restored / "gamefiles").exists()
