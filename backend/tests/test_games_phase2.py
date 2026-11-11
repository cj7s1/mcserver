"""Multi-game phases 2 and 3: the generic pieces (named ports, memory model "plain", config codecs and
the Game settings API, secrets, setup steps, saves, per-game runtime images, the first-start retry)
and Factorio as the first new game (definition, spec, create, players, software update, saves).

The Minecraft pins (tests/test_games_phase1_pins.py) cover that none of this changes a Minecraft server.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest
from django.core.cache import cache

from apps import games
from apps.games import codecs
from apps.games.base import ConfigFile, GameDefinition, PortSpec, SecretSpec, SetupStep
from apps.games.factorio import FactorioDefinition, parse_players
from apps.instances.models import Instance
from apps.instances.spec import build_spec, current_spec_hash
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus

VERSIONS = [
    {"id": "2.1.24", "label": "2.1.24", "stable": True, "latest": True},
    {"id": "2.1.23", "label": "2.1.23", "stable": True, "latest": False},
    {"id": "2.0.77", "label": "2.0.77", "stable": True, "latest": False},
]


@pytest.fixture
def factorio_versions(db: Any) -> list[dict[str, Any]]:
    """The version catalog without the network (Docker Hub + factorio.com are cached for an hour)."""
    cache.set("catalog:factorio:versions:factoriotools/factorio", VERSIONS, 3600)
    return VERSIONS


@pytest.fixture
def factorio(make_instance: Any) -> Instance:
    return make_instance(
        name="Factory",
        game="factorio",
        server_type="",
        minecraft_version="2.1.23",
        image="factoriotools/factorio:2.1.23-rootless",
        memory_mb=2048,
        stop_timeout=120,
        jvm_flags_preset="none",
        game_port=34197,
        ports=[{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34197}],
        game_options={"space_age": False, "save_name": "world"},
        rcon_password="r" * 32,
    )


def err(resp: Any) -> str:
    return resp.json()["error"]["code"]


def run(kind: str, inst: Instance, capture: Any, **params: Any) -> Job:
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    return job


def start_container(fake_agent: Any, inst: Instance, *, running: bool = True) -> None:
    fake_agent.apply(inst.id, build_spec(inst), start=running)


# --- codecs -----------------------------------------------------------------------------------------------
def test_json_codec_keeps_comments_and_unknown_keys():
    text = json.dumps({"_comment_x": "keep", "name": "a", "visibility": {"public": True, "lan": True}, "other": [1]})
    c = codecs.get("json")
    assert c.parse(text) == {"name": "a", "visibility.public": True, "visibility.lan": True, "other": [1]}
    out = json.loads(c.update(text, {"visibility.public": False, "new.deep": 3, "name": None}))
    assert out == {"_comment_x": "keep", "visibility": {"public": False, "lan": True}, "other": [1], "new": {"deep": 3}}
    with pytest.raises(ValueError, match="not valid JSON"):
        c.parse("{broken")


def test_kv_codec_updates_in_place():
    text = "# comment\nport=7777\nworld=a.wld\n\nmotd=hi\n"
    c = codecs.get("kv")
    assert c.parse(text) == {"port": "7777", "world": "a.wld", "motd": "hi"}
    assert (
        c.update(text, {"motd": "hello", "maxplayers": 8, "world": None}) == "# comment\nport=7777\n\nmotd=hello\nmaxplayers=8\n"
    )


def test_ini_codec_sections():
    text = "; top\n[GAMEPLAY]\ngame_mode = survival\n\n[NETWORK]\ncluster_name = x\n"
    c = codecs.get("ini")
    assert c.parse(text) == {"GAMEPLAY.game_mode": "survival", "NETWORK.cluster_name": "x"}
    out = c.update(text, {"GAMEPLAY.pvp": True, "NETWORK.cluster_name": "y", "MISC.max_snapshots": 6})
    assert out == (
        "; top\n[GAMEPLAY]\ngame_mode = survival\npvp = true\n\n[NETWORK]\ncluster_name = y\n\n[MISC]\nmax_snapshots = 6\n"
    )


def test_properties_codec_wraps_minecraft_properties():
    c = codecs.get("java-properties")
    assert c.parse("motd=Hi\npvp=true\n") == {"motd": "Hi", "pvp": "true"}
    assert "pvp=false" in c.update("motd=Hi\npvp=true\n", {"pvp": False})


def test_validate_values_against_the_schema():
    cfg = ConfigFile(
        path="x.json",
        codec="json",
        fields=(
            {"key": "n", "type": "int", "label": "N", "min": 1, "max": 5},
            {"key": "b", "type": "bool", "label": "B"},
            {"key": "e", "type": "enum", "label": "E", "options": ["a", "b"]},
            {"key": "s", "type": "int", "label": "Seed", "nullable": True},
            {"key": "p", "type": "string", "label": "P", "secret": True},
        ),
        managed_keys=frozenset({"port"}),
    )
    assert codecs.validate_values(cfg, {"n": "3", "b": "true", "e": "b", "s": "", "p": "x"}) == {
        "n": 3,
        "b": True,
        "e": "b",
        "s": None,
        "p": "x",
    }
    for bad, match in (
        ({"n": 9}, "at most 5"),
        ({"b": "yes"}, "true or false"),
        ({"e": "c"}, "one of"),
        ({"zz": 1}, "not a setting"),
    ):
        with pytest.raises(ValueError, match=match):
            codecs.validate_values(cfg, bad)
    with pytest.raises(ValueError, match="managed"):
        codecs.validate_values(cfg, {"port": 1})
    assert codecs.masked(cfg, {"p": "secret", "n": 1}) == ({"p": codecs.MASK, "n": 1}, ["p"])
    assert codecs.masked(cfg, {"p": ""}) == ({"p": ""}, [])
    # text formats store text and refuse line breaks
    kv = ConfigFile(path="a.txt", codec="kv", fields=({"key": "motd", "type": "string", "label": "Motd"},))
    with pytest.raises(ValueError, match="not allowed"):
        codecs.validate_values(kv, {"motd": "a\nport=1"})


# --- the Factorio definition ----------------------------------------------------------------------------
def test_factorio_is_registered_with_its_capabilities():
    d = games.get("factorio")
    assert isinstance(d, FactorioDefinition) and d.memory_model == "plain" and d.default_memory_mb() == 2048
    assert d.capabilities() == {
        "console_commands",
        "broadcast",
        "players",
        "player_actions",
        "consistent_backups",
        "world_reset",
        "world_import_export",
        "software_update",
        "config_files",
    }
    assert not d.capabilities() & {"jvm", "eula", "modpacks", "content", "properties", "query"}
    # config_files is not a Minecraft capability: a Minecraft server's list is unchanged
    assert "config_files" not in games.CAPABILITIES and "config_files" in games.ALL_CAPABILITIES


def test_factorio_spec(factorio, settings):
    settings.TIME_ZONE = "UTC"
    spec = build_spec(factorio)
    assert spec == {
        "name": "Factory",
        "image": "factoriotools/factorio:2.1.23-rootless",
        "env": {
            "TZ": "UTC",
            "DLC_SPACE_AGE": "false",
            "GENERATE_NEW_SAVE": "true",
            "LOAD_LATEST_SAVE": "true",
            "SAVE_NAME": "world",
        },
        "memory_limit_mb": 2048,  # plain: no JVM overhead
        "ports": [{"container_port": 34197, "host_port": 34197, "protocol": "udp", "host_ip": "0.0.0.0"}],
        "restart_policy": "unless-stopped",
        "stop_timeout": 120,
        "rcon_password": "r" * 32,
        "game": "factorio",
    }
    factorio.game_options = {"space_age": True, "save_name": "world"}
    factorio.env = {"TZ": "Europe/Berlin", "UPDATE_MODS_ON_START": "true"}
    env = build_spec(factorio)["env"]
    # only what the game allows reaches the image (the agent refuses everything else too)
    assert env["DLC_SPACE_AGE"] == "true" and env["TZ"] == "Europe/Berlin" and "UPDATE_MODS_ON_START" not in env
    assert factorio.container_memory_mb == 2048


def test_factorio_console_rules():
    d = games.get("factorio")
    assert d.normalise_command(" /players online ") == "/players online"
    assert d.is_stop_command("/quit") and d.is_stop_command(" /QUIT ") and not d.is_stop_command("quit")
    assert not d.is_stop_command("/stop")  # Factorio has no /stop ("Unknown command")
    mc = games.get("minecraft")
    assert mc.normalise_command("/say hi") == "say hi" and mc.stop_word == "stop" and d.stop_word == "/quit"


def test_parse_players():
    assert parse_players("Online players (0):\n") == {"online": 0, "max": None, "players": []}
    out = "Online players (2):\n  alice (online)\n  bob.the-builder (online)\n"
    assert parse_players(out) == {"online": 2, "max": None, "players": ["alice", "bob.the-builder"]}


def test_factorio_catalog_entry(clients):
    body = {g["id"]: g for g in clients["viewer"].get("/api/catalog/games").json()}
    f = body["factorio"]
    assert f["memory_model"] == "plain" and f["default_memory_mb"] == 2048 and f["has_versions"] is True
    assert f["ports"] == [
        {
            "name": "game",
            "label": "Game",
            "container_port": 34197,
            "protocol": "udp",
            "default_host_port": 34197,
            "help": "Players connect to this UDP port (Factorio's default is 34197).",
        }
    ]
    assert [o["key"] for o in f["options"]] == ["space_age"] and f["options"][0]["default"] is False
    assert [c["id"] for c in f["config_files"]] == ["server-settings", "map-gen-settings", "map-settings"]
    public = next(x for x in f["config_files"][0]["fields"] if x["key"] == "visibility.public")
    assert public["default"] is False and public["initial"] is True
    assert f["stop_words"] == ["/quit"] and f["keeps_command_slash"] is True and f["world"]["kind"] == "saves"
    assert f["player_list_labels"] == {"whitelist": "Whitelist", "ops": "Admins", "bans": "Bans"}
    # Minecraft's entry has none of the phase-2 keys
    assert set(body["minecraft"]) == {"id", "label", "variants", "capabilities", "memory_model", "default_memory_mb", "notes"}


def test_versions_from_docker_hub_and_factorio_com(clients, monkeypatch):
    from apps.catalog import providers

    pages = {
        "https://hub.docker.com/v2/repositories/factoriotools/factorio/tags": {
            "results": [
                {"name": "2.1.24-rootless"},
                {"name": "stable-2.1.24-rootless"},
                {"name": "stable-rootless"},
                {"name": "2.1-rootless"},
                {"name": "2.1.25-rootless"},
            ],
            "next": "https://hub.docker.com/page2",
        },
        "https://hub.docker.com/page2": {"results": [{"name": "2.0.77-rootless"}, {"name": "2.0.77"}], "next": None},
        "https://factorio.com/api/latest-releases": {"stable": {"headless": "2.1.24"}, "experimental": {"headless": "2.1.25"}},
    }
    monkeypatch.setattr(providers, "_get", lambda url, **_kw: pages[url])
    cache.clear()
    body = clients["viewer"].get("/api/catalog/games/factorio/versions").json()
    assert body["latest"] == "2.1.24"
    assert [(v["id"], v["stable"], v["latest"]) for v in body["versions"]] == [
        ("2.1.25", False, False),
        ("2.1.24", True, True),
        ("2.0.77", False, False),
    ]
    assert clients["viewer"].get("/api/catalog/games/minecraft/versions").status_code == 404


# --- named ports ------------------------------------------------------------------------------------------
def test_named_ports_are_protocol_aware(node, make_instance, fake_agent):
    from apps.instances import services

    d = games.get("factorio")
    # a Minecraft server on TCP 34197 does not take UDP 34197
    make_instance(game_port=34197)
    assert services.assign_named_ports(d, node) == [
        {"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34197}
    ]
    # another Factorio server on UDP 34197: the next free one
    make_instance(
        game="factorio",
        server_type="",
        game_port=34197,
        ports=[{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34197}],
    )
    assert services.assign_named_ports(d, node)[0]["host_port"] == 34198
    # a host program on UDP 34198 counts too
    fake_agent.add_foreign_port(34198, protocol="udp")
    assert services.assign_named_ports(d, node)[0]["host_port"] == 34199
    # a requested port that is taken for its protocol: 409 port_conflict with the port's name
    with pytest.raises(Exception) as exc:
        services.assign_named_ports(d, node, {"game": 34197})
    assert exc.value.error_code == "port_conflict" and exc.value.details["port_name"] == "game"
    with pytest.raises(Exception) as exc:
        services.assign_named_ports(d, node, {"rcon": 27015})
    assert exc.value.error_code == "invalid_port"
    # Minecraft's allocation never takes a named port's number, whatever its protocol
    assert 34197 in services.ports_in_use(node)


# --- create ------------------------------------------------------------------------------------------------
def factorio_body(**extra: Any) -> dict[str, Any]:
    return {"game": "factorio", "name": "Factory", "version": "2.1.23", **extra}


def test_create_factorio_server(clients, fake_agent, factorio_versions, django_capture_on_commit_callbacks):
    body = factorio_body(
        options={"space_age": False},
        settings={"server-settings": {"game_password": "pw", "max_players": 8}, "map-gen-settings": {"seed": 1234}},
        nightly_backup=True,
    )
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body, format="json")
    assert resp.status_code == 201, resp.json()
    data = resp.json()["instance"]
    assert data["game"] == "factorio" and data["memory_model"] == "plain" and data["minecraft_version"] == "2.1.23"
    assert data["ports"] == [{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34197, "label": "Game"}]
    assert data["game_options"] == {"space_age": False, "save_name": "world"} and data["setup"] is None
    assert data["image"] == "factoriotools/factorio:2.1.23-rootless" and data["stop_timeout"] == 120
    assert data["connection"]["ports"][0]["protocol"] == "udp" and "UDP 34197" in data["connection"]["notes"][1]
    assert resp.json()["backup_schedule"] is not None
    inst = Instance.objects.get(pk=data["id"])
    assert inst.game_port == 34197 and inst.memory_mb == 2048
    # the install job: initial files, the control secret, the container
    server = json.loads(fake_agent.text(inst.id, "config/server-settings.json"))
    assert server["visibility"] == {"public": False, "lan": True} and server["name"] == "Factory"
    assert server["game_password"] == "pw" and server["max_players"] == 8 and server["autosave_interval"] == 10
    assert json.loads(fake_agent.text(inst.id, "config/map-gen-settings.json"))["seed"] == 1234
    assert fake_agent.text(inst.id, "config/rconpw") == inst.rcon_password + "\n"
    spec = fake_agent.containers[str(inst.id)]["spec"]
    assert spec["game"] == "factorio" and spec["env"]["DLC_SPACE_AGE"] == "false"
    inst.refresh_from_db()
    assert inst.container_spec_hash == current_spec_hash(inst)


def test_create_factorio_validation(clients, fake_agent, factorio_versions):
    post = lambda **kw: clients["owner"].post("/api/instances/", factorio_body(**kw), format="json")  # noqa: E731
    assert post(version="9.9.9").status_code == 400
    assert err(post(version="9.9.9")) == "invalid_version"
    assert post(memory_mb=600).status_code == 400  # below the game's minimum (1 GB)
    assert post(options={"space_age": "yes"}).status_code == 400
    assert post(settings={"server-settings": {"port": 1}}).status_code == 400
    assert post(env={"UPDATE_MODS_ON_START": "true"}).status_code == 400
    resp = post(ports={"game": 25565})
    assert resp.status_code == 201 and resp.json()["instance"]["ports"][0]["host_port"] == 25565
    # Minecraft still needs the EULA
    mc = {"name": "S", "server_type": "VANILLA", "minecraft_version": "1.20.4"}
    resp = clients["owner"].post("/api/instances/", mc, format="json")
    assert resp.status_code == 400 and "accept_eula" in resp.json()["error"]["details"]["fields"]


def test_create_factorio_needs_an_agent_with_the_profile(clients, fake_agent, factorio_versions):
    fake_agent.game_profiles.pop("factorio")
    cache.clear()
    cache.set("catalog:factorio:versions:factoriotools/factorio", VERSIONS, 3600)
    resp = clients["owner"].post("/api/instances/", factorio_body(), format="json")
    assert resp.status_code == 409 and err(resp) == "agent_lacks_game"
    assert not Instance.objects.filter(game="factorio").exists()


# --- PATCH ------------------------------------------------------------------------------------------------
def test_patch_named_ports_and_refused_fields(clients, factorio, make_instance, fake_agent):
    url = f"/api/instances/{factorio.id}"
    resp = clients["owner"].patch(url, {"ports": [{"name": "game", "host_port": 34300}]}, format="json")
    assert resp.status_code == 200, resp.json()
    factorio.refresh_from_db()
    assert factorio.ports[0]["host_port"] == 34300 and factorio.game_port == 34300 and factorio.config_revision == 2
    make_instance(
        game="factorio",
        server_type="",
        game_port=34400,
        ports=[{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34400}],
    )
    resp = clients["owner"].patch(url, {"ports": [{"name": "game", "host_port": 34400}]}, format="json")
    assert resp.status_code == 409 and err(resp) == "port_conflict"
    for body in ({"game_port": 30000}, {"jvm_args": "-Xss1m"}, {"java_version": 21}, {"extra_ports": []}):
        assert clients["owner"].patch(url, body, format="json").status_code == 400, body
    assert clients["owner"].patch(url, {"env": {"TZ": "UTC"}}, format="json").status_code == 200
    assert clients["owner"].patch(url, {"env": {"SAVE_NAME": "x"}}, format="json").status_code == 400
    assert clients["owner"].patch(url, {"memory_mb": 512}, format="json").status_code == 400
    assert clients["owner"].patch(url, {"minecraft_version": "2.1.24"}, format="json").status_code == 400


def test_minecraft_patch_ignores_an_empty_ports_list(clients, make_instance):
    inst = make_instance()
    assert clients["owner"].patch(f"/api/instances/{inst.id}", {"ports": []}, format="json").status_code == 200
    resp = clients["owner"].patch(f"/api/instances/{inst.id}", {"ports": [{"name": "game", "host_port": 1}]}, format="json")
    assert resp.status_code == 400


# --- config files ------------------------------------------------------------------------------------------
def test_config_api(clients, factorio, fake_agent):
    d = games.get("factorio")
    fake_agent.put_file(
        factorio.id,
        "config/server-settings.json",
        d.initial_files(factorio, {"settings": {"server-settings": {"game_password": "pw"}}})["config/server-settings.json"],
    )
    resp = clients["viewer"].get(f"/api/instances/{factorio.id}/config")
    assert resp.status_code == 200
    files = {f["id"]: f for f in resp.json()["files"]}
    server = files["server-settings"]
    assert server["exists"] and server["values"]["game_password"] == codecs.MASK and server["redacted_keys"] == ["game_password"]
    assert server["values"]["visibility.public"] is False and server["values"]["max_players"] == 0
    assert files["map-settings"]["exists"] is False and files["map-settings"]["values"]["pollution.enabled"] is True
    url = f"/api/instances/{factorio.id}/config/server-settings"
    assert clients["viewer"].patch(url, {"values": {"max_players": 4}}, format="json").status_code == 403
    resp = clients["owner"].patch(url, {"values": {"max_players": 4, "game_password": codecs.MASK}}, format="json")
    assert resp.status_code == 200 and resp.json()["changed"] == ["max_players"]
    stored = json.loads(fake_agent.text(factorio.id, "config/server-settings.json"))
    assert stored["max_players"] == 4 and stored["game_password"] == "pw"  # the masked secret was not written back
    for bad in ({"max_players": -1}, {"nope": 1}, {"visibility.public": "maybe"}):
        assert clients["owner"].patch(url, {"values": bad}, format="json").status_code == 400, bad
    # a file that does not exist yet is created with the changes
    resp = clients["owner"].patch(
        f"/api/instances/{factorio.id}/config/map-settings", {"values": {"pollution.enabled": False}}, format="json"
    )
    assert resp.status_code == 200
    assert json.loads(fake_agent.text(factorio.id, "config/map-settings.json")) == {"pollution": {"enabled": False}}
    # Minecraft has its own Properties page
    mc_id = Instance.objects.create(
        name="mc", node=factorio.node, game_port=25570, image="itzg/minecraft-server:stable-java21"
    ).id
    assert err(clients["owner"].get(f"/api/instances/{mc_id}/config")) == "unsupported_by_game"


def test_control_secret_file_is_masked_and_read_only(clients, factorio, fake_agent):
    fake_agent.put_file(factorio.id, "config/rconpw", "topsecret\n")
    resp = clients["owner"].get(f"/api/instances/{factorio.id}/files/text", {"path": "config/rconpw"})
    assert resp.status_code == 200 and "topsecret" not in resp.content.decode() and resp.json()["redacted"] is True
    resp = clients["owner"].put(
        f"/api/instances/{factorio.id}/files/text", {"path": "config//rconpw", "content": "x"}, format="json"
    )
    assert resp.status_code == 403 and err(resp) == "managed_file"
    assert fake_agent.text(factorio.id, "config/rconpw") == "topsecret\n"


# --- secrets and setup steps (with a game that has them) --------------------------------------------------
class SetupGame(GameDefinition):
    id = "setupgame"
    label = "Setup Game"
    profile = "setupgame"
    memory_model = "plain"
    ports = (PortSpec("game", "Game", 7777, "tcp", 7777),)
    secret_specs = (SecretSpec("token", "Token", required=False),)
    setup_steps = (SetupStep("prepare", "Prepare", kind="auto"), SetupStep("claim", "Claim", kind="secret", secret="token"))

    def __init__(self) -> None:
        self.ran: list[tuple[str, str]] = []

    def image(self, instance: Instance) -> str:
        return "example/setupgame:1"

    def run_setup_step(self, instance: Instance, step: SetupStep, ctx: Any) -> str:
        self.ran.append((step.id, instance.secret_values.get("token", "")))
        return f"{step.id} ok"


@pytest.fixture
def setupgame(fake_agent: Any) -> Any:
    definition = games.register(SetupGame())
    fake_agent.add_game("setupgame", ["example/setupgame"])
    cache.clear()
    yield definition
    games.unregister(definition.id)


def test_secrets_and_setup_steps(clients, make_instance, setupgame, fake_agent, django_capture_on_commit_callbacks):
    inst = make_instance(
        game="setupgame",
        server_type="",
        game_port=7777,
        ports=[{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7777}],
    )
    data = clients["owner"].get(f"/api/instances/{inst.id}").json()
    assert data["setup"]["needed"] is True and data["setup"]["next"] == "prepare"
    # the install job runs the automatic step; the secret step waits for its secret
    job = run("install", inst, django_capture_on_commit_callbacks, start=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert setupgame.ran == [("prepare", "")]
    inst.refresh_from_db()
    status = setupgame.setup_status(inst)
    assert status["next"] == "claim" and status["steps"][0]["status"] == "done"
    # secrets: never returned, only whether they are set; setting one runs the step waiting for it
    url = f"/api/instances/{inst.id}/secrets"
    assert clients["viewer"].get(url).status_code == 403
    assert clients["owner"].get(url).json() == {
        "secrets": [{"key": "token", "label": "Token", "help": "", "required": False, "link": "", "set": False}]
    }
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].put(url, {"values": {"token": "abc"}}, format="json")
    assert resp.status_code == 200 and resp.json()["secrets"][0]["set"] is True and "abc" not in resp.content.decode()
    assert setupgame.ran[-1] == ("claim", "abc")
    inst.refresh_from_db()
    assert setupgame.setup_status(inst)["needed"] is False and inst.secret_values == {"token": "abc"}
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT secrets FROM instances_instance WHERE id = %s", [inst.pk])
        stored = cur.fetchone()[0]
    assert stored and "abc" not in stored  # encrypted at rest
    from apps.audit.models import AuditEvent

    event = AuditEvent.objects.filter(action="instance.secrets_updated").latest("id")
    assert event.details == {"keys": ["token"]}
    assert clients["owner"].put(url, {"values": {"nope": "x"}}, format="json").status_code == 400
    # nothing left to do
    resp = clients["owner"].post(f"/api/instances/{inst.id}/setup", {}, format="json")
    assert resp.status_code == 409 and err(resp) == "nothing_to_do"


# --- first start retry -----------------------------------------------------------------------------------
def test_first_start_retries_retryable_exits(make_instance, fake_agent, monkeypatch):
    from apps.instances import game_handlers

    class RetryGame(SetupGame):
        id = "retrygame"
        setup_steps = ()
        retryable_install_exits = frozenset({5, 8})

    definition = games.register(RetryGame())
    try:
        inst = make_instance(game="retrygame", server_type="", game_port=7790)
        answers = iter(
            [
                {"exists": True, "running": False, "exit_code": 5, "started_at": "t1"},
                {"exists": True, "running": False, "exit_code": 8, "started_at": "t2"},
                {"exists": True, "running": True, "health": "healthy"},
            ]
        )

        class Client:
            started = 0

            def status(self, _iid: Any) -> dict[str, Any]:
                return next(answers)

            def start(self, _iid: Any) -> None:
                Client.started += 1

        class Ctx:
            logs: list[str] = []

            def check_cancelled(self) -> None: ...

            def log(self, line: str) -> None:
                self.logs.append(line)

            def progress(self, *a: Any, **k: Any) -> None: ...

            def sleep_until(self, _t: float) -> None: ...

        ctx = Ctx()
        game_handlers.wait_first_start(inst, ctx, Client(), span=(0.9, 0.05))
        assert Client.started == 2 and ctx.logs[-1] == "The server is ready"
        # the last allowed attempt fails the job; another exit code fails at once
        answers = iter([{"exists": True, "running": False, "exit_code": 5, "started_at": f"t{i}"} for i in range(5)])
        with pytest.raises(Exception, match="failed 3 times"):
            game_handlers.wait_first_start(inst, ctx, Client(), span=(0.9, 0.05))
        answers = iter([{"exists": True, "running": False, "exit_code": 1, "started_at": "x"}])
        with pytest.raises(Exception, match="exit code 1"):
            game_handlers.wait_first_start(inst, ctx, Client(), span=(0.9, 0.05))
    finally:
        games.unregister(definition.id)


# --- commands, broadcast, players --------------------------------------------------------------------------
def test_quit_goes_through_stop(clients, factorio, fake_agent):
    start_container(fake_agent, factorio)
    resp = clients["owner"].post(f"/api/instances/{factorio.id}/command", {"command": "/quit"}, format="json")
    assert resp.status_code == 400 and err(resp) == "stop_via_power"
    resp = clients["owner"].post(f"/api/instances/{factorio.id}/command", {"command": "/players online"}, format="json")
    assert resp.status_code == 200 and resp.json()["command"] == "/players online"
    assert fake_agent.commands[-1] == (str(factorio.id), "/players online")
    sched = {"name": "q", "action": "command", "cron": "0 4 * * *", "payload": {"command": "/quit"}}
    resp = clients["owner"].post(f"/api/instances/{factorio.id}/schedules", sched, format="json")
    assert resp.status_code == 400


def test_broadcast_is_a_chat_line(factorio, fake_agent):
    from apps.nodes.client import AgentClient

    start_container(fake_agent, factorio)
    games.get("factorio").broadcast(AgentClient(factorio.node), factorio, "/Restart in 5 minutes", message="sorry\nall")
    assert fake_agent.commands[-1] == (str(factorio.id), "Restart in 5 minutes - sorry all")


def test_players(clients, factorio, fake_agent):
    fake_agent.put_file(factorio.id, "config/server-whitelist.json", '["alice"]')
    fake_agent.put_file(factorio.id, "config/server-banlist.json", '[{"username": "troll", "reason": "spam"}, "old"]')
    url = f"/api/instances/{factorio.id}/players"
    data = clients["viewer"].get(url).json()
    assert data["whitelist"] == [{"name": "alice"}] and data["ops"] == [] and data["running"] is False
    assert data["bans"] == [{"name": "troll", "reason": "spam"}, {"name": "old", "reason": ""}]
    assert data["labels"]["ops"] == "Admins"
    # stopped: the files are edited
    assert clients["owner"].post(url, {"action": "op", "name": "bob.builder-1"}, format="json").status_code == 200
    assert json.loads(fake_agent.text(factorio.id, "config/server-adminlist.json")) == ["bob.builder-1"]
    resp = clients["owner"].post(url, {"action": "ban", "name": "eve", "reason": "x"}, format="json")
    assert resp.status_code == 200
    assert json.loads(fake_agent.text(factorio.id, "config/server-banlist.json"))[-1] == {"username": "eve", "reason": "x"}
    assert clients["owner"].post(url, {"action": "kick", "name": "eve"}, format="json").status_code == 409
    assert clients["owner"].post(url, {"action": "op", "name": "bad name"}, format="json").status_code == 400
    # running: RCON (the server writes the files itself)
    start_container(fake_agent, factorio)
    fake_agent.command_handler = lambda _iid, cmd: "Online players (1):\n  alice (online)\n" if cmd == "/players online" else None
    resp = clients["owner"].post(url, {"action": "whitelist_add", "name": "carol"}, format="json")
    assert resp.status_code == 200 and resp.json()["via"] == "rcon"
    assert fake_agent.commands[-1] == (str(factorio.id), "/whitelist add carol")
    cache.clear()
    assert clients["viewer"].get(url).json()["online"] == {"online": 1, "max": None, "players": ["alice"]}


# --- software update ----------------------------------------------------------------------------------------
def test_software_update(clients, factorio, fake_agent, factorio_versions, django_capture_on_commit_callbacks):
    fake_agent.put_file(factorio.id, "saves/world.zip", b"zip")
    url = f"/api/instances/{factorio.id}/software"
    resp = clients["owner"].post(url, {"version": "2.0.77"}, format="json")
    assert resp.status_code == 409 and err(resp) == "downgrade_unconfirmed"
    assert err(clients["owner"].post(url, {"version": "2.1.23"}, format="json")) == "same_version"
    assert err(clients["owner"].post(url, {"version": "3.0.0"}, format="json")) == "invalid_version"
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(url, {"version": "2.1.24", "backup": True}, format="json")
    assert resp.status_code == 202
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    factorio.refresh_from_db()
    assert factorio.minecraft_version == "2.1.24" and factorio.image == "factoriotools/factorio:2.1.24-rootless"
    from apps.backups.models import Backup

    backup = Backup.objects.get(pk=job.result["backup_id"])
    assert backup.meta["software"] == {
        "game": "factorio",
        "version": "2.1.23",
        "image": "factoriotools/factorio:2.1.23-rootless",
        "space_age": False,
    }
    assert job.result["after"]["version"] == "2.1.24"


def test_runtime_images_setting(clients, factorio):
    url = "/api/system/settings"
    assert (
        clients["owner"]
        .patch(url, {"values": {"runtime_images": {"minecraft": {"repository": "x/y"}}}}, format="json")
        .status_code
        == 400
    )
    assert (
        clients["owner"]
        .patch(url, {"values": {"runtime_images": {"factorio": {"repository": "Bad Repo"}}}}, format="json")
        .status_code
        == 400
    )
    resp = clients["owner"].patch(
        url, {"values": {"runtime_images": {"factorio": {"repository": "registry.local/factorio"}}}}, format="json"
    )
    assert resp.status_code == 200 and resp.json()["values"]["runtime_images"] == {
        "factorio": {"repository": "registry.local/factorio"}
    }
    assert build_spec(factorio)["image"] == "registry.local/factorio:2.1.23-rootless"


# --- saves --------------------------------------------------------------------------------------------------
def save_zip(name: str = "world") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/level.dat0", b"x")
        zf.writestr(f"{name}/level-init.dat", b"x")
    return buf.getvalue()


def test_saves_list_and_reset(clients, factorio, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(factorio.id, "saves/world.zip", b"current")
    fake_agent.put_file(factorio.id, "saves/_autosave1.zip", b"auto")
    fake_agent.fs[str(factorio.id)]["mtime"]["saves/_autosave1.zip"] -= 60  # older than world.zip (the loaded save)
    fake_agent.put_file(factorio.id, "config/map-gen-settings.json", json.dumps({"seed": None, "width": 0}))
    start_container(fake_agent, factorio)
    data = clients["viewer"].get(f"/api/instances/{factorio.id}/saves").json()
    assert data["current"] == "world" and [s["name"] for s in data["saves"]] == ["world", "_autosave1"]
    assert data["saves"][0]["current"] is True and data["options"] == {"space_age": False} and data["set_aside"] == []
    url = f"/api/instances/{factorio.id}/saves/reset"
    assert clients["owner"].post(url, {"confirm_name": "nope"}, format="json").status_code == 400
    assert clients["viewer"].post(url, {"confirm_name": "Factory"}, format="json").status_code == 403
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            url, {"confirm_name": "Factory", "seed": 42, "settings": {"peaceful_mode": True}, "backup": True}, format="json"
        )
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert (
        job.result["restarted"] is True
        and job.result["backup_id"]
        and sorted(job.result["set_aside"]) == ["_autosave1.zip", "world.zip"]
    )
    folder = job.result["folder"]
    assert folder.startswith("saves-replaced/") and fake_agent.get_file(factorio.id, f"{folder}/world.zip") == b"current"
    assert fake_agent.get_file(factorio.id, "saves/world.zip") is None  # the server creates the new map
    assert json.loads(fake_agent.text(factorio.id, "config/map-gen-settings.json")) == {
        "seed": 42,
        "width": 0,
        "peaceful_mode": True,
    }
    assert fake_agent.containers[str(factorio.id)]["running"]
    data = clients["viewer"].get(f"/api/instances/{factorio.id}/saves").json()
    assert data["set_aside"] == [{"folder": folder, "files": ["_autosave1.zip", "world.zip"]}]


def test_saves_import_and_export(clients, factorio, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(factorio.id, "saves/world.zip", b"current")
    url = f"/api/instances/{factorio.id}/saves/import"

    def upload(data: bytes, name: str = "mine.zip", confirm: str = "Factory") -> Any:
        f = io.BytesIO(data)
        f.name = name
        return clients["owner"].post(url, {"file": f, "confirm_name": confirm, "backup": "false"}, format="multipart")

    assert upload(b"not a zip").status_code == 400
    assert upload(save_zip(), name="mine.txt").status_code == 400
    assert upload(save_zip(), confirm="x").status_code == 400
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("readme.txt", "x")
    assert upload(plain.getvalue()).status_code == 400
    with django_capture_on_commit_callbacks(execute=True):
        resp = upload(save_zip("other"))
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["backup_id"] is None and job.result["restarted"] is False
    assert fake_agent.get_file(factorio.id, "saves/world.zip") == save_zip("other")
    assert fake_agent.get_file(factorio.id, f"{job.result['folder']}/world.zip") == b"current"
    assert not [p for p in fake_agent.fs[str(factorio.id)]["files"] if p.startswith(".mcpanel-import-")]
    resp = clients["owner"].get(f"/api/instances/{factorio.id}/saves/export")
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == save_zip("other")
    assert "Factory-world.zip" in resp["Content-Disposition"]


def test_saves_need_their_capability(clients, make_instance):
    mc = make_instance()
    assert err(clients["owner"].get(f"/api/instances/{mc.id}/saves")) == "unsupported_by_game"


# --- backups ------------------------------------------------------------------------------------------------
def test_backups_leave_out_the_games_excludes(factorio, make_instance, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(factorio.id, "saves/world.zip", b"x")
    fake_agent.put_file(factorio.id, "saves/world.tmp.zip", b"partial")
    job = run("backup", factorio, django_capture_on_commit_callbacks, name="b")
    assert job.status == JobStatus.SUCCEEDED, job.error
    _args, kwargs = fake_agent.called("create_backup")[-1]
    assert "*.tmp.zip" in kwargs["excludes"]
    mc = make_instance()
    fake_agent.put_file(mc.id, "server.properties", "motd=x\n")
    run("backup", mc, django_capture_on_commit_callbacks, name="b")
    _args, kwargs = fake_agent.called("create_backup")[-1]
    assert "*.tmp.zip" not in kwargs["excludes"]


def test_clone_keeps_the_game_and_gets_its_own_port(clients, factorio, fake_agent):
    resp = clients["owner"].post(f"/api/instances/{factorio.id}/clone", {"name": "Copy"}, format="json")
    assert resp.status_code == 202, resp.json()
    copy = Instance.objects.get(name="Copy")
    assert copy.game == "factorio" and copy.game_options == factorio.game_options
    assert copy.ports[0]["protocol"] == "udp" and copy.ports[0]["host_port"] == 34198 and copy.game_port == 34198
