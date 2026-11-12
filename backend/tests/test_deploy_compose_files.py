"""The Compose files of deploy/, against the security review of 2026-11-11.

A-4: on an additional node the agent port is published, and the network that carries it decides the
address a game container's request to the node's published address arrives from. The file meant
`api` to carry it, but `priority` is only Compose's connect order: with equal gateway priorities
Docker picks the network whose name sorts first, so the project name decided. `gw_priority` decides
it (checked on Docker 29.9 / Compose 5.7 with both name orders: the request came from the gateway of
`api` each time; with `priority` alone, from the gateway of the network that sorted first).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
NODE = REPO / "deploy" / "docker-compose.node.yml"


def test_a4_the_node_publishes_the_agent_port_through_api_by_gateway_priority():
    spec = yaml.safe_load(NODE.read_text())
    networks = spec["services"]["agent"]["networks"]
    assert set(networks) == {"api", "instances"}
    api, instances = networks["api"] or {}, networks["instances"] or {}
    assert api.get("gw_priority", 0) > instances.get("gw_priority", 0)
    assert "priority" not in api and "priority" not in instances  # the connect order decides nothing here
    assert spec["networks"]["instances"]["name"] == "mcpanel_instances"
    assert spec["services"]["agent"]["ports"]  # still published: that is what the priority is for
    text = NODE.read_text()
    assert "has the highest priority so Docker publishes the port through it" not in text
    assert "Compose 2.33+ and Docker Engine 28+" in text
    deployment = " ".join((REPO / "DEPLOYMENT.md").read_text().split())
    assert "Docker Compose 2.33+ and Docker Engine 28+" in deployment and "`gw_priority`" in deployment


# --- the settings of the review's fixes reach the containers ---------------------------------------------
AGENT_SETTINGS = {
    "AGENT_PER_SERVER_NETWORKS": "${AGENT_PER_SERVER_NETWORKS:-true}",
    "AGENT_INSTANCE_POOL": "${AGENT_INSTANCE_POOL:-10.213.0.0/16}",
    "AGENT_REFUSE_BRIDGE_GATEWAYS": "${AGENT_REFUSE_BRIDGE_GATEWAYS:-true}",
    "AGENT_IMAGE_DIGEST_CHECK": "${AGENT_IMAGE_DIGEST_CHECK:-true}",
    "AGENT_REPAIR_PLATFORM_NETWORKS": "${AGENT_REPAIR_PLATFORM_NETWORKS:-true}",
}
PANEL_SETTINGS = ("MCPANEL_SECURE_COOKIES", "MCPANEL_HSTS_SECONDS", "MCPANEL_SESSION_BINDING", "MCPANEL_API_DOCS")


def test_the_agent_settings_of_the_review_are_passed_with_the_agents_defaults():
    for path in ("deploy/docker-compose.yml", "deploy/docker-compose.node.yml"):
        env = yaml.safe_load((REPO / path).read_text())["services"]["agent"]["environment"]
        for key, value in AGENT_SETTINGS.items():
            assert env[key] == value, (path, key)
    for path in ("deploy/.env.example", "deploy/node.env.example"):
        example = (REPO / path).read_text()
        for key in AGENT_SETTINGS:
            assert f"\n{key}=\n" in example, (path, key)
    deployment = (REPO / "DEPLOYMENT.md").read_text()
    for key in (*AGENT_SETTINGS, *PANEL_SETTINGS):
        assert key in deployment, key


def test_empty_panel_settings_leave_the_panels_defaults_alone():
    """Compose passes an empty value when .env has none: `MCPANEL_SECURE_COOKIES:-false` used to
    override the panel's own default (Secure behind an HTTPS proxy, D-8)."""
    compose = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())
    env = compose["x-panel-env"]
    for key in PANEL_SETTINGS:
        assert env[key] == "${" + key + ":-}", key
        assert f"\n{key}=\n" in (REPO / "deploy" / ".env.example").read_text(), key
    code = (
        "import mcpanel.settings as s\n"
        "print(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_HSTS_SECONDS, s.MCPANEL_SESSION_BINDING,"
        " s.MCPANEL_API_DOCS)\n"
    )
    base = {k: v for k, v in os.environ.items() if k not in (*PANEL_SETTINGS, "MCPANEL_BEHIND_TLS_PROXY")}
    base.update({key: "" for key in PANEL_SETTINGS}, DJANGO_SETTINGS_MODULE="mcpanel.settings")
    for proxy, secure in (("true", "True"), ("false", "False")):
        out = subprocess.run(
            [sys.executable, "-c", code],
            env={**base, "MCPANEL_BEHIND_TLS_PROXY": proxy},
            cwd=REPO / "backend",
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.split() == [secure, secure, "0", "True", "False"], proxy
