import ipaddress

from fastapi.testclient import TestClient

from mcagent import main


def test_requests_from_instance_network_are_refused(monkeypatch):
    monkeypatch.setattr(main, "_blocked_networks", [ipaddress.ip_network("172.30.0.0/16")])
    client = TestClient(main.app, client=("172.30.0.7", 50000))
    r = client.get("/v1/health")
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_source"


def test_requests_from_other_networks_pass(monkeypatch):
    monkeypatch.setattr(main, "_blocked_networks", [ipaddress.ip_network("172.30.0.0/16")])
    client = TestClient(main.app, client=("10.1.2.3", 50000))
    assert client.get("/v1/health").status_code == 200
