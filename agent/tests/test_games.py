"""Runtime profiles and control drivers (multi-game phase 1).

Minecraft and Factorio are registered in the agent (Factorio's own tests: test_games_phase2.py); the tests add a made-up game ("testgame", an RCON
game shaped like Factorio) to exercise what a second profile changes: profile selection,
unknown_game, the per-profile image allow-list, the environment policy, the game label, the
driver dispatch of stop / command / ping / the saving pause, and GET /v1/info.
"""

from __future__ import annotations

import ast
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from mcagent import docker_ops, drivers, fetch, main, profiles, rcon
from mcagent.docker_ops import AgentError, build_create_kwargs, validate_image
from mcagent.drivers import MinecraftDriver, NoControlDriver, RconCommands, RconDriver, RconStep
from mcagent.profiles import Health, Mount, RuntimeProfile, UserModel
from mcagent.profiles import minecraft as mc_profile
from mcagent.schemas import InstanceSpec

from .test_graceful_stop import IID, IP, PASSWORD, FakeContainer, FakeDocker

MC_IMAGE = "itzg/minecraft-server:stable-java21"
TG_IMAGE = "example/testgame:2.0.1"


def _testgame(**over) -> RuntimeProfile:
    values = {
        "game": "testgame",
        "label": "Test game",
        "variants": (),
        "images": ("example/testgame",),
        "user": UserModel("fixed"),
        "cap_add": (),
        "mounts": (Mount(".", "/srv/game"), Mount("saves", "/srv/game/saves")),
        "injected_env": lambda _p, _s, spec: {"RCON_PASSWORD": spec.rcon_password, "GAME_PORT": "7777"},
        "reserved_env": frozenset({"GAME_PORT"}),
        "env_policy": lambda key: "hooks run commands" if key.upper().endswith("_HOOK") else None,
        "container_ports": ((7777, "udp"),),
        "allow_extra_ports": False,
        "never_publish": ((27015, "tcp"),),
        "health": Health("image"),
        "stop_signal": "SIGINT",
        "tty": True,
        "stdin_open": False,
        "hostname_prefix": "tg-",
        "fetch_hosts": (),
        "control": "rcon",
        "control_options": {
            "port": 27015,
            "secret_env": "RCON_PASSWORD",
            "max_command_len": 4000,
            "commands": RconCommands(
                save_before_stop="/server-save",
                save=(RconStep("/server-save", 60, "Saving the map (/server-save)"),),
            ),
        },
    }
    values.update(over)
    return RuntimeProfile(**values)


@pytest.fixture
def testgame(monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    for var in ("AGENT_GAME_TESTGAME_IMAGES",):
        monkeypatch.delenv(var, raising=False)
    return profiles.register(_testgame())


@pytest.fixture
def settings(tmp_path, monkeypatch):
    import os

    from mcagent.config import Settings

    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    return Settings(data_root=tmp_path / "data", host_data_root=Path("/mnt/user/mcpanel"), token="t")


def mc_spec(**over) -> InstanceSpec:
    values = {
        "name": "Survival",
        "image": MC_IMAGE,
        "memory_limit_mb": 2048,
        "rcon_password": "s3cret-rcon-password",
        "ports": [{"container_port": 25565, "host_port": 25565}],
    }
    values.update(over)
    return InstanceSpec(**values)


def tg_spec(**over) -> InstanceSpec:
    values = {
        "name": "Factory",
        "image": TG_IMAGE,
        "memory_limit_mb": 2048,
        "rcon_password": "s3cret-rcon-password",
        "ports": [{"container_port": 7777, "host_port": 34197, "protocol": "udp"}],
        "game": "testgame",
    }
    values.update(over)
    return InstanceSpec(**values)


# --- profile selection, unknown_game --------------------------------------------------------------
def test_minecraft_and_factorio_are_registered():
    assert {p.game for p in profiles.all_profiles()} == {"minecraft", "factorio"}
    assert profiles.get(None) is profiles.get("minecraft") is mc_profile.PROFILE
    assert mc_profile.PROFILE.control == "minecraft-rcon"


def test_a_spec_without_game_is_minecraft():
    spec = mc_spec()
    assert (spec.game, spec.variant) == ("minecraft", None)
    assert profiles.for_spec(spec) is mc_profile.PROFILE
    assert profiles.for_spec(mc_spec(game="minecraft")) is mc_profile.PROFILE


@pytest.mark.parametrize(
    ("game", "variant"), [("nogame", None), ("minecraft", "paper"), ("terraria", "tshock")]
)
def test_a_game_without_a_profile_is_unknown_game(settings, game, variant):
    spec = mc_spec(game=game, variant=variant)
    with pytest.raises(AgentError) as exc:
        profiles.for_spec(spec)
    assert (exc.value.code, exc.value.status) == ("unknown_game", 400)
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, spec, "h")
    assert exc.value.code == "unknown_game"


