from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from django.conf import settings
from django.db import connection
from drf_spectacular.utils import extend_schema
from drf_spectacular.views import SpectacularAPIView
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.crypto import is_unreadable
from apps.core.errors import ApiError
from apps.core.permissions import require

from .models import SystemSetting

EDITABLE = {
    "runtime_image_channel": str,
    "runtime_images": dict,
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
    "memory_reserve_mb": int,
    "low_disk_space_gb": int,
}
MEMORY_RESERVE_MAX_MB = 65536
SECRETS = {"curseforge_api_key"}


def supervised_components() -> dict[str, str] | None:
    """State of each process of the single panel container by name (redis, web, worker, periodic, beat).

    Read from the status file of backend/container/supervise.py; None in the one-role-per-container
    layout, where Docker shows each role as its own container.
    """
    path = os.environ.get("MCPANEL_SUPERVISOR_STATUS")
    if not path:
        return None
    try:
        with open(path) as fh:
            data = json.load(fh)
        if time.time() - float(data["time"]) > 60:
            return {"supervisor": "stale"}
        return {str(name): str(child["state"]) for name, child in data["children"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"supervisor": "unknown"}


class HealthView(APIView):
    """Unauthenticated health probe for Docker/Compose health checks.

    ``status`` and the HTTP code only reflect what the web process itself needs (database and
    cache). ``tick_age_seconds`` (age of the last reconcile tick) and ``components`` (the other
    processes of the single panel container) are for the container health check
    (backend/container/healthcheck.py) and for showing which part is down.
    """

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
        from .uploads import upload_dir_problem

        # every request body above FILE_UPLOAD_MAX_MEMORY_SIZE is spooled there: without it they all fail
        checks["uploads"] = "error" if upload_dir_problem() else "ok"
        ok = all(v == "ok" for v in checks.values())
        from mcpanel.celery import tick_age

        age = tick_age()
        body: dict[str, Any] = {
            "status": "ok" if ok else "degraded",
            "checks": checks,
            "tick_age_seconds": None if age is None else round(age, 1),
        }
        components = supervised_components()
        if components is not None:
            body["components"] = components
        return Response(body, status=200 if ok else 503)


class SystemInfoView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        from .permissions import require_more_than_status

        require_more_than_status(request.user)
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


def _is_ip(value: Any) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    return True


class SystemSettingsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        require(request.user, "system.settings")
        values = {key: SystemSetting.get(key) for key in EDITABLE}
        stored = {key: SystemSetting.get_secret(key) for key in SECRETS}
        secrets = {key: bool(value) for key, value in stored.items()}
        if not secrets["curseforge_api_key"] and settings.CURSEFORGE_API_KEY:
            secrets["curseforge_api_key"] = True
        return Response(
            {
                "values": values,
                "secrets_configured": secrets,
                # stored but not decryptable with the current secret key: ask the admin to re-enter it
                "secrets_unreadable": {key: is_unreadable(value) for key, value in stored.items()},
                "env": {"port_range_default": [settings.MCPANEL_PORT_RANGE_START, settings.MCPANEL_PORT_RANGE_END]},
            }
        )

    @extend_schema(request=dict, responses={200: dict})
    def patch(self, request: Request) -> Response:
        require(request.user, "system.settings")
        data = request.data.get("values", {}) if isinstance(request.data, dict) else {}
        secrets = request.data.get("secrets", {}) if isinstance(request.data, dict) else {}
        if not isinstance(data, dict) or not isinstance(secrets, dict):
            raise ApiError("values and secrets must be objects", code="invalid_setting")
        warnings: list[str] = []
        for key, value in secrets.items():
            if key not in SECRETS or not isinstance(value, str) or len(value) > 500:
                raise ApiError(f"Invalid secret {key}", code="invalid_setting")
            if key == "curseforge_api_key" and value.strip():
                # one authenticated call before anything is saved: a mistyped or revoked key would be
                # passed to every CurseForge server, whose next start then fails
                from apps.modpacks.providers import check_curseforge_key

                warning = check_curseforge_key(value.strip())
                if warning:
                    warnings.append(warning)
        changed = {}
        # every value is checked before any is saved: one invalid value leaves all settings as they were
        for key, value in data.items():
            if key not in EDITABLE:
                raise ApiError(f"Unknown setting {key}", code="unknown_setting")
            typ = EDITABLE[key]
            if value is not None and not isinstance(value, typ):
                raise ApiError(f"{key} must be a {typ.__name__}", code="invalid_setting")
            if key == "runtime_image_channel" and value not in ("stable", "latest") and not str(value).startswith("pinned:"):
                raise ApiError("runtime_image_channel must be stable, latest or pinned:<version>", code="invalid_setting")
            if key == "runtime_images" and value is not None:
                _check_runtime_images(value)
            if key in ("port_range_start", "port_range_end") and value is not None and not 1024 <= value <= 65535:
                raise ApiError("Port range must be within 1024-65535", code="invalid_setting")
            if key == "default_bind_ip" and value is not None and not _is_ip(value):
                # used as the host_ip of every new server: a typo would make every create fail
                raise ApiError(
                    "default_bind_ip must be an IPv4 or IPv6 address without a port (0.0.0.0 = all interfaces)",
                    code="invalid_setting",
                )
            if key == "low_disk_space_gb" and (value is None or isinstance(value, bool) or not 0 <= value <= 100000):
                raise ApiError("low_disk_space_gb must be a whole number of GB from 0 (off) to 100000", code="invalid_setting")
            if key == "memory_reserve_mb" and (
                value is None or isinstance(value, bool) or not 0 <= value <= MEMORY_RESERVE_MAX_MB
            ):
                raise ApiError(
                    f"memory_reserve_mb must be a whole number of MB from 0 to {MEMORY_RESERVE_MAX_MB}",
                    code="invalid_setting",
                )
        for key, value in data.items():
            SystemSetting.set(key, value)
            changed[key] = value
        for key, value in secrets.items():
            SystemSetting.set_secret(key, value.strip())
            changed[key] = "[updated]" if value else "[cleared]"
        audit.record("system.settings_updated", request=request, details=changed)
        response = self.get(request)
        response.data["warnings"] = warnings
        return response


_REPOSITORY = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,199}$")


