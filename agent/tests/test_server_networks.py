"""A-1 of the security review (2026-11-11), interface I-6: one bridge network per server (mcagent/pernet.py).

The fake Docker keeps networks (name, subnet, labels, attached containers) and containers (id, labels, the
networks they are on, running or not); it refuses a subnet that overlaps another network like Docker does.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

from mcagent import docker_ops, pernet
from mcagent.config import Settings
from mcagent.schemas import InstanceSpec

OWN = "a" * 64
REAL_CANDIDATE_IDS = pernet._candidate_ids  # (the docker fixture replaces it)
A = uuid.UUID("11111111-1111-4111-8111-111111111111")
B = uuid.UUID("22222222-2222-4222-8222-222222222222")
C = uuid.UUID("33333333-3333-4333-8333-333333333333")


class Net:
    def __init__(self, docker, name, subnet, labels=None, driver="bridge"):
        self.docker, self.name, self.id = docker, name, f"id-{name}-{len(docker.created)}"
        self.attrs = {
            "Name": name,
            "Driver": driver,
            "Labels": dict(labels or {}),
            "IPAM": {"Config": [{"Subnet": str(subnet)}] if subnet else []},
        }

    def remove(self):
        attached = [c for c in self.docker.ctrs.values() if self.name in c.nets]
        if attached:
            raise APIError("network has active endpoints")
        self.docker.nets.pop(self.name)
        self.docker.removed.append(self.name)


class Ctr:
    def __init__(self, cid, labels=None, nets=(), running=False):
        self.id, self.labels, self.nets, self.running = cid, dict(labels or {}), list(nets), running

    @property
    def attrs(self):
        return {
            "NetworkSettings": {"Networks": {n: {"NetworkID": f"id-{n}"} for n in self.nets}},
            "State": {"Running": self.running},
            "Config": {"Hostname": self.id[:12]},
        }


class FakeDocker:
    def __init__(self):
        self.nets: dict[str, Net] = {}
        self.ctrs: dict[str, Ctr] = {OWN: Ctr(OWN, running=True)}
        self.created: list[str] = []
        self.removed: list[str] = []
        self.refuse: set[str] = set()  # subnets Docker refuses although nothing visible overlaps
        self.networks = SimpleNamespace(get=self._get, list=self._list, create=self._create)
        self.containers = SimpleNamespace(get=self._cget, list=self._clist)
        self.api = SimpleNamespace(
            connect_container_to_network=self._connect, disconnect_container_from_network=self._disconnect
        )

    def add(self, name, subnet, labels=None, driver="bridge"):
        self.nets[name] = Net(self, name, subnet, labels, driver)
        return self.nets[name]

    def _get(self, name):
        if name not in self.nets:
            raise NotFound("no such network")
        return self.nets[name]

    def _list(self, filters=None):
        nets = list(self.nets.values())
        if filters and "label" in filters:
            key, _, value = filters["label"].partition("=")
            nets = [n for n in nets if n.attrs["Labels"].get(key) == value]
        return nets

    def _create(self, name, driver, ipam, labels, check_duplicate):
        subnet = ipaddress.ip_network(ipam["Config"][0]["Subnet"])
        for n in self.nets.values():
            for cfg in n.attrs["IPAM"]["Config"]:
                if ipaddress.ip_network(cfg["Subnet"]).overlaps(subnet):
                    raise APIError("Pool overlaps with other one on this address space")
        if str(subnet) in self.refuse:
            raise APIError("Pool overlaps with other one on this address space")
        self.created.append(name)
        return self.add(name, subnet, labels, driver)

    def _cget(self, cid):
        for c in self.ctrs.values():
            if c.id == cid or c.id.startswith(cid):
                return c
        raise NotFound("no such container")

    def _clist(self, all=False, filters=None, **_kw):
        out = [c for c in self.ctrs.values() if all or c.running]
        if filters and "network" in filters:
            out = [c for c in out if filters["network"] in c.nets]
        if filters and "label" in filters:
            key, _, value = filters["label"].partition("=")
            out = [c for c in out if c.labels.get(key) == value]
        return out

    def _connect(self, cid, name):
        if name not in self.nets:
            raise APIError("network not found")
        c = self._cget(cid)
        if name in c.nets:
            raise APIError(f"endpoint with name x already exists in network {name}")
        c.nets.append(name)

    def _disconnect(self, cid, name, force=False):
        c = self._cget(cid)
        if name in c.nets:
            c.nets.remove(name)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    s = Settings(data_root=tmp_path / "data", token="t", runtime_uid=99, runtime_gid=100)
    s.state_dir.mkdir(parents=True)
    return s


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(pernet, "_own", {"id": OWN})
    # what the agent's own container looks like from inside (a refresh finds it again)
    monkeypatch.setattr(pernet, "_candidate_ids", lambda: [OWN[:12]])
    monkeypatch.setattr(pernet.socket, "gethostname", lambda: OWN[:12])
    return fake


def _subnet(docker, name):
    return docker.nets[name].attrs["IPAM"]["Config"][0]["Subnet"]


# --- allocation ------------------------------------------------------------------------------------------
def test_each_server_gets_its_own_bridge_and_keeps_its_subnet(settings, docker):
    a = pernet.prepare(settings, A)
    b = pernet.prepare(settings, B)
    assert (a, b) == ("mcpanel_i_111111111111", "mcpanel_i_222222222222")
    assert (_subnet(docker, a), _subnet(docker, b)) == ("10.213.0.0/29", "10.213.0.8/29")
    labels = docker.nets[a].attrs["Labels"]
    assert labels == {
        "io.mcpanel.managed": "true",
        "io.mcpanel.instance": str(A),
        "io.mcpanel.network": "server",
    }
    assert a in docker.ctrs[OWN].nets and b in docker.ctrs[OWN].nets  # the agent joined both
    # the network is deleted (Unraid at a Docker restart): created again with the same subnet
    docker.ctrs[OWN].nets.remove(a)
    docker.nets.pop(a)
    assert pernet.prepare(settings, A) == a and _subnet(docker, a) == "10.213.0.0/29"
    stored = json.loads((settings.state_dir / pernet.STATE_FILE).read_text())
    assert stored["subnets"] == {str(A): "10.213.0.0/29", str(B): "10.213.0.8/29"}


def test_a_subnet_in_use_or_refused_is_skipped(settings, docker):
    docker.add("someone_else", "10.213.0.0/28")  # covers the first two /29s
    docker.refuse.add("10.213.0.16/29")  # Docker refuses this one for its own reasons
    name = pernet.prepare(settings, A)
    assert _subnet(docker, name) == "10.213.0.24/29"
    assert json.loads((settings.state_dir / pernet.STATE_FILE).read_text())["refused"] == ["10.213.0.16/29"]


def test_an_exhausted_pool_falls_back_to_the_shared_network(settings, docker):
    small = settings.model_copy(update={"instance_pool": "10.213.0.0/28"})
    assert pernet.prepare(small, A) and pernet.prepare(small, B)
    assert pernet.prepare(small, C) is None
    status = pernet.status(small)
    assert status["enabled"] is True and "no free /29" in status["reason"]
    assert status["prefix"] == "mcpanel_i_" and status["pool"] == "10.213.0.0/28"


@pytest.mark.parametrize(
    ("pool", "why"),
    [
        ("8.8.0.0/16", "private"),
        ("10.213.0.0/29", "too small"),
        ("nonsense", "not a network"),
        ("fd00::/64", "IPv4"),
    ],
)
def test_the_pool_is_validated(settings, docker, pool, why):
    bad = settings.model_copy(update={"instance_pool": pool})
    assert pernet.prepare(bad, A) is None and docker.created == []
    assert why in pernet.status(bad)["reason"]


def test_switched_off_or_outside_a_container_means_the_shared_network(settings, docker, monkeypatch):
    off = settings.model_copy(update={"per_server_networks": False})
    assert pernet.prepare(off, A) is None and "switched off" in pernet.status(off)["reason"]
    monkeypatch.setattr(pernet, "_own", {"id": None})
    assert pernet.prepare(settings, A) is None and docker.created == []
    assert pernet.status(settings) == {
        "enabled": False,
        "reason": pernet.availability(settings),
        "prefix": "mcpanel_i_",
        "pool": "10.213.0.0/16",
        "subnet_prefix": 29,
    }


def test_a_network_the_agent_cannot_join_is_removed_again(settings, docker, monkeypatch):
    def refuse(cid, name):
        raise APIError("permission denied")

    monkeypatch.setattr(docker.api, "connect_container_to_network", refuse)
    assert pernet.prepare(settings, A) is None
    assert docker.removed == ["mcpanel_i_111111111111"]


def test_a_foreign_network_with_the_name_is_never_used(settings, docker):
    docker.add("mcpanel_i_111111111111", "10.99.0.0/24", labels={})
    with pytest.raises(Exception) as exc:
        pernet.ensure(settings, A)
    assert getattr(exc.value, "code", "") == "foreign_network"


# --- own container ---------------------------------------------------------------------------------------
def test_the_agent_finds_its_own_container(docker, monkeypatch, tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"1 2 0:1 /var/lib/docker/containers/{OWN}/hostname /etc/hostname rw - ext4 /dev/x rw\n"
        "3 4 0:2 /var/lib/docker/containers/" + "b" * 64 + "/hostname /data/x rw - ext4 /dev/y rw\n"
    )
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/\n")
    monkeypatch.setattr(pernet.socket, "gethostname", lambda: "box")
    assert REAL_CANDIDATE_IDS(mountinfo, cgroup) == ["box", OWN]  # only the /etc/hostname mount counts
    monkeypatch.setattr(pernet, "_candidate_ids", lambda: ["box", OWN])
    # the candidate's hostname is not this process's: nothing is guessed
    assert pernet.own_container_id(refresh=True) is None
    monkeypatch.setattr(pernet.socket, "gethostname", lambda: OWN[:12])
    assert pernet.own_container_id(refresh=True) == OWN
    docker.ctrs[OWN].running = False  # a stopped container is never "me"
    assert pernet.own_container_id(refresh=True) is None


# --- release and the start-up sweep ------------------------------------------------------------------------
def test_release_keeps_a_network_a_container_still_uses(settings, docker):
    name = pernet.prepare(settings, A)
    docker.ctrs["old"] = Ctr("old", {"io.mcpanel.instance": str(A)}, [name])  # stopped, still there
    assert pernet.release(settings, A) is False and name in docker.nets
    del docker.ctrs["old"]
    assert pernet.release(settings, A) is True and name not in docker.nets
    assert pernet.recorded_subnet(settings, A) == "10.213.0.0/29"  # kept for the next create
    pernet.forget(settings, A)  # the server is deleted
    assert pernet.recorded_subnet(settings, A) is None


def test_release_never_touches_a_network_without_the_agents_labels(settings, docker):
    docker.add("mcpanel_i_111111111111", "10.213.0.0/29", labels={"io.mcpanel.managed": "true"})
    assert pernet.release(settings, A) is False and "mcpanel_i_111111111111" in docker.nets


def test_startup_recreates_joins_and_sweeps(settings, docker):
    a, b = pernet.prepare(settings, A), pernet.prepare(settings, B)
    # A's container exists (stopped); its network was deleted by a Docker restart
    docker.ctrs["ca"] = Ctr("ca", {"io.mcpanel.managed": "true", "io.mcpanel.instance": str(A)}, [a])
    docker.ctrs[OWN].nets.clear()  # the agent's container is new (an upgrade)
    docker.nets.pop(a)
    # B's server was removed while the agent was down: its network is a leftover
    docker.add("bystander", "172.30.0.0/24", labels={"io.mcpanel.managed": "true"})
    locks: list = []

    @contextlib.contextmanager
    def lock_for(iid):
        locks.append(iid)
        yield

    done = pernet.startup(settings, lock_for)
    assert done["recreated"] == [a] and locks == [A]
    assert _subnet(docker, a) == "10.213.0.0/29" and a in docker.ctrs[OWN].nets
    assert done["removed"] == [b] and b not in docker.nets
    assert "bystander" in docker.nets  # not a server network


# --- docker_ops: create, reattach, retire --------------------------------------------------------------------
def _spec(**over):
    values = {
        "name": "mc",
        "image": "itzg/minecraft-server:stable-java21",
        "env": {"EULA": "TRUE"},
        "memory_limit_mb": 1024,
        "rcon_password": "x" * 16,
    }
    values.update(over)
    return InstanceSpec(**values)


def test_create_container_puts_a_spec_without_network_on_its_own_bridge(settings, docker, monkeypatch):
    created: list[dict] = []
    docker.api.api_version = "1.47"
    docker.api.create_container = lambda **kw: created.append(kw) or {"Id": "new"}
    monkeypatch.setattr(docker.containers, "get", lambda cid: SimpleNamespace(id=cid))
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: created.append({"shared": True}))
    docker_ops.create_container(settings, A, _spec(), "h")
    assert created[-1]["host_config"]["NetworkMode"] == "mcpanel_i_111111111111"
    # a spec naming a network keeps it (today's behaviour), the shared one included
    docker_ops.create_container(settings, B, _spec(network="mcpanel_instances"), "h")
    assert created[-1]["host_config"]["NetworkMode"] == "mcpanel_instances" and {"shared": True} in created
    # without the feature: the shared network
    off = settings.model_copy(update={"per_server_networks": False})
    docker_ops.create_container(off, C, _spec(), "h")
    assert created[-1]["host_config"]["NetworkMode"] == "mcpanel_instances"


def test_a_start_recreates_a_missing_server_network(settings, docker):
    name = pernet.prepare(settings, A)
    docker.ctrs[OWN].nets.clear()
    docker.nets.pop(name)

    stopped = SimpleNamespace(
        id="ca",
        name="mcpanel-a",
        status="exited",
        labels={"io.mcpanel.instance": str(A)},
        attrs={"NetworkSettings": {"Networks": {name: {"NetworkID": "gone"}}}},
        reload=lambda: None,
    )
    reconnected: list = []
    docker.api.disconnect_container_from_network = lambda cid, n, force=False: reconnected.append(("off", n))
    docker.api.connect_container_to_network = lambda cid, n: reconnected.append(("on", cid, n))
    assert docker_ops.reattach_recreated_networks(stopped, settings) == [name]
    assert name in docker.nets and _subnet(docker, name) == "10.213.0.0/29"
    assert ("on", OWN, name) in reconnected and ("on", "ca", name) in reconnected


def test_the_source_guard_refuses_the_pool_without_docker(monkeypatch):
    from mcagent import main

    monkeypatch.setattr(main, "_pool", ipaddress.ip_network("10.213.0.0/16"))
    monkeypatch.setattr(main, "_blocked_networks", [])
    monkeypatch.setattr(main, "_blocked_gateways", [])
    assert main._source_verdict(("10.213.4.2", 1)) == "blocked"
    assert main._source_verdict(("::ffff:10.213.4.2", 1)) == "blocked"
    assert main._source_verdict(("10.214.0.2", 1)) is None
    monkeypatch.setattr(main, "_blocked_networks", None)  # Docker not asked yet: still refused
    assert main._source_verdict(("10.213.0.3", 1)) == "blocked"


def test_info_reports_the_network_names(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    r = api.get("/v1/info", headers=auth)
    body = r.json()
    assert body["policy"]["instance_network"] == "mcpanel_instances"
    assert body["per_server_networks"]["prefix"] == "mcpanel_i_" and body["per_server_networks"]["pool"]
    assert "per_server_networks" in body["features"]