@pytest.mark.parametrize("value", ["Minecraft", "mine craft", "a" * 33, "", "mc_1", "../x"])
def test_game_and_variant_ids_are_validated(value):
    with pytest.raises(ValidationError):
        mc_spec(game=value)
    with pytest.raises(ValidationError):
        mc_spec(variant=value)


def test_apply_refuses_an_unknown_game_before_anything_is_queued(api, auth, no_docker):
    body = {"spec": mc_spec(game="nogame", image="example/nogame:2.0").model_dump(mode="json")}
    r = api.put(f"/v1/instances/{uuid.uuid4()}/container", json=body, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_game"


def test_an_older_panel_spec_still_validates_and_hashes_the_same():
    """A spec of a panel before multi-game support has no game/variant: the hash label is the
    hash of the spec as it always was (tests/test_games_pins.py pins the values)."""
    spec = mc_spec()
    assert "game" not in spec.hash_payload() and "variant" not in spec.hash_payload()
    assert spec.hash_payload() == {
        k: v for k, v in spec.model_dump(mode="json").items() if k not in ("game", "variant")
    }


def test_a_non_minecraft_spec_hash_includes_the_game(testgame):
    spec = tg_spec()
    assert spec.hash_payload()["game"] == "testgame" and spec.hash_payload()["variant"] is None
    assert main._spec_hash(spec) != main._spec_hash(tg_spec(game="minecraft"))


def test_register_refuses_incomplete_profiles(testgame):
    for bad in (
        _testgame(game="Bad"),
        _testgame(control="telnet"),
        _testgame(health=Health("udp-query", port=7777)),
        _testgame(health=Health("tcp")),
        _testgame(health=Health("log")),
        _testgame(health=Health("log", pattern="(")),
        _testgame(tag_pattern="["),
        _testgame(health=Health("cmd")),
        _testgame(mounts=(Mount("../other", "/data"),)),
        _testgame(mounts=(Mount(".", "data"),)),
        _testgame(user=UserModel("drop_privileges_env")),
    ):
        with pytest.raises(ValueError):
            profiles.register(bad)


# --- per-profile image allow-list ------------------------------------------------------------------
def test_images_are_allowed_per_profile(settings, testgame):
    mc = mc_profile.PROFILE
    assert validate_image(settings, TG_IMAGE, testgame) == TG_IMAGE
    assert validate_image(settings, MC_IMAGE) == MC_IMAGE  # no profile: Minecraft's
    for image, profile in ((MC_IMAGE, testgame), (TG_IMAGE, mc), (TG_IMAGE, None)):
        with pytest.raises(AgentError) as exc:
            validate_image(settings, image, profile)
        assert (exc.value.code, exc.value.status) == ("image_not_allowed", 403)
    with pytest.raises(AgentError) as exc:  # an image of another game cannot borrow a profile
        build_create_kwargs(settings, IID, tg_spec(image=MC_IMAGE), "h")
    assert exc.value.code == "image_not_allowed"


def test_agent_allowed_images_keeps_meaning_minecraft(tmp_path, testgame, monkeypatch):
    from mcagent.config import Settings

    custom = Settings(data_root=tmp_path, allowed_images="ghcr.io/me/mc")
    assert profiles.allowed_images(custom, mc_profile.PROFILE) == ["ghcr.io/me/mc"]
    assert profiles.allowed_images(custom, testgame) == ["example/testgame"]
    with pytest.raises(AgentError):
        validate_image(custom, "ghcr.io/me/mc:1", testgame)
    monkeypatch.setenv("AGENT_GAME_TESTGAME_IMAGES", "ghcr.io/me/testgame, example/testgame")
    assert profiles.allowed_images(custom, testgame) == ["ghcr.io/me/testgame", "example/testgame"]
    monkeypatch.setenv("AGENT_GAME_TESTGAME_IMAGES", '["ghcr.io/me/testgame"]')
    assert validate_image(custom, "ghcr.io/me/testgame:3", testgame) == "ghcr.io/me/testgame:3"
    with pytest.raises(AgentError):
        validate_image(custom, TG_IMAGE, testgame)
    assert profiles.all_allowed_images(custom) == {
        "ghcr.io/me/mc",
        "ghcr.io/me/testgame",
        "factoriotools/factorio",
        "docker.io/factoriotools/factorio",
    }


def test_superseded_image_cleanup_covers_every_game(settings, testgame, monkeypatch):
    removed: list[str] = []

    class Img:
        def __init__(self, repo):
            self.attrs = {"RepoTags": [], "RepoDigests": [f"{repo}@sha256:{'a' * 64}"]}

    images = {
        "sha256:mc": Img("itzg/minecraft-server"),
        "sha256:tg": Img("example/testgame"),
        "sha256:x": Img("x/y"),
    }
    fake = SimpleNamespace(
        images=SimpleNamespace(get=lambda i: images[i], remove=lambda i, noprune=False: removed.append(i)),
        containers=SimpleNamespace(list=lambda **_k: []),
    )
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    assert docker_ops.remove_superseded_images(settings, set(images)) == ["sha256:mc", "sha256:tg"]


# --- environment policy ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    [
        "MODS",
        "mods_file",
        "PLUGINS",
        "SPIGET_RESOURCES",
        "MODRINTH_PROJECTS",
        "PACKWIZ_URL",
        "GENERIC_PACKS",
        "RCON_CMDS_STARTUP",
        "RCON_PASSWORD_FILE",
        "PRE_START_SCRIPT",
        "ENABLE_SSH",
        "OPS",
        "WHITELIST",
        "DEBUG",
        "DEBUG_EXEC",
        "JAVA_TOOL_OPTIONS",
        "JVM_XX_OPTS",
        "LD_PRELOAD",
        "BASH_ENV",
        "PATH",
        "CF_PAGE_URL",
        "CF_API_KEY_FILE",
        "FTB_LEGACYJAVAFIXER",
        "LOAD_ENV_FROM_FILE",
        "SERVER_PROPERTIES_FILE",
        "CUSTOM_SERVER",
        "WORLD",
    ],
)
def test_minecraft_env_policy_refuses_what_the_panel_refuses(settings, key):
    assert mc_profile.env_policy(key) is not None
    unchecked = mc_spec().model_copy(update={"env": {key: "x"}})  # bypasses the schema
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, unchecked, "h")
    assert (exc.value.code, exc.value.status) == ("env_not_allowed", 400)


