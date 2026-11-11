"""Terraria (vanilla, tModLoader, TShock) on the panel: the definition (variants, capabilities, catalog,
versions from Docker Hub, spec, serverconfig.txt and TShock's config.json with the panel-owned keys),
create, config/secrets/setup APIs, console rules, players, saves (worlds) and the software update,
against the in-memory fake agent."""

from __future__ import annotations

import io
import json
import struct
from typing import Any

import pytest
from django.core.cache import cache

from apps import games
from apps.games import codecs, terraria
from apps.games.terraria import TerrariaDefinition, parse_banlist, parse_playing, version_key
from apps.instances.models import Instance
from apps.instances.spec import build_spec, current_spec_hash
from apps.jobs.models import Job, JobStatus

VERSIONS = {
    "vanilla": [
        {"id": "1.4.5.8", "label": "1.4.5.8", "stable": True, "latest": True},
        {"id": "1.4.5.7", "label": "1.4.5.7", "stable": True, "latest": False},
    ],
    "tmodloader": [
        {"id": "2026.10.2.3", "label": "2026.10.2.3 (Terraria 1.4.4.9)", "stable": False, "latest": False},
        {"id": "2026.09.3.1", "label": "2026.09.3.1 (Terraria 1.4.4.9)", "stable": True, "latest": True},
    ],
    "tshock": [{"id": "1.4.5.8-6.2.1", "label": "TShock 6.2.1 (Terraria 1.4.5.8)", "stable": True, "latest": True}],
}
REPO = {"vanilla": "passivelemon/terraria-docker", "tmodloader": "passivelemon/terraria-docker", "tshock": "ryshe/terraria"}


@pytest.fixture
def tr_versions(db: Any) -> dict[str, Any]:
    for variant, versions in VERSIONS.items():
        cache.set(f"catalog:terraria:versions:{variant}:{REPO[variant]}", versions, 3600)
    return VERSIONS


def make_tr(make_instance: Any, variant: str = "vanilla", **over: Any) -> Instance:
    version = {"vanilla": "1.4.5.7", "tmodloader": "2026.09.3.1", "tshock": "1.4.5.8-6.2.1"}[variant]
    options: dict[str, Any] = {"world_name": "world"}
    if variant == "tshock":
        options["owner_account"] = "owner"
    if variant == "tmodloader":
        options["modpack"] = "mcpanel"
    fields = {
        "name": f"Terra {variant}",
        "game": "terraria",
        "server_type": variant,
        "minecraft_version": version,
        "image": f"{REPO[variant]}:{terraria.TAG_PREFIX[variant]}{version}",
        "memory_mb": 1536,
        "stop_timeout": 60,
        "jvm_flags_preset": "none",
        "game_port": 7777,
        "ports": [{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7777}],
        "game_options": options,
        "rcon_password": "t" * 32,
    }
    fields.update(over)
    return make_instance(**fields)


@pytest.fixture
def vanilla(make_instance: Any) -> Instance:
    return make_tr(make_instance)


@pytest.fixture
def tshock(make_instance: Any) -> Instance:
    return make_tr(
        make_instance,
        "tshock",
        game_port=7778,
        ports=[{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7778}],
    )


def err(resp: Any) -> str:
    return resp.json()["error"]["code"]


def start_container(fake_agent: Any, inst: Instance, *, running: bool = True) -> None:
    fake_agent.apply(inst.id, build_spec(inst), start=running)


def wld(version: int = 279) -> bytes:
    """The start of a Terraria world file: version, "relogic", file type 2 (world)."""
    return struct.pack("<i", version) + b"relogic" + bytes([2]) + b"\x00" * 64


# --- the definition ---------------------------------------------------------------------------------------------
def test_terraria_is_registered_with_variants():
    d = games.get("terraria")
    assert isinstance(d, TerrariaDefinition) and [v.id for v in d.variants] == ["vanilla", "tmodloader", "tshock"]
    assert "query" in d.capabilities() and d.memory_model == "plain"
    assert d.default_memory_mb_for("tmodloader") == 3072 and d.default_memory_mb_for("vanilla") == 1536
    assert d.clean_exit_codes == frozenset({0, 143})
    assert games.get("factorio").clean_exit_codes == frozenset({0})


