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


# --- the platform's own containers (and lost compose aliases) -------------------------------------------------
AGENT = "a" * 64
PROJECT = {"com.docker.compose.project": "mcpanel"}


def svc(service, project="mcpanel", **extra):
    """The labels compose sets on a long-running service container."""
    return {
        "com.docker.compose.project": project,
        "com.docker.compose.service": service,
        "com.docker.compose.config-hash": "c0ffee",
        "com.docker.compose.container-number": "1",
        "com.docker.compose.oneoff": "False",
        **extra,
    }


class Endpointed(Ctr):
    """A container whose endpoints carry aliases, IP and MAC."""

    def __init__(self, cid, endpoints, **kw):
        super().__init__(cid, nets=list(endpoints), **kw)
        self.endpoints = {k: dict(v) for k, v in endpoints.items()}

    @property
    def attrs(self):
        base = super().attrs
        base["NetworkSettings"]["Networks"] = {n: dict(self.endpoints.get(n, {})) for n in self.nets}
        return base


class StackDocker(Docker):
    """Docker with compose networks (labels) and endpoints; ``fail(cid, net, kwargs)`` may refuse a connect."""

    def __init__(self, containers, networks=None, fail=None):
        super().__init__(containers)
        self.nets = networks or {
            "mcpanel_internal": ("mcpanel", "internal"),
            "mcpanel_instances": ("mcpanel", "instances"),
        }
        self.fail = fail
        self.calls: list = []
        self.api = SimpleNamespace(
            connect_container_to_network=self._connect_ep, disconnect_container_from_network=self._disconnect
        )
        self.networks = SimpleNamespace(get=self._net, list=self._net_list)

    def _labels(self, name):
        project, key = self.nets.get(name, (None, None))
        return {"com.docker.compose.project": project, "com.docker.compose.network": key} if project else {}

    def _net(self, name):
        return SimpleNamespace(name=name, attrs={"Labels": self._labels(name)})

    def _net_list(self, filters=None):
        key, _, value = (filters or {}).get("label", "=").partition("=")
        return [self._net(n) for n in self.nets if self._labels(n).get(key) == value]

    def _disconnect(self, cid, net, force=False):
        self.calls.append(("disconnect", cid, net))
        self.ctrs[cid].nets.remove(net)

    def _connect_ep(self, cid, net, ipv4_address=None, aliases=None, mac_address=None, **_kw):
        if self.fail and self.fail(cid, net, {"ip": ipv4_address, "aliases": aliases, "mac": mac_address}):
            raise APIError("refused")
        self.calls.append(("connect", cid, net, ipv4_address, tuple(aliases or ()), mac_address))
        c = self.ctrs[cid]
        c.endpoints[net] = {
            "Aliases": list(aliases or []),
            "IPAddress": ipv4_address or "172.30.0.9",
            "MacAddress": mac_address or "02:00:00:00:00:99",
        }
        c.nets.append(net)


def _stack(monkeypatch, *ctrs, networks=None, fail=None):
    docker = StackDocker(ctrs, networks, fail)
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    monkeypatch.setattr(pernet, "_own", {"id": AGENT})
    monkeypatch.setattr(platformnet, "_tries", {})
    monkeypatch.setattr(platformnet, "_recent", platformnet.collections.deque(maxlen=20))
    monkeypatch.setattr(platformnet, "_on_self_repaired", None)
    return docker


def _ep(name, ip, mac, aliases=None):
    return {
        "Aliases": list(aliases) if aliases is not None else [name.split("-")[1], name],
        "IPAddress": ip,
        "MacAddress": mac,
    }


def _named_agent(nets=("mcpanel_internal", "mcpanel_instances"), mode="mcpanel_instances"):
    eps = {
        n: _ep("mcpanel-agent-1", "172.30.0.2", "02:42:ac:1e:00:02", ["mcpanel-agent-1", "agent"])
        for n in nets
    }
    return Endpointed(AGENT, eps, mode=mode, status="running", labels=svc("agent"), name="mcpanel-agent-1")


def _db(aliases=("mcpanel-postgres-1", "postgres"), status="running", cid="d1"):
    return Endpointed(
        cid,
        {"mcpanel_internal": _ep("mcpanel-postgres-1", "172.30.0.5", "02:42:ac:1e:00:05", aliases)},
        mode="mcpanel_internal",
        status=status,
        labels=svc("postgres"),
        name="mcpanel-postgres-1",
    )