@pytest.mark.parametrize(
    "key",
    [
        "EULA",
        "TYPE",
        "VERSION",
        "MEMORY",
        "TZ",
        "MOTD",
        "JVM_OPTS",
        "USE_AIKAR_FLAGS",
        "CF_SLUG",
        "CF_FILE_ID",
        "CF_API_KEY",
        "CF_DOWNLOADS_REPO",
        "CF_EXCLUDE_MODS",
        "CF_IGNORE_MISSING_FILES",
        "FTB_MODPACK_ID",
        "FTB_MODPACK_VERSION_ID",
        "FTB_FORCE_REINSTALL",
        "MODRINTH_MODPACK",
        "MODRINTH_IGNORE_MISSING_FILES",
        "STOP_DURATION",
        "CREATE_CONSOLE_IN_PIPE",
        "PAPER_BUILD",
        "HTTPS_PROXY",
    ],
)
def test_minecraft_env_policy_accepts_what_the_panel_sends(settings, key):
    assert mc_profile.env_policy(key) is None
    assert build_create_kwargs(settings, IID, mc_spec(env={key: "1"}), "h")["environment"][key] == "1"


def test_agent_env_policy_matches_the_panel_lists():
    """The agent's copy of the panel's dangerous-variable lists (backend/apps/instances/spec.py)
    must stay equal to it; skipped where the backend is not checked out next to the agent."""
    backend = Path(__file__).resolve().parents[2] / "backend" / "apps"
    found: dict[str, object] = {}
    for path in sorted(backend.rglob("*.py")) if backend.is_dir() else []:
        if "_BLOCKED_EXACT" not in (text := path.read_text(encoding="utf-8")):
            continue
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in (
                    "_BLOCKED_EXACT",
                    "_BLOCKED_PREFIXES",
                    "_BLOCKED_SUFFIXES",
                    "_ALLOWED_PACK_TUNING",
                ):
                    found[name] = ast.literal_eval(node.value)
        if len(found) == 4:
            break
    if len(found) != 4:
        pytest.skip("the panel's environment policy is not next to the agent")
    assert set(found["_BLOCKED_EXACT"]) == mc_profile.BLOCKED_EXACT
    assert set(found["_BLOCKED_PREFIXES"]) == set(mc_profile.BLOCKED_PREFIXES)
    assert set(found["_BLOCKED_SUFFIXES"]) == set(mc_profile.BLOCKED_SUFFIXES)
    assert set(found["_ALLOWED_PACK_TUNING"]) == mc_profile.ALLOWED_PACK_TUNING