def test_capabilities_per_variant(vanilla, tshock):
    d = games.get("terraria")
    assert "query" not in d.capabilities(vanilla) and "query" in d.capabilities(tshock)
    assert {"console_commands", "players", "world_reset", "config_files", "software_update"} <= d.capabilities(vanilla)


def test_catalog_entry(clients):
    entry = next(g for g in clients["viewer"].get("/api/catalog/games").json() if g["id"] == "terraria")
    variants = {v["id"]: v for v in entry["variants"]}
    assert variants["tmodloader"]["default_memory_mb"] == 3072 and variants["tmodloader"]["game_version"] == "1.4.4.9"
    assert any("cannot join" in n for n in variants["tmodloader"]["notes"])
    assert "query" in variants["tshock"]["capabilities"] and "query" not in variants["vanilla"]["capabilities"]
    assert variants["tshock"]["stop_words"] == ["exit", "exit-nosave", "off", "off-nosave"]
    assert entry["ports"] == [
        {
            "name": "game",
            "label": "Game",
            "container_port": 7777,
            "protocol": "tcp",
            "default_host_port": 7777,
            "help": entry["ports"][0]["help"],
        }
    ]
    files = {(f["id"], tuple(f["variants"])): f for f in entry["config_files"]}
    assert ("tshock", ("tshock",)) in files and ("serverconfig", ("vanilla",)) in files
    assert [f["key"] for f in files[("serverconfig", ("tshock",))]["fields"]] == ["autocreate", "difficulty", "seed"]
    assert {o["key"]: o.get("variants") for o in entry["options"]} == {"world_name": None, "owner_account": ["tshock"]}
    assert entry["secrets"][0]["variants"] == ["tshock"] and entry["setup_steps"][0]["id"] == "owner_account"
    assert entry["world"]["extension"] == ".wld" and [f["key"] for f in entry["world"]["reset_fields"]] == [
        "autocreate",
        "difficulty",
        "world_seed",
    ]
    assert entry["has_versions"] is True and entry["player_list_labels"] == {"bans": "Bans (by IP)"}
    # the other games' entries have no variant data of this kind
    factorio = next(g for g in clients["viewer"].get("/api/catalog/games").json() if g["id"] == "factorio")
    assert factorio["variants"] == [] and "variants" not in factorio["config_files"][0]


def test_versions_from_docker_hub(clients, monkeypatch):
    pages = {
        "passivelemon/terraria-docker:terraria": [
            {"name": "terraria-latest", "digest": "sha256:a"},
            {"name": "terraria-1.4.5.8", "digest": "sha256:a"},
            {"name": "terraria-1.4.5.7", "digest": "sha256:b"},
            {"name": "terraria-1.4.5.10", "digest": "sha256:c"},
        ],
        "passivelemon/terraria-docker:tmodloader": [
            {"name": "tmodloader-latest", "digest": "sha256:s"},
            {"name": "tmodloader-latest-pre", "digest": "sha256:p"},
            {"name": "tmodloader-2026.09.3.1", "digest": "sha256:s"},
            {"name": "tmodloader-2026.10.2.3", "digest": "sha256:p"},
            {"name": "tmodloader-2026", "digest": "sha256:s"},
        ],
        "ryshe/terraria:tshock": [
            {"name": "tshock-latest", "digest": "sha256:t"},
            {"name": "tshock-1.4.5.8-6.2.1", "digest": "sha256:t"},
            {"name": "tshock-1.4.5.6-6.1.0", "digest": "sha256:u"},
            {"name": "tshock-1.4.4.9-5.2.4", "digest": "sha256:v"},
            {"name": "tshock-1.4.4.9-5.2.0-3", "digest": "sha256:w"},
        ],
    }
    asked: list[str] = []

    def fake_get(url: str, params: Any = None) -> Any:
        repo = url.split("/repositories/")[1].removesuffix("/tags")
        asked.append(f"{repo}:{params['name']}")
        return {"results": pages[f"{repo}:{params['name']}"], "next": None}

    monkeypatch.setattr("apps.catalog.providers._get", fake_get)
    cache.clear()
    data = clients["viewer"].get("/api/catalog/games/terraria/versions?variant=vanilla").json()
    assert [v["id"] for v in data["versions"]] == ["1.4.5.10", "1.4.5.8", "1.4.5.7"] and data["latest"] == "1.4.5.8"
    data = clients["viewer"].get("/api/catalog/games/terraria/versions?variant=tmodloader").json()
    assert data["latest"] == "2026.09.3.1"
    assert {v["id"]: v["stable"] for v in data["versions"]} == {"2026.10.2.3": False, "2026.09.3.1": True}
    assert data["versions"][0]["label"] == "2026.10.2.3 (Terraria 1.4.4.9)"
    data = clients["viewer"].get("/api/catalog/games/terraria/versions?variant=tshock").json()
    # TShock 6 and later only (the layout the agent's unprivileged profile runs)
    assert [v["id"] for v in data["versions"]] == ["1.4.5.8-6.2.1", "1.4.5.6-6.1.0"]
    assert data["versions"][0]["label"] == "TShock 6.2.1 (Terraria 1.4.5.8)" and data["latest"] == "1.4.5.8-6.2.1"
    assert asked == ["passivelemon/terraria-docker:terraria", "passivelemon/terraria-docker:tmodloader", "ryshe/terraria:tshock"]


