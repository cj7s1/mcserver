"""The six games of games-6-contract.md on the panel: Palworld, Valheim, Project Zomboid, Hollow Knight (HKMP),
Counter-Strike 2 and Rust.

Definitions (catalog entries, specs and environments exactly, the agent's profiles accepting them), creating
servers (generated and checked secrets, derived and conditional ports, virtual configs), the env value
checks (Counter-Strike 2's start line is eval'd: shell metacharacters are refused), secrets that never reach
the audit log, players per game, the folder saves model (list, reset, import, export) and the agent gate.
The pins of the games before them are tests/test_games_other_pins.py.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache

from apps import games
from apps.audit.models import AuditEvent
from apps.games import codecs
from apps.instances.models import Instance
from apps.instances.spec import build_spec, rcon_password
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient, AgentError

SIX = ("palworld", "valheim", "project-zomboid", "hkmp", "cs2", "rust")
IMAGES = {
    "palworld": "thijsvanloef/palworld-server-docker:v2.8.1",
    "valheim": "ghcr.io/community-valheim-tools/valheim-server:1.4.1",
    "project-zomboid": "indifferentbroccoli/projectzomboid-server-docker:v1.2.0",
    "hkmp": "maximalmax90/hkmpserver:v2.4.3-2",
    "cs2": "joedwards32/cs2:5.0.1",
    "rust": "didstopia/rust-server@sha256:c60a3ab044eb69019616951087834468aed478a11b449391376b23db05ddc38a",
}
AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"


def err(resp: Any) -> str:
    return resp.json()["error"]["code"]


def run(kind: str, inst: Instance, capture: Any, **params: Any) -> Job:
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    return job


def body(game: str, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"game": game, "name": f"My {game} server", "start": False, "nightly_backup": False}
    if game == "valheim":
        data["secrets"] = {"server_password": "sesame-42"}
    data.update(over)
    return data


def create(clients: Any, capture: Any, game: str, **over: Any) -> Instance:
    with capture(execute=True):
        resp = clients["owner"].post("/api/instances/", body(game, **over), format="json")
    assert resp.status_code == 201, resp.json()
    return Instance.objects.get(pk=resp.json()["instance"]["id"])


def start(fake_agent: Any, inst: Instance) -> None:
    inst.refresh_from_db()
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running")
    inst.refresh_from_db()


class FakeActions:
    """``AgentClient.game_action`` of the palworld-api and webrcon drivers, in memory."""

    def __init__(self, fake_agent: Any) -> None:
        self.agent = fake_agent
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.players: list[dict[str, Any]] = []

    def __call__(
        self, _client: Any, instance_id: Any, action: str, params: dict[str, Any] | None = None, timeout: float = 60
    ) -> Any:
        params = dict(params or {})
        self.calls.append((action, params))
        c = self.agent.containers.get(str(instance_id))
        if c is None or not c["running"]:
            raise AgentError("The server is not running", "not_running", 409)
        if action == "players":
            return {"players": list(self.players)}
        return {"ok": True, "output": f"{action} done"}


@pytest.fixture
def actions(fake_agent: Any, monkeypatch: pytest.MonkeyPatch) -> FakeActions:
    fake = FakeActions(fake_agent)
    monkeypatch.setattr(AgentClient, "game_action", lambda client, *a, **k: fake(client, *a, **k))
    return fake


# --- the definitions ------------------------------------------------------------------------------------
def test_the_six_are_registered_and_listed(clients):
    entries = {g["id"]: g for g in clients["owner"].get("/api/catalog/games").json()}
    assert set(SIX) <= set(entries)
    assert entries["hkmp"]["label"] == "Hollow Knight (HKMP)" and entries["cs2"]["label"] == "Counter-Strike 2"
    for game in SIX:
        entry = entries[game]
        assert entry["memory_model"] == "plain" and "has_versions" not in entry, game
        assert entry["fixed_version"]["id"], game
        assert "software_update" not in entry["capabilities"] and "diagnostics" in entry["capabilities"], game
        assert all(p["one_to_one"] for p in entry["ports"]), game
        assert entry["min_memory_mb"] <= entry["default_memory_mb"], game
    defaults = {g: (entries[g]["default_memory_mb"], entries[g]["min_memory_mb"]) for g in SIX}
    assert defaults == {
        "palworld": (16384, 8192),
        "valheim": (8192, 4096),
        "project-zomboid": (8192, 4096),
        "hkmp": (512, 256),
        "cs2": (4096, 2048),
        "rust": (16384, 10240),
    }
    stops = {g: entries[g]["default_stop_timeout"] for g in SIX}
    assert stops == {"palworld": 60, "valheim": 120, "project-zomboid": 120, "hkmp": 40, "cs2": 30, "rust": 180}


def test_catalog_shapes_the_frontend_renders(clients):
    entries = {g["id"]: g for g in clients["owner"].get("/api/catalog/games").json()}
    valheim = {p["name"]: p for p in entries["valheim"]["ports"]}
    assert valheim["query"]["derived_from"] == {"port": "game", "offset": 1}
    assert valheim["crossplay"]["derived_from"] == {"port": "game", "offset": 2}
    assert valheim["crossplay"]["when"] == {"config": "server", "key": "crossplay", "value": True}
    assert entries["cs2"]["ports"][1]["when"] == {"config": "server", "key": "cstv", "value": True}
    assert [(p["name"], p["protocol"]) for p in entries["rust"]["ports"]] == [("game", "udp"), ("query", "udp"), ("app", "tcp")]
    for game in ("palworld", "valheim", "project-zomboid", "rust"):
        assert entries[game]["world"]["kind"] == "folder" and entries[game]["world"]["extension"] == ".zip", game
    assert "world" not in entries["hkmp"] and "world" not in entries["cs2"]
    palworld_secrets = {s["key"]: s for s in entries["palworld"]["secrets"]}
    assert palworld_secrets["admin_password"]["generated"] and palworld_secrets["admin_password"]["reveal"]
    assert palworld_secrets["admin_password"]["pattern"] == r"^[A-Za-z0-9_-]{12,64}$"
    gslt = next(s for s in entries["cs2"]["secrets"] if s["key"] == "gslt")
    assert gslt["link"] == "https://steamcommunity.com/dev/managegameservers"
    assert next(s for s in entries["valheim"]["secrets"])["required"] is True
    assert [o["key"] for o in entries["project-zomboid"]["options"]] == ["server_name", "branch"]
    assert entries["project-zomboid"]["options"][1]["options"] == ["public", "legacy41"]
    assert [f["id"] for f in entries["project-zomboid"]["config_files"]] == ["launch", "server-ini"]
    assert entries["valheim"]["player_name_pattern"] == r"^\d{17}$"
    assert entries["valheim"]["player_list_labels"] == {"whitelist": "Permitted (whitelist)", "ops": "Admins", "bans": "Banned"}
    assert entries["cs2"]["stop_words"] == ["_restart", "exit", "quit"]
    assert "console_commands" not in entries["palworld"]["capabilities"]
    assert "console_commands" not in entries["valheim"]["capabilities"]
    assert "query" in entries["cs2"]["capabilities"] and "query" in entries["rust"]["capabilities"]


@pytest.mark.parametrize("game", SIX)
def test_create_and_spec(game, clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, game)
    spec = build_spec(inst)
    assert spec["image"] == IMAGES[game] and inst.image == IMAGES[game]
    assert spec["game"] == game and spec["restart_policy"] == "no" and inst.restart_policy == "no"
    assert "variant" not in spec
    # every published port is 1:1 and only UDP game ports (TCP only for Rust+ while it is on)
    assert all(p["container_port"] == p["host_port"] for p in spec["ports"])
    assert all(p["protocol"] == "udp" for p in spec["ports"])
    assert spec["env"]["TZ"]
    # the install job ran against the agent (fake): the container exists, stopped
    assert fake_agent.containers[str(inst.id)]["spec"]["game"] == game


def test_palworld_env(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(
        clients,
        django_capture_on_commit_callbacks,
        "palworld",
        ports={"game": 8300},
        settings={"server": {"max_players": 8, "community": True}, "gameplay": {"exp_rate": 2.5, "pvp": True}},
        secrets={"server_password": "join-me"},
    )
    admin = inst.secret_values["admin_password"]
    assert len(admin) == 24 and admin.isalnum()
    env = build_spec(inst)["env"]
    assert {
        k: env[k] for k in ("PORT", "PUBLIC_PORT", "PLAYERS", "SERVER_NAME", "COMMUNITY", "ADMIN_PASSWORD", "SERVER_PASSWORD")
    } == {
        "PORT": "8300",
        "PUBLIC_PORT": "8300",
        "PLAYERS": "8",
        "SERVER_NAME": "My palworld server",
        "COMMUNITY": "true",
        "ADMIN_PASSWORD": admin,
        "SERVER_PASSWORD": "join-me",
    }
    assert env["EXP_RATE"] == "2.500000" and env["IS_PVP"] == "True" and env["DIFFICULTY"] == "None"
    assert env["DEATH_PENALTY"] == "Item" and env["GUILD_PLAYER_MAX_NUM"] == "20"
    # the query port stays inside the container, on a number that is not the game port's
    assert "SERVER_DESCRIPTION" not in env and env["QUERY_PORT"] == "27015"
    assert build_spec(inst)["ports"] == [{"container_port": 8300, "host_port": 8300, "protocol": "udp", "host_ip": "0.0.0.0"}]


def test_valheim_env_ports_and_password_rules(clients, fake_agent, django_capture_on_commit_callbacks):
    owner = clients["owner"]
    refused = owner.post("/api/instances/", body("valheim", secrets={"server_password": "abcd"}), format="json")
    assert refused.status_code == 400 and "5 to 64" in json.dumps(refused.json())
    refused = owner.post(
        "/api/instances/", body("valheim", name="Vikings", secrets={"server_password": "vikings-rule"}), format="json"
    )
    assert refused.status_code == 400 and "must not contain the server name" in json.dumps(refused.json())
    missing = owner.post("/api/instances/", body("valheim", secrets={}), format="json")
    assert missing.status_code == 400
    inst = create(
        clients,
        django_capture_on_commit_callbacks,
        "valheim",
        options={"world_name": "Midgard"},
        settings={"server": {"preset": "hard", "modifier_raids": "none", "key_nomap": True, "save_interval": 600}},
    )
    assert [(p["name"], p["host_port"]) for p in inst.ports] == [("game", 2456), ("query", 2457), ("crossplay", 2458)]
    spec = build_spec(inst)
    assert spec["env"] == {
        "TZ": spec["env"]["TZ"],
        "SERVER_NAME": "My valheim server",
        "SERVER_PORT": "2456",
        "WORLD_NAME": "Midgard",
        "SERVER_PUBLIC": "false",
        "CROSSPLAY": "false",
        "SERVER_PASS": "sesame-42",
        "SERVER_ARGS": "-saveinterval 600 -preset hard -modifier raids none -setkey nomap",
    }
    # crossplay off: the third port is reserved but not published
    assert [(p["container_port"], p["protocol"]) for p in spec["ports"]] == [(2456, "udp"), (2457, "udp")]
    resp = owner.patch(f"/api/instances/{inst.id}/config/server", {"values": {"crossplay": True}}, format="json")
    assert resp.status_code == 200, resp.json()
    inst.refresh_from_db()
    assert [p["container_port"] for p in build_spec(inst)["ports"]] == [2456, 2457, 2458]
    # a server name inside the password is refused, and the other way round
    resp = owner.patch(f"/api/instances/{inst.id}/config/server", {"values": {"server_name": "sesame"}}, format="json")
    assert resp.status_code == 400 or err(resp) == "invalid_setting"
    resp = owner.put(f"/api/instances/{inst.id}/secrets", {"values": {"server_password": "my valheim server!"}}, format="json")
    assert resp.status_code == 400
    resp = owner.put(f"/api/instances/{inst.id}/secrets", {"values": {"server_password": ""}}, format="json")
    assert resp.status_code == 400  # required: cannot be cleared


def test_derived_ports_move_with_the_game_port_and_are_checked(clients, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.add_foreign_port(2457, protocol="udp")
    inst = create(clients, django_capture_on_commit_callbacks, "valheim")
    # 2456 is free but 2457 is not: the game port moves on to a number whose +1 and +2 are free
    assert [p["host_port"] for p in inst.ports] == [2458, 2459, 2460]
    owner = clients["owner"]
    resp = owner.patch(f"/api/instances/{inst.id}", {"ports": [{"name": "game", "host_port": 2456}]}, format="json")
    assert resp.status_code == 409 and err(resp) == "port_conflict"
    resp = owner.patch(
        f"/api/instances/{inst.id}",
        {
            "ports": [
                {"name": "game", "host_port": 3000},
                {"name": "query", "host_port": 2459},
                {"name": "crossplay", "host_port": 2460},
            ]
        },
        format="json",
    )
    assert resp.status_code == 200, resp.json()
    inst.refresh_from_db()
    assert [p["host_port"] for p in inst.ports] == [3000, 3001, 3002]
    resp = owner.patch(f"/api/instances/{inst.id}", {"ports": [{"name": "query", "host_port": 4000}]}, format="json")
    assert resp.status_code == 400 and err(resp) == "invalid_port"


def test_zomboid_env_files_and_branch(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(
        clients,
        django_capture_on_commit_callbacks,
        "project-zomboid",
        options={"server_name": "knox", "branch": "legacy41"},
        memory_mb=10240,
        settings={"launch": {"max_players": 12}, "server-ini": {"PublicName": "Knox County", "Open": False}},
    )
    assert inst.minecraft_version == "legacy41"
    env = build_spec(inst)["env"]
    admin = inst.secret_values["admin_password"]
    assert {k: v for k, v in env.items() if k != "TZ"} == {
        "SERVER_NAME": "knox",
        "DEFAULT_PORT": "16261",
        "UDP_PORT": "16262",
        "MAX_PLAYERS": "12",
        "MEMORY_XMX_GB": "8",
        "SERVER_BRANCH": "legacy41",
        "ADMIN_PASSWORD": admin,
    }
    assert "RCON_PASSWORD" not in env  # the agent's (reserved; it injects the control secret)
    ini = fake_agent.text(inst.id, "data/Server/knox.ini")
    parsed = codecs.get("kv").parse(ini)
    assert parsed["PublicName"] == "Knox County" and parsed["Open"] == "false"
    assert parsed["UPnP"] == "false" and parsed["BackupsOnStart"] == "false"
    # the editor: the per-server file, managed keys refused, the RCON password masked
    fake_agent.put_file(inst.id, "data/Server/knox.ini", ini + f"RCONPassword={rcon_password(inst)}\nPassword=\n")
    files = clients["owner"].get(f"/api/instances/{inst.id}/config").json()["files"]
    assert [f["path"] for f in files] == ["(container settings)", "data/Server/knox.ini"]
    resp = clients["owner"].patch(f"/api/instances/{inst.id}/config/server-ini", {"values": {"UPnP": True}}, format="json")
    assert resp.status_code == 400
    text = clients["owner"].get(f"/api/instances/{inst.id}/files/text", {"path": "data/Server/knox.ini"}).json()["content"]
    assert rcon_password(inst) not in text and "RCONPassword=********" in text


def test_zomboid_server_name_and_branch_are_checked(clients, fake_agent):
    owner = clients["owner"]
    for options in ({"server_name": "bad name"}, {"server_name": "../x"}, {"branch": "beta"}):
        resp = owner.post("/api/instances/", body("project-zomboid", options=options), format="json")
        assert resp.status_code == 400, options


def test_cs2_env_and_shell_metacharacters_are_refused(clients, fake_agent, django_capture_on_commit_callbacks):
    owner = clients["owner"]
    for bad in ("x; rm -rf /", "$(id)", "a`b`", 'quo"te', "back\\slash", "a/b", "new\nline", " lead"):
        resp = owner.post("/api/instances/", body("cs2", settings={"server": {"server_name": bad}}), format="json")
        assert resp.status_code == 400, bad
    for bad in ("de_dust2;quit", "De_Dust2", "../maps"):
        resp = owner.post("/api/instances/", body("cs2", settings={"server": {"start_map": bad}}), format="json")
        assert resp.status_code == 400, bad
    for key, bad in (
        ("gslt", "abc"),
        ("gslt", "0123456789abcdef0123456789abcdef"),
        ("server_password", "pa ss"),
        ("server_password", "a$b"),
    ):
        resp = owner.post("/api/instances/", body("cs2", secrets={key: bad}), format="json")
        assert resp.status_code == 400, (key, bad)
    inst = create(
        clients,
        django_capture_on_commit_callbacks,
        "cs2",
        name="Frag House #1!",
        settings={"server": {"cstv": True, "game_mode": "wingman", "bot_quota": 4}},
        secrets={"gslt": "0123456789ABCDEF0123456789ABCDEF", "server_password": "letmein"},
    )
    spec = build_spec(inst)
    assert {k: v for k, v in spec["env"].items() if k != "TZ"} == {
        "CS2_SERVERNAME": "Frag House 1",
        "CS2_PORT": "27015",
        "CS2_MAXPLAYERS": "10",
        "CS2_GAMEALIAS": "wingman",
        "CS2_STARTMAP": "de_inferno",
        "CS2_MAPGROUP": "mg_active",
        "CS2_LAN": "0",
        "TV_ENABLE": "1",
        "TV_PORT": "27020",
        "CS2_BOT_QUOTA": "4",
        "CS2_PW": "letmein",
        "SRCDS_TOKEN": "0123456789ABCDEF0123456789ABCDEF",
    }
    assert "CS2_RCONPW" not in spec["env"]  # the agent injects the control secret
    assert [(p["container_port"], p["protocol"]) for p in spec["ports"]] == [(27015, "udp"), (27020, "udp")]
    # a change in Settings is checked the same way
    resp = owner.patch(f"/api/instances/{inst.id}/config/server", {"values": {"map_group": "mg;quit"}}, format="json")
    assert resp.status_code == 400
    resp = owner.put(f"/api/instances/{inst.id}/secrets", {"values": {"server_password": "a|b"}}, format="json")
    assert resp.status_code == 400


def test_rust_env_seed_and_identity(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(
        clients,
        django_capture_on_commit_callbacks,
        "rust",
        options={"identity": "wipe1"},
        settings={"server": {"world_size": 4000, "rust_plus": True}},
    )
    seed = inst.game_options["server"]["seed"]
    assert isinstance(seed, int) and 0 < seed <= 2147483647
    spec = build_spec(inst)
    assert {k: v for k, v in spec["env"].items() if k != "TZ"} == {
        "RUST_SERVER_NAME": "My rust server",
        "RUST_SERVER_IDENTITY": "wipe1",
        "RUST_SERVER_PORT": "28015",
        "RUST_SERVER_QUERYPORT": "28017",
        "RUST_SERVER_MAXPLAYERS": "50",
        "RUST_SERVER_WORLDSIZE": "4000",
        "RUST_SERVER_SAVE_INTERVAL": "600",
        "RUST_APP_PORT": "28082",
        "RUST_SERVER_SEED": str(seed),
    }
    assert [(p["container_port"], p["protocol"]) for p in spec["ports"]] == [(28015, "udp"), (28017, "udp"), (28082, "tcp")]
    assert build_spec(inst) == spec  # the seed stays the same
    resp = clients["owner"].patch(f"/api/instances/{inst.id}/config/server", {"values": {"rust_plus": False}}, format="json")
    assert resp.status_code == 200
    inst.refresh_from_db()
    assert build_spec(inst)["env"]["RUST_APP_PORT"] == "-1" and len(build_spec(inst)["ports"]) == 2
    for bad in ("+server.secure 0", "-load", 'a"b'):
        resp = clients["owner"].patch(f"/api/instances/{inst.id}/config/server", {"values": {"server_name": bad}}, format="json")
        assert resp.status_code == 400, bad
    resp = clients["owner"].post("/api/instances/", body("rust", options={"identity": "Bad/Id"}), format="json")
    assert resp.status_code == 400


def test_rust_app_port_avoids_the_internal_ports(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    resp = clients["owner"].patch(f"/api/instances/{inst.id}", {"ports": [{"name": "app", "host_port": 28016}]}, format="json")
    assert resp.status_code == 400 and err(resp) == "invalid_port"


def test_hkmp_env_and_settings_file(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(
        clients,
        django_capture_on_commit_callbacks,
        "hkmp",
        settings={"launch": {"full_sync": True}, "serversettings": {"IsPvpEnabled": True}},
    )
    assert inst.minecraft_version == "2.4.3"
    env = build_spec(inst)["env"]
    assert {k: v for k, v in env.items() if k != "TZ"} == {"PORT": "2222", "FULL_SYNC": "true"}
    settings = json.loads(fake_agent.text(inst.id, "serversettings.json"))
    assert settings["IsPvpEnabled"] is True and settings["NailDamage"] == 1


@pytest.mark.skipif(not (AGENT_DIR / ".venv" / "bin" / "python").exists(), reason="needs the agent's virtualenv")
def test_the_agent_profiles_accept_the_specs(clients, fake_agent, django_capture_on_commit_callbacks, tmp_path):
    """Every spec the panel builds for the six games passes the agent's own checks (env allow-list and value
    patterns, ports, image, user): docker_ops.build_create_kwargs of the agent in this checkout."""
    specs = {}
    variants = {
        "palworld": {"secrets": {"server_password": "join-me"}, "settings": {"gameplay": {"pvp": True, "exp_rate": 3}}},
        "valheim": {"settings": {"server": {"crossplay": True, "preset": "hardcore", "key_passivemobs": True}}},
        "project-zomboid": {"options": {"branch": "legacy41", "server_name": "knox"}},
        "hkmp": {"settings": {"launch": {"full_sync": True}}},
        "cs2": {
            "settings": {"server": {"cstv": True, "bot_quota": 3, "bot_difficulty": "2", "bot_quota_mode": "fill", "lan": True}},
            "secrets": {"gslt": "0123456789ABCDEF0123456789ABCDEF", "server_password": "abc", "tv_password": "watch"},
        },
        "rust": {"settings": {"server": {"rust_plus": True, "description": "Weekly wipes"}}},
    }
    # game ports on the number of a port the agent defaults inside the container (CS2's CSTV port while CSTV is off,
    # Palworld's query port): the panel names those ports itself, so the agent never sees two names for one port
    collisions = {"cs2": {"ports": {"game": 27020}}, "palworld": {"ports": {"game": 27015}}}
    for game in SIX:
        cases = [("default", {}), ("variant", variants[game])]
        if game in collisions:  # first: the other servers of the game reserve those numbers afterwards
            cases.insert(0, ("collision", collisions[game]))
        for label, over in cases:
            inst = create(clients, django_capture_on_commit_callbacks, game, **over)
            specs[f"{game}-{label}"] = build_spec(inst)
    assert specs["cs2-collision"]["env"]["CS2_PORT"] == "27020" and specs["cs2-collision"]["env"]["TV_ENABLE"] == "0"
    assert specs["cs2-collision"]["env"]["TV_PORT"] not in ("", "27020")
    assert specs["palworld-collision"]["env"]["PORT"] == "27015" and specs["palworld-collision"]["env"]["QUERY_PORT"] == "27016"
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(specs))
    script = (
        "import json, sys, tempfile, uuid; from pathlib import Path\n"
        "from mcagent import docker_ops\n"
        "from mcagent.config import Settings\n"
        "from mcagent.schemas import InstanceSpec\n"
        "specs = json.loads(Path(sys.argv[1]).read_text())\n"
        "root = Path(tempfile.mkdtemp())\n"
        "s = Settings(data_root=root / 'data', host_data_root=Path('/mnt/x'), token='t', runtime_uid=99, runtime_gid=100)\n"
        "bad = {}\n"
        "for name, spec in specs.items():\n"
        "    try:\n"
        "        docker_ops.build_create_kwargs(s, uuid.uuid4(), InstanceSpec(**spec), 'validate')\n"
        "    except Exception as exc:\n"
        "        bad[name] = f'{type(exc).__name__}: {getattr(exc, \"message\", exc)}'\n"
        "print(json.dumps(bad))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "MCPANEL_", "DJANGO_"))}
    proc = subprocess.run(  # the agent of this checkout, our own script
        [str(AGENT_DIR / ".venv" / "bin" / "python"), "-c", script, str(path)],
        cwd=AGENT_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {}


# --- secrets ----------------------------------------------------------------------------------------------
def test_generated_secrets_are_revealable_and_never_audited(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "palworld", secrets={"server_password": "join-me"})
    admin = inst.secret_values["admin_password"]
    owner = clients["owner"]
    listed = owner.get(f"/api/instances/{inst.id}/secrets").json()["secrets"]
    assert {s["key"]: s["set"] for s in listed} == {"admin_password": True, "server_password": True}
    assert admin not in json.dumps(listed)
    shown = owner.post(f"/api/instances/{inst.id}/secrets/admin_password/reveal")
    assert shown.status_code == 200 and shown.json() == {"key": "admin_password", "value": admin}
    assert shown["Cache-Control"] == "no-store"
    assert owner.post(f"/api/instances/{inst.id}/secrets/server_password/reveal").status_code == 404
    assert clients["viewer"].post(f"/api/instances/{inst.id}/secrets/admin_password/reveal").status_code in (403, 404)
    resp = owner.put(f"/api/instances/{inst.id}/secrets", {"values": {"admin_password": "short"}}, format="json")
    assert resp.status_code == 400
    resp = owner.put(f"/api/instances/{inst.id}/secrets", {"values": {"admin_password": ""}}, format="json")
    assert resp.status_code == 400  # generated: never empty (the REST API needs it)
    resp = owner.put(f"/api/instances/{inst.id}/secrets", {"values": {"admin_password": "new-admin-password-1"}}, format="json")
    assert resp.status_code == 200
    inst.refresh_from_db()
    assert build_spec(inst)["env"]["ADMIN_PASSWORD"] == "new-admin-password-1"
    logged = json.dumps(list(AuditEvent.objects.filter(instance=inst).values("action", "details")))
    for value in (admin, "join-me", "new-admin-password-1"):
        assert value not in logged
    assert "instance.secret_revealed" in logged


# --- players --------------------------------------------------------------------------------------------
def test_palworld_players(clients, fake_agent, actions, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "palworld")
    fake_agent.put_file(inst.id, "Pal/Saved/SaveGames/banlist.txt", "steam_76561190000000002,33DBE761\n")
    start(fake_agent, inst)
    actions.players = [{"name": "Kiki", "accountName": "kiki", "userId": "steam_76561190000000009", "level": 3}]
    data = clients["owner"].get(f"/api/instances/{inst.id}/players").json()
    assert data["bans"] == [{"name": "steam_76561190000000002", "reason": ""}]
    assert data["online"]["players"] == ["Kiki"] and data["online"]["online"] == 1 and data["online"]["max"] == 16
    assert data["online"]["entries"] == [{"name": "Kiki", "id": "steam_76561190000000009"}]
    resp = clients["owner"].post(f"/api/instances/{inst.id}/players", {"action": "kick", "name": "Kiki"}, format="json")
    assert resp.status_code == 200 and actions.calls[-1] == ("kick", {"userid": "steam_76561190000000009"})
    resp = clients["owner"].post(
        f"/api/instances/{inst.id}/players",
        {"action": "ban", "name": "steam_76561190000000005", "reason": "grief"},
        format="json",
    )
    assert actions.calls[-1] == ("ban", {"userid": "steam_76561190000000005", "message": "grief"})
    resp = clients["owner"].post(f"/api/instances/{inst.id}/players", {"action": "op", "name": "Kiki"}, format="json")
    assert resp.status_code == 400
    # stopped: unban edits banlist.txt
    fake_agent.stop(inst.id)
    resp = clients["owner"].post(
        f"/api/instances/{inst.id}/players", {"action": "pardon", "name": "steam_76561190000000002"}, format="json"
    )
    assert resp.status_code == 200 and resp.json()["via"] == "file"
    assert fake_agent.text(inst.id, "Pal/Saved/SaveGames/banlist.txt") == ""


def test_valheim_lists_are_files(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "valheim")
    fake_agent.put_file(inst.id, "config/adminlist.txt", "// List admin players ID  ONE per line\n76561190000000001\n")
    owner = clients["owner"]
    data = owner.get(f"/api/instances/{inst.id}/players").json()
    assert data["ops"] == [{"name": "76561190000000001"}] and data["bans"] == [] and data["no_online_list"] is True
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "ban", "name": "76561190000000002"}, format="json")
    assert resp.status_code == 200 and "next start" in resp.json()["output"]
    assert fake_agent.text(inst.id, "config/bannedlist.txt").splitlines()[-1] == "76561190000000002"
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "deop", "name": "76561190000000001"}, format="json")
    assert resp.status_code == 200
    assert fake_agent.text(inst.id, "config/adminlist.txt") == "// List admin players ID  ONE per line\n"
    for bad in ("Kiki", "7656119000000000", "76561190000000001; x"):
        resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "op", "name": bad}, format="json")
        assert resp.status_code == 400, bad
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "kick", "name": "76561190000000001"}, format="json")
    assert resp.status_code == 400


def test_zomboid_players_through_rcon(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    owner = clients["owner"]
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "ban", "name": "zed"}, format="json")
    assert resp.status_code == 409 and err(resp) == "not_running"
    start(fake_agent, inst)
    fake_agent.command_handler = lambda iid, cmd: "Players connected (2): \n-alice\n-bob\n" if cmd == "players" else "ok"
    data = owner.get(f"/api/instances/{inst.id}/players").json()
    assert data["online"]["players"] == ["alice", "bob"] and data["lists_unknown"] is True
    for action, expected in (
        ("kick", 'kickuser "alice" -r "afk"'),
        ("ban", 'banuser "alice" -r "afk"'),
        ("pardon", 'unbanuser "alice"'),
        ("op", 'setaccesslevel "alice" admin'),
        ("deop", 'setaccesslevel "alice" user'),
        ("whitelist_remove", 'removeuserfromwhitelist "alice"'),
    ):
        resp = owner.post(
            f"/api/instances/{inst.id}/players", {"action": action, "name": "alice", "reason": "afk"}, format="json"
        )
        assert resp.status_code == 200, resp.json()
        assert fake_agent.commands[-1] == (str(inst.id), expected)
    # a whitelist entry is an account with a password (adduser "name" "password"): the name alone would only get
    # the game's usage text back, so the panel says how instead of reporting a success
    sent = len(fake_agent.commands)
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "whitelist_add", "name": "alice"}, format="json")
    assert resp.status_code == 400 and err(resp) == "invalid_action"
    assert 'adduser "name" "password"' in resp.json()["error"]["message"] and len(fake_agent.commands) == sent
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "kick", "name": 'a" -r "x'}, format="json")
    assert resp.status_code == 400
    # a stop word typed into the console becomes the Stop job (not a command)
    resp = owner.post(f"/api/instances/{inst.id}/command", {"command": "quit"}, format="json")
    assert resp.status_code in (400, 409)


def test_hkmp_players_and_console(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "hkmp")
    fake_agent.put_file(inst.id, "whitelist.json", json.dumps({"enabled": True, "pre-listed": ["knight"], "approved": []}))
    fake_agent.put_file(inst.id, "banned.json", json.dumps({"ip-addresses": ["10.9.8.7"], "approved": ["KEY123"]}))
    start(fake_agent, inst)
    fake_agent.command_handler = lambda iid, cmd: (
        "[INFO] list\n[INFO] Online players (2): Hornet, Quirrel" if cmd == "list" else "[INFO] ok"
    )
    owner = clients["owner"]
    data = owner.get(f"/api/instances/{inst.id}/players").json()
    assert data["online"] == {"online": 2, "max": None, "players": ["Hornet", "Quirrel"]}
    assert data["whitelist"] == [{"name": "knight"}] and data["bans"] == [{"name": "KEY123", "reason": ""}]
    assert data["ip_bans"] == [{"ip": "10.9.8.7", "reason": ""}]
    for action, name, expected in (
        ("kick", "Hornet", "kick Hornet"),
        ("ban", "10.0.0.5", "banip 10.0.0.5"),
        ("pardon", "KEY123", "unban KEY123"),
        ("op", "Hornet", "auth Hornet"),
        ("whitelist_add", "Zote", "whitelist add Zote"),
    ):
        resp = owner.post(f"/api/instances/{inst.id}/players", {"action": action, "name": name}, format="json")
        assert resp.status_code == 200, resp.json()
        assert fake_agent.commands[-1] == (str(inst.id), expected)
    for bad in ("a^Mlist", "a b", "x\\015list"):
        resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "kick", "name": bad}, format="json")
        assert resp.status_code == 400, bad


def test_cs2_players_through_the_status_query(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "cs2")
    start(fake_agent, inst)
    fake_agent.ping_answer = {"online": 3, "max": 10, "sample": ["s1mple", "Zy w00"]}
    owner = clients["owner"]
    data = owner.get(f"/api/instances/{inst.id}/players").json()
    assert data["online"]["online"] == 3 and data["online"]["players"] == ["s1mple", "Zy w00"] and data["labels"] == {}
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "kick", "name": "Zy w00"}, format="json")
    assert resp.status_code == 200 and fake_agent.commands[-1] == (str(inst.id), 'kick "Zy w00"')
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "kick", "name": "x;quit"}, format="json")
    assert resp.status_code == 400
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "ban", "name": "x"}, format="json")
    assert resp.status_code == 400


def test_rust_players_owners_and_bans(clients, fake_agent, actions, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    fake_agent.put_file(inst.id, "server/rust/cfg/users.cfg", 'ownerid 76561190000000001 "Boss" "panel"\n')
    fake_agent.put_file(inst.id, "server/rust/cfg/bans.cfg", 'banid 76561190000000002 "Cheater" "aimbot"\n')
    start(fake_agent, inst)
    actions.players = [{"SteamID": "76561190000000003", "DisplayName": "Naked Guy", "Ping": 40}]
    owner = clients["owner"]
    data = owner.get(f"/api/instances/{inst.id}/players").json()
    assert data["ops"] == [{"name": "76561190000000001", "display_name": "Boss"}]
    assert data["bans"] == [{"name": "76561190000000002", "display_name": "Cheater", "reason": "aimbot"}]
    assert data["online"]["players"] == ["Naked Guy"]
    resp = owner.post(f"/api/instances/{inst.id}/players", {"action": "op", "name": "Naked Guy"}, format="json")
    assert resp.status_code == 200
    assert actions.calls[-1] == ("owner_add", {"steamid": "76561190000000003", "reason": "", "name": "Naked Guy"})
    owner.post(f"/api/instances/{inst.id}/players", {"action": "pardon", "name": "76561190000000002"}, format="json")
    assert actions.calls[-1] == ("unban", {"steamid": "76561190000000002"})
    resp = owner.post(
        f"/api/instances/{inst.id}/players", {"action": "whitelist_add", "name": "76561190000000002"}, format="json"
    )
    assert resp.status_code == 400


def test_metrics_player_counts(clients, fake_agent, actions, django_capture_on_commit_callbacks):
    from apps.metrics.tasks import player_count

    pal = create(clients, django_capture_on_commit_callbacks, "palworld")
    start(fake_agent, pal)
    actions.players = [{"name": "Kiki", "userId": "steam_76561190000000009"}]
    assert player_count(pal, AgentClient(pal.node))["online"] == 1
    cs = create(clients, django_capture_on_commit_callbacks, "cs2")
    start(fake_agent, cs)
    fake_agent.ping_answer = {"online": 5, "max": 10, "sample": []}
    assert player_count(cs, AgentClient(cs.node))["online"] == 5
    pz = create(clients, django_capture_on_commit_callbacks, "project-zomboid")
    start(fake_agent, pz)
    fake_agent.ping_answer = {"online": 2, "max": 16, "sample": []}
    assert player_count(pz, AgentClient(pz.node))["online"] == 2
    assert not [c for c in fake_agent.commands if c[0] == str(pz.id)]  # (no RCON for the count)
    vh = create(clients, django_capture_on_commit_callbacks, "valheim")
    start(fake_agent, vh)
    assert player_count(vh, AgentClient(vh.node)) is None


# --- folder saves ---------------------------------------------------------------------------------------
def world_zip(files: dict[str, bytes], *, link: str | None = None) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if link:
            info = zipfile.ZipInfo(link)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
    buf.seek(0)
    buf.name = "world.zip"
    return buf


WORLD = "6218B180AAAA4F1B9C3D2E1F00000001"


def palworld_with_world(clients: Any, fake_agent: Any, capture: Any) -> Instance:
    inst = create(clients, capture, "palworld")
    fake_agent.put_file(inst.id, f"Pal/Saved/SaveGames/0/{WORLD}/Level.sav", b"LEVEL")
    fake_agent.put_file(inst.id, f"Pal/Saved/SaveGames/0/{WORLD}/Players/0001.sav", b"PLAYER")
    fake_agent.put_file(inst.id, f"Pal/Saved/SaveGames/0/{WORLD}/backup/world/2026.11.10/Level.sav", b"OLD")
    fake_agent.put_file(
        inst.id,
        "Pal/Saved/Config/LinuxServer/GameUserSettings.ini",
        f"[/Script/Pal.PalGameLocalSettings]\nDedicatedServerName={WORLD}\n",
    )
    return inst


def test_folder_saves_list_and_reset(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = palworld_with_world(clients, fake_agent, django_capture_on_commit_callbacks)
    owner = clients["owner"]
    data = owner.get(f"/api/instances/{inst.id}/saves").json()
    assert data["current"] == WORLD
    assert data["saves"] == [
        {
            "name": WORLD,
            "path": f"Pal/Saved/SaveGames/0/{WORLD}",
            "type": "directory",
            "size": 14,
            "size_complete": True,
            "modified": data["saves"][0]["modified"],
            "current": True,
        }
    ]
    start(fake_agent, inst)
    with django_capture_on_commit_callbacks(execute=True):
        resp = owner.post(f"/api/instances/{inst.id}/saves/reset", {"confirm_name": inst.name, "backup": False}, format="json")
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["restarted"] is True and job.result["set_aside"] == [WORLD]
    folder = job.result["folder"]
    assert fake_agent.get_file(inst.id, f"{folder}/{WORLD}/Level.sav") == b"LEVEL"
    assert fake_agent.get_file(inst.id, f"Pal/Saved/SaveGames/0/{WORLD}/Level.sav") is None
    gus = fake_agent.text(inst.id, "Pal/Saved/Config/LinuxServer/GameUserSettings.ini")
    assert "DedicatedServerName=\n" in gus
    data = owner.get(f"/api/instances/{inst.id}/saves").json()
    assert data["saves"] == [] and data["set_aside"][0]["files"] == [WORLD]


def test_folder_saves_export_saves_first_and_leaves_out_the_games_backups(
    clients, fake_agent, actions, django_capture_on_commit_callbacks
):
    inst = palworld_with_world(clients, fake_agent, django_capture_on_commit_callbacks)
    start(fake_agent, inst)
    resp = clients["owner"].get(f"/api/instances/{inst.id}/saves/export")
    assert resp.status_code == 200, resp.content[:300]
    assert ("save", {}) in actions.calls
    names = zipfile.ZipFile(io.BytesIO(b"".join(resp.streaming_content))).namelist()
    assert sorted(names) == [f"{WORLD}/Level.sav", f"{WORLD}/Players/0001.sav"]
    assert '.zip"' in resp["Content-Disposition"]


def test_folder_saves_import(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = palworld_with_world(clients, fake_agent, django_capture_on_commit_callbacks)
    owner = clients["owner"]
    new = "ABCDEF0123456789ABCDEF0123456789"

    def upload(data: io.BytesIO) -> Any:
        return owner.post(
            f"/api/instances/{inst.id}/saves/import",
            {"file": data, "confirm_name": inst.name, "backup": "false"},
            format="multipart",
        )

    assert upload(world_zip({"readme.txt": b"x"})).status_code == 400
    assert upload(world_zip({f"{new}/Level.sav": b"x"}, link=f"{new}/evil")).status_code == 400
    assert upload(world_zip({"../x/Level.sav": b"x"})).status_code == 400
    assert upload(world_zip({"a/Level.sav": b"x", "b/Level.sav": b"y"})).status_code == 400
    with django_capture_on_commit_callbacks(execute=True):
        resp = upload(world_zip({f"{new}/Level.sav": b"NEW", f"{new}/Players/0002.sav": b"P2"}))
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.params["root"] == new
    assert fake_agent.get_file(inst.id, f"Pal/Saved/SaveGames/0/{new}/Level.sav") == b"NEW"
    assert fake_agent.get_file(inst.id, f"{job.result['folder']}/{WORLD}/Level.sav") == b"LEVEL"
    gus = fake_agent.text(inst.id, "Pal/Saved/Config/LinuxServer/GameUserSettings.ini")
    assert f"DedicatedServerName={new}" in gus
    leftovers = [p for p in fake_agent.fs[str(inst.id)]["files"] if p.startswith(".mcpanel-import-")]
    assert leftovers == []


def test_valheim_and_zomboid_and_rust_worlds(clients, fake_agent, django_capture_on_commit_callbacks):
    vh = create(clients, django_capture_on_commit_callbacks, "valheim", options={"world_name": "Midgard"})
    fake_agent.put_file(vh.id, "config/worlds_local/Midgard/_main.3.db2", b"DB")
    fake_agent.put_file(vh.id, "config/worlds_local/Midgard/_main.3.fwl2", b"FWL")
    fake_agent.put_file(vh.id, "config/worlds_local/Midgard.fwl", b"OLD")
    job = run("saves_reset", vh, django_capture_on_commit_callbacks, backup=False, settings={"world_name": "Asgard"}, seed=None)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert sorted(job.result["set_aside"]) == ["Midgard", "Midgard.fwl"]
    vh.refresh_from_db()
    assert vh.game_options["world_name"] == "Asgard" and build_spec(vh)["env"]["WORLD_NAME"] == "Asgard"
    # an older world (.fwl + .db) imported: the world name follows it
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{vh.id}/saves/import",
            {"file": world_zip({"Old_World.fwl": b"F", "Old_World.db": b"D"}), "confirm_name": vh.name, "backup": "false"},
            format="multipart",
        )
    assert resp.status_code == 202
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    vh.refresh_from_db()
    assert vh.game_options["world_name"] == "Old_World"
    assert fake_agent.get_file(vh.id, "config/worlds_local/Old_World.db") == b"D"

    pz = create(clients, django_capture_on_commit_callbacks, "project-zomboid", options={"server_name": "knox"})
    fake_agent.put_file(pz.id, "data/Saves/Multiplayer/knox/map_t.bin", b"MAP")
    fake_agent.put_file(pz.id, "data/db/knox.db", b"USERS")
    job = run("saves_reset", pz, django_capture_on_commit_callbacks, backup=False, settings={"reset_accounts": True}, seed=None)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert sorted(job.result["set_aside"]) == ["knox", "knox.db"]

    ru = create(clients, django_capture_on_commit_callbacks, "rust")
    seed = ru.game_options["server"]["seed"]
    for name in (
        "proceduralmap.3500.1.260.map",
        "proceduralmap.3500.1.260.sav",
        "proceduralmap.3500.1.260.sav.1",
        "player.blueprints.5.db",
        "player.states.260.db",
        "cfg/users.cfg",
    ):
        fake_agent.put_file(ru.id, f"server/rust/{name}", b"x")
    job = run("saves_reset", ru, django_capture_on_commit_callbacks, backup=False, settings={"seed": 777}, seed=None)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert sorted(job.result["set_aside"]) == [
        "player.states.260.db",
        "proceduralmap.3500.1.260.map",
        "proceduralmap.3500.1.260.sav",
        "proceduralmap.3500.1.260.sav.1",
    ]
    assert fake_agent.get_file(ru.id, "server/rust/player.blueprints.5.db") == b"x"
    ru.refresh_from_db()
    assert ru.game_options["server"]["seed"] == 777 != seed and job.result["seed"] == "777"
    job = run("saves_reset", ru, django_capture_on_commit_callbacks, backup=False, settings={"wipe_blueprints": True}, seed=None)
    assert job.result["set_aside"] == ["player.blueprints.5.db"]
    ru.refresh_from_db()
    assert ru.game_options["server"]["seed"] != 777  # a new random map


def test_saves_need_the_capability(clients, fake_agent, django_capture_on_commit_callbacks):
    for game in ("hkmp", "cs2"):
        inst = create(clients, django_capture_on_commit_callbacks, game)
        resp = clients["owner"].get(f"/api/instances/{inst.id}/saves")
        assert resp.status_code == 409 and err(resp) == "unsupported_by_game", game


# --- diagnostics and the agent gate ----------------------------------------------------------------------------
def test_diagnostics_find_download_and_memory_problems(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    start(fake_agent, inst)
    fake_agent.log_lines[str(inst.id)] = [
        "Redirecting stderr to '/steamcmd/rust/steamcmd.log'",
        "Error! App '258550' state is 0x202 after update job.",
        "Exception: OutOfMemoryError",
    ]
    issues = {i["code"] for i in clients["owner"].get(f"/api/instances/{inst.id}/diagnostics").json()["issues"]}
    assert {"game_download_failed", "out_of_memory"} <= issues
    fake_agent.log_lines[str(inst.id)] = ["write failed: No space left on device"]
    issues = {i["code"] for i in clients["owner"].get(f"/api/instances/{inst.id}/diagnostics").json()["issues"]}
    assert "disk_full" in issues


@pytest.mark.parametrize("game", SIX)
def test_creating_needs_an_agent_with_the_game(game, clients, fake_agent):
    fake_agent.game_profiles.pop(game)
    resp = clients["owner"].post("/api/instances/", body(game), format="json")
    assert resp.status_code == 409 and err(resp) == "agent_lacks_game"


def test_starting_needs_an_agent_with_the_game(clients, fake_agent, django_capture_on_commit_callbacks):
    inst = create(clients, django_capture_on_commit_callbacks, "hkmp")
    fake_agent.containers.pop(str(inst.id))  # a stopped server has no container: the start creates it
    fake_agent.game_profiles.pop("hkmp")
    cache.clear()  # (the panel remembers what the agent reported for a while)
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED and job.error_code == "agent_lacks_game"


def test_first_start_waits_longer_for_big_downloads():
    assert games.get("cs2").first_start_timeout == 4 * 3600
    assert games.get("rust").first_start_timeout == 2 * 3600
    assert games.get("satisfactory").first_start_timeout is None  # the job's default, 30 minutes
    assert games.get("project-zomboid").retryable_install_exits == frozenset({1})
    assert not games.get("palworld").retryable_install_exits


# --- review of feature/more-games (audit-2026-11-09/more-games/review.md) -----------------------------------
def test_catalog_options_carry_their_patterns_and_the_update_rule(clients):
    entries = {g["id"]: g for g in clients["owner"].get("/api/catalog/games").json()}
    # the create form checks the pinned options that name files the way the panel and the agent do
    patterns = {g: {o["key"]: o.get("pattern") for o in entries[g]["options"]} for g in ("valheim", "rust", "project-zomboid")}
    assert patterns == {
        "valheim": {"world_name": r"^[A-Za-z0-9_-]{1,32}$"},
        "rust": {"identity": r"^[a-z0-9_-]{1,32}$"},
        "project-zomboid": {"server_name": r"^[A-Za-z0-9_-]{1,32}$", "branch": None},
    }
    assert all(o["pattern_help"] for g in patterns for o in entries[g]["options"] if o.get("pattern"))
    # whether a restart updates the game is said, not guessed from the version id
    updates = {g: entries[g]["fixed_version"]["updates_on_restart"] for g in SIX}
    assert updates == {g: g != "hkmp" for g in SIX}
    assert "updates_on_restart" not in entries["satisfactory"]["fixed_version"]  # (its pinned entry is unchanged)
    owner = clients["owner"]
    for game, option in (("valheim", "world_name"), ("rust", "identity"), ("project-zomboid", "server_name")):
        resp = owner.post("/api/instances/", body(game, options={option: "../x"}), format="json")
        assert resp.status_code == 400, game


def test_players_say_whether_lists_change_while_stopped(clients, fake_agent, django_capture_on_commit_callbacks):
    expected = {"palworld": False, "valheim": True, "project-zomboid": False, "hkmp": False, "cs2": False, "rust": False}
    for game, flag in expected.items():
        inst = create(clients, django_capture_on_commit_callbacks, game)
        data = clients["owner"].get(f"/api/instances/{inst.id}/players").json()
        assert data["running"] is False and data["changes_while_stopped"] is flag, game


def test_connection_lists_only_published_ports_with_their_real_container_port(
    clients, fake_agent, django_capture_on_commit_callbacks
):
    owner = clients["owner"]
    for game, port_name, setting, game_port_number in (
        ("cs2", "tv", "cstv", 30100),
        ("valheim", "crossplay", "crossplay", 30200),
        ("rust", "app", "rust_plus", 30300),
    ):
        inst = create(clients, django_capture_on_commit_callbacks, game, ports={"game": game_port_number})
        # the reserved number of the conditional port stays with the server, published or not
        assert port_name in {p["name"] for p in inst.ports}
        reserved = next(p["host_port"] for p in inst.ports if p["name"] == port_name)
        connection = owner.get(f"/api/instances/{inst.id}").json()["connection"]
        assert port_name not in {p["name"] for p in connection["ports"]}, game
        assert f" {reserved}" not in " ".join(connection["notes"]), game
        assert spec_names(inst) == {p["name"] for p in connection["ports"]}, game
        # published 1:1: the container listens on the host port's number
        game_port = next(p for p in connection["ports"] if p["name"] == "game")
        assert game_port["host_port"] == game_port["container_port"] == game_port_number, game
        assert all(p["container_port"] == p["host_port"] for p in inst.ports), game
        resp = owner.patch(f"/api/instances/{inst.id}/config/server", {"values": {setting: True}}, format="json")
        assert resp.status_code == 200, resp.json()
        connection = owner.get(f"/api/instances/{inst.id}").json()["connection"]
        on = next(p for p in connection["ports"] if p["name"] == port_name)
        assert on["host_port"] == on["container_port"] == reserved, game
        assert f"{on['protocol'].upper()} {reserved}" in " ".join(connection["notes"]), game


def spec_names(inst: Instance) -> set[str]:
    """The names of the ports the agent's spec publishes (by host port)."""
    inst.refresh_from_db()
    published = {p["host_port"] for p in build_spec(inst)["ports"]}
    return {p["name"] for p in inst.ports if p["host_port"] in published}


