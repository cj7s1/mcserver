from __future__ import annotations

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps import games
from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import get_instance_for, require
from apps.instances import services
from apps.instances.models import Instance, Source
from apps.instances.serializers import InstanceSerializer
from apps.jobs.runner import enqueue
from apps.jobs.serializers import JobSerializer
from apps.nodes.client import AgentClient

from .handlers import backup_agent, policy_for, repository_node_id, require_local_backup, require_same_game
from .models import Backup, BackupStatus, RetentionPolicy


class BackupSerializer(serializers.ModelSerializer):
    """A backup row. ``node_name``/``on_current_node`` say which node's restic repository holds it.

    Every node keeps its own repository and a migration does not copy it, so after a move the
    older backups stay on the previous node: they can be browsed, downloaded, deleted and restored
    as a new server (created on that node) but not restored in place. Pass ``instance`` and
    ``node_names`` ({id: name}) in the context to avoid a query per row.
    """

    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default=None)
    node_id = serializers.SerializerMethodField()
    node_name = serializers.SerializerMethodField()
    on_current_node = serializers.SerializerMethodField()

    def _instance(self, obj: Backup) -> Instance:
        return self.context.get("instance") or obj.instance

    def _node_names(self) -> dict[int, str]:
        names = self.context.get("node_names")
        if names is None:
            from apps.nodes.models import Node

            names = self.context["node_names"] = dict(Node.objects.values_list("pk", "name"))
        return names

    def get_node_id(self, obj: Backup) -> int | None:
        """The node holding the backup; None when that node no longer exists (backup unreadable)."""
        node_id = repository_node_id(obj, self._instance(obj))
        return node_id if node_id in self._node_names() else None

    def get_node_name(self, obj: Backup) -> str:
        node_id = repository_node_id(obj, self._instance(obj))
        names = self._node_names()
        if node_id is not None and node_id in names:
            return names[node_id]
        meta = obj.meta or {}  # a deleted node: the name recorded with the backup
        return str(meta.get("node_name") or meta.get("left_on_node") or "")

    def get_on_current_node(self, obj: Backup) -> bool:
        return repository_node_id(obj, self._instance(obj)) == self._instance(obj).node_id

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
            "node_id",
            "node_name",
            "on_current_node",
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
        from apps.nodes.models import Node

        context = {"instance": instance, "node_names": dict(Node.objects.values_list("pk", "name"))}
        return Response(
            {
                "backups": BackupSerializer(qs, many=True, context=context).data,
                "policy": PolicySerializer(policy_for(instance)).data,
            }
        )

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
        ser = BackupSerializer(backup, data=request.data, partial=True, context={"instance": instance})
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
        require_local_backup(backup, instance)  # 409 backup_on_other_node now, not when the job runs
        require_same_game(backup, instance)  # 409 backup_game_mismatch
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
        # restic restores on the node whose repository holds the snapshot (after a migration that
        # can be the server's previous node), so the new server is created on that node
        repo_node = backup_agent(backup, source).node  # 409 backup_node_missing if it is gone
        require_same_game(backup, source)  # the new server is of the source's game (409 backup_game_mismatch)
        games.require_agent_support(source.game, AgentClient(repo_node))  # 409 agent_lacks_game
        name = str(request.data.get("name") or f"{source.name} (restored)")[:80]
        port = request.data.get("game_port")
        copy_fields = [
            "description",
            "icon_url",
            "group",
            "game",
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
        try:
            port = int(port) if port else None
        except (TypeError, ValueError):
            raise ApiError("game_port must be a port number", code="invalid_port") from None
        # One transaction: when the source is busy (409 instance_busy) the new server record is
        # rolled back with the failed enqueue instead of being left behind as an orphan.
        with transaction.atomic():
            if port:
                services.check_port_available(repo_node, port)
            else:
                port = services.allocate_port(repo_node)
            target = Instance.objects.create(
                name=name,
                node=repo_node,
                game_port=port,
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
        audit.record(
            "backup.restore_as_new",
            request=request,
            instance=source,
            target=target,
            details={"backup": str(backup.id), "node": repo_node.name},
        )
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
        instance, backup = BackupDetailView()._get(request, pk, bid, "instance.backup")
        if not backup.snapshot_id:
            raise ApiError("This backup has no snapshot", code="not_restorable", status_code=409)
        require(request.user, "instance.files.read", instance)
        # the repository may be on the server's previous node (migration): ask that node's agent
        client = backup_agent(backup, instance)
        return Response(client.backup_files(instance.id, backup.snapshot_id, request.query_params.get("path", "")))


class BackupFileDownloadView(APIView):
    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY})
    def get(self, request: Request, pk: str, bid: str):  # type: ignore[no-untyped-def]
        from apps.core.streaming import AgentStreamingResponse

        instance, backup = BackupDetailView()._get(request, pk, bid, "instance.backup")
        if not backup.snapshot_id:
            raise ApiError("This backup has no snapshot", code="not_restorable", status_code=409)
        require(request.user, "instance.files.read", instance)
        path = request.query_params.get("path", "")
        chunks, upstream = backup_agent(backup, instance).backup_download(instance.id, backup.snapshot_id, path)
        filename = (path.rsplit("/", 1)[-1] or "file").replace('"', "").replace("\\", "")
        # streamed chunk by chunk under ASGI; the agent's response is closed when the browser goes away
        response = AgentStreamingResponse(chunks, upstream, content_type="application/octet-stream")
        from apps.core.utils import content_disposition

        response["Content-Disposition"] = content_disposition(filename)
        response["X-Content-Type-Options"] = "nosniff"
        audit.record("backup.file_download", request=request, instance=instance, target=backup, details={"path": path})
        return response


class BackupRestorePathView(APIView):
    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str, bid: str) -> Response:
        instance, backup = BackupDetailView()._get(request, pk, bid, "instance.restore")
        if backup.status != BackupStatus.SUCCEEDED or not backup.snapshot_id:
            raise ApiError("Only completed backups can be restored", code="not_restorable", status_code=409)
        require_local_backup(backup, instance)  # 409 backup_on_other_node now, not when the job runs
        require_same_game(backup, instance)  # 409 backup_game_mismatch
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
