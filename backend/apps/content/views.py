"""Mods/plugins content manager API (mounted at /api/instances/<uuid:pk>/content/).

Reading requires ``instance.view``; every mutation requires ``instance.content`` and
is written to the audit log (including failures).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import get_instance_for
from apps.instances.models import Instance

from . import service

READ = "instance.view"
WRITE = "instance.content"
MAX_QUERY_LENGTH = 200
# multipart framing overhead allowed on top of the jar size limit
UPLOAD_OVERHEAD = 1024 * 1024


def _int(value: Any, default: int, lo: int, hi: int, field: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{field} must be a whole number", code="invalid_parameter") from exc
    if not lo <= number <= hi:
        raise ApiError(f"{field} must be between {lo} and {hi}", code="invalid_parameter")
    return number


def _bool(value: Any, field: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        return value
    raise ApiError(f"{field} must be true or false", code="invalid_parameter")


def _body(request: Request) -> dict[str, Any]:
    if not isinstance(request.data, dict):
        raise ApiError("Expected a JSON object", code="invalid_body")
    return request.data


def _audited(
    request: Request, instance: Instance, action: str, details: dict[str, Any], fn: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Run a mutation and audit its outcome (successful or not)."""
    try:
        result = fn()
    except ApiError as exc:
        audit.record(
            action,
            request=request,
            instance=instance,
            target=instance,
            success=False,
            details={**details, "error": exc.error_code, "message": str(exc.detail)[:300]},
        )
        raise
    return result


class ContentListView(APIView):
    @extend_schema(responses={200: dict}, summary="Installed mods/plugins, identified on Modrinth")
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, READ)
        return Response(service.inventory(instance))


class ContentUpdatesView(APIView):
    @extend_schema(responses={200: dict}, summary="Newer compatible versions for installed content")
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, READ)
        return Response(service.check_updates(instance))


class ContentSearchView(APIView):
    @extend_schema(responses={200: dict}, summary="Search Modrinth for content compatible with this server")
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, READ)
        qp = request.query_params
        query = qp.get("q", "")
        if len(query) > MAX_QUERY_LENGTH:
            raise ApiError(f"Search text may be at most {MAX_QUERY_LENGTH} characters", code="invalid_parameter")
        sort = qp.get("sort") or "relevance"
        if sort not in service.SEARCH_SORTS:
            raise ApiError(f"sort must be one of {', '.join(service.SEARCH_SORTS)}", code="invalid_parameter")
        return Response(
            service.search(
                instance,
                query=query.strip(),
                offset=_int(qp.get("offset"), 0, 0, 10000, "offset"),
                limit=_int(qp.get("limit"), 20, 1, 50, "limit"),
                sort=sort,
            )
        )


class ContentProjectVersionsView(APIView):
    @extend_schema(responses={200: dict}, summary="Versions of a Modrinth project compatible with this server")
    def get(self, request: Request, pk: str, project_id: str) -> Response:
        instance = get_instance_for(request.user, pk, READ)
        return Response(service.project_versions(instance, service.validate_project_id(project_id)))


class ContentInstallView(APIView):
    parser_classes = [JSONParser]

    @extend_schema(request=dict, responses={201: dict}, summary="Install a compatible Modrinth mod/plugin version")
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, WRITE)
        data = _body(request)
        project_id = service.validate_project_id(data.get("project_id"))
        version_id = data.get("version_id") or None
        if version_id is not None:
            version_id = service.validate_version_id(version_id)
        with_deps = _bool(data.get("install_dependencies"), "install_dependencies", default=False)
        details = {"project_id": project_id, "version_id": version_id, "install_dependencies": with_deps}
        result = _audited(
            request,
            instance,
            "content.install",
            details,
            lambda: service.install(instance, project_id=project_id, version_id=version_id, install_dependencies=with_deps),
        )
        audit.record(
            "content.install",
            request=request,
            instance=instance,
            target=instance,
            details={
                **details,
                "installed": [
                    {"project_id": i["project_id"], "version_id": i["version_id"], "filename": i["filename"]}
                    for i in result["installed"]
                ],
                "missing_dependencies": [d.get("project_id") or d.get("file_name") for d in result["missing_dependencies"]],
            },
        )
        return Response(result, status=status.HTTP_201_CREATED)


