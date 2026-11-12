import ipaddress
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mcagent import docker_ops, main, netguard
from mcagent.config import Settings, get_settings


def test_requests_from_instance_network_are_refused(monkeypatch):
    monkeypatch.setattr(main, "_blocked_networks", [ipaddress.ip_network("172.30.0.0/16")])
    client = TestClient(main.app, client=("172.30.0.7", 50000))
    r = client.get("/v1/health")
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_source"


def test_requests_from_other_networks_pass(monkeypatch):
    monkeypatch.setattr(main, "_blocked_networks", [ipaddress.ip_network("172.30.0.0/16")])
    client = TestClient(main.app, client=("10.1.2.3", 50000))
    assert client.get("/v1/health").status_code == 200


def test_docker_daemon_down_reported_as_docker_unavailable(monkeypatch):
    import requests

    def boom():
        raise requests.exceptions.ConnectionError("Connection aborted.")

    monkeypatch.setattr(main, "_blocked_networks", [])
    monkeypatch.setattr(docker_ops, "list_managed", boom)
    main.app.dependency_overrides[get_settings] = lambda: Settings(token="t" * 32)
    try:
        r = TestClient(main.app).get("/v1/instances", headers={"Authorization": "Bearer " + "t" * 32})
    finally:
        main.app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["error"]["code"] == "docker_unavailable"


# --- Docker unreachable at start-up (audit contract G) ---------------------------------------------
TOKEN = "t" * 32
INTERNAL = ipaddress.ip_network("172.18.0.0/16")  # the panel's network
INSTANCES = ipaddress.ip_network("172.19.0.0/16")
POOL = ipaddress.ip_network("10.213.0.0/16")  # AGENT_INSTANCE_POOL's default: every server's own network


@pytest.fixture
def guard(monkeypatch, tmp_path):
    """The guard as a new agent process sets it up; ``attached`` are this process's networks."""
    settings = Settings(token=TOKEN, data_root=tmp_path / "data")
    settings.state_dir.mkdir(parents=True)
    attached = [INTERNAL, INSTANCES]
    monkeypatch.setattr(netguard, "attached_subnets", lambda *a: list(attached))
    for name in (
        "_blocked_networks",
        "_blocked_gateways",
        "_remembered_networks",
        "_attached_networks",
        "_pool",
    ):
        monkeypatch.setattr(main, name, getattr(main, name))
    main.app.dependency_overrides[get_settings] = lambda: settings
    yield settings, attached
    main.app.dependency_overrides.clear()


def _get(ip: str, path: str = "/v1/instances") -> int:
    client = TestClient(main.app, client=(ip, 50000))
    return client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code


class _Net:
    def __init__(self, subnet: str, gateway: str | None = None, driver: str = "bridge") -> None:
        cfg = {"Subnet": subnet, **({"Gateway": gateway} if gateway else {})}
        self.attrs = {"Driver": driver, "IPAM": {"Config": [cfg]}}


class _Docker:
    def __init__(self, failures: int = 0, extra: list[_Net] | None = None) -> None:
        self.failures = failures
        self.networks = self
        self.extra = list(extra or [])

    def get(self, name):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("Docker is not reachable")
        return _Net(str(INSTANCES))

    def list(self):
        # the panel's internal network (gateway by Docker's default: its first address), the instance network
        return [_Net(str(INTERNAL)), _Net(str(INSTANCES), "172.19.0.1"), *self.extra]


def test_first_start_without_docker_only_answers_loopback_and_health(guard, monkeypatch):
    settings, _attached = guard
    main._prepare_guard(settings)
    monkeypatch.setattr(docker_ops, "list_managed", lambda: [])
    assert _get("172.18.0.3") == 503 and _get("172.19.0.5") == 503 and _get("192.168.1.50") == 503
    r = TestClient(main.app, client=("172.18.0.3", 50000)).get(
        "/v1/instances", headers={"Authorization": "Bearer " + TOKEN}
    )
    assert r.json()["error"]["code"] == "agent_starting"
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker(failures=99))
    assert _get("172.19.0.5", "/v1/health") == 200  # reveals nothing
    assert _get("127.0.0.1") == 200


def test_later_starts_without_docker_keep_the_panel_working(guard, monkeypatch):
    """What the guard saw the last time Docker answered is kept; the panel's network passes,
    the instance network is refused, a network it has never seen waits for Docker."""
    settings, attached = guard
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker())
    assert main._load_blocked_networks(settings) is True  # an earlier run
    main._prepare_guard(settings)  # the agent restarts; Docker does not answer
    assert main._blocked_networks is None
    monkeypatch.setattr(docker_ops, "list_managed", lambda: [])
    assert _get("172.18.0.3") == 200  # the panel on the internal network
    assert _get("172.19.0.5") == 403  # a Minecraft container
    assert _get("192.168.1.50") == 200  # a remote panel, reached through a published port
    attached.append(ipaddress.ip_network("172.20.0.0/16"))  # e.g. the instance network, recreated
    main._prepare_guard(settings)
    assert _get("172.20.0.9") == 503 and _get("172.18.0.3") == 200


