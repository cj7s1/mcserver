"""Boot-network repair (2026-11-12): Docker drops a container's network from its configuration when a start
fails while it programs a published port; every later start then runs it with no network but ``lo``.

* game containers: repaired before every start and right after a failed one (docker_ops);
* the platform's own containers (the agent's compose project) and the agent itself (platformnet).
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

from mcagent import docker_ops, pernet, platformnet
from mcagent.config import Settings
from mcagent.errors import AgentError

IID = uuid.UUID("b0b0b0b0-0000-4000-8000-0000000000b0")


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    s = Settings(data_root=tmp_path / "data", token="t", runtime_uid=99, runtime_gid=100)
    s.state_dir.mkdir(parents=True)
    return s


class Ctr:
    """A container as docker-py shows it; ``nets`` is what Docker still has, ``mode`` its NetworkMode."""

    def __init__(self, cid, *, mode="mcpanel_instances", nets=(), status="exited", labels=None, name=None):
        self.id, self.mode, self.nets, self.status = cid, mode, list(nets), status
        self.labels = dict(labels or {})
        self.name = name or f"c-{cid}"
        self.events: list = []

    @property
    def attrs(self):
        return {
            "HostConfig": {"NetworkMode": self.mode},
            "NetworkSettings": {"Networks": {n: {} for n in self.nets}},
            "State": {"Running": self.status == "running", "Restarting": self.status == "restarting"},
            "Config": {"Labels": self.labels, "Hostname": self.id[:12]},
        }

    def reload(self):
        pass

    def stop(self, timeout=None):
        self.events.append("stop")
        self.status = "exited"

    def start(self):
        self.events.append("start")
        self.status = "running"

    def remove(self, v=False, force=False):
        self.events.append("remove")


class Docker:
    def __init__(self, containers=(), refuse=False):
        self.ctrs = {c.id: c for c in containers}
        self.connected: list = []
        self.refuse = refuse
        self.api = SimpleNamespace(connect_container_to_network=self._connect)
        self.containers = SimpleNamespace(get=self._get, list=self._list)

    def _connect(self, cid, net, aliases=None, **_kw):
        if self.refuse:
            raise APIError("network not found")
        self.connected.append((cid, net, tuple(aliases or ())))
        self.ctrs[cid].nets.append(net)

    def _get(self, cid):
        if cid not in self.ctrs:
            raise NotFound("no such container")
        return self.ctrs[cid]

    def _list(self, all=False, filters=None, **_kw):
        out = list(self.ctrs.values())
        if filters and "label" in filters:
            key, _, value = filters["label"].partition("=")
            out = [c for c in out if c.labels.get(key) == value]
        return out


# --- game containers ---------------------------------------------------------------------------------------
def test_a_dropped_network_is_attached_again(settings, monkeypatch):
    c = Ctr("g1")
    docker = Docker([c])
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    ensured: list = []
    monkeypatch.setattr(docker_ops, "ensure_network", lambda s: ensured.append(s.instance_network))
    assert docker_ops.repair_dropped_network(settings, c) == "mcpanel_instances"
    assert docker.connected == [("g1", "mcpanel_instances", ())] and ensured == ["mcpanel_instances"]


def test_a_server_network_is_made_again_before_the_attach(settings, monkeypatch):
    name = pernet.network_name(IID)
    c = Ctr("g2", mode=name, labels={"io.mcpanel.instance": str(IID)})
    docker = Docker([c])
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    made: list = []
    monkeypatch.setattr(pernet, "ensure", lambda s, iid, attach=True: made.append((iid, attach)) or name)
    assert docker_ops.repair_dropped_network(settings, c) == name
    assert made == [(IID, False)] and docker.connected == [("g2", name, ())]


@pytest.mark.parametrize(
    ("ctr", "why"),
    [
        (Ctr("g3", nets=["mcpanel_instances"]), "still on its network"),
        (Ctr("g4", status="running"), "running"),
        (Ctr("g5", mode="host"), "host network"),
        (Ctr("g6", mode="default"), "default"),
    ],
)
def test_nothing_to_repair(settings, monkeypatch, ctr, why):
    docker = Docker([ctr])
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda s: None)
    assert docker_ops.repair_dropped_network(settings, ctr) is None and docker.connected == [], why


def _start_fixture(settings, monkeypatch, c, docker):
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    monkeypatch.setattr(docker_ops, "require_container", lambda _iid: c)
    monkeypatch.setattr(docker_ops, "start_guard", lambda *a: None)
    monkeypatch.setattr(docker_ops, "prepare_start", lambda *a: None)
    monkeypatch.setattr(docker_ops, "reattach_recreated_networks", lambda *a: [])
    monkeypatch.setattr(docker_ops, "ensure_network", lambda s: None)
    monkeypatch.setattr(docker_ops, "check_bind_sources", lambda *a: None)
    monkeypatch.setattr(docker_ops, "save_last_run_record", lambda *a: True)


def test_a_failed_start_puts_the_network_back_and_keeps_its_answer(settings, monkeypatch):
    c = Ctr("g7", nets=["mcpanel_instances"], labels={"io.mcpanel.instance": str(IID)})

    def fail():  # Docker refuses the published port and drops the network from the container
        c.nets.clear()
        raise APIError(
            "driver failed programming external connectivity: Bind for 0.0.0.0:25565 failed: port is already allocated"
        )

    c.start = fail
    docker = Docker([c])
    _start_fixture(settings, monkeypatch, c, docker)
    with pytest.raises(AgentError) as err:
        docker_ops.start(IID, settings)
    assert err.value.code == "port_conflict" and err.value.status == 409  # the answer is unchanged
    assert c.nets == ["mcpanel_instances"] and "remove" not in c.events


def test_the_next_start_repairs_a_container_that_lost_its_network(settings, monkeypatch):
    c = Ctr("g8", labels={"io.mcpanel.instance": str(IID)})  # what an older agent left behind
    docker = Docker([c])
    _start_fixture(settings, monkeypatch, c, docker)
    docker_ops.start(IID, settings)
    assert docker.connected == [("g8", "mcpanel_instances", ())] and c.events == ["start"]


def test_a_container_that_cannot_be_repaired_is_removed(settings, monkeypatch):
    c = Ctr("g9", labels={"io.mcpanel.instance": str(IID)})
    docker = Docker([c], refuse=True)
    _start_fixture(settings, monkeypatch, c, docker)
    monkeypatch.setattr(pernet, "release", lambda *a: False)
    with pytest.raises(AgentError) as err:
        docker_ops.start(IID, settings)
    assert err.value.code == "no_container" and err.value.status == 404  # the panel then creates one
    assert c.events == ["remove"]


# --- the platform's own containers -----------------------------------------------------------------------------
AGENT = "a" * 64
PROJECT = {"com.docker.compose.project": "mcpanel"}


def _platform(monkeypatch, *ctrs):
    docker = Docker(ctrs)
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    monkeypatch.setattr(pernet, "_own", {"id": AGENT})
    monkeypatch.setattr(platformnet, "_tries", {})
    monkeypatch.setattr(platformnet, "_recent", platformnet.collections.deque(maxlen=20))
    return docker


def _agent(nets=("mcpanel_internal", "mcpanel_instances")):
    return Ctr(
        AGENT,
        mode="mcpanel_internal",
        nets=nets,
        status="running",
        labels={**PROJECT, "com.docker.compose.service": "agent"},
        name="mcpanel-agent-1",
    )


def test_a_stack_container_without_its_network_is_repaired(settings, monkeypatch):
    panel = Ctr(
        "p1",
        mode="mcpanel_internal",
        status="restarting",
        labels={**PROJECT, "com.docker.compose.service": "panel"},
        name="mcpanel-panel-1",
    )
    other = Ctr("x1", mode="other_default", status="running", labels={"com.docker.compose.project": "other"})
    fine = Ctr(
        "p2", mode="mcpanel_internal", nets=["mcpanel_internal"], status="running", labels=dict(PROJECT)
    )
    docker = _platform(monkeypatch, _agent(), panel, other, fine)
    done = platformnet.repair_once(settings, now=1000.0)
    assert done == [{"container": "mcpanel-panel-1", "result": "repaired"}]
    assert docker.connected == [("p1", "mcpanel_internal", ("panel", "mcpanel-panel-1"))]
    assert panel.events == ["stop", "start"]
    assert other.events == [] and fine.events == []  # another project, and one that has its network
    status = platformnet.status(settings)
    assert status["enabled"] and status["project"] == "mcpanel"
    assert (
        status["recent"][0]["container"] == "mcpanel-panel-1" and status["recent"][0]["result"] == "repaired"
    )


def test_the_agent_reconnects_itself_without_a_restart(settings, monkeypatch):
    me = _agent(nets=())
    docker = _platform(monkeypatch, me)
    platformnet.repair_once(settings, now=1000.0)
    assert docker.connected == [
        (AGENT, "mcpanel_internal", ("agent", "mcpanel-agent-1")),
        (AGENT, "mcpanel_instances", ("agent", "mcpanel-agent-1")),
    ]
    assert me.events == []  # never stopped


def test_the_agent_on_a_server_network_only_still_gets_its_stack_networks_back(settings, monkeypatch):
    """Its server networks do not count: disconnected by hand from the stack's networks while a server runs."""
    me = _agent(nets=[pernet.network_name(IID)])
    docker = _platform(monkeypatch, me)
    platformnet.repair_once(settings, now=1000.0)
    assert [net for _cid, net, _a in docker.connected] == ["mcpanel_internal", "mcpanel_instances"]