def test_version_order():
    assert version_key("1.4.5.10") > version_key("1.4.5.8")
    assert version_key("1.4.5.8-6.2.1") > version_key("1.4.5.6-6.1.0")
    assert games.get("terraria").version_key("2026.10.2.3") > games.get("terraria").version_key("2026.09.3.1")


def test_spec_per_variant(vanilla, tshock, make_instance):
    spec = build_spec(vanilla)
    assert spec["game"] == "terraria" and spec["variant"] == "vanilla"
    assert spec["image"] == "passivelemon/terraria-docker:terraria-1.4.5.7"
    assert spec["ports"] == [{"container_port": 7777, "host_port": 7777, "protocol": "tcp", "host_ip": "0.0.0.0"}]
    assert set(spec["env"]) <= {"TZ"}  # serverconfig.txt carries the settings; the image's variables are refused
    assert build_spec(tshock)["image"] == "ryshe/terraria:tshock-1.4.5.8-6.2.1"
    tml = make_tr(
        make_instance,
        "tmodloader",
        game_port=7779,
        ports=[{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7779}],
    )
    assert build_spec(tml)["image"] == "passivelemon/terraria-docker:tmodloader-2026.09.3.1"
    # its start script exits 0 without MODPACK (the agent's profile accepts exactly this name pattern)
    assert build_spec(tml)["env"]["MODPACK"] == "mcpanel" and "MODPACK" not in spec["env"]


def test_console_rules():
    d = games.get("terraria")
    assert d.normalise_command("/playing ") == "playing"
    for word in ("exit", "/exit", "EXIT", "off", "exit-nosave"):
        assert d.is_stop_command(word)
    assert not d.is_stop_command("say exit")


def test_parse_playing_and_banlist():
    out = ": Ann (172.18.0.1:52344)\n: Bob Two (10.0.0.5:1)\n2 players connected.\n"
    assert parse_playing(out) == {"online": 2, "max": None, "players": ["Ann", "Bob Two"]}
    assert parse_playing(": No players connected.") == {"online": 0, "max": None, "players": []}
    assert parse_playing("playing\n: 1 player connected.\n")["online"] == 1
    assert parse_banlist("//Troll\n10.0.0.9\n//Other\n10.0.0.7\n") == ["Troll", "Other"]


# --- create ---------------------------------------------------------------------------------------------------------
def tr_body(variant: str = "vanilla", **extra: Any) -> dict[str, Any]:
    version = {"vanilla": "1.4.5.8", "tmodloader": "2026.09.3.1", "tshock": "1.4.5.8-6.2.1"}[variant]
    return {"game": "terraria", "variant": variant, "name": f"Terra {variant}", "version": version, **extra}