def test_a_member_without_its_network_is_stopped_connected_and_started(settings, monkeypatch):
    panel = Endpointed(
        "p1", {}, mode="mcpanel_internal", status="restarting", labels=svc("panel"), name="mcpanel-panel-1"
    )
    other = Endpointed("x1", {}, mode="other_default", status="running", labels=svc("web", project="other"))
    docker = _stack(monkeypatch, _named_agent(), panel, other, _db())
    done = platformnet.repair_once(settings, now=1000.0)
    assert done == [{"container": "mcpanel-panel-1", "result": "repaired"}]
    assert docker.calls == [("connect", "p1", "mcpanel_internal", None, ("panel", "mcpanel-panel-1"), None)]
    assert panel.events == ["stop", "start"] and other.events == []
    status = platformnet.status(settings)
    assert (
        status["enabled"] and status["project"] == "mcpanel" and status["recent"][0]["result"] == "repaired"
    )


def test_the_agent_reconnects_itself_live_to_its_stack_networks(settings, monkeypatch):
    """Even when it never recorded them (a first boot of this version): the shared instance network and the
    project's internal network are always the agent's (review: proof_agent_without_seen)."""
    me = _named_agent(nets=(), mode="mcpanel_instances")
    docker = _stack(monkeypatch, me)
    refreshed: list = []
    platformnet.on_self_repaired(lambda: refreshed.append(True))
    platformnet.repair_once(settings, now=1000.0)
    assert sorted(c[2] for c in docker.calls) == ["mcpanel_instances", "mcpanel_internal"]
    assert me.events == [] and refreshed == [True]  # no restart; the source guard refreshed at once (F-19)


def test_a_member_that_lost_one_of_two_networks_gets_it_back(settings, monkeypatch):
    """F-5: the memory only grows (per container id): a partial loss is repaired, live."""
    me = _named_agent()
    docker = _stack(monkeypatch, me)
    platformnet.repair_once(settings, now=1000.0)  # healthy: remembered
    me.nets.remove("mcpanel_internal")  # e.g. docker network disconnect
    platformnet.repair_once(settings, now=2000.0)
    assert "mcpanel_internal" in me.nets and me.events == []
    assert [c[2] for c in docker.calls] == ["mcpanel_internal"]


def test_a_new_container_of_a_service_starts_its_memory_afresh(settings, monkeypatch):
    old = _db(cid="d-old")
    _stack(monkeypatch, _named_agent(), old)
    platformnet.repair_once(settings, now=1000.0)
    import json

    assert "d-old" in json.loads((settings.state_dir / platformnet.STATE_FILE).read_text())
    _stack(monkeypatch, _named_agent(), _db(cid="d-new"))
    platformnet.repair_once(settings, now=1000.0)
    seen = json.loads((settings.state_dir / platformnet.STATE_FILE).read_text())
    assert "d-old" not in seen and seen["d-new"] == ["mcpanel_internal"]


def test_lost_aliases_come_back_with_ip_and_mac_kept(settings, monkeypatch):
    db = _db(aliases=())  # Unraid's reconnect: MAC only, no aliases
    docker = _stack(monkeypatch, _named_agent(), db)
    done = platformnet.repair_once(settings, now=1000.0)
    assert done == [{"container": "mcpanel-postgres-1", "result": "restored"}]
    assert docker.calls == [
        ("disconnect", "d1", "mcpanel_internal"),
        (
            "connect",
            "d1",
            "mcpanel_internal",
            "172.30.0.5",
            ("postgres", "mcpanel-postgres-1"),
            "02:42:ac:1e:00:05",
        ),
    ]
    assert db.events == [] and platformnet.status(settings)["recent"][0]["action"] == "reconnect with aliases"


def test_a_failed_reconnect_falls_back_in_steps(settings, monkeypatch):
    """F-6: the address was taken meanwhile: the member still ends up on its network, with its name."""
    db = _db(aliases=())
    docker = _stack(monkeypatch, _named_agent(), db, fail=lambda cid, net, kw: bool(kw["ip"]))
    assert platformnet.repair_once(settings, now=1000.0)[0]["result"] == "restored"
    assert "mcpanel_internal" in db.nets
    assert docker.calls[-1] == (
        "connect",
        "d1",
        "mcpanel_internal",
        None,
        ("postgres", "mcpanel-postgres-1"),
        "02:42:ac:1e:00:05",
    )


def test_a_member_left_off_its_network_is_tried_again_past_the_limit(settings, monkeypatch, caplog):
    db = _db(aliases=())
    refuse = {"on": True}
    _stack(monkeypatch, _named_agent(), db, fail=lambda cid, net, kw: refuse["on"] and cid == "d1")
    platformnet.repair_once(settings, now=1000.0)
    assert db.nets == [] and any("is OFF network" in r.message for r in caplog.records)
    refuse["on"] = False
    platformnet.repair_once(settings, now=1060.0)  # one minute later, not five
    assert db.nets == ["mcpanel_internal"]


