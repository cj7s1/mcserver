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