def test_injected_keys_are_replaced_not_refused(settings):
    """The agent's own values win over a spec that bypassed validation (test_docker_ops), also
    for keys whose family the policy refuses (RCON_PASSWORD)."""
    unchecked = mc_spec().model_copy(update={"env": {"RCON_PASSWORD": "x", "RCON_PORT": "1"}})
    env = build_create_kwargs(settings, IID, unchecked, "h")["environment"]
    assert env["RCON_PASSWORD"] == "s3cret-rcon-password" and env["RCON_PORT"] == "25575"


def test_profile_env_policy_and_reserved_env(settings, testgame):
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, tg_spec(env={"PRESTART_HOOK": "curl x | sh"}), "h")
    assert exc.value.code == "env_not_allowed"
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, tg_spec().model_copy(update={"env": {"game_port": "1"}}), "h")
    assert exc.value.code == "env_not_allowed" and "managed by the agent" in str(exc.value)
    # Minecraft's policy is Minecraft's: the other game's image may use such names
    assert (
        build_create_kwargs(settings, IID, tg_spec(env={"MODS": "a,b"}), "h")["environment"]["MODS"] == "a,b"
    )


# --- ports, the label rule and the rest of the profile ------------------------------------------------
@pytest.mark.parametrize("protocol", ["tcp", "udp"])
def test_minecraft_never_publishes_rcon(settings, protocol):
    ports = [
        {"container_port": 25565, "host_port": 25565},
        {"container_port": 25575, "host_port": 25575, "protocol": protocol},
    ]
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, mc_spec(ports=ports), "h")
    assert (exc.value.code, exc.value.status) == ("port_not_allowed", 403)


