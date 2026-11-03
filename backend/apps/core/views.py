from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import require

from .models import SystemSetting

EDITABLE = {
    "runtime_image_channel": str,
    "default_memory_mb": int,
    "port_range_start": int,
    "port_range_end": int,
    "default_bind_ip": str,
    "backup_default_retention": dict,
    "pre_restore_backup": bool,
    "pre_update_backup": bool,
    "public_address": str,
    "lan_address": str,
    "timezone": str,
}
SECRETS = {"curseforge_api_key"}


class HealthView(APIView):
    """Unauthenticated health probe for Docker/Compose health checks."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []

    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        checks: dict[str, Any] = {}
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "error"
        try:
            from django.core.cache import cache

            cache.set("health", "1", 5)
            checks["cache"] = "ok" if cache.get("health") == "1" else "error"
        except Exception:
            checks["cache"] = "error"
        ok = all(v == "ok" for v in checks.values())
        return Response({"status": "ok" if ok else "degraded", "checks": checks}, status=200 if ok else 503)


class SystemInfoView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        from apps.instances.models import Instance
        from apps.nodes.client import AgentClient
        from apps.nodes.models import Node

        node = Node.default()
        agent: dict[str, Any] = {"reachable": False}
        try:
            info = AgentClient(node).info()
            agent = {"reachable": True, **info}
        except ApiError as exc:
            agent["error"] = str(exc.detail)
        if not request.user.has_panel_perm("system.nodes"):
            # non-admins only learn whether the node works and its capacity summary
            agent = {
                "reachable": agent.get("reachable"),
                "docker": {k: (agent.get("docker") or {}).get(k) for k in ("cpus", "memory_bytes")},
            }
        counts = {
            "instances": Instance.objects.filter(archived_at__isnull=True).count(),
            "running": Instance.objects.filter(status="running").count(),
        }
        return Response({"version": "0.1.0", "node": {"id": node.id, "name": node.name}, "agent": agent, "counts": counts})


class SystemSettingsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        require(request.user, "system.settings")
        values = {key: SystemSetting.get(key) for key in EDITABLE}
        secrets = {key: bool(SystemSetting.get_secret(key)) for key in SECRETS}
        if not secrets["curseforge_api_key"] and settings.CURSEFORGE_API_KEY:
            secrets["curseforge_api_key"] = True
        return Response(
            {
                "values": values,
                "secrets_configured": secrets,
                "env": {"port_range_default": [settings.MCPANEL_PORT_RANGE_START, settings.MCPANEL_PORT_RANGE_END]},
            }
        )

    @extend_schema(request=dict, responses={200: dict})
    def patch(self, request: Request) -> Response:
        require(request.user, "system.settings")
        data = request.data.get("values", {}) if isinstance(request.data, dict) else {}
        changed = {}
        for key, value in data.items():
            if key not in EDITABLE:
                raise ApiError(f"Unknown setting {key}", code="unknown_setting")
            typ = EDITABLE[key]
            if value is not None and not isinstance(value, typ):
                raise ApiError(f"{key} must be a {typ.__name__}", code="invalid_setting")
            if key == "runtime_image_channel" and value not in ("stable", "latest") and not str(value).startswith("pinned:"):
                raise ApiError("runtime_image_channel must be stable, latest or pinned:<version>", code="invalid_setting")
            if key in ("port_range_start", "port_range_end") and value is not None and not 1024 <= value <= 65535:
                raise ApiError("Port range must be within 1024-65535", code="invalid_setting")
            SystemSetting.set(key, value)
            changed[key] = value
        for key, value in (request.data.get("secrets", {}) if isinstance(request.data, dict) else {}).items():
            if key not in SECRETS or not isinstance(value, str) or len(value) > 500:
                raise ApiError(f"Invalid secret {key}", code="invalid_setting")
            SystemSetting.set_secret(key, value.strip())
            changed[key] = "[updated]" if value else "[cleared]"
        audit.record("system.settings_updated", request=request, details=changed)
        return self.get(request)


class PortsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        from apps.instances.services import allocate_port, port_range, ports_in_use
        from apps.nodes.models import Node

        node = (
            Node.objects.filter(pk=request.query_params.get("node")).first()
            if request.query_params.get("node")
            else Node.default()
        )
        if node is None:
            raise ApiError("Unknown node", code="not_found", status_code=404)
        used = ports_in_use(node)
        try:
            suggestion = allocate_port(node)
        except ApiError:
            suggestion = None
        return Response(
            {
                "range": port_range(),
                "in_use": [{"port": p, "used_by": d} for p, d in sorted(used.items())],
                "suggested": suggestion,
            }
        )


def csrf_failure(request, reason: str = ""):  # type: ignore[no-untyped-def]
    """JSON CSRF failure response in the API error format (Django's default is an HTML page)."""
    from django.http import JsonResponse

    return JsonResponse(
        {"error": {"code": "csrf_failed", "message": f"CSRF check failed: {reason.rstrip('.')}. Reload the page and try again.",
                   "details": {}}},
        status=403,
    )