def test_create_vanilla(clients, fake_agent, tr_versions, django_capture_on_commit_callbacks):
    body = tr_body(
        options={"world_name": "Hollow_1"},
        settings={"serverconfig": {"autocreate": "1", "maxplayers": 4, "password": "pw", "motd": "hi", "seed": "for the worthy"}},
    )
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body, format="json")
    assert resp.status_code == 201, resp.json()
    data = resp.json()["instance"]
    assert data["game"] == "terraria" and data["server_type"] == "vanilla" and data["setup"] is None
    assert data["game_options"] == {"world_name": "Hollow_1"} and data["memory_mb"] == 1536
    assert data["ports"][0]["protocol"] == "tcp" and data["ports"][0]["host_port"] == 7777
    inst = Instance.objects.get(pk=data["id"])
    cfg = codecs.get("kv").parse(fake_agent.text(inst.id, "serverconfig.txt"))
    assert cfg["world"] == "/opt/terraria/config/Worlds/Hollow_1.wld" and cfg["worldpath"] == "/opt/terraria/config/Worlds"
    assert cfg["worldname"] == "Hollow_1" and cfg["port"] == "7777" and cfg["banlist"] == "/opt/terraria/config/banlist.txt"
    assert cfg["autocreate"] == "1" and cfg["maxplayers"] == "4" and cfg["password"] == "pw" and cfg["seed"] == "for the worthy"
    assert "modpack" not in cfg
    spec = fake_agent.containers[str(inst.id)]["spec"]
    assert spec["variant"] == "vanilla" and spec["image"] == "passivelemon/terraria-docker:terraria-1.4.5.8"
    inst.refresh_from_db()
    assert inst.container_spec_hash == current_spec_hash(inst)


def test_create_tmodloader(clients, fake_agent, tr_versions, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", tr_body("tmodloader"), format="json")
    assert resp.status_code == 201, resp.json()
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    assert inst.memory_mb == 3072 and inst.game_options == {"world_name": "world", "modpack": "mcpanel"}
    assert fake_agent.text(inst.id, "ModPacks/mcpanel/Mods/enabled.json") == "[]\n"
    cfg = codecs.get("kv").parse(fake_agent.text(inst.id, "serverconfig.txt"))
    assert cfg["modpack"] == "/opt/terraria/config/ModPacks/mcpanel/Mods/enabled.json"
    assert cfg["modpath"] == "/opt/terraria/config/ModPacks/mcpanel/Mods/" and cfg["autocreate"] == "2"


def test_create_tshock_with_its_owner_account(clients, fake_agent, tr_versions, django_capture_on_commit_callbacks, monkeypatch):
    actions: list = []
    monkeypatch.setattr(
        terraria, "_game_action", lambda client, inst, action, params: actions.append((action, params)) or {"created": True}
    )
    body = tr_body(
        "tshock",
        options={"owner_account": "Boss"},
        secrets={"owner_password": "Owner-pw-1"},
        settings={"tshock": {"Settings.MaxSlots": 6}},
    )
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body, format="json")
    assert resp.status_code == 201, resp.json()
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    config = json.loads(fake_agent.text(inst.id, "config.json"))["Settings"]
    assert config["RestApiEnabled"] is True and config["RestApiPort"] == 7878 and config["LogRest"] is False
    assert config["ApplicationRestTokens"] == {inst.rcon_password: {"Username": "mcpanel", "UserGroupName": "superadmin"}}
    assert config["ServerPort"] == 7777 and config["MaxSlots"] == 6
    cfg = codecs.get("kv").parse(fake_agent.text(inst.id, "serverconfig.txt"))
    assert cfg["world"] == "/config/Worlds/world.wld" and "banlist" not in cfg
    # the setup job created the owner account through the agent (the password only travels there)
    assert actions == [("owner_account", {"user": "Boss", "password": "Owner-pw-1", "group": "owner"})]
    inst.refresh_from_db()
    setup = games.get("terraria").setup_status(inst)
    assert setup is not None and setup["needed"] is False and setup["steps"][0]["status"] == "done"
    job = Job.objects.filter(instance=inst, kind="setup").get()
    assert job.status == JobStatus.SUCCEEDED and "Owner-pw-1" not in json.dumps(job.log)