def _check_runtime_images(value: dict[str, Any]) -> None:
    """``runtime_images``: {game: {"repository": "..."}} for games other than Minecraft (Minecraft has
    runtime_image_repository / runtime_image_channel). The agent still has to allow the repository
    (AGENT_GAME_<ID>_IMAGES)."""
    from apps import games

    for game, entry in value.items():
        definition = games.get_or_none(game)
        if definition is None or game == games.DEFAULT_GAME:
            raise ApiError(f"runtime_images: unknown game {game!r} (Minecraft has its own settings)", code="invalid_setting")
        if not isinstance(entry, dict) or set(entry) - {"repository"}:
            raise ApiError(f'runtime_images.{game} must be {{"repository": "..."}}', code="invalid_setting")
        repo = entry.get("repository")
        if repo is not None and (not isinstance(repo, str) or not _REPOSITORY.match(repo) or ".." in repo or "//" in repo):
            raise ApiError(f"runtime_images.{game}.repository is not a valid image repository", code="invalid_setting")


class PortsView(APIView):
    """Ports in use on a node (``?node=<id>``; default node without it) and a free suggestion.

    Any signed-in user may ask for any node (the node list is visible to everyone too); names
    are only revealed for servers the user can see and for host programs to ``system.nodes``.
    Pages about an existing server pass that server's node.
    """

    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        from apps.instances.services import allocate_port, port_range, ports_in_use
        from apps.nodes.models import Node

        from .permissions import require_more_than_status

        require_more_than_status(request.user)

        raw = str(request.query_params.get("node") or "").strip()
        if raw and not raw.isdigit():
            raise ApiError("Unknown node", code="not_found", status_code=404)
        node = Node.objects.filter(pk=int(raw)).first() if raw else Node.default()
        if node is None:
            raise ApiError("Unknown node", code="not_found", status_code=404)
        used = ports_in_use(node)
        # Only reveal names of servers the user can see; others are just "in use".
        from apps.core.permissions import visible_instances
        from apps.instances.models import Instance

        visible = {i.name for i in visible_instances(request.user, Instance.objects.filter(node=node)).only("name")}

        def describe(desc: str) -> str:
            if desc.startswith("server '"):
                name = desc[len("server '") :].split("'", 1)[0]
                return desc if name in visible else "another server"
            return desc if request.user.has_panel_perm("system.nodes") else "another program on the host"

        used = {p: describe(d) for p, d in used.items()}
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
        {
            "error": {
                "code": "csrf_failed",
                "message": f"CSRF check failed: {reason.rstrip('.')}. Reload the page and try again.",
                "details": {},
            }
        },
        status=403,
    )


class ApiSchemaView(SpectacularAPIView):
    """``GET /api/schema``: the OpenAPI document for signed-in users, only when ``MCPANEL_API_DOCS`` is on (off by
    default, D-7/D-13). Anyone else gets what the permissions say first (401/403), then 404."""

    @extend_schema(exclude=True)
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        if not getattr(settings, "MCPANEL_API_DOCS", False):
            from rest_framework.exceptions import NotFound

            raise NotFound()
        return super().get(request, *args, **kwargs)
