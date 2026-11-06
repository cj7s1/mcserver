"""Regression tests for the HTTP hardening / input validation / deployment audit findings.

1  WebSocket Origin check compares scheme, host and port (apps/core/middleware.py)
2  X-Frame-Options + CSP on files WhiteNoise serves (SecurityHeadersMiddleware)
3  instance.env refuses code-download, command and op variables (apps/instances/spec.py)
4  server.properties guard follows java.util.Properties syntax (apps/instances/properties.py)
5  webhook destinations: no loopback/link-local/internal services, pinned IP (notifications)
6  CurseForge key only for AUTO_CURSEFORGE servers
7-12 deployment files and docs
13 undecryptable secrets are loud, kept and recoverable (apps/core/crypto.py)
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgiref.testing import ApplicationCommunicator
from django.test import override_settings

from apps.core import crypto
from apps.core.middleware import WebSocketOriginValidator, websocket_origin_allowed
from apps.instances import properties
from apps.instances.spec import MANAGED_ENV, build_env, build_spec, env_override_error
from apps.notifications import service as notify_service

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------------------
# 1. WebSocket origin
# --------------------------------------------------------------------------------------
def ws_scope(origin: str | list[str] | None, host: str | None = "192.168.1.10:8080", scheme: str = "ws", **extra: str):
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode()))
    for value in [origin] if isinstance(origin, str) else origin or []:
        headers.append((b"origin", value.encode()))
    for name, value in extra.items():
        headers.append((name.replace("_", "-").encode(), value.encode()))
    return {"type": "websocket", "scheme": scheme, "path": "/ws/events/", "headers": headers}


LAN = {"ALLOWED_HOSTS": ["localhost", "192.168.1.10"], "CSRF_TRUSTED_ORIGINS": ["http://localhost:5173"]}


@override_settings(**LAN)
@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("http://192.168.1.10:8080", True),
        ("HTTP://192.168.1.10:8080", True),
        ("http://192.168.1.10:3000", False),  # another web app on the same box
        ("http://192.168.1.10", False),
        ("https://192.168.1.10:8080", False),
        ("http://evil.example:8080", False),
        ("null", False),
        ("", False),
        ("http://192.168.1.10:8080/path", False),
        ("http://user@192.168.1.10:8080", False),
        ("http://localhost:5173", True),  # CSRF_TRUSTED_ORIGINS (Vite dev server)
        ("http://localhost:5174", False),
    ],
)
def test_websocket_origin_must_match_scheme_host_and_port(origin, allowed):
    assert websocket_origin_allowed(ws_scope(origin)) is allowed


@override_settings(**LAN)
def test_websocket_origin_missing_duplicated_or_unknown_host():
    assert not websocket_origin_allowed(ws_scope(None))
    assert not websocket_origin_allowed(ws_scope(["http://192.168.1.10:8080", "http://192.168.1.10:8080"]))
    # Host header not in ALLOWED_HOSTS: its own origin is not trusted
    assert not websocket_origin_allowed(ws_scope("http://attacker.lan:8080", host="attacker.lan:8080"))
    # default ports are equivalent to no port
    assert websocket_origin_allowed(ws_scope("http://192.168.1.10", host="192.168.1.10:80"))


@override_settings(
    ALLOWED_HOSTS=["panel.example.com"],
    CSRF_TRUSTED_ORIGINS=["https://*.example.org"],
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    USE_X_FORWARDED_HOST=True,
)
def test_websocket_origin_behind_tls_proxy():
    behind = {"x_forwarded_proto": "https", "x_forwarded_host": "panel.example.com"}
    assert websocket_origin_allowed(ws_scope("https://panel.example.com", host="10.0.0.2:8000", **behind))
    # the proxy terminates TLS, so a plain-http page is a different origin
    assert not websocket_origin_allowed(ws_scope("http://panel.example.com", host="10.0.0.2:8000", **behind))
    assert not websocket_origin_allowed(ws_scope("https://panel.example.com:8443", host="10.0.0.2:8000", **behind))
    # wildcard trusted origins follow Django's CSRF semantics
    assert websocket_origin_allowed(ws_scope("https://app.example.org", host="10.0.0.2:8000", **behind))
    assert not websocket_origin_allowed(ws_scope("http://app.example.org", host="10.0.0.2:8000", **behind))


@pytest.mark.asyncio
@override_settings(**LAN)
async def test_websocket_validator_denies_before_the_app_runs():
    calls: list[str] = []

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        calls.append(scope["path"])
        await receive()
        await send({"type": "websocket.accept"})

    for origin, accepted in (("http://192.168.1.10:3000", False), ("http://192.168.1.10:8080", True)):
        comm = ApplicationCommunicator(WebSocketOriginValidator(inner), ws_scope(origin))
        await comm.send_input({"type": "websocket.connect"})
        out = await comm.receive_output(2)
        assert (out["type"] == "websocket.accept") is accepted, (origin, out)
    assert calls == ["/ws/events/"]


def test_asgi_application_uses_the_strict_origin_validator():
    from mcpanel.asgi import application

    assert isinstance(application.application_mapping["websocket"], WebSocketOriginValidator)


# --------------------------------------------------------------------------------------
# 2. X-Frame-Options / CSP
# --------------------------------------------------------------------------------------
@pytest.fixture
def frontend_dist(tmp_path):
    (tmp_path / "index.html").write_text('<!doctype html><script type="module" src="/assets/app.js"></script>')
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    with override_settings(WHITENOISE_ROOT=tmp_path, FRONTEND_DIST=tmp_path):
        yield tmp_path


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/index.html", "/assets/app.js", "/", "/servers/123"])
def test_spa_files_cannot_be_framed(frontend_dist, path):
    from django.test import Client

    resp = Client().get(path)
    assert resp.status_code == 200
    assert resp["X-Frame-Options"] == "DENY"
    csp = resp["Content-Security-Policy"]
    for directive in ("frame-ancestors 'none'", "script-src 'self'", "object-src 'none'", "base-uri 'self'"):
        assert directive in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp
    assert "connect-src 'self' ws://testserver" in csp


@pytest.mark.django_db
def test_api_responses_only_restrict_framing():
    from django.test import Client

    resp = Client().get("/api/health")
    assert resp["X-Frame-Options"] == "DENY"
    assert resp["Content-Security-Policy"] == "frame-ancestors 'none'"


@pytest.mark.django_db
@override_settings(ALLOWED_HOSTS=["panel.example.com"], SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
def test_spa_csp_allows_the_secure_websocket_of_its_own_host(frontend_dist):
    from django.test import Client

    csp = Client().get("/", HTTP_HOST="panel.example.com", HTTP_X_FORWARDED_PROTO="https")["Content-Security-Policy"]
    assert "connect-src 'self' wss://panel.example.com;" in csp


def test_built_frontend_has_no_inline_scripts():
    """The CSP has no 'unsafe-inline' for scripts: the production index.html must not need it."""
    index = REPO / "frontend" / "dist" / "index.html"
    if not index.exists():
        pytest.skip("frontend not built")
    import re

    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", index.read_text(), flags=re.S)
    assert scripts, "expected the module entry script"
    for attrs, body in scripts:
        assert "src=" in attrs and not body.strip(), attrs


# --------------------------------------------------------------------------------------
# 3. instance.env policy
# --------------------------------------------------------------------------------------
BLOCKED_ENV = [
    "MODS",
    "mods_file",
    "PLUGINS",
    "PLUGINS_FILE",
    "SPIGET_RESOURCES",
    "MODRINTH_PROJECTS",
    "CURSEFORGE_FILES",
    "MODPACK",
    "GENERIC_PACK",
    "GENERIC_PACKS",
    "PACKWIZ_URL",
    "DATAPACKS",
    "VANILLATWEAKS_SHARECODE",
    "WORLD",
    "CUSTOM_SERVER",
    "CUSTOM_SERVER_PROPERTIES",
    "PAPER_DOWNLOAD_URL",
    "FORGE_INSTALLER",
    "FORGE_INSTALLER_URL",
    "PURPUR_CONFIG_REPO",
    "DOWNLOAD_DEFAULT_CONFIGS",
    "APPLY_EXTRA_FILES",
    "RCON_CMDS_STARTUP",
    "RCON_CMDS_ON_CONNECT",
    "STOP_SERVER_DELAY_COMMAND",
    "PRE_START_SCRIPT",
    "SERVER_RUNNER",
    "ENABLE_SSH",
    "ENABLE_JMX",
    "JMX_HOST",
    "JVM_XX_OPTS",
    "JVM_DD_OPTS",
    "JAVA_TOOL_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "_JAVA_OPTIONS",
    "JAVA_AGENTS",
    "LD_PRELOAD",
    "PS4",
    "BASH_ENV",
    "PATH",
    "OPS",
    "OPS_FILE",
    "WHITELIST",
    "WHITELIST_FILE",
    "CF_API_KEY_FILE",
    "CF_MODPACK_ZIP",
    "CF_FILENAME_MATCHER",
    "FTB_SERVER_MOD",
    "SKIP_LOG4J_PATCHER",
    "DEBUG",
    "DEBUG_EXEC",
    "DUMP_SERVER_PROPERTIES",
    "MANAGEMENT_SERVER_ENABLED",
    "LOAD_ENV_FROM_ARCHIVE_ENTRY",
    "SKIP_SERVER_PROPERTIES",
    "OVERRIDE_SERVER_PROPERTIES",
    "MODRINTH_LOADER",
]
ALLOWED_ENV = [
    "MOTD",
    "DIFFICULTY",
    "MAX_PLAYERS",
    "TZ",
    "PAPER_CHANNEL",
    "ENABLE_AUTOPAUSE",
    "ENABLE_WHITELIST",
    "MODRINTH_EXCLUDE_FILES",
    "MODRINTH_FORCE_INCLUDE_FILES",
    "CF_EXCLUDE_MODS",
    "CF_FORCE_SYNCHRONIZE",
    "FTB_FORCE_REINSTALL",
    "ICON",
    "LOG_TIMESTAMP",
    "EXTRA_ARGS",
    "USE_FLARE_FLAGS",
]


@pytest.mark.parametrize("key", BLOCKED_ENV)
def test_dangerous_env_variables_are_refused(key):
    assert key in MANAGED_ENV and key.lower() in MANAGED_ENV
    assert env_override_error(key)


@pytest.mark.parametrize("key", ALLOWED_ENV)
def test_tuning_env_variables_still_work(key):
    assert key not in MANAGED_ENV
    assert env_override_error(key) is None


@pytest.mark.django_db
def test_refused_env_variables_never_reach_the_container(make_instance):
    inst = make_instance(env={k: "x" for k in BLOCKED_ENV + ALLOWED_ENV})
    env = build_env(inst)
    assert not set(BLOCKED_ENV) & set(env)
    assert {k: env[k] for k in ALLOWED_ENV} == dict.fromkeys(ALLOWED_ENV, "x")


@pytest.mark.django_db
@pytest.mark.parametrize("key", ["RCON_CMDS_STARTUP", "MODS", "OPS", "JAVA_TOOL_OPTIONS"])
def test_api_rejects_dangerous_env_variables(clients, make_instance, fake_agent, key):
    inst = make_instance()
    resp = clients["owner"].patch(f"/api/instances/{inst.id}", {"env": {key: "x", "MOTD": "hi"}}, format="json")
    assert resp.status_code == 400, resp.content
    assert key in str(resp.json())
    ok = clients["owner"].patch(f"/api/instances/{inst.id}", {"env": {"MOTD": "hi"}}, format="json")
    assert ok.status_code == 200


# --------------------------------------------------------------------------------------
# 4. server.properties managed-key guard
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    [
        "rcon\\.password",
        "server\\u002dport",
        "rcon.password\t",
        "rcon.password\f",
        "enable-rcon\\",
        "rcon.port\u2028",
        "rcon.port\u0085",
        "x\u2029rcon.password",
        "RCON.PASSWORD",
        "Server-Port",
        "!comment",
    ],
)
def test_property_names_cannot_smuggle_managed_keys(key):
    with pytest.raises(ValueError, match=r"Invalid property name|managed by MCPanel"):
        properties.validate_changes({key: "x"})


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("rcon\\.password=evil\n", "rcon.password"),
        ("server\\u002dport=1\n", "server-port"),
        ("\\u0072con.password=evil\n", "rcon.password"),
        ("rcon.password evil\n", "rcon.password"),
        ("rcon.password\tevil\n", "rcon.password"),
        ("rcon.password\fevil\n", "rcon.password"),
        ("rcon.pass\\\n    word=evil\n", "rcon.password"),
        ("motd=a\\\n# not a comment\nrcon.password=evil\n", "rcon.password"),
        ("\\\n# comment while still empty\nrcon.password=evil\n", "rcon.password"),
        ("x=1\rrcon.password=evil\r", "rcon.password"),
    ],
)
def test_parse_reads_keys_like_java_properties(text, key):
    assert key in properties.parse(text)


def test_parse_does_not_split_on_unicode_line_separators():
    for sep in ("\u2028", "\u2029", "\u0085", "\x0b", "\x1c"):
        values = properties.parse(f"motd=a{sep}rcon.password=evil\n")
        assert values == {"motd": f"a{sep}rcon.password=evil"}


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\u0085", "\x0b", "\x1c", "\x1e"])
def test_values_cannot_inject_keys_through_later_rewrites(sep):
    text = properties.update("server-port=25565\n", {"motd": f"hi{sep}rcon.password=evil{sep}enable-rcon=false"})
    assert sep not in text  # escaped as \\uXXXX
    # a later, unrelated update must not turn the separator into a line break
    text = properties.update(text, {"pvp": "true"})
    values = properties.parse(text)
    assert values["motd"] == f"hi{sep}rcon.password=evil{sep}enable-rcon=false"
    assert "rcon.password" not in values and "enable-rcon" not in values


def test_trailing_backslashes_and_leading_spaces_round_trip():
    for value in ("ends with \\", "\\", "\\\\", "  indented", "\tTab", "a\\\nb"):
        text = properties.update("pvp=true\n", {"motd": value, "zz": "after"})
        assert properties.parse(text) == {"pvp": "true", "motd": value, "zz": "after"}


def test_dangling_continuation_at_end_of_file_does_not_swallow_new_keys():
    text = properties.update("motd=hello\\", {"pvp": "false"})
    assert properties.parse(text) == {"motd": "hello", "pvp": "false"}


def test_update_replaces_escaped_spellings_of_a_key():
    text = properties.update("m\\otd=old\nmax\\-players = 5\n", {"motd": "new", "max-players": "9"})
    assert properties.parse(text) == {"motd": "new", "max-players": "9"}
    assert "old" not in text and "5" not in text


@pytest.mark.django_db
@pytest.mark.parametrize(
    "raw",
    [
        "motd=x\nrcon\\.password=evil\n",
        "motd=x\nserver\\u002dport=1\n",
        "motd=x\nrcon.password evil\n",
        "motd=x\nenable-rcon\\\n  =false\n",
    ],
)
def test_raw_properties_editor_guard_uses_java_semantics(clients, make_instance, fake_agent, raw):
    inst = make_instance()
    fake_agent.put_file(inst.id, "server.properties", "server-port=25565\nenable-rcon=true\nrcon.password=abc\n")
    resp = clients["owner"].patch(f"/api/instances/{inst.id}/properties", {"raw": raw}, format="json")
    assert resp.status_code == 400, resp.content


JAVA_DUMP = r"""
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class Dump {
    public static void main(String[] args) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (String f : args) {
            Properties p = new Properties();
            try (Reader r = new InputStreamReader(new FileInputStream(f), StandardCharsets.UTF_8)) {
                p.load(r);
                List<String> keys = new ArrayList<>(p.stringPropertyNames());
                Collections.sort(keys);
                for (String k : keys) sb.append(hex(k)).append(' ').append(hex(p.getProperty(k))).append('\n');
            } catch (IllegalArgumentException e) {
                sb.append("ERROR\n");
            }
            sb.append("--\n");
        }
        System.out.print(sb);
    }
    static String hex(String s) {
        StringBuilder b = new StringBuilder("x");
        for (char c : s.toCharArray()) b.append(String.format("%04x", (int) c));
        return b.toString();
    }
}
"""


@pytest.mark.skipif(shutil.which("java") is None, reason="needs a JDK (java single-file launcher)")
def test_parse_and_update_agree_with_java_util_properties(tmp_path):
    import random

    rng = random.Random(20261106)
    alphabet = [*"ab=: \t\f\\#!\n\r", "\r\n", "\u2028", "\u0085", "u", "0", "2", "d", "\\u002d", "rcon.password"]
    texts = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24))) for _ in range(800)]
    texts += [properties.update(t, {"motd": rng.choice(["", " x", "a\nb", "e\\", "\u2028"]), "zz": "1"}) for t in texts[:200]]
    (tmp_path / "Dump.java").write_text(JAVA_DUMP)
    files = []
    for i, text in enumerate(texts):
        f = tmp_path / f"{i}.properties"
        f.write_bytes(text.encode("utf-8"))
        files.append(str(f))
    env = {k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"}
    out = subprocess.run(["java", str(tmp_path / "Dump.java"), *files], capture_output=True, text=True, check=True, env=env)
    blocks = out.stdout.split("--\n")[:-1]
    assert len(blocks) == len(texts)

    def hexs(s: str) -> str:
        return "x" + "".join(f"{ord(c):04x}" for c in s)

    for text, block in zip(texts, blocks, strict=True):
        if block.startswith("ERROR"):  # malformed \\uXXXX: Java refuses the whole file
            continue
        # Java may also report an empty key for a lone trailing backslash (buffer-dependent)
        expected = "".join(line + "\n" for line in block.splitlines() if not line.startswith("x "))
        mine = "".join(f"{hexs(k)} {hexs(v)}\n" for k, v in sorted(properties.parse(text).items()) if k)
        assert mine == expected, repr(text)


# --------------------------------------------------------------------------------------
# 5. webhook destinations
# --------------------------------------------------------------------------------------
@pytest.fixture
def resolver(monkeypatch):
    """Fake DNS: name -> list of addresses (unknown names do not resolve)."""
    table: dict[str, list[str]] = {}

    def fake_getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in table[host]]

    monkeypatch.setattr(notify_service, "_getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(notify_service, "_internal_cache", None)
    monkeypatch.setattr(notify_service, "_env_proxy", lambda scheme, host: None)
    return table


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/hook",
        "http://sub.localhost/hook",
        "http://127.0.0.1/hook",
        "http://127.1.2.3/hook",
        "http://[::1]/hook",
        "http://0.0.0.0/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1]/hook",
        "http://[::ffff:127.0.0.1]/hook",
        "http://[fd00:ec2::254]/hook",
        "http://100.100.100.200/hook",
        "http://224.0.0.1/hook",
        "http://agent:8700/v1/info",
        "http://postgres:5432/",
        "http://redis:6379/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_webhook_urls_to_internal_targets_are_refused_when_saved(url):
    with pytest.raises(ValueError, match="not allowed"):
        notify_service.validate_url("webhook", url)


def test_webhook_lan_targets_follow_the_setting():
    assert notify_service.validate_url("webhook", "http://192.168.1.50:8123/api/webhook/x")
    with override_settings(MCPANEL_WEBHOOK_ALLOW_LAN=False), pytest.raises(ValueError, match="LAN"):
        notify_service.validate_url("webhook", "http://192.168.1.50:8123/api/webhook/x")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("addresses", "blocked"),
    [
        (["203.0.113.10"], False),
        (["127.0.0.1"], True),
        (["203.0.113.10", "127.0.0.1"], True),  # every address must be acceptable
        (["169.254.169.254"], True),
        (["::ffff:169.254.169.254"], True),
        (["64:ff9b::7f00:1"], True),  # NAT64 of 127.0.0.1
        (["2002:7f00:1::1"], True),  # 6to4 of 127.0.0.1
        (["192.168.1.50"], False),
        (["172.20.0.3"], True),  # the agent container
    ],
)
def test_webhook_destination_uses_resolved_addresses(resolver, addresses, blocked):
    resolver["hooks.example"] = addresses
    resolver["agent"] = ["172.20.0.3"]
    if blocked:
        with pytest.raises(notify_service.BlockedDestination):
            notify_service.check_destination("https://hooks.example/x")
    else:
        dest = notify_service.check_destination("https://hooks.example/x")
        assert dest.address == ipaddress.ip_address(addresses[0])


@pytest.mark.django_db
def test_webhook_cannot_target_node_agents_or_unresolvable_hosts(resolver, node):
    from apps.nodes.models import Node

    Node.objects.create(name="n2", agent_url="https://192.168.1.20:8700", is_default=False)
    resolver["nas.lan"] = ["192.168.1.20"]
    with pytest.raises(notify_service.BlockedDestination):
        notify_service.check_destination("https://nas.lan:8700/v1/info")
    # other services on the same host stay reachable
    assert notify_service.check_destination("http://nas.lan:8123/api/webhook/x").port == 8123
    with pytest.raises(notify_service.BlockedDestination, match="resolve"):
        notify_service.check_destination("https://nowhere.example/x")
    with override_settings(MCPANEL_WEBHOOK_ALLOW_LAN=False), pytest.raises(notify_service.BlockedDestination):
        notify_service.check_destination("http://nas.lan:8123/api/webhook/x")


@pytest.fixture
def captured(monkeypatch):
    """Route webhook requests to a mock transport and record them."""
    requests: list[httpx.Request] = []
    state: dict[str, Any] = {"response": httpx.Response(204)}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(state["response"], Exception):
            raise state["response"]
        return state["response"]

    def client(*, direct: bool) -> httpx.Client:
        state["direct"] = direct
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    monkeypatch.setattr(notify_service, "_client", client)
    state["requests"] = requests
    return state


def _channel(url: str):  # type: ignore[no-untyped-def]
    from apps.notifications.models import NotificationChannel

    return NotificationChannel.objects.create(name="hook", kind="webhook", url=url, events=["job.failed"])


@pytest.mark.django_db
def test_webhook_is_sent_to_the_checked_address(resolver, captured):
    resolver["hooks.example"] = ["203.0.113.10"]
    ch = _channel("https://hooks.example:8443/x?y=1")
    notify_service.deliver(ch.pk, "job.failed", "t", "m", {})
    (request,) = captured["requests"]
    assert request.url.host == "203.0.113.10" and request.url.port == 8443 and request.url.path == "/x"
    assert request.headers["Host"] == "hooks.example:8443"
    assert request.extensions["sni_hostname"] == "hooks.example"
    assert captured["direct"] is True
    ch.refresh_from_db()
    assert ch.last_error == "" and ch.last_sent_at is not None


@pytest.mark.django_db
def test_webhook_through_an_egress_proxy_keeps_the_host_name(resolver, captured, monkeypatch):
    resolver["hooks.example"] = ["203.0.113.10"]
    monkeypatch.setattr(notify_service, "_env_proxy", lambda scheme, host: "http://proxy.lan:3128")
    ch = _channel("https://hooks.example/x")
    notify_service.deliver(ch.pk, "job.failed", "t", "m", {})
    (request,) = captured["requests"]
    assert request.url.host == "hooks.example" and captured["direct"] is False


def test_direct_webhook_client_ignores_environment_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    with notify_service._client(direct=True) as client:
        assert client._transport_for_url(httpx.URL("https://203.0.113.10/")) is client._transport


@pytest.mark.django_db
def test_webhook_blocked_destination_is_not_contacted(resolver, captured):
    resolver["rebind.example"] = ["127.0.0.1"]
    ch = _channel("https://rebind.example/x")
    notify_service.deliver(ch.pk, "job.failed", "t", "m", {})
    assert captured["requests"] == []
    ch.refresh_from_db()
    assert "not allowed" in ch.last_error


@pytest.mark.django_db
def test_webhook_redirects_are_not_followed_and_errors_are_generic(resolver, captured):
    resolver["hooks.example"] = ["203.0.113.10"]
    ch = _channel("https://hooks.example/x")
    captured["response"] = httpx.Response(302, headers={"Location": "http://127.0.0.1:8700/v1/info"})
    notify_service.deliver(ch.pk, "job.failed", "t", "m", {})
    assert len(captured["requests"]) == 1
    ch.refresh_from_db()
    assert ch.last_error == "HTTP 302 (redirects are not followed)"
    captured["response"] = httpx.ConnectError("[Errno 111] Connection refused to 203.0.113.10:443")
    with pytest.raises(httpx.ConnectError):
        notify_service.deliver(ch.pk, "job.failed", "t", "m", {})
    ch.refresh_from_db()
    assert ch.last_error == notify_service.DELIVERY_FAILED


# --------------------------------------------------------------------------------------
# 6. CurseForge API key
# --------------------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("server_type", ["VANILLA", "PAPER", "FABRIC", "FORGE", "MODRINTH", "FTBA"])
def test_curseforge_key_only_reaches_curseforge_servers(make_instance, server_type):
    from apps.core.models import SystemSetting

    SystemSetting.set_secret("curseforge_api_key", "cf-secret-key")
    inst = make_instance(server_type=server_type, modpack_project_id="1", env={"CF_API_KEY_FILE": "/data/x"})
    env = build_env(inst)
    assert "CF_API_KEY" not in env and "CF_API_KEY_FILE" not in env
    assert "cf-secret-key" not in str(build_spec(inst))
    cf = make_instance(server_type="AUTO_CURSEFORGE", modpack_slug="pack")
    assert build_env(cf)["CF_API_KEY"] == "cf-secret-key"


# --------------------------------------------------------------------------------------
# 7-12. deployment files
# --------------------------------------------------------------------------------------
def test_node_stack_requires_an_explicit_bind_address():
    compose = (REPO / "deploy" / "docker-compose.node.yml").read_text()
    assert '"${MCPANEL_NODE_BIND:?' in compose
    assert "MCPANEL_NODE_BIND:-" not in compose
    example = (REPO / "deploy" / "node.env.example").read_text()
    assert "\nMCPANEL_NODE_BIND=\n" in example


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose")
def test_node_stack_refuses_to_start_without_bind(tmp_path):
    env = tmp_path / "node.env"
    env.write_text(
        "AGENT_TOKEN=" + "a" * 64 + "\nAGENT_RESTIC_PASSWORD=" + "b" * 64 + "\nMCPANEL_DATA_DIR=/srv/x\nMCPANEL_NODE_BIND=\n"
    )
    cmd = ["docker", "compose", "-f", str(REPO / "deploy" / "docker-compose.node.yml"), "--env-file", str(env), "config", "-q"]
    try:
        missing = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("docker compose not usable here")
    if "unknown command" in missing.stderr or "is not a docker command" in missing.stderr:
        pytest.skip("docker compose plugin missing")
    assert missing.returncode != 0 and "MCPANEL_NODE_BIND" in missing.stderr
    env.write_text(env.read_text().replace("MCPANEL_NODE_BIND=\n", "MCPANEL_NODE_BIND=192.168.1.20\n"))
    assert subprocess.run(cmd, capture_output=True, text=True, timeout=60).returncode == 0


def _run_entrypoint(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the panel entrypoint in web mode with stub python/uvicorn that print their argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("python", "cat >/dev/null 2>&1 || true\nexit 0\n"), ("uvicorn", 'printf "%s\\n" "$@"\n')):
        script = bin_dir / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    full_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", **env}
    entrypoint = REPO / "backend" / "container" / "entrypoint.sh"
    return subprocess.run(
        ["sh", str(entrypoint), "web"], capture_output=True, text=True, env=full_env, timeout=30, stdin=subprocess.DEVNULL
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_entrypoint_trusts_only_loopback_proxies_by_default(tmp_path):
    out = _run_entrypoint(tmp_path, MCPANEL_BEHIND_TLS_PROXY="true")
    assert out.returncode == 0, out.stderr
    args = out.stdout.splitlines()
    assert args[args.index("--forwarded-allow-ips") + 1] == "127.0.0.1,::1"
    assert "172.16.0.0/12" not in out.stdout and "--proxy-headers" in args
    assert "MCPANEL_FORWARDED_ALLOW_IPS" in out.stderr  # tells the admin to set it

    out = _run_entrypoint(tmp_path, MCPANEL_BEHIND_TLS_PROXY="true", MCPANEL_FORWARDED_ALLOW_IPS="192.168.1.10")
    args = out.stdout.splitlines()
    assert args[args.index("--forwarded-allow-ips") + 1] == "192.168.1.10"
    assert out.stderr == ""

    out = _run_entrypoint(tmp_path)
    assert "--no-proxy-headers" in out.stdout.splitlines() and "--forwarded-allow-ips" not in out.stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("path", ["deploy/node.env", "deploy/.env", "deploy/other.env", "deploy/mcpanel-db.sql"])
def test_generated_env_files_and_dumps_are_git_ignored(path):
    try:
        res = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git not usable here")
    if res.returncode == 128:
        pytest.skip("not a git checkout")
    assert res.returncode == 0, path
    examples = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "deploy/node.env.example"], timeout=30)
    assert examples.returncode == 1


def test_docs_keep_secrets_out_of_history_shares_and_stale_images():
    deployment = (REPO / "DEPLOYMENT.md").read_text()
    quickstart = (REPO / "docs" / "QUICKSTART-UNRAID.md").read_text()
    assert "build --pull" in deployment and "pull --ignore-buildable" in deployment
    assert "build --pull" in quickstart
    assert "read -rs MCPANEL_NODE_TOKEN" in deployment
    assert "MCPANEL_NODE_TOKEN=<" not in deployment
    for doc in (deployment, quickstart):
        assert "umask 077" in doc and "--exclude-table-data=django_session" in doc
    assert "/mnt/user/backups/mcpanel-db.sql" not in quickstart


# --------------------------------------------------------------------------------------
# 13. secret key rotation / unreadable secrets
# --------------------------------------------------------------------------------------
OLD_KEY = "old-secret-key-" + "o" * 40
NEW_KEY = "new-secret-key-" + "n" * 40


@pytest.fixture
def fresh_crypto_log(monkeypatch):
    monkeypatch.setattr(crypto, "_reported", set())


def _raw_secret(key: str) -> str:
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT secret FROM core_systemsetting WHERE key = %s", [key])
        return cur.fetchone()[0]


@pytest.mark.django_db
def test_unreadable_secret_is_loud_kept_and_recoverable(fresh_crypto_log, caplog):
    from apps.core.models import SystemSetting

    with override_settings(SECRET_KEY=OLD_KEY, SECRET_KEY_FALLBACKS=[]):
        SystemSetting.set_secret("curseforge_api_key", "cf-key")
        stored = _raw_secret("curseforge_api_key")
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[]):
        with caplog.at_level(logging.ERROR, logger="apps.core.crypto"):
            value = SystemSetting.get_secret("curseforge_api_key")
            SystemSetting.get_secret("curseforge_api_key")
        assert value == "" and not value and crypto.is_unreadable(value)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1 and "core.SystemSetting.secret" in errors[0].getMessage()
        assert "cf-key" not in caplog.text and stored not in caplog.text
        # saving the row for another reason keeps the ciphertext
        row = SystemSetting.objects.get(key="curseforge_api_key")
        row.value = "touched"
        row.save()
        assert _raw_secret("curseforge_api_key") == stored
    # with the old key as a fallback the secret is readable again
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        assert SystemSetting.get_secret("curseforge_api_key") == "cf-key"


@pytest.mark.django_db
def test_system_settings_api_reports_unreadable_secrets(clients, fresh_crypto_log, settings):
    from apps.core.models import SystemSetting

    with override_settings(SECRET_KEY=OLD_KEY):
        SystemSetting.set_secret("curseforge_api_key", "cf-key")
    # the test session was signed with the suite's key: keep it valid as a fallback
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[settings.SECRET_KEY], CURSEFORGE_API_KEY=""):
        body = clients["owner"].get("/api/system/settings").json()
    assert body["secrets_unreadable"] == {"curseforge_api_key": True}
    assert body["secrets_configured"] == {"curseforge_api_key": False}


@pytest.mark.django_db
def test_reencrypt_secrets_command_completes_a_rotation(make_instance, fresh_crypto_log):
    from io import StringIO

    from django.core.management import call_command

    from apps.core.models import SystemSetting
    from apps.instances.models import Instance

    with override_settings(SECRET_KEY=OLD_KEY):
        SystemSetting.set_secret("curseforge_api_key", "cf-key")
        inst = make_instance()
        password = Instance.objects.get(pk=inst.pk).rcon_password
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[OLD_KEY]):
        assert crypto.needs_reencryption(_raw_secret("curseforge_api_key"))
        out = StringIO()
        call_command("reencrypt_secrets", stdout=out, stderr=StringIO())
        assert "Re-encrypted: 2 secret(s). Unreadable: 0." in out.getvalue()
    with override_settings(SECRET_KEY=NEW_KEY, SECRET_KEY_FALLBACKS=[]):
        assert SystemSetting.get_secret("curseforge_api_key") == "cf-key"
        assert Instance.objects.get(pk=inst.pk).rcon_password == password


@pytest.mark.django_db
def test_unreadable_rcon_password_is_regenerated_for_the_next_container(make_instance, fresh_crypto_log):
    from apps.instances.models import Instance

    with override_settings(SECRET_KEY=OLD_KEY):
        inst = make_instance()
    with override_settings(SECRET_KEY=NEW_KEY):
        loaded = Instance.objects.get(pk=inst.pk)
        assert crypto.is_unreadable(loaded.rcon_password)
        spec = build_spec(loaded)
        assert len(spec["rcon_password"]) >= 8  # the agent requires 8+ characters
        assert Instance.objects.get(pk=inst.pk).rcon_password == spec["rcon_password"]
        assert build_spec(Instance.objects.get(pk=inst.pk))["rcon_password"] == spec["rcon_password"]


@pytest.mark.django_db
def test_webhook_with_unreadable_url_says_so(resolver, captured, fresh_crypto_log):
    from apps.notifications.models import NotificationChannel

    with override_settings(SECRET_KEY=OLD_KEY):
        ch = _channel("https://hooks.example/x")
    with override_settings(SECRET_KEY=NEW_KEY):
        notify_service.deliver(ch.pk, "job.failed", "t", "m", {})
        assert captured["requests"] == []
        assert NotificationChannel.objects.get(pk=ch.pk).last_error == notify_service.UNREADABLE_URL


def test_secret_key_fallbacks_setting_is_parsed(monkeypatch):
    import importlib

    import mcpanel.settings as settings_module

    monkeypatch.setenv("MCPANEL_SECRET_KEY_FALLBACKS", "old-one, older-two  third")
    try:
        reloaded = importlib.reload(settings_module)
        assert reloaded.SECRET_KEY_FALLBACKS == ["old-one", "older-two", "third"]
    finally:
        monkeypatch.delenv("MCPANEL_SECRET_KEY_FALLBACKS")
        importlib.reload(settings_module)
