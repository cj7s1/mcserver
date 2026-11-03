"""Add or update an additional node from the command line.

    docker compose exec panel python manage.py add_node --name node2 \
        --url https://192.168.1.20:8700 --token <AGENT_TOKEN> --fingerprint AB:CD:...

Uses the same validation as the API: the agent certificate is read and must match the
fingerprint before it is pinned. The token can also come from $MCPANEL_NODE_TOKEN so it
does not end up in shell history.
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from apps.audit import service as audit
from apps.nodes.client import AgentClient
from apps.nodes.models import Node
from apps.nodes.views import NodeSerializer


class Command(BaseCommand):
    help = "Add (or update) a node agent, pinning its TLS certificate fingerprint"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--name", required=True)
        parser.add_argument("--url", required=True, help="https://<host>:<port> of the agent")
        parser.add_argument("--token", default="", help="AGENT_TOKEN of that node (or set MCPANEL_NODE_TOKEN)")
        parser.add_argument("--fingerprint", default="", help="output of `python -m mcagent fingerprint`")
        parser.add_argument("--lan-address", default="", help="address players on the LAN use for this node")
        parser.add_argument("--public-address", default="")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        token = opts["token"] or os.environ.get("MCPANEL_NODE_TOKEN", "")
        existing = Node.objects.filter(name=opts["name"]).first()
        data = {
            "name": opts["name"],
            "agent_url": opts["url"],
            "tls_fingerprint": opts["fingerprint"],
            "use_settings_token": False,
            "lan_address": opts["lan_address"],
            "public_address": opts["public_address"],
        }
        if token:
            data["agent_token"] = token
        ser = NodeSerializer(instance=existing, data=data, partial=existing is not None)
        if not ser.is_valid():
            raise CommandError("; ".join(f"{k}: {' '.join(map(str, v))}" for k, v in ser.errors.items()))
        node = ser.save()
        audit.record("node.updated" if existing else "node.created", target=node, details={"url": node.agent_url, "via": "cli"})
        out = ser.to_representation(node)
        self.stdout.write(
            f"{'Updated' if existing else 'Added'} node {node.name} ({out['transport']}, pinned={out['tls_pinned']})"
        )
        if out.get("security_warning"):
            self.stdout.write(self.style.WARNING(out["security_warning"]))
        try:
            info = AgentClient(node, timeout=10).info()
            self.stdout.write(self.style.SUCCESS(f"Agent reachable: version {info.get('version')}"))
        except Exception as exc:  # report, the node is saved either way
            self.stdout.write(self.style.WARNING(f"Agent check failed: {exc}"))
