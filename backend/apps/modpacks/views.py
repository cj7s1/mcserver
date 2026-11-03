from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import ApiError

from .providers import PROVIDERS, curseforge_key, get_provider


def _int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value or default), hi))
    except ValueError:
        return default


class ProvidersView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        return Response(
            {
                "providers": [
                    {"id": "modrinth", "label": "Modrinth", "available": True, "requires_key": False},
                    {
                        "id": "curseforge",
                        "label": "CurseForge",
                        "available": bool(curseforge_key()),
                        "requires_key": True,
                        "note": None if curseforge_key() else "Add a CurseForge API key in System settings to browse CurseForge.",
                    },
                    {"id": "ftb", "label": "Feed The Beast", "available": True, "requires_key": False},
                ]
            }
        )


class SearchView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, provider: str) -> Response:
        p = get_provider(provider)
        qp = request.query_params
        loaders = [x for x in qp.get("loaders", "").split(",") if x]
        categories = [x for x in qp.get("categories", "").split(",") if x]
        project_type = qp.get("type", "modpack")
        if project_type not in ("modpack", "mod", "plugin"):
            raise ApiError("type must be modpack, mod or plugin", code="invalid_type")
        result = p.search(
            query=qp.get("q", "")[:200],
            project_type=project_type,
            loaders=loaders or None,
            game_version=qp.get("game_version") or None,
            categories=categories or None,
            sort=qp.get("sort", "relevance"),
            offset=_int(qp.get("offset"), 0, 0, 10000),
            limit=_int(qp.get("limit"), 20, 1, 50),
            server_only=qp.get("server_only", "true") != "false",
        )
        return Response(result)


class ProjectView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, provider: str, project_id: str) -> Response:
        return Response(get_provider(provider).project(project_id))


class ReleasesView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, provider: str, project_id: str) -> Response:
        qp = request.query_params
        loaders = [x for x in qp.get("loaders", "").split(",") if x] or None
        game_versions = [x for x in qp.get("game_versions", "").split(",") if x] or None
        releases = get_provider(provider).releases(project_id, loaders=loaders, game_versions=game_versions)
        if qp.get("include_prerelease") != "true":
            stable = [r for r in releases if r.get("version_type") in (None, "release")]
            releases = stable or releases
        return Response({"releases": releases[:100]})


class ResolveView(APIView):
    """Validate a modpack release for server deployment and report what will be installed."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, provider: str, project_id: str, version_id: str) -> Response:
        resolved = get_provider(provider).resolve_modpack(project_id, version_id)
        from apps.catalog.providers import resolve_java

        java = resolve_java(resolved["minecraft_version"], "FORGE" if resolved.get("loader") == "forge" else "")
        resolved["java"] = java
        return Response(resolved)


class ReleaseDetailView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, provider: str, project_id: str, version_id: str) -> Response:
        p = get_provider(provider)
        if provider == "modrinth":
            return Response(PROVIDERS["modrinth"].release(version_id))
        if provider == "curseforge":
            rel = p.release(project_id, version_id)
            rel["changelog"] = p.changelog(project_id, version_id)
            return Response(rel)
        rel = next((r for r in p.releases(project_id) if r["id"] == str(version_id)), None)
        if rel is None:
            raise ApiError("Release not found", code="not_found", status_code=404)
        return Response(rel)


class InstanceModpackView(APIView):
    """Current pin, available releases and update history for a modpack server."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from apps.backups.models import Backup, BackupKind, BackupStatus
        from apps.core.permissions import get_instance_for

        instance = get_instance_for(request.user, pk, "instance.view")
        if not instance.is_modpack:
            raise ApiError("This server is not a modpack server", code="not_modpack", status_code=409)
        provider = get_provider(instance.modpack_provider)
        releases = provider.releases(instance.modpack_project_id)
        if request.query_params.get("include_prerelease") != "true":
            releases = [r for r in releases if r.get("version_type") in (None, "release")] or releases
        current = next((r for r in releases if r["id"] == instance.modpack_version_id), None)
        # releases are sorted newest first by the provider adapters
        newer = releases[: releases.index(current)] if current is not None else []
        rollback_points = [
            {"backup_id": str(b.id), "name": b.name, "created_at": b.created_at, "before": (b.meta or {}).get("modpack_before")}
            for b in Backup.objects.filter(instance=instance, kind=BackupKind.PRE_UPDATE, status=BackupStatus.SUCCEEDED)
            if (b.meta or {}).get("modpack_before")
        ]
        return Response(
            {
                "provider": instance.modpack_provider,
                "project_id": instance.modpack_project_id,
                "name": instance.modpack_name,
                "current": current or {"id": instance.modpack_version_id, "version_number": instance.modpack_version_name},
                "releases": releases[:50],
                "updates_available": len(newer),
                "history": (instance.modpack_meta or {}).get("history", []),
                "rollback_points": rollback_points,
            }
        )


class InstanceModpackPlanView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str, version_id: str) -> Response:
        from apps.core.permissions import get_instance_for

        from .handlers import plan_update

        instance = get_instance_for(request.user, pk, "instance.content")
        return Response(plan_update(instance, version_id))


class InstanceModpackUpdateView(APIView):
    @extend_schema(request=dict, responses={202: dict})
    def post(self, request: Request, pk: str) -> Response:
        from apps.audit import service as audit
        from apps.core.permissions import get_instance_for
        from apps.jobs.runner import enqueue
        from apps.jobs.serializers import JobSerializer

        instance = get_instance_for(request.user, pk, "instance.content")
        version_id = str(request.data.get("version_id") or "")
        if not version_id:
            raise ApiError("version_id is required", code="missing_params")
        job = enqueue(
            "modpack_update",
            title=f"Update {instance.modpack_name or instance.name}",
            instance=instance,
            user=request.user,
            params={
                "version_id": version_id,
                "backup": bool(request.data.get("backup", True)),
                "allow_minecraft_change": bool(request.data.get("allow_minecraft_change", False)),
                "start": bool(request.data.get("start", False)),
            },
        )
        audit.record(
            "modpack.update",
            request=request,
            instance=instance,
            target=instance,
            details={"from": instance.modpack_version_id, "to": version_id},
        )
        return Response(JobSerializer(job).data, status=202)


class InstanceModpackRollbackView(APIView):
    @extend_schema(request=dict, responses={202: dict})
    def post(self, request: Request, pk: str) -> Response:
        from apps.audit import service as audit
        from apps.core.permissions import get_instance_for, require
        from apps.jobs.runner import enqueue
        from apps.jobs.serializers import JobSerializer

        instance = get_instance_for(request.user, pk, "instance.content")
        require(request.user, "instance.restore", instance)
        if request.data.get("confirm") is not True:
            raise ApiError(
                "Rollback replaces the current server files with the pre-update backup. Confirm to continue.",
                code="confirmation_required",
            )
        backup_id = str(request.data.get("backup_id") or "")
        job = enqueue(
            "modpack_rollback",
            title=f"Roll back {instance.modpack_name or instance.name}",
            instance=instance,
            user=request.user,
            params={"backup_id": backup_id},
        )
        audit.record("modpack.rollback", request=request, instance=instance, target=instance, details={"backup": backup_id})
        return Response(JobSerializer(job).data, status=202)
