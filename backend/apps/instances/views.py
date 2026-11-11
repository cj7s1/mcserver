from __future__ import annotations

import logging
import mimetypes
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import StreamingHttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FileUploadParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps import games
from apps.accounts.models import InstanceAccess, Role
from apps.accounts.permissions import INSTANCE_CODES
from apps.audit import service as audit
from apps.catalog.providers import resolve_java
from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.core.permissions import get_instance_for, require, visible_instances
from apps.core.streaming import AgentStreamingResponse, read_bytes
from apps.core.utils import content_disposition
from apps.jobs.models import ACTIVE_STATUSES, Job
from apps.jobs.runner import enqueue
from apps.jobs.serializers import JobSerializer
from apps.nodes.client import FILE_OPERATION_TIMEOUT, AgentClient, AgentTimeout, AgentUnavailable
from apps.nodes.models import Node

from . import capacity, power_warnings, properties, services
from .models import Instance, Schedule, Source
from .serializers import CreateInstanceSerializer, InstanceSerializer, ScheduleSerializer
from .spec import image_for
from .world import RESET_KEEP, SEED_MODES


def create_other_game(request: Request, data: dict[str, Any], node: Node, definition: Any) -> Response:
    from .game_views import create_other_game as create

    return create(request, data, node, definition)


TEXT_EDIT_LIMIT = 5 * 1024 * 1024

log = logging.getLogger(__name__)


def _ser(instance: Instance, request: Request) -> dict[str, Any]:
    return InstanceSerializer(instance, context={"request": request}).data