def test_create_validation(clients, fake_agent, tr_versions):
    post = lambda body: clients["owner"].post("/api/instances/", body, format="json")  # noqa: E731
    resp = post({**tr_body(), "variant": "calamity"})
    assert resp.status_code == 400 and "variant" in resp.json()["error"]["details"]["fields"]
    assert err(post({**tr_body(), "variant": ""})) == "invalid_variant"
    assert err(post(tr_body(version="2026.09.3.1"))) == "invalid_version"  # a tModLoader version for vanilla
    assert err(post(tr_body("tshock", version="1.4.5.8"))) == "invalid_version"
    assert post(tr_body(options={"world_name": "../x"})).status_code == 400
    assert post(tr_body("tshock", options={"owner_account": "a b"})).status_code == 400
    assert post(tr_body(settings={"serverconfig": {"port": 1}})).status_code == 400  # managed by the panel
    assert post(tr_body(env={"WORLDNAME": "x"})).status_code == 400
    assert post(tr_body(memory_mb=600)).status_code == 400


def test_create_needs_the_variants_profile(clients, fake_agent, tr_versions):
    fake_agent.game_profiles["terraria"]["variants"] = ["vanilla", "tmodloader"]  # an agent without TShock
    resp = clients["owner"].post("/api/instances/", tr_body("tshock"), format="json")
    assert resp.status_code == 409 and err(resp) == "agent_lacks_game"
    assert clients["owner"].post("/api/instances/", tr_body(), format="json").status_code == 201


# --- before every apply ------------------------------------------------------------------------------------------------
def test_apply_rewrites_the_panel_owned_keys(vanilla, tshock, fake_agent):
    fake_agent.put_file(vanilla.id, "serverconfig.txt", "# mine\nworld=/etc/passwd\nport=1\nmotd=keep me\n")
    start_container(fake_agent, vanilla)
    from apps.nodes.client import AgentClient

    games.get("terraria").before_apply(vanilla, AgentClient(vanilla.node))
    text = fake_agent.text(vanilla.id, "serverconfig.txt")
    cfg = codecs.get("kv").parse(text)
    assert text.startswith("# mine\n") and cfg["motd"] == "keep me"
    assert cfg["world"] == "/opt/terraria/config/Worlds/world.wld" and cfg["port"] == "7777" and cfg["autocreate"] == "2"
    # TShock: a changed or extra token is replaced by the panel's
    fake_agent.put_file(
        tshock.id,
        "config.json",
        json.dumps({"Settings": {"MaxSlots": 3, "ApplicationRestTokens": {"evil": {"Username": "x"}}, "RestApiEnabled": False}}),
    )
    games.get("terraria").before_apply(tshock, AgentClient(tshock.node))
    settings = json.loads(fake_agent.text(tshock.id, "config.json"))["Settings"]
    assert settings["MaxSlots"] == 3 and settings["RestApiEnabled"] is True
    assert settings["ApplicationRestTokens"] == {tshock.rcon_password: {"Username": "mcpanel", "UserGroupName": "superadmin"}}


def test_apply_refuses_a_broken_tshock_config(tshock, fake_agent):
    from apps.core.errors import ConflictError
    from apps.nodes.client import AgentClient

    fake_agent.put_file(tshock.id, "config.json", "{broken")
    with pytest.raises(ConflictError) as exc:
        games.get("terraria").before_apply(tshock, AgentClient(tshock.node))
    assert "config.json" in str(exc.value.detail)


