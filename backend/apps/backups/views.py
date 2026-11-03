from __future__ import annotations

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import get_instance_for, require
from apps.instances import services
from apps.instances.models import Instance, Source
from apps.instances.serializers import InstanceSerializer
from apps.jobs.runner import enqueue
from apps.jobs.serializers import JobSerializer

from .handlers import policy_for
from .models import Backup, BackupStatus, RetentionPolicy


class BackupSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default=None)

    class Meta:
        model = Backup
        fields = [
            "id",
            "name",
            "kind",
            "status",
            "engine",
            "snapshot_id",
            "data_added_bytes",
            "total_bytes",
            "files_count",
            "duration_seconds",
            "consistent",
            "warnings",
            "error",
            "pinned",
            "notes",
            "created_by_name",
            "created_at",
            "finished_at",
            "verified_at",
            "verify_ok",
        ]
        read_only_fields = [f for f in fields if f not in ("name", "pinned", "notes")]


class PolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = RetentionPolicy
        fields = ["keep_last", "keep_daily", "keep_weekly", "keep_monthly", "keep_safety_days", "excludes"]

    def validate_keep_last(self, v: int) -> int:
        if v < 1:
            raise serializers.ValidationError("Keep at least one backup")
        return v

    def validate_excludes(self, v: list) -> list:
        if not isinstance(v, list) or len(v) > 50:
            raise serializers.ValidationError("Up to 50 patterns")
        import re

        for p in v:
            if not isinstance(p, str) or not re.match(r"^[A-Za-z0-9_.:/*@-]{1,200}$", p) or ".." in p:
                raise serializers.ValidationError(f"Invalid pattern {p!r}")
        return v


