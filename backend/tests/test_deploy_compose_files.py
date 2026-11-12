"""The Compose files of deploy/, against the security review of 2026-11-11.

A-4: on an additional node the agent port is published, and the network that carries it decides the
address a game container's request to the node's published address arrives from. The file meant
`api` to carry it, but `priority` is only Compose's connect order: with equal gateway priorities
Docker picks the network whose name sorts first, so the project name decided. `gw_priority` decides
it (checked on Docker 29.9 / Compose 5.7 with both name orders: the request came from the gateway of
`api` each time; with `priority` alone, from the gateway of the network that sorted first).
"""

from __future__ import annotations

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