def test_minecraft_keeps_free_extra_ports(settings):
    ports = [{"container_port": 25565, "host_port": 25565}, {"container_port": 8123, "host_port": 8123}]
    assert set(build_create_kwargs(settings, IID, mc_spec(ports=ports), "h")["ports"]) == {
        "25565/tcp",
        "8123/tcp",
    }


def test_a_profile_publishes_only_its_ports(settings, testgame):
    for port in ({"container_port": 7777, "host_port": 7777}, {"container_port": 27015, "host_port": 27015}):
        spec = tg_spec(ports=[*tg_spec().model_dump()["ports"], port])
        with pytest.raises(AgentError) as exc:
            build_create_kwargs(settings, IID, spec, "h")
        assert exc.value.code == "port_not_allowed"


def test_the_game_label_is_only_on_containers_that_are_not_minecraft(settings, testgame):
    assert profiles.labels_for(mc_spec()) == {}
    assert "io.mcpanel.game" not in build_create_kwargs(settings, IID, mc_spec(), "h")["labels"]
    labels = build_create_kwargs(settings, IID, tg_spec(), "h")["labels"]
    assert labels["io.mcpanel.game"] == "testgame" and "io.mcpanel.variant" not in labels
    monkey = profiles.register(_testgame(variants=("modded",)))
    labels = build_create_kwargs(settings, IID, tg_spec(variant="modded"), "h")["labels"]
    assert (labels["io.mcpanel.game"], labels["io.mcpanel.variant"]) == ("testgame", "modded")
    assert profiles.for_labels(labels) is monkey
    assert profiles.for_labels({}) is mc_profile.PROFILE
    assert profiles.for_labels({"io.mcpanel.game": "nogame"}) is None


def test_the_profile_decides_the_rest_of_the_container(settings, testgame):
    kw = build_create_kwargs(settings, IID, tg_spec(), "h")
    assert kw["user"] == f"{settings.runtime_uid}:{settings.runtime_gid}"
    assert (
        kw["cap_add"] == [] and kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges:true"]
    )
    assert kw["volumes"] == {
        f"/mnt/user/mcpanel/instances/{IID}": {"bind": "/srv/game", "mode": "rw"},
        f"/mnt/user/mcpanel/instances/{IID}/saves": {"bind": "/srv/game/saves", "mode": "rw"},
    }
    assert "healthcheck" not in kw  # the image's own
    assert (kw["stop_signal"], kw["tty"], kw["stdin_open"]) == ("SIGINT", True, False)
    assert kw["hostname"] == f"tg-{IID.hex[:8]}"
    assert kw["environment"] == {"RCON_PASSWORD": "s3cret-rcon-password", "GAME_PORT": "7777"}
    assert kw["ports"] == {"7777/udp": [("0.0.0.0", 34197)]}


def test_the_java_truststore_is_only_mounted_for_a_jvm(settings, testgame):
    settings.runtime_ca_bundle = Path("/etc/ssl/ca.crt")
    settings.runtime_java_truststore = Path("/etc/ssl/cacerts")
    assert set(build_create_kwargs(settings, IID, mc_spec(), "h")["volumes"]) >= {
        "/etc/ssl/ca.crt",
        "/etc/ssl/cacerts",
    }
    vols = build_create_kwargs(settings, IID, tg_spec(), "h")["volumes"]
    assert "/etc/ssl/ca.crt" in vols and "/etc/ssl/cacerts" not in vols


def test_minecraft_fetch_hosts_are_the_fetch_allow_list():
    assert set(mc_profile.PROFILE.fetch_hosts) == fetch.DEFAULT_ALLOWED_HOSTS