class InstanceListView(APIView):
    @extend_schema(responses={200: InstanceSerializer(many=True)})
    def get(self, request: Request) -> Response:
        power_jobs = Job.objects.filter(kind__in=("restart", "stop"), status__in=ACTIVE_STATUSES)
        prefetch = [Prefetch("jobs", queryset=power_jobs, to_attr="active_power_jobs")]
        if not request.user.is_owner:
            # the caller's own grants, so the per-row permission list costs no query
            grants = InstanceAccess.objects.filter(user=request.user).select_related("role")
            prefetch.append(Prefetch("access_grants", queryset=grants, to_attr="viewer_grants"))
        qs = visible_instances(
            request.user,
            Instance.objects.select_related("node", "active_job", "modpack_update_check")
            .prefetch_related(*prefetch)
            # last_backup_at / backup_schedules of the serializer, as subqueries of the list query
            .annotate(**services.backup_overview_annotations()),
        )
        archived = request.query_params.get("archived")
        if archived == "true":
            qs = qs.filter(archived_at__isnull=False)
        elif archived != "all":
            qs = qs.filter(archived_at__isnull=True)
        return Response(InstanceSerializer(qs, many=True, context={"request": request}).data)

    @extend_schema(operation_id="instances_create", request=CreateInstanceSerializer, responses={201: dict})
    def post(self, request: Request) -> Response:
        require(request.user, "instance.create")
        ser = CreateInstanceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        node = Node.objects.filter(pk=data["node"]).first() if data.get("node") else Node.default()
        if node is None:
            raise ValidationError({"node": ["Unknown node"]})
        game = data.get("game") or games.DEFAULT_GAME
        if game != games.DEFAULT_GAME:
            # the node's agent must have the game's runtime profile (409 agent_lacks_game)
            games.require_agent_support(game, AgentClient(node), data.get("variant") or None)
            return create_other_game(request, data, node, games.get(game))
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
        # a server that starts right away must fit into the node's memory (409 insufficient_memory before
        # anything is created; the client asks "Start anyway?" and sends allow_overcommit)
        overcommit = capacity.admit_request(
            Instance(**fields), starts=bool(data.get("start", True)), allow_overcommit=data.get("allow_overcommit")
        )

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
                # The wizard's "Nightly backup" option: same rule as creating the schedule by hand
                # (checked after the grant above, which is what gives most creators the rights).
                # Without them the server is still created, just without the schedule.
                backup_schedule = None
                if data.get("nightly_backup") and may_create_schedule(request.user, instance, "backup"):
                    backup_schedule = services.add_nightly_backup_schedule(instance)
        except IntegrityError as exc:
            raise ConflictError("That port was just taken by another server; choose another", code="port_conflict") from exc

        props = {"motd": data["name"][:59]}
        props.update(data.get("server_properties") or {})
        job = enqueue(
            "install",
            title=f"Install {instance.name}",
            instance=instance,
            user=request.user,
            params={"server_properties": props, "start": data.get("start", True), **overcommit},
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
        schedule_data = ScheduleSerializer(backup_schedule).data if backup_schedule is not None else None
        if schedule_data is not None:
            audit.record(
                "schedule.created",
                request=request,
                instance=instance,
                target=backup_schedule,
                details={**schedule_data, "via": "server_created"},
            )
        instance.refresh_from_db()
        return Response(
            {
                "instance": _ser(instance, request),
                "job": JobSerializer(job).data,
                "warnings": warnings,
                "java": java_info,
                # the nightly backup schedule made with the server; null when not asked for or not permitted
                "backup_schedule": schedule_data,
            },
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
        "ports",
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
        version_fields = sorted({"minecraft_version", "loader_version"} & changed.keys())
        if version_fields and (instance.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
            raise ValidationError(
                {field: ["Change the version through the software update (POST .../software)"] for field in version_fields}
            )
        if version_fields:
            if instance.is_modpack:
                message = "Modpack servers take their versions from the pinned modpack release"
            else:
                # The software update validates the version against the catalog, asks before a
                # downgrade and takes a pre-update backup; a plain field edit would skip all that.
                message = "Change the Minecraft or loader version through the software update (POST .../software)"
            raise ValidationError({field: [message] for field in version_fields})
        if "ports" in ser.validated_data and (instance.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
            definition = games.for_instance(instance)
            requested = {
                str(p["name"]): {"host_port": p.get("host_port"), "host_ip": p.get("host_ip") or ""}
                for p in ser.validated_data["ports"]
            }
            ports = services.assign_named_ports(definition, instance.node, requested, exclude=instance, current=instance.ports)
            changed.pop("ports", None)
            if ports != instance.ports:
                changed["ports"] = ports
                changed["game_port"] = int(ports[0]["host_port"]) if ports else instance.game_port
                if changed["game_port"] == instance.game_port:
                    changed.pop("game_port")
        elif "game_port" in changed:
            services.check_port_available(instance.node, changed["game_port"], exclude=instance)
        for extra in changed.get("extra_ports", []):
            services.check_port_available(instance.node, extra["host_port"], exclude=instance)
        if "java_version" in changed:
            changed["image"] = image_for(changed["java_version"])
        if changed:
            for field, value in changed.items():
                setattr(instance, field, value)
            try:
                # only the edited columns: a full-row save would write back the stale job lock,
                # status and whatever a running job changed since this request loaded the row
                instance.save(update_fields=[*changed, "updated_at"])
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
    warn_minutes = serializers.ChoiceField(
        choices=list(power_warnings.WARN_MINUTES_CHOICES),
        default=0,
        help_text="stop/restart: warn online players in game this many minutes before (0 = no warning)",
    )
    warn_message = serializers.CharField(
        max_length=power_warnings.MESSAGE_MAX, required=False, allow_blank=True, default="", trim_whitespace=True
    )
    allow_overcommit = serializers.BooleanField(
        default=False,
        help_text="start/restart: start even when the server does not fit into the node's memory next to the "
        "servers running there (otherwise 409 insufficient_memory, with the numbers in details)",
    )

    def validate_warn_message(self, value: str) -> str:
        try:
            return power_warnings.validate_message(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs.get("warn_minutes") and attrs["action"] not in power_warnings.WARNABLE_ACTIONS:
            raise serializers.ValidationError({"warn_minutes": ["Player warnings apply to stop and restart only"]})
        return attrs


class InstancePowerView(APIView):
    @extend_schema(request=PowerSerializer, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.control")
        ser = PowerSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        result = services.power(
            instance,
            data["action"],
            request.user,
            request,
            warn_minutes=int(data.get("warn_minutes") or 0),
            warn_message=data.get("warn_message") or "",
            allow_overcommit=bool(data.get("allow_overcommit")),
        )
        if isinstance(result, dict):
            return Response({"status": result})
        return Response(JobSerializer(result).data, status=status.HTTP_202_ACCEPTED)


class InstancePowerCancelView(APIView):
    """Cancel a pending restart/stop countdown (players are told in game)."""

    @extend_schema(request=None, responses={200: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.control")
        pending = power_warnings.pending_countdown(instance)
        if pending is None:
            raise ConflictError("No restart or stop is counting down on this server", code="no_countdown")
        job = power_warnings.cancel_countdown(pending.id, message=f"Cancelled by {request.user.username}")
        audit.record(
            "instance.power_cancelled",
            request=request,
            instance=instance,
            target=instance,
            details={"job": str(job.id), "action": job.kind},
        )
        return Response(JobSerializer(job).data)


class InstanceActionView(APIView):
    """recreate | archive | unarchive"""

    ACTIONS = {
        "recreate": ("instance.configure", "Recreate container"),
        "archive": ("instance.delete", "Archive server"),
        "unarchive": ("instance.delete", "Unarchive server"),
    }

    @extend_schema(operation_id="instances_action", request=None, responses={202: JobSerializer})
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
        if action == "recreate" and instance.archived_at:
            raise ConflictError("Unarchive this server before recreating its container", code="archived")
        params: dict[str, Any] = {}
        if action == "recreate":
            # a running server is started again from the new container: the same memory check as a start
            data = request.data if isinstance(request.data, dict) else {}
            params = capacity.admit_request(
                instance, starts=capacity.runs_now(instance), allow_overcommit=data.get("allow_overcommit")
            )
        job = enqueue(action, title=f"{title}: {instance.name}", instance=instance, user=request.user, params=params or None)
        audit.record(f"instance.{action}", request=request, instance=instance, target=instance, details=params or None)
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
        fields = {f: getattr(source, f) for f in copy_fields}
        games.require_agent_support(source.game, AgentClient(source.node))  # 409 agent_lacks_game
        port = ser.validated_data.get("game_port")
        if (source.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
            fields["game_options"] = dict(source.game_options or {})
            requested = {source.ports[0]["name"]: port} if port and source.ports else None
            fields["ports"] = services.assign_named_ports(games.for_instance(source), source.node, requested, host_ip="")
            port = int(fields["ports"][0]["host_port"]) if fields["ports"] else 0
        # One transaction: if the source is busy (409 instance_busy) the new record is rolled
        # back instead of being left behind as an empty orphan server.
        with transaction.atomic():
            if port and "ports" in fields:
                pass  # named ports: checked per protocol above
            elif port:
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
            stats["players"] = _polled_players(instance, client)
        return Response(stats)


POLLED_PLAYERS_MAX_AGE = 20  # seconds; the dashboard and the overview ask every 5 s


def _polled_players(instance: Instance, client: AgentClient) -> dict[str, Any] | None:
    """Players online for pages that poll: a status ping when the agent can (it leaves nothing in
    the server's log), RCON ``list`` only for agents without the ``ping`` feature; cached briefly.
    None when unknown (a server that is still starting does not answer the ping)."""
    from django.core.cache import cache

    from apps.metrics.tasks import player_count

    key = f"players:polled:{instance.pk}"
    hit = cache.get(key)
    if hit is not None:
        return hit or None
    players = player_count(instance, client)
    cache.set(key, players or {}, POLLED_PLAYERS_MAX_AGE)
    return players


class InstanceLogsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.console")
        try:
            tail = max(1, min(int(request.query_params.get("tail", "500")), 20000))
        except ValueError:
            tail = 500
        try:
            lines = AgentClient(instance.node).logs(instance.id, tail=tail)
        except ApiError as exc:
            # A server that never ran has neither a container nor a record of a last run: its
            # console history is empty, not an error.
            if exc.error_code != "no_container":
                raise
            lines = []
        return Response({"lines": lines})


class CommandSerializer(serializers.Serializer):
    command = serializers.CharField(max_length=1400, trim_whitespace=True)


class InstanceCommandView(APIView):
    @extend_schema(request=CommandSerializer, responses={200: dict})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.command")
        definition = games.for_instance(instance)
        definition.require(instance, "console_commands")
        ser = CommandSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        command = definition.normalise_command(ser.validated_data["command"])
        if "\n" in command or "\r" in command:
            raise ValidationError({"command": ["Commands must be a single line"]})
        if definition.refuse_stop_commands and definition.is_stop_command(command):
            raise ApiError(
                f"'{command}' would make the server exit, and Docker would start it again at once; use Stop instead",
                code="stop_via_power",
            )
        output = AgentClient(instance.node).command(instance.id, command)
        audit.record("instance.command", request=request, instance=instance, target=instance, details={"command": command[:300]})
        return Response({"command": command, "output": output})


# server.properties keys whose values are credentials. They are never returned by the
# properties API (anyone with instance.view reads it); the file manager (instance.files.read)
# still shows the file as it is.
SECRET_PROPERTIES = ("rcon.password", "management-server-secret")
REDACTED_VALUE = "********"


def redact_properties(text: str) -> str:
    """``text`` with the values of SECRET_PROPERTIES replaced by REDACTED_VALUE."""
    present = properties.parse(text)
    secrets = {key: REDACTED_VALUE for key in SECRET_PROPERTIES if present.get(key)}
    return properties.update(text, secrets) if secrets else text


def _restore_secrets(new_text: str, current: dict[str, str]) -> str:
    """Put back the real value of every secret the client sent back as REDACTED_VALUE."""
    sent = properties.parse(new_text)
    keep = {key: current.get(key) for key in SECRET_PROPERTIES if sent.get(key) == REDACTED_VALUE}
    return properties.update(new_text, keep) if keep else new_text


def _properties_file(instance: Instance) -> str:
    """The server.properties file of the game (409 unsupported_by_game without the editor)."""
    definition = games.for_instance(instance)
    definition.require(instance, "properties")
    return definition.config_files()[0].path


class PropertiesView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.view")
        path = _properties_file(instance)
        client = AgentClient(instance.node)
        raw = properties.read_file(client, instance.id, path)
        exists = raw is not None
        raw = redact_properties(raw or "")
        values = properties.parse(raw)
        return Response(
            {
                "exists": exists,
                "values": values,
                "raw": raw,
                "schema": properties.SCHEMA,
                "managed_keys": sorted(properties.MANAGED_KEYS),
                "redacted_keys": [k for k in SECRET_PROPERTIES if values.get(k) == REDACTED_VALUE],
            }
        )

    @extend_schema(request=dict, responses={200: dict})
    def patch(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.configure")
        path = _properties_file(instance)
        changes = request.data.get("values") if isinstance(request.data, dict) else None
        raw_text = request.data.get("raw") if isinstance(request.data, dict) else None
        client = AgentClient(instance.node)
        current = properties.read_file(client, instance.id, path) or ""
        before = properties.parse(current)
        if raw_text is not None:
            if not isinstance(raw_text, str) or len(raw_text) > 512 * 1024:
                raise ValidationError({"raw": ["Invalid properties text"]})
            # the GET returned secrets masked; a masked value sent back means "unchanged"
            raw_text = _restore_secrets(raw_text, before)
            after = properties.parse(raw_text)
            for key in properties.MANAGED_KEYS:
                if after.get(key) != before.get(key) and key in after:
                    raise ValidationError({"raw": [f"'{key}' is managed by MCPanel"]})
            # ASCII with \uXXXX escapes: read alike by servers before 1.20 (ISO-8859-1) and after (UTF-8)
            new_text = properties.ascii_text(raw_text if raw_text.endswith("\n") else raw_text + "\n")
            changed_keys = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        else:
            if not isinstance(changes, dict):
                raise ValidationError({"values": ["Provide an object of property changes"]})
            # a masked secret sent back unchanged is not a change
            changes = {k: v for k, v in changes.items() if not (k in SECRET_PROPERTIES and v == REDACTED_VALUE)}
            try:
                validated = properties.validate_changes(changes)
            except ValueError as exc:
                raise ValidationError({"values": [str(exc)]}) from exc
            new_text = properties.update(current, validated)
            changed_keys = sorted(validated)
        client.write_text(instance.id, path, new_text)
        audit.record(
            "instance.properties_updated",
            request=request,
            instance=instance,
            target=instance,
            details={"keys": changed_keys[:50]},
        )
        restart = any(properties.SCHEMA_KEYS.get(k, {}).get("restart", True) for k in changed_keys)
        running = instance.status in ("running", "starting", "unhealthy")
        shown = redact_properties(new_text)
        shown_values = properties.parse(shown)
        return Response(
            {
                "values": shown_values,
                "raw": shown,
                "redacted_keys": [k for k in SECRET_PROPERTIES if shown_values.get(k) == REDACTED_VALUE],
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


def _not_utf8(client: AgentClient, instance: Instance, path: str, answer: dict[str, Any]) -> bool:
    """Whether the text of a read is a lossy decoding of the file (saving it back would replace the
    characters that could not be read for good).

    An agent that decodes strictly says so itself: it returns the ``encoding`` it read (the text is
    then exact) and refuses other files with 415 ``not_utf8``. An older agent decodes bytes that are
    not UTF-8 as U+FFFD without telling; for it the bytes are checked."""
    if answer.get("binary_or_non_utf8") is not None:
        return bool(answer["binary_or_non_utf8"])
    if answer.get("encoding"):
        return False
    if "\ufffd" not in str(answer.get("content") or ""):
        return False
    # U+FFFD can also be a real character of a UTF-8 file: look at the bytes
    try:
        return properties.decode_text(read_bytes(client, instance.id, path, TEXT_EDIT_LIMIT + 1))[1] != "utf-8"
    except ApiError:
        return True


def _non_utf8_preview(client: AgentClient, instance: Instance, path: str) -> dict[str, Any]:
    """The answer for a file the agent refused with 415 ``not_utf8``: the editor's usual shape with
    ``binary_or_non_utf8: true`` and a lossy, read-only preview of the text (``encoding`` null)."""
    answer: dict[str, Any] = {"path": path, "content": "", "encoding": None, "size": 0, "modified": 0}
    try:
        entry = client.stat_file(instance.id, path)
        answer.update(path=entry.get("path", path), size=entry.get("size", 0), modified=entry.get("modified", 0))
        answer["content"] = read_bytes(client, instance.id, path, TEXT_EDIT_LIMIT).decode("utf-8", errors="replace")
    except ApiError as exc:
        log.info("no preview of the non-UTF-8 file %s of %s: %s", path, instance.pk, exc)
    return {**answer, "binary_or_non_utf8": True}


def _is_properties(path: str) -> bool:
    return path.lower().endswith(".properties")


def _exact_properties(client: AgentClient, instance: Instance, path: str, answer: dict[str, Any]) -> dict[str, Any]:
    """An older agent (no ``encoding`` in its answer) decodes a ``*.properties`` file that is not
    UTF-8 lossily: such a file is ISO-8859-1 (what servers before 1.20 write), so its bytes are
    read and decoded as that, as the Properties tab does (``properties.read_file``). The answer is
    then marked ``legacy_latin1`` (the agent cannot be asked to write ISO-8859-1)."""
    if answer.get("encoding") or not _is_properties(path) or "\ufffd" not in str(answer.get("content") or ""):
        return answer
    try:
        text, encoding = properties.decode_text(read_bytes(client, instance.id, path, TEXT_EDIT_LIMIT + 1))
    except ApiError:
        return answer
    if encoding != "iso-8859-1":
        return answer  # U+FFFD is a real character of this UTF-8 file
    return {**answer, "content": text, "legacy_latin1": True}


def _properties_text(path: str, content: str, current: dict[str, Any] | None) -> tuple[str | None, str]:
    """(encoding to ask the agent for, text) of a ``*.properties`` file saved from the editor,
    written as the Properties tab writes server.properties:

    * a file the agent keeps in ISO-8859-1: ISO-8859-1 again, what it cannot store as ``\\uXXXX``;
    * server.properties otherwise (UTF-8, a new file) and an ISO-8859-1 file on an older agent
      (it writes only UTF-8): pure ASCII, everything above U+007E as ``\\uXXXX``, which every
      Minecraft version reads alike (before 1.20 as ISO-8859-1, since then as UTF-8).
    Other files keep what the user typed."""
    if not _is_properties(path):
        return None, content
    if current is not None and current.get("encoding") == "iso-8859-1":
        return "iso-8859-1", properties.ascii_text(content, above=0xFF)
    if path.strip("/").lower() == "server.properties" or (current is not None and current.get("legacy_latin1")):
        return None, properties.ascii_text(content)
    return None, content


def _is_control_secret_file(instance: Instance, path: str) -> bool:
    """Whether ``path`` is the file the game reads its control secret from (Factorio: config/rconpw)."""
    definition = games.get_or_none(instance.game)
    secret_file = definition.control_secret_file if definition is not None else None
    if not secret_file:
        return False
    return "/".join(p for p in str(path).replace("\\", "/").split("/") if p and p != ".") == secret_file


class FilesTextView(APIView):
    """Read and write a text file. ``GET`` answers ``binary_or_non_utf8: true`` for a file that is
    not valid UTF-8 (its ``content`` is then a read-only preview with replacement characters);
    ``PUT`` refuses to overwrite such a file (409 ``non_utf8_file``) unless the body has
    ``confirm_non_utf8: true``. ``*.properties`` files are read and written as the Properties tab
    does (``_exact_properties``, ``_properties_text``)."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.read")
        client = AgentClient(instance.node)
        path = request.query_params.get("path", "")
        if _is_control_secret_file(instance, path):
            # the control secret (Factorio: config/rconpw) is not shown in the editor
            return Response({"path": path, "content": REDACTED_VALUE + "\n", "redacted": True, "binary_or_non_utf8": False})
        try:
            answer = _exact_properties(client, instance, path, client.read_text(instance.id, path))
        except ApiError as exc:
            if exc.error_code != "not_utf8":
                raise
            return Response(_non_utf8_preview(client, instance, path))
        latin1 = bool(answer.pop("legacy_latin1", False))
        not_utf8 = False if latin1 else _not_utf8(client, instance, path, answer)
        definition = games.get_or_none(instance.game)
        if definition is not None and isinstance(answer.get("content"), str):
            # a secret the game keeps inside one of its files (TShock's REST token in config.json)
            answer["content"] = definition.masked_text(instance, path, answer["content"])
        return Response({**answer, **({"encoding": "iso-8859-1"} if latin1 else {}), "binary_or_non_utf8": not_utf8})

    @extend_schema(request=dict, responses={200: dict})
    def put(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.files.write")
        data = request.data if isinstance(request.data, dict) else {}
        path = str(data.get("path", ""))
        content = data.get("content")
        if not isinstance(content, str) or len(content.encode()) > TEXT_EDIT_LIMIT:
            raise ValidationError({"content": ["Text content is required (max 5 MB)"]})
        if _is_control_secret_file(instance, path):
            raise ApiError(
                "This file holds the server's control password; the panel writes it before every start",
                code="managed_file",
                status_code=403,
            )
        client = AgentClient(instance.node)
        create_only = bool(data.get("create_only"))
        current: dict[str, Any] | None = None
        if not create_only:
            try:
                current = _exact_properties(client, instance, path, client.read_text(instance.id, path))
            except ApiError as exc:
                if exc.error_code == "not_utf8":
                    current = {"binary_or_non_utf8": True}
                elif exc.error_code in ("not_found", "binary_file", "too_large"):
                    current = None
                else:
                    raise
        lossy = current is not None and not current.get("legacy_latin1") and _not_utf8(client, instance, path, current)
        if lossy and data.get("confirm_non_utf8") is not True:
            raise ConflictError(
                "This file is not valid UTF-8 text (it may use another encoding such as Latin-1). Saving it from "
                "the editor replaces the characters that could not be read. Download it and edit it with an "
                "editor that keeps its encoding, or confirm to overwrite it anyway.",
                code="non_utf8_file",
            )
        encoding, content = _properties_text(path, content, current)
        result = client.write_text(instance.id, path, content, create_only, encoding=encoding)
        audit.record("files.write", request=request, instance=instance, target=instance, details={"path": path})
        return Response(result)


class FilesDownloadView(APIView):
    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY})
    def get(self, request: Request, pk: str) -> StreamingHttpResponse:
        instance = get_instance_for(request.user, pk, "instance.files.read")
        path = request.query_params.get("path", "")
        chunks, length, upstream = AgentClient(instance.node).download(instance.id, path)
        filename = path.rsplit("/", 1)[-1] or "download"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = AgentStreamingResponse(chunks, upstream, content_type=content_type)
        response["Content-Disposition"] = content_disposition(filename)
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
        targets = []
        for f in files:
            name = f.name.replace("\\", "/").rsplit("/", 1)[-1]
            if not name or name in (".", ".."):
                raise ValidationError({"file": ["Invalid file name"]})
            targets.append((f, name, f"{directory}/{name}" if directory else name))
        client = AgentClient(instance.node)
        results: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        first_error: ApiError | None = None
        for f, name, target in targets:
            try:
                results.append(client.upload(instance.id, target, f.chunks(256 * 1024), overwrite=overwrite))
            except AgentUnavailable:
                raise  # the node is gone: the files after this one would fail the same way
            except ApiError as exc:
                # one file that cannot be stored (exists already, bad name on the node) does not
                # stop the others; each is reported
                first_error = first_error or exc
                failed.append({"name": name, "path": target, "code": exc.error_code, "message": str(exc.detail)})
        if not results and first_error is not None:
            raise first_error
        audit.record(
            "files.upload",
            request=request,
            instance=instance,
            target=instance,
            details={
                "paths": [r["path"] for r in results][:50],
                "count": len(results),
                "failed": [f["path"] for f in failed][:50],
            },
        )
        return Response({"uploaded": results, "failed": failed}, status=status.HTTP_201_CREATED)


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
        elif op in ("rename", "delete"):
            return self._long_operation(request, instance, client, op, data)
        elif op == "search":
            query = str(data.get("query", ""))
            if len(query) < 2:
                raise ValidationError({"query": ["Enter at least 2 characters"]})
            return Response({"results": client.search_files(instance.id, query, str(data.get("path", "")))})
        else:
            raise ApiError("Unknown file operation", code="invalid_action", status_code=404)
        audit.record(f"files.{op}", request=request, instance=instance, target=instance, details=data)
        return Response(result)

    @staticmethod
    def _long_operation(request: Request, instance: Instance, client: AgentClient, op: str, data: dict[str, Any]) -> Response:
        """rename / delete: a big folder can take minutes on the node, which works inside the request.

        The panel waits FILE_OPERATION_TIMEOUT. If the answer does not come in time the node is
        most likely still at it: 202 ``{"still_running": true, "message"}`` (the client refreshes the
        listing), never an error, and the audit entry says so.
        """
        # only one jar deleted or renamed directly in mods/ (or plugins/) counts as a pack file the
        # user removed on purpose; folders and config files are never recorded (packfiles.recordable)
        if op == "rename":
            source, destination = str(data.get("source", "")), str(data.get("destination", ""))
            removed = [source]
            details: dict[str, Any] = {"source": source, "destination": destination, "overwrite": bool(data.get("overwrite"))}

            def run() -> dict[str, Any]:
                return client.rename(
                    instance.id, source, destination, bool(data.get("overwrite")), timeout=FILE_OPERATION_TIMEOUT
                )
        else:
            paths = data.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ValidationError({"paths": ["Provide a list of paths"]})
            removed = [str(p) for p in paths][:500]
            details = {"paths": removed, "recursive": bool(data.get("recursive"))}

            def run() -> dict[str, Any]:
                return client.delete_files(instance.id, removed, bool(data.get("recursive")), timeout=FILE_OPERATION_TIMEOUT)

        try:
            result = run()
        except AgentTimeout:
            # the outcome is unknown (it may still fail): no pack file is recorded as removed
            audit.record(
                f"files.{op}", request=request, instance=instance, target=instance, details={**details, "still_running": True}
            )
            return Response(
                {
                    "still_running": True,
                    "message": "The node is still working on this (a big folder takes a while). Refresh the list in a "
                    "minute to see the result.",
                },
                status=status.HTTP_202_ACCEPTED,
            )
        except ApiError as exc:
            audit.record(
                f"files.{op}",
                request=request,
                instance=instance,
                target=instance,
                success=False,
                details={**details, "error": exc.error_code},
            )
            raise
        audit.record(f"files.{op}", request=request, instance=instance, target=instance, details=details)
        if len(removed) == 1:
            _record_pack_files(instance, client, removed[0])
        if op == "rename":
            _forget_pack_files(instance, [details["destination"]])
        return Response(result)


def _record_pack_files(instance: Instance, client: AgentClient, path: str) -> None:
    """One jar of a CurseForge/Modrinth pack deleted or moved away directly in mods/ (or plugins/) in
    the file manager: keep the runtime from re-installing the whole pack at the next start
    (apps/content/packfiles.py). A folder, a config file or several files at once are not recorded."""
    from apps.content import packfiles

    if not packfiles.applies(instance) or not packfiles.recordable(path):
        return
    try:
        packfiles.record_removed(instance, client, [path])
    except Exception:  # the file operation is done; only the re-install protection is missing
        log.exception("could not record removed pack files of %s", instance.pk)


def _forget_pack_files(instance: Instance, paths: list[str]) -> None:
    from apps.content import packfiles

    if packfiles.applies(instance):
        packfiles.forget(instance, paths)


# --- schedules ---------------------------------------------------------------------------
# A schedule runs its action with the panel's authority, so creating or changing one needs the
# permission the action itself needs (instance.schedules alone only lets you see/delete them).
SCHEDULE_ACTION_PERMISSIONS = {
    "restart": "instance.control",
    "start": "instance.control",
    "stop": "instance.control",
    "backup": "instance.backup",
    "snapshot": "instance.backup",
    "command": "instance.command",
}


def require_schedule_action(user: Any, instance: Instance, action: str, payload: dict[str, Any] | None) -> None:
    require(user, SCHEDULE_ACTION_PERMISSIONS.get(action, "instance.configure"), instance)
    if action == "snapshot":
        # Every snapshot schedule prunes older scheduled snapshots (its own keep count, or the
        # default of 24 when none is set), and deleting snapshots is a restore-level operation.
        require(user, "instance.restore", instance)


def may_create_schedule(user: Any, instance: Instance, action: str, payload: dict[str, Any] | None = None) -> bool:
    """Whether ``user`` could create this schedule through ScheduleListView.post (same checks, no error)."""
    try:
        require(user, "instance.schedules", instance)
        require_schedule_action(user, instance, action, payload)
    except PermissionDenied:
        return False
    return True


class ScheduleListView(APIView):
    @extend_schema(responses={200: ScheduleSerializer(many=True)})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.view")
        return Response(ScheduleSerializer(instance.schedules.all(), many=True).data)

    @extend_schema(request=ScheduleSerializer, responses={201: ScheduleSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.schedules")
        ser = ScheduleSerializer(data=request.data, context={"instance": instance})
        ser.is_valid(raise_exception=True)
        require_schedule_action(request.user, instance, ser.validated_data["action"], ser.validated_data.get("payload"))
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
        data = ser.validated_data
        changed = {k for k, v in data.items() if getattr(sched, k) != v}
        if changed <= {"enabled"} and data.get("enabled", sched.enabled) is False:
            pass  # switching a schedule off (or a no-op) never runs anything
        else:
            # every other edit (action, payload, cron, re-enabling) decides what runs and when
            require_schedule_action(
                request.user, sched.instance, data.get("action", sched.action), data.get("payload", sched.payload)
            )
        # a new due time, and the wait of an earlier due time (deferral while busy) is over too
        sched = ser.save(next_run_at=None, deferred_at=None)
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
        try:
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
        except Exception:
            # no job will extract (and so delete) the uploaded archive (e.g. another job holds the server)
            from .handlers import drop_import_archive

            drop_import_archive(client, instance, temp_name)
            raise
        audit.record(
            "instance.imported",
            request=request,
            instance=instance,
            target=instance,
            details={"archive": upload.name[:200], "size": upload.size, "overwrite": overwrite},
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


def _flag(value: Any, default: bool = False) -> bool:
    """Booleans from JSON or multipart form fields ("true"/"false")."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


RUNNING_STATUSES = ("running", "starting", "installing", "unhealthy", "restarting")


class WorldView(APIView):
    """The world folder (level-name), the folders that exist and their size, plus a zip export link."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from django.conf import settings

        from . import world

        instance = get_instance_for(request.user, pk, "instance.files.read")
        games.for_instance(instance).require(instance, "world_import_export")
        flush = world.may_flush(request.user, instance)
        data = world.describe(instance, AgentClient(instance.node), flush=flush)
        perms = request.user.instance_permissions(instance.pk)
        data.update(
            can_replace={"instance.files.write", "instance.restore"} <= perms,
            running=instance.status in RUNNING_STATUSES,
            # whether the download link asks a running server to save first (needs instance.command/backup)
            download_flushes=flush,
            max_upload_bytes=settings.MCPANEL_MAX_UPLOAD_BYTES or None,
            pre_restore_backup_default=bool(SystemSetting.get("pre_restore_backup")),
            archive_suffixes=list(ARCHIVE_SUFFIXES),
        )
        return Response(data)


class WorldReplaceView(APIView):
    """Replace the world with one from an archive (multipart ``file``, or ``archive``: a path in the server folder).

    Requires ``instance.restore`` *and* ``instance.files.write``: it is a destructive restore of the
    world (same stop / safety backup / start-again semantics as restoring a backup), and it writes
    uploaded content into the server folder. Body fields: ``confirm`` (must be true),
    ``stop_if_running``, ``pre_restore_backup`` (default: the panel setting) and, for an archive in
    the server folder, ``delete_archive`` (removed after a successful replace).
    """

    parser_classes = [MultiPartParser, JSONParser]

    @extend_schema(request=None, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        import secrets as _secrets

        from apps.jobs.models import ACTIVE_STATUSES, Job

        from . import world

        instance = get_instance_for(request.user, pk, "instance.restore")
        require(request.user, "instance.files.write", instance)
        games.for_instance(instance).require(instance, "world_import_export")
        data = request.data
        if not _flag(data.get("confirm")):
            raise ApiError(
                "Replacing the world deletes the current one (a safety backup is taken first unless you turn it off). "
                "Confirm to continue.",
                code="confirmation_required",
            )
        if instance.archived_at:
            raise ConflictError("Unarchive the server first", code="archived")
        current = (
            Job.objects.filter(pk=instance.active_job_id, status__in=ACTIVE_STATUSES).first() if instance.active_job_id else None
        )
        if current is not None:
            raise ConflictError(f"Another operation is in progress: {current.title}", code="instance_busy")
        stop = _flag(data.get("stop_if_running"))
        if instance.status in RUNNING_STATUSES and not stop:
            raise ConflictError(
                "The server is running. Confirm that it may be stopped while the world is replaced.", code="must_stop"
            )
        client = AgentClient(instance.node)
        level, _source = world.level_name(instance, client)
        level = world.validate_level_name(level)
        pre = _flag(data.get("pre_restore_backup"), default=bool(SystemSetting.get("pre_restore_backup")))
        upload = request.FILES.get("file")
        if upload is not None:
            lower = upload.name.lower()
            if not lower.endswith(ARCHIVE_SUFFIXES):
                raise ValidationError({"file": ["Upload the world as a .zip, .tar, .tar.gz or .tar.xz archive"]})
            suffix = next(s for s in ARCHIVE_SUFFIXES if lower.endswith(s))
            archive = f".mcpanel-world-upload-{_secrets.token_hex(6)}{suffix}"
            client.upload(instance.id, archive, upload.chunks(1024 * 1024), overwrite=False)
            uploaded, label, size, delete_after = True, upload.name, upload.size, False
        else:
            archive = str(data.get("archive") or "").strip().strip("/")
            if not archive:
                raise ValidationError({"file": ["Upload a world archive, or choose an archive in the server folder"]})
            if not archive.lower().endswith(ARCHIVE_SUFFIXES):
                raise ValidationError({"archive": ["Choose a .zip, .tar, .tar.gz or .tar.xz archive"]})
            world.check_archive_location(archive, level, world.layout(instance, client))
            entry = client.stat_file(instance.id, archive)
            if entry.get("type") != "file":
                raise ValidationError({"archive": ["That is not a regular file"]})
            uploaded, label, size, delete_after = False, archive, entry.get("size"), _flag(data.get("delete_archive"))
        try:
            job = enqueue(
                "world_replace",
                title=f"Replace world of {instance.name}",
                instance=instance,
                user=request.user,
                params={
                    "archive": archive,
                    "uploaded": uploaded,
                    "delete_archive": delete_after,
                    "pre_restore_backup": pre,
                    "stop_if_running": stop,
                    "original_name": label[:200],
                },
            )
        except ApiError:
            if uploaded:
                try:
                    client.delete_files(instance.id, [archive])
                except ApiError:
                    pass
            raise
        audit.record(
            "instance.world_replace",
            request=request,
            instance=instance,
            target=instance,
            details={
                "archive": label[:200],
                "size": size,
                "level_name": level,
                "uploaded": uploaded,
                "pre_restore_backup": pre,
                "stop_if_running": stop,
            },
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class WorldResetSerializer(serializers.Serializer):
    confirm_name = serializers.CharField(
        max_length=200, trim_whitespace=False, help_text="The server's name, typed to confirm (compared exactly)"
    )
    folders = serializers.ListField(
        child=serializers.CharField(max_length=300, trim_whitespace=False),
        min_length=1,
        max_length=3,
        help_text="The world folders shown in the dialog: the plan's 'delete' list",
    )
    stop_if_running = serializers.BooleanField(default=False)
    pre_restore_backup = serializers.BooleanField(
        required=False, allow_null=True, default=None, help_text="Take a safety backup first (default: the panel setting)"
    )
    seed_mode = serializers.ChoiceField(choices=list(SEED_MODES), default="keep")
    seed = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, help_text="level-seed for seed_mode 'custom' (1-200 characters)"
    )
    keep = serializers.ListField(
        child=serializers.ChoiceField(choices=list(RESET_KEEP)),
        required=False,
        max_length=2,
        help_text="What to carry over into the new world (default: both)",
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        from . import world

        if attrs["seed_mode"] == "custom":
            try:
                attrs["seed"] = world.validate_seed(attrs.get("seed", ""))
            except ApiError as exc:
                raise serializers.ValidationError({"seed": [str(exc.detail)]}) from exc
        else:
            attrs["seed"] = ""  # only a custom seed is ever stored
        attrs["keep"] = [k for k in RESET_KEEP if k in attrs.get("keep", RESET_KEEP)]
        return attrs


class WorldResetView(APIView):
    """Reset the world: delete the world folders so the server generates a new world on its next start.

    ``GET`` returns the plan: the folders that would be deleted (with their size), what can be
    carried over, the current seed setting and the reasons a reset is refused (``blockers``).
    ``POST`` queues the ``world_reset`` job: stop (only with ``stop_if_running``), safety backup
    (``pre_restore_backup``, default: the panel setting), optional seed change, delete, start again
    if the server was running.

    Both need ``instance.restore`` *and* ``instance.files.write``, as replacing the world does.
    The request must repeat the server's name (``confirm_name``) and the folders the dialog
    showed (``folders``); a reset is refused with 409 ``world_changed`` when those are no longer
    the world folders.
    """

    parser_classes = [JSONParser]

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from . import world

        instance = get_instance_for(request.user, pk, "instance.restore")
        require(request.user, "instance.files.write", instance)
        games.for_instance(instance).require(instance, "world_reset")
        return Response(world.reset_plan(instance, AgentClient(instance.node)))

    @extend_schema(request=WorldResetSerializer, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        from . import world

        instance = get_instance_for(request.user, pk, "instance.restore")
        require(request.user, "instance.files.write", instance)
        games.for_instance(instance).require(instance, "world_reset")
        ser = WorldResetSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if data["confirm_name"] != instance.name:
            raise ApiError("Type the server name exactly to confirm resetting its world", code="confirmation_required")
        if instance.archived_at:
            raise ConflictError("Unarchive the server first", code="archived")
        plan = world.reset_plan(instance, AgentClient(instance.node), sizes=False)
        world.raise_reset_blocker(plan)
        if sorted(data["folders"]) != sorted(plan["delete"]):
            raise ConflictError(
                "The world folders are not the ones this reset was confirmed for (they are now: "
                f"{', '.join(plan['delete'])}). Check what would be deleted and confirm again.",
                code="world_changed",
                details={"delete": plan["delete"]},
            )
        stop = data["stop_if_running"]
        if plan["running"] and not stop:
            raise ConflictError(
                "The server is running. Confirm that it may be stopped while the world is reset.", code="must_stop"
            )
        world.check_seed_env(instance, data["seed_mode"])
        pre = data["pre_restore_backup"]
        if pre is None:
            pre = bool(SystemSetting.get("pre_restore_backup"))
        job = enqueue(
            "world_reset",
            title=f"Reset world of {instance.name}",
            instance=instance,
            user=request.user,
            params={
                "level_name": plan["level_name"],
                "layout": plan["layout"],
                "folders": plan["delete"],
                "seed_mode": data["seed_mode"],
                "seed": data["seed"],
                "keep": data["keep"],
                "pre_restore_backup": pre,
                "stop_if_running": stop,
            },
        )
        # the seed itself is not recorded (it stays in the job's parameters, which the API does not expose)
        audit.record(
            "instance.world_reset",
            request=request,
            instance=instance,
            target=instance,
            details={
                "level_name": plan["level_name"],
                "layout": plan["layout"],
                "folders": plan["delete"],
                "seed_mode": data["seed_mode"],
                "keep": data["keep"],
                "pre_restore_backup": pre,
                "stop_if_running": stop,
            },
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class DiagnosticsView(APIView):
    """Known failure signatures found in the recent server log, with suggested fixes."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from .diagnostics import diagnose

        instance = get_instance_for(request.user, pk, "instance.console")
        games.for_instance(instance).require(instance, "diagnostics")
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
        games.for_instance(instance).require(instance, "software_update")
        if (instance.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
            from .game_views import game_software_update

            return game_software_update(request, instance)
        if instance.is_modpack:
            raise ApiError("Modpack servers are updated through the modpack release picker", code="use_modpack_update")
        if instance.archived_at:
            raise ConflictError("Unarchive this server before updating or starting it", code="archived")
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
        # a stopped server started afterwards must fit into the node's memory (a running one is started
        # again without the check: it ran when the job began)
        overcommit = capacity.admit_request(
            instance,
            starts=bool(data.get("start")) and not capacity.runs_now(instance),
            allow_overcommit=data.get("allow_overcommit"),
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
                "backup": bool(data.get("backup", SystemSetting.get("pre_update_backup"))),
                "start": bool(data.get("start")),
                **overcommit,
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


class FilesArchiveView(APIView):
    """Download one or more folders (e.g. a world and its dimension folders) as a zip built on the fly.

    ``path`` may be repeated (up to 8). ``name`` sets the download file name; ``flush=true`` asks a
    running server to write all chunks to disk first (``save-all flush``, best effort; only for users
    with ``instance.command`` or ``instance.backup``, since it is a console command).
    """

    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY})
    def get(self, request: Request, pk: str) -> StreamingHttpResponse:
        from . import world

        instance = get_instance_for(request.user, pk, "instance.files.read")
        paths = request.query_params.getlist("path") or [""]
        if len(paths) > 8:
            raise ValidationError({"path": ["At most 8 folders can be downloaded at once"]})
        client = AgentClient(instance.node)
        flushed = False
        flush_command = games.for_instance(instance).flush_command
        if (
            request.query_params.get("flush") == "true"
            and flush_command
            and instance.status in ("running", "unhealthy")
            and world.may_flush(request.user, instance)  # a console command: not for read-only file access
        ):
            try:
                client.command(instance.id, flush_command, timeout=120)
                flushed = True
            except ApiError:
                pass  # not running after all / RCON not ready: download what is on disk
        chunks, _length, upstream = client.download(instance.id, paths[0] if len(paths) == 1 else paths, endpoint="archive")
        name = (request.query_params.get("name") or "").strip()[:100] or paths[0].strip("/").rsplit("/", 1)[-1] or instance.name
        response = AgentStreamingResponse(chunks, upstream, content_type="application/zip")
        response["Content-Disposition"] = content_disposition(f"{name}.zip")
        response["X-Content-Type-Options"] = "nosniff"
        details: dict[str, Any] = {"path": paths[0]} if len(paths) == 1 else {"paths": paths}
        if flushed:
            details["flushed"] = True
        audit.record("files.download_archive", request=request, instance=instance, target=instance, details=details)
        return response


class MigrateView(APIView):
    """Move a server to another node (admin: needs system.nodes and instance.configure)."""

    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.configure")
        require(request.user, "system.nodes")
        data = request.data if isinstance(request.data, dict) else {}
        target = Node.objects.filter(pk=data.get("node")).first()
        if target is None:
            raise ValidationError({"node": ["Choose a target node"]})
        if target.pk == instance.node_id:
            raise ConflictError("The server is already on that node", code="same_node")
        if data.get("confirm") is not True:
            raise ApiError(
                "Migration stops the server and copies all of its files. Confirm to continue.", code="confirmation_required"
            )
        # a running server is started on the target node: it must fit there (the job checks it again before copying)
        overcommit = capacity.admit_request(
            instance, starts=capacity.runs_now(instance), allow_overcommit=data.get("allow_overcommit"), node=target
        )
        job = enqueue(
            "migrate",
            title=f"Move {instance.name} to {target.name}",
            instance=instance,
            user=request.user,
            params={"target_node": target.pk, "delete_source": bool(data.get("delete_source")), **overcommit},
        )
        audit.record(
            "instance.migrate",
            request=request,
            instance=instance,
            target=instance,
            details={"from": instance.node.name, "to": target.name, "delete_source": bool(data.get("delete_source"))},
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)
