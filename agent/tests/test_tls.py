"""TLS for the agent API: self-signed certificate generation, modes and a real HTTPS listener."""

from __future__ import annotations

import os
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcagent import tls
from mcagent.__main__ import main as cli_main
from mcagent.config import Settings

AGENT_DIR = Path(__file__).resolve().parents[1]


def _settings(base: Settings, **overrides) -> Settings:
    return base.model_copy(update=overrides)


def test_off_mode_serves_plain_http(agent_settings):
    assert tls.cert_paths(agent_settings) is None
    assert tls.uvicorn_ssl_kwargs(agent_settings) == {}
    assert tls.server_fingerprint(agent_settings) is None


def test_auto_mode_generates_a_private_key_once(agent_settings):
    settings = _settings(agent_settings, tls="auto", tls_hostnames=["node2.lan", "192.168.1.20"])
    assert tls.server_fingerprint(settings) is None  # nothing generated yet, and no side effects
    cert, key = tls.cert_paths(settings)
    assert cert.parent == settings.state_dir / "tls"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(cert.parent.stat().st_mode) == 0o700
    first = tls.fingerprint_pem(cert.read_text())
    # a second start reuses the pair (the panel's pin must stay valid)
    assert tls.cert_paths(settings) == (cert, key)
    assert tls.fingerprint_pem(cert.read_text()) == first
    assert tls.server_fingerprint(settings) == first
    assert len(first) == 95 and first.count(":") == 31 and first == first.upper()

    from cryptography import x509

    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "node2.lan" in san.get_values_for_type(x509.DNSName)
    assert "192.168.1.20" in {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    assert parsed.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False


def test_fingerprint_matches_the_der_digest(agent_settings):
    import hashlib

    cert, _ = tls.cert_paths(_settings(agent_settings, tls="auto"))
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    assert tls.fingerprint_pem(cert.read_bytes()).replace(":", "").lower() == hashlib.sha256(der).hexdigest()


def test_files_mode_requires_both_files(agent_settings, tmp_path):
    with pytest.raises(tls.TlsConfigError, match="AGENT_TLS_CERT_FILE"):
        tls.cert_paths(_settings(agent_settings, tls="files"))
    with pytest.raises(tls.TlsConfigError, match="not found"):
        tls.cert_paths(
            _settings(
                agent_settings, tls="files", tls_cert_file=tmp_path / "x.crt", tls_key_file=tmp_path / "x.key"
            )
        )


def test_files_mode_rejects_a_mismatched_pair(agent_settings, tmp_path):
    cert_a, _ = tls.generate_self_signed(tmp_path / "a", [])
    _, key_b = tls.generate_self_signed(tmp_path / "b", [])
    settings = _settings(agent_settings, tls="files", tls_cert_file=cert_a, tls_key_file=key_b)
    with pytest.raises(tls.TlsConfigError, match="Cannot load"):
        tls.uvicorn_ssl_kwargs(settings)
    good = _settings(
        agent_settings, tls="files", tls_cert_file=cert_a, tls_key_file=tmp_path / "a" / "agent.key"
    )
    assert tls.uvicorn_ssl_kwargs(good) == {
        "ssl_certfile": str(cert_a),
        "ssl_keyfile": str(tmp_path / "a" / "agent.key"),
    }


def test_tls_settings_from_environment(monkeypatch):
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    monkeypatch.setenv("AGENT_TLS", "AUTO")
    monkeypatch.setenv("AGENT_TLS_HOSTNAMES", "node2.lan, 10.0.0.5")
    s = Settings()
    assert s.tls == "auto"
    assert s.tls_hostnames == ["node2.lan", "10.0.0.5"]
    monkeypatch.setenv("AGENT_TLS", "yes")
    with pytest.raises(ValidationError):
        Settings()


def test_cli_fingerprint(agent_settings, monkeypatch, capsys):
    settings = _settings(agent_settings, tls="auto")
    monkeypatch.setattr("mcagent.__main__.get_settings", lambda: settings)
    assert cli_main(["fingerprint"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == tls.server_fingerprint(settings)
    monkeypatch.setattr("mcagent.__main__.get_settings", lambda: agent_settings)
    assert cli_main(["fingerprint"]) == 1
    assert cli_main(["bogus"]) == 64


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pinned_context(pem: str) -> ssl.SSLContext:
    """What the panel does: trust exactly this certificate, ignore the host name."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.load_verify_locations(cadata=pem)
    return ctx


@pytest.fixture
def https_agent(tmp_path):
    """A real ``python -m mcagent`` process serving HTTPS with an auto-generated certificate."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
    env.update(
        AGENT_TLS="auto",
        AGENT_DATA_ROOT=str(tmp_path / "data"),
        AGENT_TOKEN="tls-test-token-0123456789abcdef",
        AGENT_LISTEN_HOST="127.0.0.1",
        AGENT_LISTEN_PORT=str(port),
        DOCKER_HOST="unix:///nonexistent.sock",  # health reports degraded; nothing else needed
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcagent"],
        cwd=AGENT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    cert = tmp_path / "data" / "agent" / "tls" / "agent.crt"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if cert.exists():
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
        if proc.poll() is not None:
            pytest.fail(proc.stdout.read().decode())
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("agent did not start")
    yield port, cert.read_text(), env
    proc.terminate()
    out, _ = proc.communicate(timeout=10)
    assert b"Certificate SHA-256 fingerprint: " + tls.fingerprint_pem(cert.read_text()).encode() in out


def test_https_listener_accepts_the_pinned_certificate(https_agent):
    port, pem, _ = https_agent
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_pinned_context(pem))
    )
    with opener.open(f"https://127.0.0.1:{port}/v1/health", timeout=10) as resp:
        assert resp.status == 200


def test_https_listener_fails_a_different_pin(https_agent, tmp_path):
    port, _, _ = https_agent
    other, _ = tls.generate_self_signed(tmp_path / "other", [])
    ctx = _pinned_context(other.read_text())
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with pytest.raises(ssl.SSLCertVerificationError):
            with ctx.wrap_socket(raw, server_hostname="127.0.0.1"):
                pass


def test_plain_http_to_the_tls_listener_fails(https_agent):
    port, _, _ = https_agent
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(Exception):
        opener.open(f"http://127.0.0.1:{port}/v1/health", timeout=5).read()


def test_container_healthcheck_follows_the_tls_mode(https_agent):
    _port, _, env = https_agent
    script = AGENT_DIR / "container" / "healthcheck.py"
    result = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=20, check=False
    )
    # Docker is unreachable in this fixture, so the probe connects over HTTPS and reports "degraded"
    assert result.returncode == 1
    assert "degraded" in result.stderr
    env_plain = {**env, "AGENT_TLS": "off"}
    result = subprocess.run(
        [sys.executable, str(script)], env=env_plain, capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 1 and "degraded" not in result.stderr  # plain HTTP to a TLS port fails


def test_list_settings_accept_csv_and_json_from_environment(monkeypatch):
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    monkeypatch.setenv("AGENT_ALLOWED_NETWORKS", "proxy_net, games")
    monkeypatch.setenv("AGENT_ALLOWED_IMAGES", '["itzg/minecraft-server"]')
    s = Settings()
    assert s.allowed_networks == ["proxy_net", "games"]
    assert s.allowed_images == ["itzg/minecraft-server"]
