from __future__ import annotations

import mimetypes
from typing import Any

from django.db import IntegrityError, transaction
from django.http import StreamingHttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FileUploadParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import InstanceAccess, Role
from apps.accounts.permissions import INSTANCE_CODES
from apps.audit import service as audit
from apps.catalog.providers import resolve_java
from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.core.permissions import get_instance_for, require, visible_instances
from apps.jobs.runner import enqueue
from apps.jobs.serializers import JobSerializer
from apps.nodes.client import AgentClient
from apps.nodes.models import Node

from . import properties, services
from .models import Instance, Schedule, Source
from .serializers import CreateInstanceSerializer, InstanceSerializer, ScheduleSerializer
from .spec import image_for

TEXT_EDIT_LIMIT = 5 * 1024 * 1024


def _ser(instance: Instance, request: Request) -> dict[str, Any]:
    return InstanceSerializer(instance, context={"request": request}).data


class InstanceListView(APIView):
    @extend_schema(responses={200: InstanceSerializer(many=True)})
    def get(self, request: Request) -> Response:
        qs = visible_instances(request.user, Instance.objects.select_related("node", "active_job"))
        archived = request.query_params.get("archived")
        if archived == "true":
            qs = qs.filter(archived_at__isnull=False)
        elif archived != "all":
            qs = qs.filter(archived_at__isnull=True)
        return Response(InstanceSerializer(qs, many=True, context={"request": request}).data)

    @extend_schema(request=CreateInstanceSerializer, responses={201: dict})
    def post(self, request: Request) -> Response:
        require(request.user, "instance.create")
        ser = CreateInstanceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        node = Node.objects.filter(pk=data["node"]).first() if data.get("node") else Node.default()
        if node is None:
            raise ValidationError({"node": ["Unknown node"]})
        fields: dict[str, Any] = {
            "name": data["name"],
            "description": data.get("description", ""),
            "group": data.get("group", ""),
            "icon_url": data.get("icon_url", ""),
            "node": node,
            "created_by": request.user,
            "jvm_flags_preset": data.get("jvm_flags_preset", "aikar"),
            "jvm_args": data.get("jvm_args", ""),
            "env": data.get("env") or {},
            "host_ip": data.get("host_ip") or SystemSetting.get("default_bind_ip") or "0.0.0.0",  # noqa: S104
        }
        warnings: list[str] = []
        if data["mode"] == "standard":
            fields.update(
                server_type=data["server_type"],
                minecraft_version=data["minecraft_version"],
                loader_version=data.get("loader_version") or pinned_loader(data["server_type"], data["minecraft_version"]),
                source=Source.STANDARD,
            )
            java_info = resolve_java(data["minecraft_version"], data["server_type"])
        else:
            from apps.modpacks.providers import get_provider

            if data.get("curseforge_slug"):
                resolved = keyless_curseforge(data)
            else:
                resolved = get_provider(data["modpack_provider"]).resolve_modpack(
                    data["modpack_project_id"], data["modpack_version_id"]
                )
            warnings = resolved.get("warnings", [])
            fields.update(
                server_type=resolved["server_type"],
                minecraft_version=resolved["minecraft_version"],
                source={"modrinth": Source.MODRINTH, "curseforge": Source.CURSEFORGE, "ftb": Source.FTB}[resolved["provider"]],
                modpack_provider=resolved["provider"],
                modpack_project_id=resolved["project_id"],
                modpack_slug=resolved.get("slug") or "",
                modpack_name=resolved["name"],
                modpack_version_id=resolved["version_id"],
                modpack_version_name=resolved.get("version_name") or "",
                modpack_loader=resolved.get("loader") or "",
                loader_version=resolved.get("loader_version") or "",
                modpack_meta={"resolved": {k: v for k, v in resolved.items() if k not in ("warnings",)}},
            )
            if not fields["icon_url"] and resolved.get("icon_url"):
                fields["icon_url"] = resolved["icon_url"][:500]
            java_info = resolve_java(resolved["minecraft_version"], "FORGE" if resolved.get("loader") == "forge" else "")
            if resolved.get("recommended_memory_mb") and not data.get("memory_mb"):
                data["memory_mb"] = int(resolved["recommended_memory_mb"])
        java = data.get("java_version") or java_info["java_version"]
        fields["java_version"] = java
        fields["image"] = image_for(java)
        default_mem = 6144 if data["mode"] == "modpack" else int(SystemSetting.get("default_memory_mb") or 4096)
        fields["memory_mb"] = data.get("memory_mb") or default_mem

        requested_port = data.get("game_port")
        try:
            with transaction.atomic():
                if requested_port:
                    services.check_port_available(node, requested_port)
                    fields["game_port"] = requested_port
                else:
                    fields["game_port"] = services.allocate_port(node)
                instance = Instance.objects.create(**fields)
                manager = Role.objects.filter(key="server_manager").first()
                if manager and not request.user.is_owner and "instance.view" not in request.user.global_permissions():
                    InstanceAccess.objects.create(user=request.user, instance=instance, role=manager, created_by=request.user)
        except IntegrityError as exc:
            raise ConflictError("That port was just taken by another server; choose another", code="port_conflict") from exc

        props = {"motd": data["name"][:59]}
        props.update(data.get("server_properties") or {})
        job = enqueue(
            "install",
            title=f"Install {instance.name}",
            instance=instance,
            user=request.user,
            params={"server_properties": props, "start": data.get("start", True)},
        )
        audit.record(
            "instance.created",
            request=request,
            instance=instance,
            target=instance,
            details={
                "server_type": instance.server_type,
                "version": instance.minecraft_version,
                "modpack": instance.modpack_slug,
                "modpack_version": instance.modpack_version_id,
            },
        )
        instance.refresh_from_db()
        return Response(
            {"instance": _ser(instance, request), "job": JobSerializer(job).data, "warnings": warnings, "java": java_info},
            status=status.HTTP_201_CREATED,
        )