class BackupListView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.backup")
        qs = instance.backups.select_related("created_by")
        if request.query_params.get("all") != "true":
            qs = qs.exclude(status=BackupStatus.DELETED)
        return Response({"backups": BackupSerializer(qs, many=True).data, "policy": PolicySerializer(policy_for(instance)).data})

    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.backup")
        name = str(request.data.get("name") or "").strip()[:120]
        consistency = request.data.get("consistency", "auto")
        if consistency not in ("auto", "require", "none"):
            raise ApiError("consistency must be auto, require or none", code="invalid_consistency")
        job = enqueue(
            "backup",
            title=f"Backup {instance.name}",
            instance=instance,
            user=request.user,
            params={"name": name or None, "kind": "manual", "consistency": consistency},
        )
        audit.record("backup.requested", request=request, instance=instance, target=instance, details={"name": name})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class BackupPolicyView(APIView):
    @extend_schema(request=PolicySerializer, responses={200: PolicySerializer})
    def put(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.restore")
        policy = policy_for(instance)
        ser = PolicySerializer(policy, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        audit.record("backup.policy_updated", request=request, instance=instance, target=instance, details=ser.data)
        return Response(ser.data)


class BackupDetailView(APIView):
    def _get(self, request: Request, pk: str, bid: str, code: str) -> tuple[Instance, Backup]:
        instance = get_instance_for(request.user, pk, code)
        backup = instance.backups.filter(pk=bid).exclude(status=BackupStatus.DELETED).first()
        if backup is None:
            raise ApiError("Backup not found", code="not_found", status_code=404)
        return instance, backup

    @extend_schema(request=BackupSerializer, responses={200: BackupSerializer})
    def patch(self, request: Request, pk: str, bid: str) -> Response:
        instance, backup = self._get(request, pk, bid, "instance.backup")
        if "pinned" in request.data:
            require(request.user, "instance.restore", instance)
        ser = BackupSerializer(backup, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    @extend_schema(responses={202: JobSerializer})
    def delete(self, request: Request, pk: str, bid: str) -> Response:
        instance, backup = self._get(request, pk, bid, "instance.restore")
        if backup.pinned:
            raise ApiError("Unpin this backup before deleting it", code="pinned", status_code=409)
        if backup.status != BackupStatus.SUCCEEDED:
            backup.status = BackupStatus.DELETED
            backup.save(update_fields=["status"])
            return Response(status=status.HTTP_204_NO_CONTENT)
        job = enqueue(
            "backup_delete",
            title=f"Delete backup '{backup.name}'",
            instance=instance,
            user=request.user,
            params={"backup_id": str(backup.id)},
        )
        audit.record("backup.delete", request=request, instance=instance, target=backup)
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class BackupRestoreView(APIView):
    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str, bid: str) -> Response:
        instance, backup = BackupDetailView()._get(request, pk, bid, "instance.restore")
        if backup.status != BackupStatus.SUCCEEDED or not backup.snapshot_id:
            raise ApiError("Only completed backups can be restored", code="not_restorable", status_code=409)
        if request.data.get("confirm") is not True:
            raise ApiError("Restoring replaces the current server files. Confirm to continue.", code="confirmation_required")
        from apps.core.models import SystemSetting

        pre = bool(request.data.get("pre_restore_backup", SystemSetting.get("pre_restore_backup")))
        job = enqueue(
            "restore",
            title=f"Restore '{backup.name}' on {instance.name}",
            instance=instance,
            user=request.user,
            params={"backup_id": str(backup.id), "pre_restore_backup": pre},
        )
        audit.record("backup.restore", request=request, instance=instance, target=backup, details={"pre_restore_backup": pre})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class BackupRestoreNewView(APIView):
    @extend_schema(request=dict, responses={202: dict})
    def post(self, request: Request, pk: str, bid: str) -> Response:
        source, backup = BackupDetailView()._get(request, pk, bid, "instance.restore")
        require(request.user, "instance.create")
        if backup.status != BackupStatus.SUCCEEDED or not backup.snapshot_id:
            raise ApiError("Only completed backups can be restored", code="not_restorable", status_code=409)
        name = str(request.data.get("name") or f"{source.name} (restored)")[:80]
        port = request.data.get("game_port")
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
        with transaction.atomic():
            if port:
                services.check_port_available(source.node, int(port))
            else:
                port = services.allocate_port(source.node)
            target = Instance.objects.create(
                name=name,
                game_port=int(port),
                source=Source.RESTORED,
                created_by=request.user,
                **{f: getattr(source, f) for f in copy_fields},
            )
        job = enqueue(
            "restore_new",
            title=f"Restore '{backup.name}' as {target.name}",
            instance=source,
            user=request.user,
            params={"backup_id": str(backup.id), "target_id": str(target.id)},
        )
        Instance.objects.filter(pk=target.pk).update(active_job=job)
        audit.record("backup.restore_as_new", request=request, instance=source, target=target, details={"backup": str(backup.id)})
        target.refresh_from_db()
        return Response(
            {"instance": InstanceSerializer(target, context={"request": request}).data, "job": JobSerializer(job).data},
            status=status.HTTP_202_ACCEPTED,
        )


class BackupVerifyView(APIView):
    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.backup")
        subset = str(request.data.get("read_data_subset") or "10%")
        import re

        if not re.match(r"^(100|[1-9]?\d)(\.\d+)?%$", subset):
            raise ApiError("read_data_subset must be a percentage like '10%'", code="invalid_subset")
        job = enqueue(
            "backup_verify",
            title=f"Verify backups of {instance.name}",
            instance=instance,
            user=request.user,
            params={"read_data_subset": subset},
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class BackupBrowseView(APIView):
    """List files inside a backup snapshot."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str, bid: str) -> Response:
        from apps.nodes.client import AgentClient

        instance, backup = BackupDetailView()._get(request, pk, bid, "instance.backup")
        if not backup.snapshot_id:
            raise ApiError("This backup has no snapshot", code="not_restorable", status_code=409)
        require(request.user, "instance.files.read", instance)
        return Response(
            AgentClient(instance.node).backup_files(instance.id, backup.snapshot_id, request.query_params.get("path", ""))
        )


class BackupFileDownloadView(APIView):
    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY})
    def get(self, request: Request, pk: str, bid: str):  # type: ignore[no-untyped-def]
        from django.http import StreamingHttpResponse

        from apps.nodes.client import AgentClient

        instance, backup = BackupDetailView()._get(request, pk, bid, "instance.backup")
        require(request.user, "instance.files.read", instance)
        path = request.query_params.get("path", "")
        chunks, _resp = AgentClient(instance.node).backup_download(instance.id, backup.snapshot_id, path)
        filename = (path.rsplit("/", 1)[-1] or "file").replace('"', "").replace("\\", "")
        response = StreamingHttpResponse(chunks, content_type="application/octet-stream")
        from apps.core.utils import content_disposition

        response["Content-Disposition"] = content_disposition(filename)
        response["X-Content-Type-Options"] = "nosniff"
        audit.record("backup.file_download", request=request, instance=instance, target=backup, details={"path": path})
        return response


class BackupRestorePathView(APIView):
    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str, bid: str) -> Response:
        instance, backup = BackupDetailView()._get(request, pk, bid, "instance.restore")
        path = str(request.data.get("path") or "").strip("/")
        if not path:
            raise ApiError("Choose a file or folder to restore", code="invalid_path")
        if request.data.get("confirm") is not True:
            raise ApiError(f"Restoring replaces the current '{path}'. Confirm to continue.", code="confirmation_required")
        job = enqueue(
            "restore_path",
            title=f"Restore {path} from '{backup.name}'",
            instance=instance,
            user=request.user,
            params={"backup_id": str(backup.id), "path": path},
        )
        audit.record("backup.restore_path", request=request, instance=instance, target=backup, details={"path": path})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


SNAPSHOT_NAME = r"^\d{8}T\d{6}Z(-[a-z0-9-]{1,40})?$"


class SnapshotListView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from apps.nodes.client import AgentClient

        instance = get_instance_for(request.user, pk, "instance.backup")
        return Response(AgentClient(instance.node).snapshots(instance.id))

    @extend_schema(operation_id="snapshots_create", request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.backup")
        label = str(request.data.get("label") or "")[:120]
        consistency = request.data.get("consistency", "auto")
        if consistency not in ("auto", "require", "none"):
            raise ApiError("consistency must be auto, require or none", code="invalid_consistency")
        job = enqueue(
            "snapshot_create",
            title=f"Snapshot {instance.name}",
            instance=instance,
            user=request.user,
            params={"label": label, "consistency": consistency, "kind": "manual"},
        )
        audit.record("snapshot.create", request=request, instance=instance, target=instance, details={"label": label})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class SnapshotConvertView(APIView):
    @extend_schema(request=None, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.restore")
        job = enqueue("snapshot_convert", title=f"Enable snapshots for {instance.name}", instance=instance, user=request.user)
        audit.record("snapshot.convert", request=request, instance=instance, target=instance)
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class SnapshotDetailView(APIView):
    def _check(self, name: str) -> str:
        import re

        if not re.match(SNAPSHOT_NAME, name):
            raise ApiError("Invalid snapshot name", code="invalid_snapshot")
        return name

    @extend_schema(responses={202: JobSerializer})
    def delete(self, request: Request, pk: str, name: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.restore")
        job = enqueue(
            "snapshot_delete",
            title=f"Delete snapshot {self._check(name)}",
            instance=instance,
            user=request.user,
            params={"name": name},
        )
        audit.record("snapshot.delete", request=request, instance=instance, target=instance, details={"name": name})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(operation_id="snapshots_rollback", request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str, name: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.restore")
        if request.data.get("confirm") is not True:
            raise ApiError(
                "Rolling back replaces the current server files with the snapshot. Confirm to continue.",
                code="confirmation_required",
            )
        job = enqueue(
            "snapshot_restore",
            title=f"Roll back to snapshot {self._check(name)}",
            instance=instance,
            user=request.user,
            params={"name": name},
        )
        audit.record("snapshot.restore", request=request, instance=instance, target=instance, details={"name": name})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)