class ContentUpdateView(APIView):
    parser_classes = [JSONParser]

    @extend_schema(request=dict, responses={200: dict}, summary="Update installed files to their newest compatible version")
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, WRITE)
        files = _body(request).get("files")
        if not isinstance(files, list):
            raise ApiError("files must be a list of file names", code="invalid_files")
        details = {"files": [str(f)[:255] for f in files[: service.MAX_FILES_PER_UPDATE]]}
        result = _audited(request, instance, "content.update", details, lambda: service.update(instance, files))
        audit.record(
            "content.update",
            request=request,
            instance=instance,
            target=instance,
            success=result["failed"] == 0,
            details={
                "results": [
                    {
                        "filename": r["filename"],
                        "status": r["status"],
                        "new_filename": r.get("new_filename"),
                        "to_version": (r.get("to_version") or {}).get("id"),
                        "reason": r.get("reason"),
                    }
                    for r in result["results"]
                ]
            },
        )
        return Response(result)


class ContentToggleView(APIView):
    parser_classes = [JSONParser]

    @extend_schema(request=dict, responses={200: dict}, summary="Enable or disable a jar (.jar <-> .jar.disabled)")
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, WRITE)
        data = _body(request)
        filename = service.validate_filename(data.get("filename"))
        enabled = _bool(data.get("enabled"), "enabled")
        details = {"filename": filename, "enabled": enabled}
        result = _audited(request, instance, "content.toggle", details, lambda: service.toggle(instance, filename, enabled))
        if result["changed"]:
            audit.record(
                "content.toggle",
                request=request,
                instance=instance,
                target=instance,
                details={**details, "new_filename": result["filename"]},
            )
        return Response(result)


class ContentFilesView(APIView):
    @extend_schema(responses={200: dict}, summary="Delete a mod/plugin jar")
    def delete(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, WRITE)
        filename = service.validate_filename(request.query_params.get("filename"))
        details = {"filename": filename}
        result = _audited(request, instance, "content.delete", details, lambda: service.delete(instance, filename))
        audit.record("content.delete", request=request, instance=instance, target=instance, details=details)
        return Response(result)


class ContentUploadView(APIView):
    parser_classes = [MultiPartParser]

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        # Refuse oversized bodies before anything parses them. This must run before
        # authentication: SessionAuthentication's CSRF check reads request.POST, which
        # parses (and spools to disk) the whole multipart body.
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > service.MAX_UPLOAD_BYTES + UPLOAD_OVERHEAD:
            raise ApiError(
                f"Jar files may be at most {service.MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                code="file_too_large",
                status_code=413,
            )
        super().initial(request, *args, **kwargs)

    @extend_schema(request=None, responses={201: dict}, summary="Upload a .jar (untrusted third-party code)")
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, WRITE)
        overwrite = request.query_params.get("overwrite") == "true"
        files = request.FILES.getlist("file")
        if len(files) != 1:
            raise ApiError("Upload exactly one .jar file in the 'file' field", code="invalid_upload")
        uploaded = files[0]
        details = {"filename": str(uploaded.name)[:255], "size": uploaded.size, "overwrite": overwrite}
        result = _audited(
            request, instance, "content.upload", details, lambda: service.upload(instance, uploaded, overwrite=overwrite)
        )
        audit.record(
            "content.upload",
            request=request,
            instance=instance,
            target=instance,
            details={
                **details,
                "filename": result["uploaded"]["filename"],
                "sha1": result["uploaded"]["sha1"],
                "identified_project": ((result.get("identified") or {}).get("project") or {}).get("id"),
            },
        )
        return Response(result, status=status.HTTP_201_CREATED)