def test_one_attempt_per_five_minutes_and_three_in_all(settings, monkeypatch, caplog):
    panel = Ctr(
        "p3",
        mode="mcpanel_internal",
        status="running",
        labels={**PROJECT, "com.docker.compose.service": "panel"},
    )
    _platform(monkeypatch, _agent(), panel)

    def undo():  # someone disconnects it again every time (a deliberate docker network disconnect)
        panel.nets.clear()

    attempts = 0
    for t in (0, 60, 300, 400, 600, 900, 1200, 1500):
        undo()
        attempts += len(platformnet.repair_once(settings, now=1000.0 + t))
    assert attempts == 3  # at 0, 300, 600; nothing in between; given up afterwards
    assert any("not repairing it any more" in r.message for r in caplog.records)
    assert platformnet.status(settings)["given_up"] == ["p3"]


def test_nothing_happens_when_switched_off_or_without_a_project(settings, monkeypatch):
    panel = Ctr("p4", mode="mcpanel_internal", status="running", labels=dict(PROJECT))
    _platform(monkeypatch, _agent(), panel)
    off = settings.model_copy(update={"repair_platform_networks": False})
    assert platformnet.repair_once(off) == [] and panel.events == []
    me = _agent()
    me.labels = {}
    _platform(monkeypatch, me, panel)
    assert platformnet.repair_once(settings) == [] and panel.events == []
    monkeypatch.setattr(pernet, "_own", {"id": None})  # not in a container
    assert platformnet.repair_once(settings) == []