def test_the_guard_retries_until_docker_answers(guard, monkeypatch):
    settings, _attached = guard
    fake = _Docker(failures=3)
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    monkeypatch.setattr(main, "GUARD_RETRY_SECONDS", 0.01)
    main._prepare_guard(settings)
    stop = threading.Event()
    t = threading.Thread(target=main._guard_loop, args=(settings, stop), daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while main._blocked_networks is None and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(5)
    assert main._blocked_networks == [INSTANCES, POOL] and fake.failures == 0
    assert _get("172.19.0.5") == 403 and _get("10.213.0.3") == 403
    # the gateway of the panel's bridge is refused too (A-4); its subnet stays trusted
    assert netguard.recall(settings.state_dir) == {
        "blocked": [POOL, ipaddress.ip_network("172.18.0.1/32"), INSTANCES],
        "trusted": [INTERNAL],
    }


def test_a_websocket_is_refused_while_the_networks_are_unknown(guard, monkeypatch):
    settings, _attached = guard
    main._prepare_guard(settings)
    monkeypatch.setenv("AGENT_TOKEN", TOKEN)
    client = TestClient(main.app, client=("172.19.0.5", 50000))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/v1/instances/{'0' * 8}-0000-4000-8000-{'0' * 12}/logs/stream",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as ws:
            ws.receive_json()
    assert exc.value.code == 1013


def test_attached_subnets_from_the_routing_table(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"  # default route: not attached
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth1\t000013AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    )
    route6 = tmp_path / "ipv6_route"
    route6.write_text(
        "fe800000000000000000000000000000 40 "
        + "0" * 32
        + " 00 "
        + "0" * 32
        + " 00000100 00000001 00000000 00000001 eth0\n"
        "fd001234000000000000000000000000 40 "
        + "0" * 32
        + " 00 "
        + "0" * 32
        + " 00000100 00000001 00000000 00000001 eth1\n"
        "00000000000000000000000000000001 80 "
        + "0" * 32
        + " 00 "
        + "0" * 32
        + " 00000000 00000001 00000000 00000001 lo\n"
    )
    assert netguard.attached_subnets(route, route6) == [
        INTERNAL,
        INSTANCES,
        ipaddress.ip_network("fd00:1234::/64"),
    ]
    assert netguard.attached_subnets(tmp_path / "missing", tmp_path / "missing6") == []


# --- A-4 (security review 2026-11-11): bridge gateways and IPv4-mapped addresses --------------------------
def test_the_gateway_of_an_attached_bridge_is_refused(guard, monkeypatch):
    """A game container that calls a published agent port through the host arrives NATed from the gateway
    of the bridge that carries the port; a panel is never there."""
    settings, _attached = guard
    lan = _Net("192.168.1.0/24", "192.168.1.1", driver="macvlan")  # not attached anyway; never a bridge
    other = _Net("172.40.0.0/16", "172.40.0.1")  # a bridge the agent is not attached to
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker(extra=[lan, other]))
    assert main._load_blocked_networks(settings) is True
    monkeypatch.setattr(docker_ops, "list_managed", lambda: [])
    assert _get("172.18.0.1") == 403  # the api bridge's gateway
    assert _get("172.18.0.3") == 200  # the panel on the same bridge
    assert _get("172.40.0.1") == 200 and _get("192.168.1.1") == 200 and _get("192.168.1.50") == 200
    # the same while Docker does not answer after a restart
    main._prepare_guard(settings)
    assert _get("172.18.0.1") == 403 and _get("172.18.0.3") == 200


def test_the_gateway_rule_can_be_switched_off(guard, monkeypatch):
    settings, _attached = guard
    settings = settings.model_copy(update={"refuse_bridge_gateways": False})
    main.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(docker_ops, "client", lambda: _Docker())
    assert main._load_blocked_networks(settings) is True
    monkeypatch.setattr(docker_ops, "list_managed", lambda: [])
    assert _get("172.18.0.1") == 200 and _get("172.19.0.5") == 403


def test_an_ipv4_mapped_address_is_judged_as_ipv4(monkeypatch):
    monkeypatch.setattr(main, "_blocked_networks", [INSTANCES])
    monkeypatch.setattr(main, "_blocked_gateways", [ipaddress.ip_network("172.18.0.1/32")])
    assert main._source_verdict(("::ffff:172.19.0.5", 1)) == "blocked"
    assert main._source_verdict(("::ffff:172.18.0.1", 1)) == "blocked"
    assert main._source_verdict(("::ffff:172.18.0.3", 1)) is None