# --- config, secrets, file editor ---------------------------------------------------------------------------------------
def test_config_api_per_variant(clients, vanilla, tshock, fake_agent):
    fake_agent.put_file(vanilla.id, "serverconfig.txt", "world=x\nmaxplayers=8\npassword=secret\n")
    files = clients["viewer"].get(f"/api/instances/{vanilla.id}/config").json()["files"]
    assert [f["id"] for f in files] == ["serverconfig"]
    assert files[0]["values"]["password"] == "********" and files[0]["values"]["maxplayers"] == 8
    url = f"/api/instances/{vanilla.id}/config/serverconfig"
    assert clients["owner"].patch(url, {"values": {"port": "8888"}}, format="json").status_code == 400
    resp = clients["owner"].patch(url, {"values": {"maxplayers": 16, "password": "********"}}, format="json")
    assert resp.status_code == 200 and resp.json()["changed"] == ["maxplayers"]
    assert "password=secret" in fake_agent.text(vanilla.id, "serverconfig.txt")
    assert clients["owner"].patch(f"/api/instances/{vanilla.id}/config/tshock", {"values": {}}, format="json").status_code == 404
    fake_agent.put_file(tshock.id, "config.json", json.dumps({"Settings": {"MaxSlots": 8}}))
    files = clients["viewer"].get(f"/api/instances/{tshock.id}/config").json()["files"]
    assert [f["id"] for f in files] == ["serverconfig", "tshock"]
    assert [f["key"] for f in files[0]["fields"]] == ["autocreate", "difficulty", "seed"]
    url = f"/api/instances/{tshock.id}/config/tshock"
    assert clients["owner"].patch(url, {"values": {"Settings.RestApiEnabled": False}}, format="json").status_code == 400
    assert clients["owner"].patch(url, {"values": {"Settings.MaxSlots": 12}}, format="json").status_code == 200


def test_secrets_per_variant(clients, vanilla, tshock, fake_agent):
    assert clients["owner"].get(f"/api/instances/{vanilla.id}/secrets").json() == {"secrets": []}
    resp = clients["owner"].put(f"/api/instances/{vanilla.id}/secrets", {"values": {"owner_password": "x" * 8}}, format="json")
    assert resp.status_code == 400
    secrets = clients["owner"].get(f"/api/instances/{tshock.id}/secrets").json()["secrets"]
    assert [s["key"] for s in secrets] == ["owner_password"] and secrets[0]["set"] is False
    assert vanilla.game == "terraria" and games.get("terraria").setup_status(vanilla) is None
    assert games.get("terraria").setup_status(tshock)["next"] == "owner_account"


def test_file_editor_hides_the_rest_token(clients, tshock, fake_agent):
    fake_agent.put_file(tshock.id, "config.json", json.dumps({"Settings": {"ApplicationRestTokens": {tshock.rcon_password: {}}}}))
    content = clients["owner"].get(f"/api/instances/{tshock.id}/files/text?path=config.json").json()["content"]
    assert tshock.rcon_password not in content and "********" in content


# --- console, players --------------------------------------------------------------------------------------------------------
def test_stop_words_go_through_stop(clients, vanilla, fake_agent):
    start_container(fake_agent, vanilla)
    url = f"/api/instances/{vanilla.id}/command"
    for word in ("exit", "/off"):
        resp = clients["owner"].post(url, {"command": word}, format="json")
        assert resp.status_code == 400 and err(resp) == "stop_via_power"
    resp = clients["owner"].post(url, {"command": "/playing"}, format="json")
    assert resp.status_code == 200 and fake_agent.commands[-1] == (str(vanilla.id), "playing")


def test_broadcast_is_a_say_line(vanilla, fake_agent):
    from apps.nodes.client import AgentClient

    start_container(fake_agent, vanilla)
    games.get("terraria").broadcast(AgentClient(vanilla.node), vanilla, "/Restart in 5 minutes;", message="sorry\nall")
    assert fake_agent.commands[-1] == (str(vanilla.id), "say Restart in 5 minutes; - sorry all")


def test_vanilla_players(clients, vanilla, fake_agent):
    fake_agent.put_file(vanilla.id, "banlist.txt", "//Troll\n10.0.0.9\n//Other\n10.0.0.7\n")
    url = f"/api/instances/{vanilla.id}/players"
    data = clients["viewer"].get(url).json()
    assert data["bans"] == [{"name": "Troll", "reason": ""}, {"name": "Other", "reason": ""}] and data["running"] is False
    assert data["labels"] == {"bans": "Bans (by IP)"}
    assert clients["owner"].post(url, {"action": "kick", "name": "Ann"}, format="json").status_code == 409
    assert clients["owner"].post(url, {"action": "op", "name": "Ann"}, format="json").status_code == 400
    resp = clients["owner"].post(url, {"action": "pardon", "name": "troll"}, format="json")
    assert resp.status_code == 200 and fake_agent.text(vanilla.id, "banlist.txt") == "//Other\n10.0.0.7\n"
    assert clients["owner"].post(url, {"action": "pardon", "name": "Nobody"}, format="json").status_code == 404
    start_container(fake_agent, vanilla)
    fake_agent.command_handler = lambda _iid, cmd: ": Ann (172.18.0.1:5000)\n1 player connected." if cmd == "playing" else None
    cache.clear()
    assert clients["viewer"].get(url).json()["online"] == {"online": 1, "max": None, "players": ["Ann"]}
    resp = clients["owner"].post(url, {"action": "kick", "name": "Ann"}, format="json")
    assert resp.status_code == 200 and fake_agent.commands[-1] == (str(vanilla.id), "kick Ann")
    assert clients["owner"].post(url, {"action": "ban", "name": 'A"b'}, format="json").status_code == 400