class InstanceDetailView(APIView):
    EDITABLE_RESTART = {
        "memory_mb",
        "memory_overhead_mb",
        "cpu_limit",
        "jvm_flags_preset",
        "jvm_args",
        "env",
        "restart_policy",
        "stop_timeout",
        "host_ip",
        "game_port",
        "extra_ports",
        "network",
        "minecraft_version",
        "loader_version",
        "java_version",
    }

    @extend_schema(responses={200: InstanceSerializer})
    def get(self, request: Request, pk: str) -> Response:
        return Response(_ser(get_instance_for(request.user, pk), request))

    @extend_schema(request=InstanceSerializer, responses={200: InstanceSerializer})
    def patch(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.configure")
        ser = InstanceSerializer(instance, data=request.data, partial=True, context={"request": request})
        ser.is_valid(raise_exception=True)
        changed = {k: v for k, v in ser.validated_data.items() if getattr(instance, k) != v}
        if instance.is_modpack and {"minecraft_version", "loader_version"} & changed.keys():
            raise ValidationError({"minecraft_version": ["Modpack servers take their versions from the pinned modpack release"]})
        if "game_port" in changed:
            services.check_port_available(instance.node, changed["game_port"], exclude=instance)
        for extra in changed.get("extra_ports", []):
            services.check_port_available(instance.node, extra["host_port"], exclude=instance)
        if "java_version" in changed:
            ser.validated_data["image"] = image_for(changed["java_version"])
        try:
            instance = ser.save()
        except IntegrityError as exc:
            raise ConflictError("That port is already used by another server", code="port_conflict") from exc
        if changed.keys() & self.EDITABLE_RESTART:
            services.bump_revision(instance)
        audit.record(
            "instance.updated",
            request=request,
            instance=instance,
            target=instance,
            details={k: (v if k != "env" else sorted(v)) for k, v in changed.items()},
        )
        instance.refresh_from_db()
        return Response(_ser(instance, request))

    @extend_schema(request=None, responses={202: JobSerializer})
    def delete(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.delete")
        confirm = request.data.get("confirm_name") if isinstance(request.data, dict) else None
        if confirm != instance.name:
            raise ApiError("Type the server name exactly to confirm permanent deletion", code="confirmation_required")
        job = enqueue("delete", title=f"Delete {instance.name}", instance=instance, user=request.user)
        audit.record("instance.delete_requested", request=request, instance=instance, target=instance)
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class PowerSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["start", "stop", "restart", "kill"])


class InstancePowerView(APIView):
    @extend_schema(request=PowerSerializer, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.control")
        ser = PowerSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        result = services.power(instance, ser.validated_data["action"], request.user, request)
        if isinstance(result, dict):
            return Response({"status": result})
        return Response(JobSerializer(result).data, status=status.HTTP_202_ACCEPTED)


class InstanceActionView(APIView):
    """recreate | archive | unarchive"""

    ACTIONS = {
        "recreate": ("instance.configure", "Recreate container"),
        "archive": ("instance.delete", "Archive server"),
        "unarchive": ("instance.delete", "Unarchive server"),
    }

    @extend_schema(request=None, responses={202: JobSerializer})
    def post(self, request: Request, pk: str, action: str) -> Response:
        if action not in self.ACTIONS:
            raise ApiError("Unknown action", code="invalid_action", status_code=404)
        perm, title = self.ACTIONS[action]
        instance = get_instance_for(request.user, pk, perm)
        if action == "unarchive":
            if not instance.archived_at:
                raise ConflictError("Server is not archived", code="not_archived")
            instance.archived_at = None
            instance.status = "stopped"
            instance.save(update_fields=["archived_at", "status"])
            audit.record("instance.unarchived", request=request, instance=instance, target=instance)
            return Response(_ser(instance, request))
        if action == "archive" and instance.archived_at:
            raise ConflictError("Server is already archived", code="archived")
        job = enqueue(action, title=f"{title}: {instance.name}", instance=instance, user=request.user)
        audit.record(f"instance.{action}", request=request, instance=instance, target=instance)
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class CloneSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    game_port = serializers.IntegerField(min_value=1024, max_value=65535, required=False)
    exclude_worlds = serializers.BooleanField(default=False)


class InstanceCloneView(APIView):
    @extend_schema(request=CloneSerializer, responses={202: dict})
    def post(self, request: Request, pk: str) -> Response:
        source = get_instance_for(request.user, pk, "instance.view")
        require(request.user, "instance.create")
        require(request.user, "instance.files.read", source)
        ser = CloneSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        copy_fields = [
            "description",
            "icon_url",
            "group",
            "node",
            "edition",
            "server_type",
            "minecraft_version",
            "loader_version",
            "java_version",
            "image",
            "modpack_provider",
            "modpack_project_id",
            "modpack_slug",
            "modpack_name",
            "modpack_version_id",
            "modpack_version_name",
            "modpack_loader",
            "modpack_meta",
            "memory_mb",
            "memory_overhead_mb",
            "cpu_limit",
            "jvm_flags_preset",
            "jvm_args",
            "env",
            "restart_policy",
            "stop_timeout",
            "host_ip",
            "network",
        ]
        fields = {f: getattr(source, f) for f in copy_fields}
        port = ser.validated_data.get("game_port")
        with transaction.atomic():
            if port:
                services.check_port_available(source.node, port)
            else:
                port = services.allocate_port(source.node)
            target = Instance.objects.create(
                name=ser.validated_data["name"], game_port=port, source=Source.CLONED, created_by=request.user, **fields
            )
        job = enqueue(
            "clone",
            title=f"Clone {source.name} → {target.name}",
            instance=source,
            user=request.user,
            params={"target_id": str(target.id), "exclude_worlds": ser.validated_data["exclude_worlds"]},
        )
        audit.record("instance.cloned", request=request, instance=source, target=target, details={"source": str(source.id)})
        return Response({"instance": _ser(target, request), "job": JobSerializer(job).data}, status=status.HTTP_202_ACCEPTED)


class InstanceStatsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk)
        client = AgentClient(instance.node)
        stats: dict[str, Any] = {"running": False}
        try:
            stats = client.stats(instance.id)
        except ApiError as exc:
            if getattr(exc, "error_code", "") not in ("no_container",):
                raise
        if request.query_params.get("disk") == "true":
            stats["disk"] = client.disk(instance.id)
        if stats.get("running") and request.query_params.get("players") != "false":
            try:
                from apps.players.service import online_players

                stats["players"] = online_players(instance)
            except ApiError:
                stats["players"] = None
        return Response(stats)


class InstanceLogsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.console")
        try:
            tail = max(1, min(int(request.query_params.get("tail", "500")), 20000))
        except ValueError:
            tail = 500
        return Response({"lines": AgentClient(instance.node).logs(instance.id, tail=tail)})


class CommandSerializer(serializers.Serializer):
    command = serializers.CharField(max_length=1400, trim_whitespace=True)


class InstanceCommandView(APIView):
    @extend_schema(request=CommandSerializer, responses={200: dict})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.command")
        ser = CommandSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        command = ser.validated_data["command"].lstrip("/")
        if "\n" in command or "\r" in command:
            raise ValidationError({"command": ["Commands must be a single line"]})
        output = AgentClient(instance.node).command(instance.id, command)
        audit.record("instance.command", request=request, instance=instance, target=instance, details={"command": command[:300]})
        return Response({"command": command, "output": output})


class PropertiesView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.view")
        client = AgentClient(instance.node)
        try:
            raw = client.read_text(instance.id, "server.properties")["content"]
            exists = True
        except ApiError as exc:
            if exc.error_code != "not_found":
                raise
            raw, exists = "", False
        return Response(
            {
                "exists": exists,
                "values": properties.parse(raw),
                "raw": raw,
                "schema": properties.SCHEMA,
                "managed_keys": sorted(properties.MANAGED_KEYS),
            }
        )

    @extend_schema(request=dict, responses={200: dict})
    def patch(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.configure")
        changes = request.data.get("values") if isinstance(request.data, dict) else None
        raw_text = request.data.get("raw") if isinstance(request.data, dict) else None
        client = AgentClient(instance.node)
        try:
            current = client.read_text(instance.id, "server.properties")["content"]
        except ApiError as exc:
            if exc.error_code != "not_found":
                raise
            current = ""
        if raw_text is not None:
            if not isinstance(raw_text, str) or len(raw_text) > 512 * 1024:
                raise ValidationError({"raw": ["Invalid properties text"]})
            before, after = properties.parse(current), properties.parse(raw_text)
            for key in properties.MANAGED_KEYS:
                if after.get(key) != before.get(key) and key in after:
                    raise ValidationError({"raw": [f"'{key}' is managed by MCPanel"]})
            new_text = raw_text if raw_text.endswith("\n") else raw_text + "\n"
            changed_keys = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        else:
            if not isinstance(changes, dict):
                raise ValidationError({"values": ["Provide an object of property changes"]})
            try:
                validated = properties.validate_changes(changes)
            except ValueError as exc:
                raise ValidationError({"values": [str(exc)]}) from exc
            new_text = properties.update(current, validated)
            changed_keys = sorted(validated)
        client.write_text(instance.id, "server.properties", new_text)
        audit.record(
            "instance.properties_updated",
            request=request,
            instance=instance,
            target=instance,
            details={"keys": changed_keys[:50]},
        )
        restart = any(properties.SCHEMA_KEYS.get(k, {}).get("restart", True) for k in changed_keys)
        running = instance.status in ("running", "starting", "unhealthy")
        return Response(
            {
                "values": properties.parse(new_text),
                "raw": new_text,
                "changed": changed_keys,
                "restart_required": restart and running,
            }
        )


# --- files -----------------------------------------------------------------------------
class FilesListView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.read")
        return Response(AgentClient(instance.node).list_files(instance.id, request.query_params.get("path", "")))


class FilesTextView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.read")
        return Response(AgentClient(instance.node).read_text(instance.id, request.query_params.get("path", "")))

    @extend_schema(request=dict, responses={200: dict})
    def put(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.write")
        path = str(request.data.get("path", ""))
        content = request.data.get("content")
        if not isinstance(content, str) or len(content.encode()) > TEXT_EDIT_LIMIT:
            raise ValidationError({"content": ["Text content is required (max 5 MB)"]})
        result = AgentClient(instance.node).write_text(instance.id, path, content, bool(request.data.get("create_only")))
        audit.record("files.write", request=request, instance=instance, target=instance, details={"path": path})
        return Response(result)


class FilesDownloadView(APIView):
    def get(self, request: Request, pk: str) -> StreamingHttpResponse:
        instance = get_instance_for(request.user, pk, "instance.files.read")
        path = request.query_params.get("path", "")
        chunks, length, _resp = AgentClient(instance.node).download(instance.id, path)
        filename = path.rsplit("/", 1)[-1] or "download"
        response = StreamingHttpResponse(chunks, content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")
        safe = filename.replace('"', "").replace("\\", "")
        response["Content-Disposition"] = f'attachment; filename="{safe}"'
        if length is not None:
            response["Content-Length"] = str(length)
        response["X-Content-Type-Options"] = "nosniff"
        audit.record("files.download", request=request, instance=instance, target=instance, details={"path": path})
        return response


class FilesUploadView(APIView):
    parser_classes = [MultiPartParser, FileUploadParser]

    @extend_schema(request=None, responses={201: dict})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.write")
        directory = request.query_params.get("path", "").strip("/")
        overwrite = request.query_params.get("overwrite") == "true"
        files = request.FILES.getlist("file") or list(request.FILES.values())
        if not files:
            raise ValidationError({"file": ["No file uploaded"]})
        client = AgentClient(instance.node)
        results = []
        for f in files:
            name = f.name.replace("\\", "/").rsplit("/", 1)[-1]
            if not name or name in (".", ".."):
                raise ValidationError({"file": ["Invalid file name"]})
            target = f"{directory}/{name}" if directory else name
            results.append(client.upload(instance.id, target, f.chunks(256 * 1024), overwrite=overwrite))
        audit.record(
            "files.upload",
            request=request,
            instance=instance,
            target=instance,
            details={"paths": [r["path"] for r in results][:50]},
        )
        return Response({"uploaded": results}, status=status.HTTP_201_CREATED)


class FilesOpView(APIView):
    """mkdir | rename | delete | search"""

    parser_classes = [JSONParser]

    @extend_schema(request=dict, responses={200: dict})
    def post(self, request: Request, pk: str, op: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.read" if op == "search" else "instance.files.write")
        client = AgentClient(instance.node)
        data = request.data if isinstance(request.data, dict) else {}
        if op == "mkdir":
            result = client.mkdir(instance.id, str(data.get("path", "")), bool(data.get("parents")))
        elif op == "rename":
            result = client.rename(
                instance.id, str(data.get("source", "")), str(data.get("destination", "")), bool(data.get("overwrite"))
            )
        elif op == "delete":
            paths = data.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ValidationError({"paths": ["Provide a list of paths"]})
            result = client.delete_files(instance.id, [str(p) for p in paths][:500], bool(data.get("recursive")))
        elif op == "search":
            query = str(data.get("query", ""))
            if len(query) < 2:
                raise ValidationError({"query": ["Enter at least 2 characters"]})
            return Response({"results": client.search_files(instance.id, query, str(data.get("path", "")))})
        else:
            raise ApiError("Unknown file operation", code="invalid_action", status_code=404)
        audit.record(f"files.{op}", request=request, instance=instance, target=instance, details=data)
        return Response(result)


# --- schedules ---------------------------------------------------------------------------
class ScheduleListView(APIView):
    @extend_schema(responses={200: ScheduleSerializer(many=True)})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.view")
        return Response(ScheduleSerializer(instance.schedules.all(), many=True).data)

    @extend_schema(request=ScheduleSerializer, responses={201: ScheduleSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.schedules")
        ser = ScheduleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if ser.validated_data["action"] == "backup":
            require(request.user, "instance.backup", instance)
        sched = ser.save(instance=instance)
        audit.record("schedule.created", request=request, instance=instance, target=sched, details=ser.data)
        return Response(ScheduleSerializer(sched).data, status=status.HTTP_201_CREATED)


class ScheduleDetailView(APIView):
    def _get(self, request: Request, pk: str, sid: int) -> Schedule:
        instance = get_instance_for(request.user, pk, "instance.schedules")
        sched = instance.schedules.filter(pk=sid).first()
        if sched is None:
            raise ApiError("Schedule not found", code="not_found", status_code=404)
        return sched

    @extend_schema(request=ScheduleSerializer, responses={200: ScheduleSerializer})
    def patch(self, request: Request, pk: str, sid: int) -> Response:
        sched = self._get(request, pk, sid)
        ser = ScheduleSerializer(sched, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        sched = ser.save(next_run_at=None)
        audit.record("schedule.updated", request=request, instance=sched.instance, target=sched, details=ser.data)
        return Response(ScheduleSerializer(sched).data)

    @extend_schema(responses={204: None})
    def delete(self, request: Request, pk: str, sid: int) -> Response:
        sched = self._get(request, pk, sid)
        audit.record("schedule.deleted", request=request, instance=sched.instance, target=sched)
        sched.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- access grants -------------------------------------------------------------------------
def _positive_int(value: Any) -> int | None:
    """Primary keys from JSON or a query string; anything else is invalid (not a 500)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 2**63 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18:
        return int(value) or None
    return None


def _guard_existing_grant(instance: Instance, user_id: int, own: frozenset[str]) -> None:
    """Nobody may replace or revoke a grant that carries permissions they do not hold themselves."""
    existing = InstanceAccess.objects.filter(instance=instance, user_id=user_id).select_related("role").first()
    if existing is not None and not (existing.role.permission_set & INSTANCE_CODES) <= own:
        raise PermissionDenied("You cannot change the access of a user who has permissions you do not have on this server")


class AccessListView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.access")
        from apps.accounts.serializers import InstanceAccessSerializer

        grants = InstanceAccess.objects.filter(instance=instance).select_related("user", "role")
        return Response(InstanceAccessSerializer(grants, many=True).data)

    @extend_schema(request=dict, responses={200: dict})
    def put(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.access")
        from apps.accounts.models import User
        from apps.accounts.serializers import InstanceAccessSerializer

        data = request.data if isinstance(request.data, dict) else {}
        user_id, role_id = _positive_int(data.get("user")), _positive_int(data.get("role"))
        user = User.objects.filter(pk=user_id).first() if user_id else None
        role = Role.objects.filter(pk=role_id).first() if role_id else None
        if user is None or role is None:
            raise ValidationError({"detail": ["A valid user and role are required"]})
        if "instance.access" in role.permission_set and not request.user.has_panel_perm("system.users"):
            raise ValidationError({"role": ["Only user administrators can grant access-management rights"]})
        own = request.user.instance_permissions(instance.pk)
        missing = sorted((role.permission_set & INSTANCE_CODES) - own)
        if missing:
            raise PermissionDenied(f"You cannot grant permissions you do not have on this server: {', '.join(missing)}")
        _guard_existing_grant(instance, user.pk, own)
        grant, _ = InstanceAccess.objects.update_or_create(
            user=user, instance=instance, defaults={"role": role, "created_by": request.user}
        )
        audit.record("instance.access_granted", request=request, instance=instance, target=user, details={"role": role.key})
        return Response(InstanceAccessSerializer(grant).data)

    @extend_schema(responses={204: None})
    def delete(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.access")
        user_id = _positive_int(request.query_params.get("user"))
        if user_id is None:
            raise ValidationError({"user": ["A valid user id is required"]})
        _guard_existing_grant(instance, user_id, request.user.instance_permissions(instance.pk))
        InstanceAccess.objects.filter(instance=instance, user_id=user_id).delete()
        audit.record("instance.access_revoked", request=request, instance=instance, target_type="user", target_id=str(user_id))
        return Response(status=status.HTTP_204_NO_CONTENT)


class InstanceJobsView(APIView):
    @extend_schema(responses={200: JobSerializer(many=True)})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk)
        jobs = instance.jobs.all()[:50]
        return Response(JobSerializer(jobs, many=True).data)


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz")


class ExtractView(APIView):
    """Extract an archive that is already in the server folder (uploaded via the file manager)."""

    parser_classes = [JSONParser]

    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.write")
        data = request.data if isinstance(request.data, dict) else {}
        path = str(data.get("path", ""))
        if not path.lower().endswith(ARCHIVE_SUFFIXES):
            raise ValidationError({"path": ["Choose a .zip, .tar, .tar.gz or .tar.xz archive"]})
        strip = data.get("strip", "auto")
        if strip not in ("auto", "none"):
            raise ValidationError({"strip": ["Must be auto or none"]})
        job = enqueue(
            "extract_archive",
            title=f"Extract {path.rsplit('/', 1)[-1]}",
            instance=instance,
            user=request.user,
            params={
                "path": path,
                "destination": str(data.get("destination", "")),
                "strip": strip,
                "overwrite": bool(data.get("overwrite")),
                "delete_archive": bool(data.get("delete_archive")),
            },
        )
        audit.record("files.extract", request=request, instance=instance, target=instance, details={"path": path})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class ImportArchiveView(APIView):
    """Import an existing server: upload an archive of its folder and extract it into /data.

    The server must be stopped. Existing files are kept unless ``overwrite=true``.
    """

    parser_classes = [MultiPartParser, FileUploadParser]

    @extend_schema(request=None, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.write")
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": ["Upload an archive in the 'file' field"]})
        name = upload.name.lower()
        if not name.endswith(ARCHIVE_SUFFIXES):
            raise ValidationError({"file": ["Supported archives: .zip, .tar, .tar.gz, .tar.xz"]})
        if instance.status in ("running", "starting", "installing", "unhealthy"):
            raise ConflictError("Stop the server before importing files into it", code="must_stop")
        import secrets as _secrets

        suffix = next(s for s in ARCHIVE_SUFFIXES if name.endswith(s))
        temp_name = f".mcpanel-import-{_secrets.token_hex(6)}{suffix}"
        client = AgentClient(instance.node)
        client.upload(instance.id, temp_name, upload.chunks(1024 * 1024), overwrite=False)
        overwrite = request.query_params.get("overwrite") == "true"
        job = enqueue(
            "extract_archive",
            title=f"Import {upload.name}",
            instance=instance,
            user=request.user,
            params={
                "path": temp_name,
                "destination": "",
                "strip": "auto",
                "overwrite": overwrite,
                "delete_archive": True,
                "mark_installed": True,
            },
        )
        audit.record(
            "instance.imported",
            request=request,
            instance=instance,
            target=instance,
            details={"archive": upload.name[:200], "size": upload.size, "overwrite": overwrite},
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class DiagnosticsView(APIView):
    """Known failure signatures found in the recent server log, with suggested fixes."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from .diagnostics import diagnose

        instance = get_instance_for(request.user, pk, "instance.console")
        return Response(diagnose(instance))


class ManualDownloadsView(APIView):
    """Files a modpack needs that could not be downloaded automatically (author restrictions)."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from .diagnostics import manual_downloads

        instance = get_instance_for(request.user, pk, "instance.files.read")
        return Response(manual_downloads(instance))


def keyless_curseforge(data: dict[str, Any]) -> dict[str, Any]:
    """Describe a CurseForge pack identified only by slug/URL (no API key configured)."""
    slug = data["curseforge_slug"]
    file_id = str(data.get("modpack_version_id") or "")
    warnings = [
        "No CurseForge API key is configured, so the release could not be verified in advance. "
        "The runtime installs it with its built-in key.",
    ]
    if not file_id:
        warnings.append("No file was selected: the newest release is installed and then pinned automatically.")
    return {
        "provider": "curseforge",
        "server_type": "AUTO_CURSEFORGE",
        "project_id": "",
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "icon_url": "",
        "version_id": file_id,
        "version_name": "",
        "minecraft_version": data["minecraft_version"],
        "loader": data.get("modpack_loader") or "",
        "loader_version": "",
        "warnings": warnings,
        "recommended_memory_mb": None,
    }


class SoftwareUpdateView(APIView):
    """Explicit version change for standard servers with a pre-update backup (default on)."""

    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        from apps.catalog.providers import game_versions, loader_versions, version_key

        instance = get_instance_for(request.user, pk, "instance.configure")
        if instance.is_modpack:
            raise ApiError("Modpack servers are updated through the modpack release picker", code="use_modpack_update")
        data = request.data if isinstance(request.data, dict) else {}
        mc = str(data.get("minecraft_version") or "").strip()
        loader = str(data.get("loader_version") or "").strip()
        if not mc:
            raise ValidationError({"minecraft_version": ["Choose a Minecraft version"]})
        known = {v["version"] for v in game_versions(instance.server_type, include_snapshots=True)}
        if mc.upper() != "LATEST" and mc not in known:
            raise ValidationError({"minecraft_version": [f"{mc} is not available for {instance.server_type}"]})
        if loader and instance.server_type != "VANILLA":
            loaders = {v["version"] for v in loader_versions(instance.server_type, mc)}
            if loaders and loader not in loaders:
                raise ValidationError({"loader_version": [f"{loader} is not available for {mc}"]})
        java = data.get("java_version") or resolve_java(mc, instance.server_type)["java_version"]
        if int(java) not in (8, 11, 16, 17, 21, 25):
            raise ValidationError({"java_version": ["Unsupported Java version"]})
        downgrade = (
            instance.minecraft_version.upper() != "LATEST"
            and mc.upper() != "LATEST"
            and version_key(mc) < version_key(instance.minecraft_version)
        )
        if downgrade and data.get("allow_downgrade") is not True:
            raise ApiError(
                f"Downgrading from {instance.minecraft_version} to {mc} can corrupt worlds. Restore an older backup "
                "instead, or confirm the downgrade explicitly.",
                code="downgrade_unconfirmed",
                status_code=409,
            )
        job = enqueue(
            "software_update",
            title=f"Update {instance.name} to {mc}",
            instance=instance,
            user=request.user,
            params={
                "minecraft_version": mc,
                "loader_version": loader,
                "java_version": int(java),
                "backup": data.get("backup", True) is not False,
                "start": bool(data.get("start")),
            },
        )
        audit.record(
            "instance.software_update",
            request=request,
            instance=instance,
            target=instance,
            details={"from": instance.minecraft_version, "to": mc, "loader": loader, "java": java},
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


def pinned_loader(server_type: str, minecraft_version: str) -> str:
    """Resolve 'latest/recommended' to a concrete loader/build so the config is reproducible.

    Falls back to '' (runtime default) if the catalog is unreachable; the user can pin later.
    """
    from apps.catalog.providers import loader_versions

    if server_type == "VANILLA" or minecraft_version.upper() == "LATEST":
        return ""
    try:
        versions = loader_versions(server_type, minecraft_version)
    except ApiError:
        return ""
    if not versions:
        return ""
    preferred = next((v for v in versions if v.get("recommended")), None) or next((v for v in versions if v.get("stable")), None)
    return str((preferred or versions[0])["version"])