def test_timezone_values_follow_the_agents_pattern(clients, fake_agent, django_capture_on_commit_callbacks):
    from apps.core.models import SystemSetting

    owner = clients["owner"]
    for bad in ("../../etc/passwd", "Europe/Berlin;id", "a b", "/UTC"):
        resp = owner.post("/api/instances/", body("cs2", env={"TZ": bad}), format="json")
        assert resp.status_code == 400, bad
    inst = create(clients, django_capture_on_commit_callbacks, "cs2", env={"TZ": "America/Argentina/Buenos_Aires"})
    assert build_spec(inst)["env"]["TZ"] == "America/Argentina/Buenos_Aires"
    resp = owner.patch(f"/api/instances/{inst.id}", {"env": {"TZ": "$(id)"}}, format="json")
    assert resp.status_code == 400
    # the panel's own time zone reaches a server only when the agent would take it (else the image runs in UTC)
    other = create(clients, django_capture_on_commit_callbacks, "rust")
    SystemSetting.set("timezone", "Etc/GMT+5")
    assert build_spec(other)["env"]["TZ"] == "Etc/GMT+5"
    SystemSetting.set("timezone", "Bad Zone")
    assert "TZ" not in build_spec(other)["env"]
    # the games before the six keep their rule (any value without a line break)
    from apps.instances.serializers import validate_game_env

    for game in ("satisfactory", "factorio", "terraria"):
        assert validate_game_env(games.get(game), {"TZ": "Bad Zone"}) == {"TZ": "Bad Zone"}