def test_tshock_players(clients, tshock, fake_agent):
    start_container(fake_agent, tshock)
    fake_agent.ping_answer = {"online": 1, "max": 8, "sample": ["Ann"]}
    cache.clear()
    data = clients["viewer"].get(f"/api/instances/{tshock.id}/players").json()
    assert data["online"] == {"online": 1, "max": 8, "players": ["Ann"]} and data["labels"] == {}
    url = f"/api/instances/{tshock.id}/players"
    resp = clients["owner"].post(url, {"action": "ban", "name": "Ann Lee", "reason": "grief"}, format="json")
    assert resp.status_code == 200 and fake_agent.commands[-1] == (str(tshock.id), 'ban add "Ann Lee" grief')
    assert clients["owner"].post(url, {"action": "pardon", "name": "Ann"}, format="json").status_code == 400


def test_metrics_player_count(vanilla, tshock, fake_agent):
    from apps.metrics.tasks import player_count
    from apps.nodes.client import AgentClient

    start_container(fake_agent, tshock)
    fake_agent.ping_answer = {"online": 2, "max": 8, "sample": ["Ann", "Bob"]}
    assert player_count(tshock, AgentClient(tshock.node)) == {"online": 2, "max": 8, "players": ["Ann", "Bob"]}
    start_container(fake_agent, vanilla)
    cache.clear()
    fake_agent.command_handler = lambda _iid, cmd: ": No players connected." if cmd == "playing" else None
    assert player_count(vanilla, AgentClient(vanilla.node))["online"] == 0