# --- driver selection -------------------------------------------------------------------------------
def test_driver_selection_from_labels(testgame):
    assert isinstance(drivers.for_labels({}), MinecraftDriver)
    assert isinstance(drivers.for_status({"running": True}), MinecraftDriver)
    rcon_driver = drivers.for_labels({"io.mcpanel.game": "testgame"})
    assert type(rcon_driver) is RconDriver and (rcon_driver.port, rcon_driver.max_command_len) == (
        27015,
        4000,
    )
    assert isinstance(drivers.for_status({"game": "testgame"}), RconDriver)
    assert type(drivers.for_labels({"io.mcpanel.game": "nogame"})) is NoControlDriver
    with pytest.raises(ValueError):
        drivers.get("telnet")


def test_the_generic_rcon_driver_from_plain_options():
    d = drivers.get(
        "rcon",
        {
            "port": 27015,
            "secret_env": "RCON_PW",
            "commands": {"save_before_stop": "/server-save", "save": [("/server-save", 60, "Saving")]},
        },
    )
    assert (d.port, d.secret_env, d.max_command_len) == (27015, "RCON_PW", rcon.MAX_COMMAND_LEN)
    assert d.commands.save == (RconStep("/server-save", 60, "Saving"),)
    assert d.can_save and not d.can_pause_saving
    with pytest.raises(ValueError):
        RconDriver(port=0, secret_env="X", commands=RconCommands())


def test_minecraft_driver_is_todays_rcon():
    d = drivers.get("minecraft-rcon")
    assert (d.port, d.secret_env, d.max_command_len) == (25575, "RCON_PASSWORD", 1446)
    assert (d.commands.save_before_stop, d.commands.stop) == ("save-all flush", "stop")
    assert [s.command for s in d.commands.pause_saving] == ["save-off", "save-all flush"]
    assert [s.command for s in d.commands.resume_saving] == ["save-on"] and d.resume_label == "save-on"


# --- dispatch: stop, command, ping --------------------------------------------------------------------
@pytest.fixture
def game_server(monkeypatch, testgame):
    """A running container of the test game (RCON 27015) with fake Docker and fake RCON."""
    log: list = []
    container = FakeContainer(log)
    container.labels["io.mcpanel.game"] = "testgame"
    container.ports = {"7777/udp": [{"HostIp": "0.0.0.0", "HostPort": "34197"}]}
    sent: list[tuple] = []

    def fake_rcon(host, port, password, command, *, timeout, connect_timeout):
        sent.append((host, port, password, command))
        log.append(("rcon", command))
        return ""

    monkeypatch.setattr(docker_ops, "client", lambda: FakeDocker(container))
    monkeypatch.setattr(docker_ops, "run_command_sync", fake_rcon)
    container.stop_may_return.set()
    return SimpleNamespace(container=container, log=log, sent=sent)


def test_graceful_stop_goes_through_the_containers_driver(game_server, settings):
    result = docker_ops.stop(IID, 120, settings)
    assert game_server.sent == [(IP, 27015, PASSWORD, "/server-save")]
    assert game_server.log == [("rcon", "/server-save"), ("docker stop", 120)]
    assert result.as_dict() == {"flushed": True, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}


def test_an_rcon_game_without_a_stop_command_gets_no_courier(game_server, settings, monkeypatch):
    def refused(*_a, **_k):
        game_server.log.append(("rcon refused",))
        raise rcon.RconUnavailable("Connection refused")

    monkeypatch.setattr(docker_ops, "run_command_sync", refused)
    result = docker_ops.stop(IID, 60, settings)
    assert game_server.log == [("rcon refused",), ("docker stop", 60)]
    assert result.stop_sent_via_rcon is False and "RCON is not available" in result.warnings[0]
    assert not [t for t in threading.enumerate() if t.name.startswith("rcon-stop-")]


def test_a_container_without_a_driver_gets_a_plain_docker_stop(game_server, settings):
    game_server.container.labels["io.mcpanel.game"] = "nogame"  # no such profile here
    result = docker_ops.stop(IID, 30, settings)
    assert game_server.log == [("docker stop", 30)] and game_server.sent == []
    assert result.as_dict() == {"flushed": False, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}