def test_a_one_off_is_never_touched(settings, monkeypatch):
    """F-3: upgrade.sh's `docker compose run panel manage migrate --check` has no service alias."""
    oneoff = Endpointed(
        "r1",
        {
            "mcpanel_internal": _ep(
                "mcpanel-panel-run-3f2a1b", "172.30.0.9", "02:42:ac:1e:00:09", ["mcpanel-panel-run-3f2a1b"]
            )
        },
        mode="mcpanel_internal",
        status="running",
        labels=svc("panel", **{"com.docker.compose.oneoff": "True"}),
        name="mcpanel-panel-run-3f2a1b",
    )
    docker = _stack(monkeypatch, _named_agent(), oneoff)
    assert platformnet.repair_once(settings, now=1000.0) == [] and docker.calls == []


def test_a_game_container_with_compose_labels_is_never_a_member(settings, monkeypatch):
    """F-18: Docker copies an image's labels; a game image labelled project=mcpanel, service=postgres must not
    get the alias, and the real postgres must never be put on the instance network because of it."""
    game = Endpointed(
        "g1",
        {
            "mcpanel_instances": {
                "Aliases": None,
                "IPAddress": "172.19.0.3",
                "MacAddress": "02:42:ac:13:00:03",
            }
        },
        mode="mcpanel_instances",
        status="running",
        labels=svc("postgres", **{"io.mcpanel.managed": "true", "io.mcpanel.instance": str(IID)}),
        name="mcpanel-b0b0b0b00000",
    )
    db = _db()
    docker = _stack(monkeypatch, _named_agent(), db, game)
    assert platformnet.repair_once(settings, now=1000.0) == [] and docker.calls == []
    db.nets.clear()  # later the real one loses its network
    platformnet.repair_once(settings, now=2000.0)
    assert db.nets == ["mcpanel_internal"]  # back on its own network only


def test_labels_without_what_compose_always_sets_are_not_a_member(settings, monkeypatch):
    fake = Endpointed(
        "f1",
        {},
        mode="mcpanel_internal",
        status="running",
        labels={**PROJECT, "com.docker.compose.service": "postgres"},
    )
    docker = _stack(monkeypatch, _named_agent(), fake)
    assert platformnet.repair_once(settings, now=1000.0) == [] and docker.calls == []


def test_one_attempt_per_five_minutes_and_three_in_all(settings, monkeypatch, caplog):
    db = _db(aliases=())
    _stack(monkeypatch, _named_agent(), db)
    tries = 0
    for t in (0, 100, 300, 600, 900, 1200):
        db.endpoints["mcpanel_internal"]["Aliases"] = []  # removed again (deliberately, or by the script)
        tries += len(platformnet.repair_once(settings, now=1000.0 + t))
    assert tries == 3 and any("not repairing it any more" in r.message for r in caplog.records)
    assert platformnet.status(settings)["given_up"] == ["d1"]


def test_nothing_happens_when_switched_off_or_without_a_project(settings, monkeypatch):
    panel = Endpointed("p4", {}, mode="mcpanel_internal", status="running", labels=svc("panel"))
    _stack(monkeypatch, _named_agent(), panel)
    off = settings.model_copy(update={"repair_platform_networks": False})
    assert platformnet.repair_once(off) == [] and panel.events == []
    me = _named_agent()
    me.labels = {}
    _stack(monkeypatch, me, panel)
    assert platformnet.repair_once(settings) == [] and panel.events == []
    monkeypatch.setattr(pernet, "_own", {"id": None})
    assert platformnet.repair_once(settings) == []


def test_info_reports_the_repairs(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    body = api.get("/v1/info", headers=auth).json()
    assert body["platform_network_repair"] == {"enabled": True, "project": None, "recent": [], "given_up": []}


# --- game containers Docker started itself without a network (F-19) ---------------------------------------
def test_a_running_game_container_without_network_is_restarted_through_the_agent(settings, monkeypatch):
    import contextlib

    game = Ctr("g9", status="running", labels={"io.mcpanel.managed": "true", "io.mcpanel.instance": str(IID)})
    docker = Docker([game])
    docker.containers = SimpleNamespace(get=docker._get, list=lambda **kw: [game])
    monkeypatch.setattr(docker_ops, "client", lambda: docker)
    monkeypatch.setattr(platformnet, "_tries", {})
    monkeypatch.setattr(platformnet, "_recent", platformnet.collections.deque(maxlen=20))
    steps: list = []

    @contextlib.contextmanager
    def lock(iid, wait=None):
        steps.append(("lock", iid, wait))
        yield

    monkeypatch.setattr(docker_ops, "instance_lock", lock)
    monkeypatch.setattr(docker_ops, "graceful_stop", lambda s, c, t: steps.append("stop"))

    def start(iid, s):
        steps.append("start")
        game.nets.append("mcpanel_instances")

    monkeypatch.setattr(docker_ops, "start", start)
    done = platformnet.repair_game_containers(settings, now=1000.0)
    assert done == [{"container": "c-g9", "result": "repaired"}]
    assert steps == [("lock", IID, 0.0), "stop", "start"]
    assert platformnet.repair_game_containers(settings, now=1010.0) == []  # (on its network now)