# --- saves ----------------------------------------------------------------------------------------------------------------
def test_saves_list_and_reset(clients, vanilla, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(vanilla.id, "Worlds/world.wld", wld())
    fake_agent.put_file(vanilla.id, "Worlds/world.wld.bak", b"bak")
    fake_agent.put_file(vanilla.id, "Worlds/other.wld", wld())
    fake_agent.put_file(vanilla.id, "serverconfig.txt", "autocreate=3\ndifficulty=0\nseed=old\nmotd=keep\n")
    start_container(fake_agent, vanilla)
    data = clients["viewer"].get(f"/api/instances/{vanilla.id}/saves").json()
    assert data["current"] == "world" and [s["name"] for s in data["saves"]] == ["world", "other"]
    url = f"/api/instances/{vanilla.id}/saves/reset"
    body = {
        "confirm_name": vanilla.name,
        "settings": {"autocreate": "1", "difficulty": "2", "world_seed": "not the bees"},
        "backup": True,
    }
    assert clients["owner"].post(url, {**body, "settings": {"autocreate": "9"}}, format="json").status_code == 400
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(url, body, format="json")
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert sorted(job.result["set_aside"]) == ["other.wld", "world.wld", "world.wld.bak"] and job.result["restarted"]
    assert job.result["folder"].startswith("Worlds-replaced/") and fake_agent.get_file(vanilla.id, "Worlds/world.wld") is None
    cfg = codecs.get("kv").parse(fake_agent.text(vanilla.id, "serverconfig.txt"))
    assert (cfg["autocreate"], cfg["difficulty"], cfg["seed"], cfg["motd"]) == ("1", "2", "not the bees", "keep")
    assert any("not the bees" in line for line in job.log) and job.result["seed"] == "not the bees"
    # a reset without a size keeps the configured one (always written explicitly) and a random seed
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(url, {"confirm_name": vanilla.name, "backup": False}, format="json")
    cfg = codecs.get("kv").parse(fake_agent.text(vanilla.id, "serverconfig.txt"))
    assert cfg["autocreate"] == "1" and "seed" not in cfg


def test_saves_import_and_export(clients, vanilla, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(vanilla.id, "Worlds/world.wld", wld(1))
    url = f"/api/instances/{vanilla.id}/saves/import"

    def upload(data: bytes, name: str = "mine.wld", confirm: str | None = None) -> Any:
        f = io.BytesIO(data)
        f.name = name
        return clients["owner"].post(
            url, {"file": f, "confirm_name": confirm or vanilla.name, "backup": "false"}, format="multipart"
        )

    assert upload(b"not a world").status_code == 400
    assert upload(wld(), name="mine.zip").status_code == 400
    assert upload(struct.pack("<i", 279) + b"relogic" + bytes([3]) + b"\x00" * 8).status_code == 400  # a player file
    with django_capture_on_commit_callbacks(execute=True):
        resp = upload(wld(2))
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.get_file(vanilla.id, "Worlds/world.wld") == wld(2)
    assert fake_agent.get_file(vanilla.id, f"{job.result['folder']}/world.wld") == wld(1)
    resp = clients["owner"].get(f"/api/instances/{vanilla.id}/saves/export")
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == wld(2)
    assert resp["Content-Type"] == "application/octet-stream" and "world.wld" in resp["Content-Disposition"]


# --- software, stop, state -------------------------------------------------------------------------------------------------------
def test_software_update(clients, vanilla, tshock, fake_agent, tr_versions, django_capture_on_commit_callbacks):
    fake_agent.put_file(vanilla.id, "Worlds/world.wld", wld())
    url = f"/api/instances/{vanilla.id}/software"
    assert err(clients["owner"].post(url, {"version": "1.4.5.8-6.2.1"}, format="json")) == "invalid_version"
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(url, {"version": "1.4.5.8", "backup": True}, format="json")
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    vanilla.refresh_from_db()
    assert vanilla.image == "passivelemon/terraria-docker:terraria-1.4.5.8"
    from apps.backups.models import Backup

    assert Backup.objects.get(pk=job.result["backup_id"]).meta["software"] == {
        "game": "terraria",
        "variant": "vanilla",
        "version": "1.4.5.7",
        "image": "passivelemon/terraria-docker:terraria-1.4.5.7",
    }
    resp = clients["owner"].post(url, {"version": "1.4.5.7"}, format="json")
    assert resp.status_code == 409 and err(resp) == "downgrade_unconfirmed"
    assert (
        err(clients["owner"].post(f"/api/instances/{tshock.id}/software", {"version": "1.4.5.8"}, format="json"))
        == "invalid_version"
    )


def test_exit_143_is_a_clean_stop(vanilla, make_instance):
    from apps.instances.state import compute

    vanilla.desired_state = "running"
    container = {"exists": True, "status": "exited", "exit_code": 143, "running": False}
    status, detail = compute(vanilla, container)
    assert status != "failed" and "unexpectedly" not in detail
    mc = make_instance()
    mc.desired_state = "running"
    assert compute(mc, container)[0] == "failed"  # Minecraft: unchanged


def test_stop_log_has_no_warning_for_143(vanilla, fake_agent, django_capture_on_commit_callbacks):
    from apps.jobs import runner

    start_container(fake_agent, vanilla)
    fake_agent.stop_exit_code = 143
    with django_capture_on_commit_callbacks(execute=True):
        job = runner.enqueue("stop", title="stop", instance=vanilla, params={})
    job.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert not any("did not shut down cleanly" in line for line in job.log)


def test_clone_keeps_the_variant(clients, tshock, fake_agent):
    resp = clients["owner"].post(f"/api/instances/{tshock.id}/clone", {"name": "Copy"}, format="json")
    assert resp.status_code == 202, resp.json()
    copy = Instance.objects.get(name="Copy")
    assert copy.server_type == "tshock" and copy.game_options == tshock.game_options and copy.ports[0]["protocol"] == "tcp"