def test_command_goes_through_the_containers_driver(api, auth, game_server, monkeypatch):
    calls: list[tuple] = []

    async def fake_run_command(host, port, password, command, timeout=30.0, *, max_command_len):
        calls.append((host, port, password, command, timeout, max_command_len))
        return "Saved"

    monkeypatch.setattr(rcon, "run_command", fake_run_command)
    r = api.post(f"/v1/instances/{IID}/command", json={"command": "/server-save", "timeout": 7}, headers=auth)
    assert r.status_code == 200 and r.json() == {"output": "Saved"}
    assert calls == [(IP, 27015, PASSWORD, "/server-save", 7.0, 4000)]
    game_server.container.labels["io.mcpanel.game"] = "nogame"
    r = api.post(f"/v1/instances/{IID}/command", json={"command": "x"}, headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "command_unsupported"


def test_minecraft_command_still_goes_to_rcon_25575(api, auth, monkeypatch):
    log: list = []
    container = FakeContainer(log)
    monkeypatch.setattr(docker_ops, "client", lambda: FakeDocker(container))
    calls: list[tuple] = []

    async def fake_run_command(host, port, password, command, timeout=30.0, *, max_command_len):
        calls.append((host, port, password, command, timeout, max_command_len))
        return "There are 0 of a max of 20 players online"

    monkeypatch.setattr(rcon, "run_command", fake_run_command)
    r = api.post(f"/v1/instances/{IID}/command", json={"command": "list"}, headers=auth)
    assert r.json() == {"output": "There are 0 of a max of 20 players online"}
    assert calls == [(IP, 25575, PASSWORD, "list", 30.0, 1446)]


def test_ping_is_refused_for_a_game_without_a_query(api, auth, game_server, settings):
    r = api.get(f"/v1/instances/{IID}/ping", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ping_unavailable"
    with pytest.raises(AgentError) as exc:
        docker_ops.game_endpoint(settings, IID)
    assert exc.value.code == "ping_unavailable"
    del game_server.container.labels["io.mcpanel.game"]  # the same container as Minecraft
    assert docker_ops.game_endpoint(settings, IID) == (IP, 25565)


def test_rcon_endpoint_follows_the_driver(game_server, settings):
    assert docker_ops.rcon_endpoint(settings, IID) == (IP, 27015, PASSWORD)
    settings.rcon_mode = "published"
    with pytest.raises(AgentError) as exc:
        docker_ops.rcon_endpoint(settings, IID)
    assert str(exc.value) == "RCON port is not published"
    game_server.container.labels["io.mcpanel.game"] = "nogame"
    with pytest.raises(AgentError) as exc:
        docker_ops.rcon_endpoint(settings, IID)
    assert exc.value.code == "rcon_unavailable"


def test_describe_reports_the_game_only_for_other_games(game_server):
    container = game_server.container
    assert docker_ops.describe(container)["game"] == "testgame"
    assert "variant" not in docker_ops.describe(container)
    del container.labels["io.mcpanel.game"]
    described = docker_ops.describe(container)
    assert "game" not in described and "variant" not in described


# --- dispatch: the saving pause -------------------------------------------------------------------------
class _Op:
    def __init__(self) -> None:
        self.updates: list[tuple] = []

    def update(self, progress=None, message=None):
        self.updates.append((progress, message))


@pytest.fixture
def saving(agent_settings, monkeypatch, testgame):
    sent: list[tuple[str, float]] = []
    status = {"running": True, "game": "testgame"}

    async def fake_run_command(host, port, password, command, timeout=30.0):
        sent.append((command, timeout))
        return ""

    monkeypatch.setattr(docker_ops, "status", lambda _iid: dict(status))
    monkeypatch.setattr(docker_ops, "rcon_endpoint", lambda _s, _iid: ("127.0.0.1", 27015, "pw"))
    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main, "SAVE_SETTLE_SECONDS", 0)
    return SimpleNamespace(settings=agent_settings, sent=sent, status=status)


def test_a_game_that_cannot_pause_saves_before_the_copy(saving):
    op = _Op()
    with main._world_saving_paused(saving.settings, IID, "require", op, "backup", 0.02) as state:
        assert not (saving.settings.state_dir / "saving-paused" / str(IID)).exists()  # nothing to resume
    assert saving.sent == [("/server-save", 60)]
    assert op.updates == [(0.02, "Saving the map (/server-save)")]
    assert state.consistent is True and state.warnings == []


@pytest.mark.parametrize("consistency", ["auto", "require"])
def test_a_game_without_saving_makes_the_copy_inconsistent(saving, consistency):
    saving.status["game"] = "nogame"  # no profile: the none driver
    if consistency == "require":
        with pytest.raises(AgentError) as exc:
            with main._world_saving_paused(saving.settings, IID, consistency, _Op(), "backup", 0.02):
                pass
        assert exc.value.code == "consistency_failed"
    else:
        with main._world_saving_paused(saving.settings, IID, consistency, _Op(), "snapshot", 0.02) as state:
            pass
        assert state.consistent is False and "snapshot may be inconsistent" in state.warnings[0]
    assert saving.sent == []


def test_recovery_clears_markers_of_games_that_cannot_pause(saving):
    marker = saving.settings.state_dir / "saving-paused" / str(IID)
    marker.parent.mkdir(parents=True)
    marker.write_text("1\n")
    assert main._recover_saving(saving.settings) == 0
    assert saving.sent == [] and not marker.exists()
    marker.write_text("1\n")
    main._resume_all_saving(saving.settings, 3.0)
    assert saving.sent == [] and not marker.exists()


def test_minecraft_saving_pause_is_dispatched_to_its_driver(saving):
    del saving.status["game"]
    with main._world_saving_paused(saving.settings, IID, "auto", _Op(), "backup", 0.02) as state:
        pass
    assert [c for c, _t in saving.sent] == ["save-off", "save-all flush", "save-on"] and state.consistent


# --- GET /v1/info ---------------------------------------------------------------------------------
@pytest.fixture
def info(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    monkeypatch.setattr(main.storage, "capabilities", lambda _settings: {})
    monkeypatch.setattr(main, "restic_available", lambda _settings: None)
    return lambda: api.get("/v1/info", headers=auth).json()


def test_info_lists_the_games(info):
    body = info()
    assert "games" in body["features"]
    assert body["games"] == {
        "minecraft": {
            "variants": [],
            "images": ["itzg/minecraft-server", "docker.io/itzg/minecraft-server"],
            "control": "minecraft-rcon",
            "ports": [[25565, "tcp"]],
            "never_publish": [[25575, None]],
            "health": "cmd",
            "install_retry_exits": [],
        },
        "factorio": {
            "variants": [],
            "images": ["factoriotools/factorio", "docker.io/factoriotools/factorio"],
            "control": "rcon",
            "ports": [[34197, "udp"]],
            "never_publish": [[27015, None]],
            "health": "log",
            "install_retry_exits": [],
        },
    }


def test_info_lists_a_registered_profile(info, testgame, monkeypatch):
    monkeypatch.setenv("AGENT_GAME_TESTGAME_IMAGES", "ghcr.io/me/testgame")
    profiles.register(_testgame(variants=("modded", "vanilla")))
    assert info()["games"]["testgame"] == {
        "variants": ["modded", "vanilla"],
        "images": ["ghcr.io/me/testgame"],
        "control": "rcon",
        "ports": [[7777, "udp"]],
        "never_publish": [[27015, "tcp"]],
        "health": "image",
        "install_retry_exits": [],
    }
