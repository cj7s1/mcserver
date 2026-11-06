"""TLS trust for node agents: pinning self-signed agents, CA verification, and no token leaks.

A real threaded HTTPS server stands in for an agent so the handshake, the pin and the
"token only after verification" property are exercised end to end.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.test import override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.nodes import tls
from apps.nodes.client import AgentClient
from apps.nodes.models import Node

pytestmark = pytest.mark.django_db
TOKEN = "node2-token-0123456789abcdefgh"


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(
    directory: Path,
    stem: str,
    *,
    issuer_key: ec.EllipticCurvePrivateKey | None = None,
    issuer_name: x509.Name | None = None,
    ca: bool = False,
    names: tuple[str, ...] = ("localhost", "127.0.0.1"),
) -> tuple[Path, Path, ec.EllipticCurvePrivateKey, x509.Name]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = _name(f"{stem}-cn")
    now = dt.datetime.now(dt.UTC)
    san: list[x509.GeneralName] = []
    for n in names:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            san.append(x509.DNSName(n))
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name or subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    else:
        builder = builder.add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True
        ).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    if issuer_key is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False
        )
    cert = builder.sign(issuer_key or key, hashes.SHA256())
    cert_path, key_path = directory / f"{stem}.crt", directory / f"{stem}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path, key, subject


class FakeTlsAgent:
    """Minimal HTTPS agent: records every HTTP request (and its Authorization header)."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests.append((self.path, self.headers.get("Authorization")))
                body = json.dumps({"version": "test", "docker": {}, "restic": True, "policy": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.url = f"https://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def certs(tmp_path):
    return tmp_path


@pytest.fixture
def agent_a(certs):
    cert, key, *_ = _cert(certs, "a")
    srv = FakeTlsAgent(cert, key)
    srv.pem = cert.read_text()
    srv.fp = tls.fingerprint_of_pem(srv.pem)
    yield srv
    srv.close()


@pytest.fixture
def agent_b(certs):
    cert, key, *_ = _cert(certs, "b")
    srv = FakeTlsAgent(cert, key)
    srv.pem = cert.read_text()
    srv.fp = tls.fingerprint_of_pem(srv.pem)
    yield srv
    srv.close()


@pytest.fixture
def owner_client():
    owner = User.objects.create_user("o", password="x" * 12, is_owner=True)
    c = APIClient()
    c.force_login(owner)
    return c


def _node(url: str, fp: str = "", pem: str = "") -> Node:
    return Node.objects.create(
        name=f"n-{url[-5:]}", agent_url=url, agent_token=TOKEN, use_settings_token=False, tls_fingerprint=fp, tls_cert_pem=pem
    )


# -- fingerprints ---------------------------------------------------------------------
def test_fingerprint_formats():
    hexed = "ab" * 32
    for raw in (
        hexed,
        hexed.upper(),
        tls.format_fingerprint(hexed),
        "SHA256 Fingerprint=" + tls.format_fingerprint(hexed),
        "sha256:" + hexed,
        " ".join(hexed[i : i + 2] for i in range(0, 64, 2)),
    ):
        assert tls.normalize_fingerprint(raw) == hexed
    for bad in ("", "ab" * 31, "zz" * 32, "ab" * 33):
        with pytest.raises(ValueError):
            tls.normalize_fingerprint(bad)
    assert tls.format_fingerprint(hexed) == ":".join(["AB"] * 32)


def test_fetch_certificate_reads_the_presented_cert_without_a_request(agent_a):
    assert tls.fingerprint_of_pem(tls.fetch_certificate(agent_a.url)) == agent_a.fp
    assert agent_a.requests == []


# -- pinned agents --------------------------------------------------------------------
def test_pinned_agent_receives_requests_with_the_token(agent_a):
    node = _node(agent_a.url, agent_a.fp, agent_a.pem)
    assert AgentClient(node).info()["version"] == "test"
    assert agent_a.requests == [("/v1/info", f"Bearer {TOKEN}")]


def test_wrong_certificate_never_sees_the_token(agent_a, agent_b):
    """The node is pinned to A; B answers at the URL (e.g. a MITM or a regenerated cert)."""
    node = _node(agent_b.url, agent_a.fp, agent_a.pem)
    with pytest.raises(tls.AgentTlsError) as err:
        AgentClient(node).info()
    assert err.value.error_code == "agent_tls"
    assert "pinned certificate" in str(err.value.detail)
    assert agent_b.requests == []  # handshake failed before any HTTP request (no token leak)


def test_streaming_download_uses_the_pin(agent_a, agent_b):
    node = _node(agent_b.url, agent_a.fp, agent_a.pem)
    with pytest.raises(tls.AgentTlsError):
        AgentClient(node).download("00000000-0000-0000-0000-000000000000", "x")  # type: ignore[arg-type]
    assert agent_b.requests == []


def test_fingerprint_without_stored_cert_pins_lazily(agent_a):
    node = _node(agent_a.url, agent_a.fp)  # e.g. the agent was offline when the node was added
    AgentClient(node).info()
    node.refresh_from_db()
    assert node.tls_cert_pem.strip() == agent_a.pem.strip()


def test_lazy_pin_refuses_a_mismatching_agent(agent_a, agent_b):
    node = _node(agent_b.url, agent_a.fp)
    with pytest.raises(tls.AgentTlsError) as err:
        AgentClient(node).info()
    assert err.value.error_code == "agent_tls_mismatch"
    assert agent_b.requests == []
    node.refresh_from_db()
    assert node.tls_cert_pem == ""


def test_https_without_pin_or_ca_is_refused(agent_a):
    node = _node(agent_a.url)
    with pytest.raises(tls.AgentTlsError) as err:
        AgentClient(node).info()
    assert "set its fingerprint" in str(err.value.detail)
    assert agent_a.requests == []


def test_private_ca_bundle_verifies_agents(certs):
    ca_cert, _ca_key_path, ca_key, ca_name = _cert(certs, "ca", ca=True)
    leaf, leaf_key, *_ = _cert(certs, "leaf", issuer_key=ca_key, issuer_name=ca_name, names=("localhost",))
    srv = FakeTlsAgent(leaf, leaf_key)
    try:
        url = f"https://localhost:{srv.port}"
        with override_settings(MCPANEL_AGENT_CA_BUNDLE=str(ca_cert)):
            node = _node(url)
            assert tls.transport(node) == "tls_ca"
            assert AgentClient(node).info()["version"] == "test"
            # host names are verified in CA mode
            node.agent_url = f"https://127.0.0.1:{srv.port}"
            with pytest.raises(tls.AgentTlsError):
                AgentClient(node).info()
        assert len(srv.requests) == 1
    finally:
        srv.close()


# -- node API -----------------------------------------------------------------------------
def test_create_pinned_node(owner_client, agent_a):
    r = owner_client.post(
        "/api/nodes",
        {"name": "n2", "agent_url": agent_a.url, "agent_token": TOKEN, "tls_fingerprint": tls.format_fingerprint(agent_a.fp)},
        format="json",
    )
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["transport"] == "tls_pinned" and body["tls_pinned"] is True and body["security_warning"] is None
    assert body["tls_fingerprint"] == tls.format_fingerprint(agent_a.fp)
    assert "tls_cert_pem" not in body
    assert Node.objects.get(name="n2").tls_cert_pem.strip() == agent_a.pem.strip()
    assert agent_a.requests == []  # pinning is a handshake only
    r = owner_client.post(f"/api/nodes/{body['id']}/check")
    assert r.json()["ok"] is True
    assert agent_a.requests == [("/v1/info", f"Bearer {TOKEN}")]


def test_create_with_a_wrong_fingerprint_is_rejected(owner_client, agent_a, agent_b):
    r = owner_client.post(
        "/api/nodes",
        {"name": "n2", "agent_url": agent_b.url, "agent_token": TOKEN, "tls_fingerprint": agent_a.fp},
        format="json",
    )
    assert r.status_code == 400
    msg = json.dumps(r.json())
    assert "does not match" in msg and tls.format_fingerprint(agent_b.fp) in msg
    assert not Node.objects.filter(name="n2").exists()
    assert agent_b.requests == []


def test_fingerprint_needs_https(owner_client):
    r = owner_client.post(
        "/api/nodes",
        {"name": "n2", "agent_url": "http://10.0.0.2:8700", "agent_token": TOKEN, "tls_fingerprint": "ab" * 32},
        format="json",
    )
    assert r.status_code == 400 and "https" in json.dumps(r.json())
    r = owner_client.post(
        "/api/nodes",
        {"name": "n2", "agent_url": "https://10.0.0.2:8700", "agent_token": TOKEN, "tls_fingerprint": "nope"},
        format="json",
    )
    assert r.status_code == 400 and "64 hex" in json.dumps(r.json())


def test_create_while_the_agent_is_offline_keeps_the_fingerprint(owner_client, agent_a):
    url = agent_a.url
    agent_a.close()
    r = owner_client.post(
        "/api/nodes", {"name": "n2", "agent_url": url, "agent_token": TOKEN, "tls_fingerprint": agent_a.fp}, format="json"
    )
    assert r.status_code == 201, r.json()
    assert r.json()["transport"] == "tls_pinned" and r.json()["tls_pinned"] is False


def test_update_fingerprint_repins_and_clearing_it_drops_the_cert(owner_client, agent_a, agent_b):
    node = _node(agent_a.url, agent_a.fp, agent_a.pem)
    # the agent moved to a new certificate (B): admin updates URL + fingerprint
    # (a new URL needs the token again; the stored one is never sent to a different URL)
    body = {"agent_url": agent_b.url, "tls_fingerprint": agent_b.fp}
    assert owner_client.patch(f"/api/nodes/{node.id}", body, format="json").status_code == 400
    r = owner_client.patch(f"/api/nodes/{node.id}", {**body, "agent_token": TOKEN}, format="json")
    assert r.status_code == 200, r.json()
    node.refresh_from_db()
    assert tls.fingerprint_of_pem(node.tls_cert_pem) == agent_b.fp
    r = owner_client.patch(f"/api/nodes/{node.id}", {"tls_fingerprint": ""}, format="json")
    assert r.status_code == 200 and r.json()["transport"] == "tls_ca"
    node.refresh_from_db()
    assert node.tls_cert_pem == "" and node.tls_fingerprint == ""


def test_plain_http_remote_node_carries_a_warning(owner_client):
    r = owner_client.post("/api/nodes", {"name": "n2", "agent_url": "http://10.0.0.2:8700", "agent_token": TOKEN}, format="json")
    assert r.json()["transport"] == "plain" and "plain HTTP" in r.json()["security_warning"]
    default = next(n for n in owner_client.get("/api/nodes").json() if n["is_default"])
    assert default["security_warning"] is None


def test_tls_probe(owner_client, agent_a):
    r = owner_client.post("/api/nodes/tls-probe", {"agent_url": agent_a.url}, format="json")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["fingerprint"] == tls.format_fingerprint(agent_a.fp)
    assert body["self_signed"] is True and "127.0.0.1" in body["names"]
    assert agent_a.requests == []
    assert owner_client.post("/api/nodes/tls-probe", {"agent_url": "http://x:1"}, format="json").status_code == 400


def test_tls_probe_requires_node_admin(agent_a):
    u = User.objects.create_user("u", password="x" * 12)
    c = APIClient()
    c.force_login(u)
    assert c.post("/api/nodes/tls-probe", {"agent_url": agent_a.url}, format="json").status_code == 403


def test_default_node_follows_the_fingerprint_setting(agent_a):
    with override_settings(MCPANEL_AGENT_URL=agent_a.url, MCPANEL_AGENT_TLS_FINGERPRINT=agent_a.fp):
        node = Node.default()
        assert node.agent_url == agent_a.url and node.tls_fingerprint == agent_a.fp
        assert AgentClient(node).info()["version"] == "test"
        node.refresh_from_db()
        assert node.tls_cert_pem
    with override_settings(MCPANEL_AGENT_URL="http://agent:8700", MCPANEL_AGENT_TLS_FINGERPRINT=""):
        node = Node.default()
        assert node.tls_fingerprint == "" and node.tls_cert_pem == "" and tls.transport(node) == "plain"


def test_add_node_command_pins_and_checks(agent_a, agent_b):
    from io import StringIO

    from django.core.management import CommandError, call_command

    out = StringIO()
    call_command("add_node", name="cli-node", url=agent_a.url, token=TOKEN, fingerprint=agent_a.fp, stdout=out)
    assert "pinned=True" in out.getvalue() and "Agent reachable" in out.getvalue()
    node = Node.objects.get(name="cli-node")
    assert node.tls_cert_pem and node.agent_token == TOKEN
    with pytest.raises(CommandError, match="does not match"):
        call_command("add_node", name="cli-node", url=agent_b.url, fingerprint=agent_a.fp, stdout=StringIO())
    node.refresh_from_db()
    assert node.agent_url == agent_a.url  # unchanged after the refused update