def test_info_reports_the_repairs(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    body = api.get("/v1/info", headers=auth).json()
    assert body["platform_network_repair"] == {"enabled": True, "project": None, "recent": [], "given_up": []}


def test_every_stack_network_seen_before_is_given_back(settings, monkeypatch):
    """Docker keeps only the first network in NetworkMode; the agent remembers what each service was on."""
    me = _agent()
    me.mode = "mcpanel_instances"  # compose picks one of the two
    docker = _platform(monkeypatch, me)
    platformnet.repair_once(settings, now=1000.0)  # fine: remembered
    me.nets.clear()
    platformnet.repair_once(settings, now=2000.0)
    assert sorted(net for _cid, net, _a in docker.connected) == ["mcpanel_instances", "mcpanel_internal"]
    stored = (settings.state_dir / platformnet.STATE_FILE).read_text()
    assert "mcpanel/agent" in stored


# --- lost compose aliases (Unraid's Docker start script reconnects endpoints without them) -----------------------
class Endpointed(Ctr):
    """A container whose endpoints carry aliases, IP and MAC."""

    def __init__(self, cid, endpoints, **kw):
        super().__init__(cid, nets=list(endpoints), **kw)
        self.endpoints = endpoints

    @property
    def attrs(self):
        base = super().attrs
        base["NetworkSettings"]["Networks"] = {n: dict(self.endpoints[n]) for n in self.nets}
        return base


class AliasDocker(Docker):
    def __init__(self, containers, projects, refuse_ip=False):
        super().__init__(containers)
        self.projects = projects
        self.refuse_ip = refuse_ip
        self.calls: list = []
        self.api = SimpleNamespace(
            connect_container_to_network=self._connect_ep, disconnect_container_from_network=self._disconnect
        )
        self.networks = SimpleNamespace(get=self._net)

    def _net(self, name):
        project = self.projects.get(name)
        return SimpleNamespace(attrs={"Labels": {"com.docker.compose.project": project} if project else {}})

    def _disconnect(self, cid, net, force=False):
        self.calls.append(("disconnect", cid, net, force))
        self.ctrs[cid].nets.remove(net)

    def _connect_ep(self, cid, net, ipv4_address=None, aliases=None, mac_address=None, **_kw):
        if ipv4_address and self.refuse_ip:
            raise APIError(
                "user specified IP address is supported only when connecting to networks with user configured subnets"
            )
        self.calls.append(("connect", cid, net, ipv4_address, tuple(aliases or ()), mac_address))
        c = self.ctrs[cid]
        c.endpoints[net] = {
            **c.endpoints.get(net, {}),
            "Aliases": list(aliases or []),
            "IPAddress": ipv4_address or "172.30.0.9",
        }
        c.nets.append(net)


def _db(aliases=()):
    return Endpointed(
        "d1",
        {
            "mcpanel_internal": {
                "Aliases": list(aliases),
                "DNSNames": ["mcpanel-postgres-1", "d1"],
                "IPAddress": "172.30.0.5",
                "MacAddress": "02:42:ac:1e:00:05",
            }
        },
        mode="mcpanel_internal",
        status="running",
        labels={**PROJECT, "com.docker.compose.service": "postgres"},
        name="mcpanel-postgres-1",
    )


def _alias_platform(monkeypatch, *ctrs, projects=None, refuse_ip=False):
    docker = AliasDocker(
        ctrs, projects or {"mcpanel_internal": "mcpanel", "mcpanel_instances": "mcpanel"}, refuse_ip
    )
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    monkeypatch.setattr(pernet, "_own", {"id": AGENT})
    monkeypatch.setattr(platformnet, "_tries", {})
    monkeypatch.setattr(platformnet, "_recent", platformnet.collections.deque(maxlen=20))
    return docker


def _named_agent():
    names = {
        "Aliases": ["mcpanel-agent-1", "agent"],
        "IPAddress": "172.30.0.2",
        "MacAddress": "02:42:ac:1e:00:02",
    }
    return Endpointed(
        AGENT, {"mcpanel_internal": dict(names), "mcpanel_instances": dict(names)}, mode="mcpanel_internal",
        status="running", labels={**PROJECT, "com.docker.compose.service": "agent"}, name="mcpanel-agent-1",
    )  # fmt: skip


def test_lost_aliases_come_back_with_ip_and_mac_kept(settings, monkeypatch):
    db = _db(aliases=())  # Unraid's reconnect: MAC only, no aliases
    docker = _alias_platform(monkeypatch, _named_agent(), db)
    done = platformnet.repair_once(settings, now=1000.0)
    assert done == [{"container": "mcpanel-postgres-1", "result": "restored"}]
    assert docker.calls == [
        ("disconnect", "d1", "mcpanel_internal", True),
        (
            "connect",
            "d1",
            "mcpanel_internal",
            "172.30.0.5",
            ("postgres", "mcpanel-postgres-1"),
            "02:42:ac:1e:00:05",
        ),
    ]
    assert db.events == []  # a running container is not stopped for this
    recent = platformnet.status(settings)["recent"][0]
    assert recent["action"] == "reconnect with aliases" and recent["result"] == "restored"


def test_a_network_without_a_configured_subnet_lets_docker_pick_the_address(settings, monkeypatch):
    db = _db()
    docker = _alias_platform(monkeypatch, _named_agent(), db, refuse_ip=True)
    assert platformnet.repair_once(settings, now=1000.0)[0]["result"] == "restored"
    assert docker.calls[-1] == (
        "connect",
        "d1",
        "mcpanel_internal",
        None,
        ("postgres", "mcpanel-postgres-1"),
        "02:42:ac:1e:00:05",
    )


def test_named_endpoints_and_other_projects_are_left_alone(settings, monkeypatch):
    fine = _db(aliases=["mcpanel-postgres-1", "postgres"])
    foreign_net = Endpointed(
        "e1", {"elsewhere_default": {"Aliases": [], "IPAddress": "172.31.0.2"}}, mode="elsewhere_default",
        status="running", labels={**PROJECT, "com.docker.compose.service": "postgres"}, name="mcpanel-x-1",
    )  # fmt: skip
    other_project = Endpointed(
        "o1", {"other_default": {"Aliases": [], "IPAddress": "172.32.0.2"}}, mode="other_default",
        status="running", labels={"com.docker.compose.project": "other", "com.docker.compose.service": "web"},
    )  # fmt: skip
    docker = _alias_platform(
        monkeypatch, _named_agent(), fine, foreign_net, other_project,
        projects={"mcpanel_internal": "mcpanel", "mcpanel_instances": "mcpanel", "elsewhere_default": "elsewhere", "other_default": "other"},
    )  # fmt: skip
    assert platformnet.repair_once(settings, now=1000.0) == [] and docker.calls == []


def test_the_agents_own_aliases_come_back(settings, monkeypatch):
    me = _named_agent()
    me.endpoints["mcpanel_internal"]["Aliases"] = []
    docker = _alias_platform(monkeypatch, me)
    assert platformnet.repair_once(settings, now=1000.0)[0]["result"] == "restored"
    assert [c[2] for c in docker.calls] == ["mcpanel_internal", "mcpanel_internal"]


def test_aliases_removed_again_and_again_end_in_giving_up(settings, monkeypatch, caplog):
    db = _db()
    _alias_platform(monkeypatch, _named_agent(), db)
    tries = 0
    for t in (0, 100, 300, 600, 900, 1200):
        db.endpoints["mcpanel_internal"]["Aliases"] = []  # removed again (deliberately, or by the script)
        tries += len(platformnet.repair_once(settings, now=1000.0 + t))
    assert tries == 3 and any("not repairing it any more" in r.message for r in caplog.records)
