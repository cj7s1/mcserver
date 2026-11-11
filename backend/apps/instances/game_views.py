"""API of games other than Minecraft (contract of multi-game phases 2 and 3).

Creating a server of another game, its config files ("Game settings"), its secrets and setup
steps, its save files (list, reset to a new map, import, export) and its software update. Minecraft
keeps its own endpoints (properties, world, software); these refuse a Minecraft server.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import time
import zipfile
from typing import Any

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps import games
from apps.accounts.models import InstanceAccess, Role
from apps.audit import service as audit
from apps.core.errors import ApiError, ConflictError
from apps.core.models import SystemSetting
from apps.core.permissions import get_instance_for, require
from apps.core.streaming import AgentStreamingResponse
from apps.core.utils import content_disposition
from apps.games import codecs
from apps.jobs.runner import enqueue
from apps.jobs.serializers import JobSerializer
from apps.nodes.client import AgentClient
from apps.nodes.models import Node

from . import capacity, services
from .handlers import IMPORT_ARCHIVE_PREFIX
from .models import Instance, Source

log = logging.getLogger(__name__)

RUNNING = ("running", "starting", "installing", "unhealthy", "restarting")


def _other_game(instance: Instance) -> Any:
    """The definition of a server that is not a Minecraft server (409 ``unsupported_by_game`` for Minecraft)."""
    definition = games.for_instance(instance)
    if definition.id == games.DEFAULT_GAME:
        raise ConflictError("Minecraft servers use their own settings pages", code="unsupported_by_game")
    return definition


# --- create --------------------------------------------------------------------------------------
def create_other_game(request: Request, data: dict[str, Any], node: Node, definition: Any) -> Response:
    """``POST /api/instances/`` for a game other than Minecraft (contract B5)."""
    from .serializers import ScheduleSerializer
    from .views import _ser, may_create_schedule

    fields: dict[str, Any] = definition.create_fields(data)
    fields.update(
        game=definition.id,
        name=data["name"],
        description=data.get("description", ""),
        group=data.get("group", ""),
        icon_url=data.get("icon_url", ""),
        node=node,
        created_by=request.user,
        env=data.get("env") or {},
        host_ip=data.get("host_ip") or SystemSetting.get("default_bind_ip") or "0.0.0.0",  # noqa: S104
        source=Source.STANDARD,
    )
    probe = Instance(**fields)
    fields["image"] = definition.image(probe)
    overcommit = capacity.admit_request(
        Instance(**fields), starts=bool(data.get("start", True)), allow_overcommit=data.get("allow_overcommit")
    )
    with transaction.atomic():
        ports = services.assign_named_ports(definition, node, data.get("ports"))
        fields["ports"] = ports
        fields["game_port"] = int(ports[0]["host_port"]) if ports else services.allocate_port(node)
        instance = Instance.objects.create(**fields)
        # the request's secrets, plus generated ones (Palworld's admin password); never logged
        if initial := definition.initial_secrets(data):
            instance.set_secret_values(initial)
            instance.save(update_fields=["secrets"])
        manager = Role.objects.filter(key="server_manager").first()
        if manager and not request.user.is_owner and "instance.view" not in request.user.global_permissions():
            InstanceAccess.objects.create(user=request.user, instance=instance, role=manager, created_by=request.user)
        backup_schedule = None
        if data.get("nightly_backup") and may_create_schedule(request.user, instance, "backup"):
            backup_schedule = services.add_nightly_backup_schedule(instance)
    job = enqueue(
        "install",
        title=f"Install {instance.name}",
        instance=instance,
        user=request.user,
        params={"settings": data.get("settings") or {}, "start": data.get("start", True), **overcommit},
    )
    audit.record(
        "instance.created",
        request=request,
        instance=instance,
        target=instance,
        details={
            "game": definition.id,
            "version": instance.minecraft_version,
            "options": {k: v for k, v in (instance.game_options or {}).items()},
            "ports": [f"{p['protocol']}/{p['host_port']}" for p in ports],
            "secrets": sorted(data.get("secrets") or {}),
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
            "warnings": [],
            "java": None,
            "backup_schedule": schedule_data,
        },
        status=status.HTTP_201_CREATED,
    )


# --- software update --------------------------------------------------------------------------------
def game_software_update(request: Request, instance: Instance) -> Response:
    """``POST /software`` for another game: ``{"version", "backup", "start", "allow_downgrade"}`` (B10)."""
    definition = _other_game(instance)
    if instance.archived_at:
        raise ConflictError("Unarchive this server before updating or starting it", code="archived")
    data = request.data if isinstance(request.data, dict) else {}
    version = definition.validate_version(str(data.get("version") or ""), definition.agent_variant(instance))
    current = instance.minecraft_version
    if version == current:
        raise ApiError(f"The server already runs {definition.label} {version}", code="same_version")
    try:
        downgrade = definition.version_key(version) < definition.version_key(current)
    except ValueError:
        downgrade = False
    if downgrade and data.get("allow_downgrade") is not True:
        raise ApiError(
            f"Going back from {current} to {version} may not load saves written by the newer version. Restore an "
            "older backup instead, or confirm the downgrade explicitly.",
            code="downgrade_unconfirmed",
            status_code=409,
        )
    overcommit = capacity.admit_request(
        instance,
        starts=bool(data.get("start")) and not capacity.runs_now(instance),
        allow_overcommit=data.get("allow_overcommit"),
    )
    job = enqueue(
        "software_update",
        title=f"Update {instance.name} to {definition.label} {version}",
        instance=instance,
        user=request.user,
        params={
            "version": version,
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
        details={"game": definition.id, "from": current, "to": version},
    )
    return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


# --- config files -----------------------------------------------------------------------------------
def _read(client: AgentClient, instance: Instance, path: str) -> str | None:
    try:
        return client.read_text(instance.id, path)["content"]
    except ApiError as exc:
        if exc.error_code == "not_found":
            return None
        raise


def _file_values(cfg: Any, text: str | None) -> dict[str, Any]:
    """The schema keys of a file as the form shows them (missing keys: the field's default)."""
    parsed = codecs.get(cfg.codec).parse(text or "")
    values: dict[str, Any] = {}
    for field in cfg.fields:
        key = field["key"]
        if key in parsed:
            raw = parsed[key]
            if cfg.codec != "json":  # text formats: typed for the form
                try:
                    raw = codecs.coerce(field, raw)
                except ValueError:
                    pass  # shown as it is in the file
            values[key] = raw
        elif "default" in field:
            values[key] = field["default"]
        else:
            values[key] = None
    return values


def _describe_file(cfg: Any, text: str | None) -> dict[str, Any]:
    try:
        values, redacted = codecs.masked(cfg, _file_values(cfg, text))
        error = None
    except ValueError as exc:
        values, redacted, error = {}, [], str(exc)
    return {**cfg.as_dict(), "exists": text is not None, "values": values, "redacted_keys": redacted, "error": error}


def _describe_values(cfg: Any, values: dict[str, Any]) -> dict[str, Any]:
    """A config the definition keeps itself or reads from the game's API (``is_virtual_config``)."""
    shown, redacted = codecs.masked(cfg, values)
    return {**cfg.as_dict(), "exists": True, "values": shown, "redacted_keys": redacted, "error": None}


class ConfigFilesView(APIView):
    """``GET /api/instances/<id>/config``: the game's config files with their schema and values (B6)."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.view")
        definition = _other_game(instance)
        definition.require(instance, "config_files")
        client = AgentClient(instance.node)
        files = []
        for cfg in definition.config_files_for(instance):
            if not cfg.fields:
                continue
            if definition.is_virtual_config(cfg):
                # values the panel keeps itself or reads from the game's API (Satisfactory); None: not now
                values = definition.virtual_config_values(client, instance, cfg)
                if values is not None:
                    files.append(_describe_values(cfg, values))
                continue
            files.append(_describe_file(cfg, _read(client, instance, cfg.path)))
        return Response({"files": files})


class ConfigFileView(APIView):
    """``PATCH /api/instances/<id>/config/<file id>`` ``{"values": {key: value|null}}`` (B6)."""

    @extend_schema(request=dict, responses={200: dict})
    def patch(self, request: Request, pk: str, file_id: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.configure")
        definition = _other_game(instance)
        definition.require(instance, "config_files")
        cfg = definition.config_file_for(instance, file_id)
        if cfg is None or not cfg.fields:
            raise ApiError("Unknown config file", code="not_found", status_code=404)
        values = request.data.get("values") if isinstance(request.data, dict) else None
        if not isinstance(values, dict):
            raise ValidationError({"values": ["Provide an object of setting changes"]})
        secret = set(cfg.secret_keys) | {f["key"] for f in cfg.fields if f.get("secret")}
        # a masked secret sent back unchanged is not a change
        values = {k: v for k, v in values.items() if not (k in secret and v == codecs.MASK)}
        try:
            validated = codecs.validate_values(cfg, values)
        except ValueError as exc:
            raise ValidationError({"values": [str(exc)]}) from exc
        client = AgentClient(instance.node)
        if definition.is_virtual_config(cfg):
            return self._patch_virtual(request, instance, definition, client, cfg, validated)
        current = _read(client, instance, cfg.path)
        before = _file_values(cfg, current)
        codec = codecs.get(cfg.codec)
        try:
            new_text = codec.update(current or "", validated)
        except ValueError as exc:
            raise ConflictError(str(exc), code="invalid_file") from exc
        changed = sorted(k for k, v in validated.items() if before.get(k) != v)
        if changed:
            client.write_text(instance.id, cfg.path, new_text)
        audit.record(
            "instance.config_updated",
            request=request,
            instance=instance,
            target=instance,
            details={"file": cfg.path, "keys": changed[:50]},
        )
        restart = any((cfg.field(k) or {}).get("restart", True) for k in changed)
        shown = _describe_file(cfg, new_text if changed else current)
        return Response(
            {
                "file": shown,
                "values": shown["values"],
                "redacted_keys": shown["redacted_keys"],
                "changed": changed,
                "restart_required": restart and instance.status in RUNNING,
            }
        )

    def _patch_virtual(
        self, request: Request, instance: Instance, definition: Any, client: AgentClient, cfg: Any, validated: dict[str, Any]
    ) -> Response:
        before = definition.virtual_config_values(client, instance, cfg)
        if before is None:
            raise ConflictError(f"{cfg.label} can be changed only while the server runs", code="not_running")
        changed = sorted(k for k, v in validated.items() if before.get(k) != v)
        after = definition.write_virtual_config(client, instance, cfg, {k: validated[k] for k in changed}) if changed else before
        audit.record(
            "instance.config_updated",
            request=request,
            instance=instance,
            target=instance,
            details={"file": cfg.file_id, "keys": changed[:50]},
        )
        restart = any((cfg.field(k) or {}).get("restart", True) for k in changed)
        shown = _describe_values(cfg, after)
        return Response(
            {
                "file": shown,
                "values": shown["values"],
                "redacted_keys": shown["redacted_keys"],
                "changed": changed,
                "restart_required": restart and instance.status in RUNNING,
            }
        )


# --- secrets and setup -----------------------------------------------------------------------------
class SecretsView(APIView):
    """``GET``/``PUT /api/instances/<id>/secrets``: which secrets are set; set or clear them (B7)."""

    def _answer(self, instance: Instance, definition: Any) -> dict[str, Any]:
        values = instance.secret_values
        return {"secrets": [{**s.as_dict(), "set": bool(values.get(s.key))} for s in definition.secret_specs_for(instance)]}

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.configure")
        return Response(self._answer(instance, games.for_instance(instance)))

    @extend_schema(request=dict, responses={200: dict})
    def put(self, request: Request, pk: str) -> Response:
        from .game_handlers import enqueue_setup

        instance = get_instance_for(request.user, pk, "instance.configure")
        definition = games.for_instance(instance)
        values = request.data.get("values") if isinstance(request.data, dict) else None
        if not isinstance(values, dict):
            raise ValidationError({"values": ["Provide an object of secrets"]})
        current = instance.secret_values
        known = {s.key for s in definition.secret_specs_for(instance)}
        definition.check_secret_change(instance, values, request.data)
        for key, value in values.items():
            spec = definition.secret_spec(str(key))
            if spec is None or str(key) not in known:
                raise ValidationError({"values": [f"Unknown secret '{key}'"]})
            if value is None or value == "":
                if spec.generate or spec.required:
                    # (Palworld's admin password, Valheim's join password: the server needs one)
                    raise ValidationError({"values": [f"{spec.label} cannot be empty; enter a new value instead"]})
                current.pop(str(key), None)
            elif not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise ValidationError({"values": [f"Invalid value for '{key}'"]})
            elif (error := definition.secret_error(str(key), value, instance=instance)) is not None:
                raise ValidationError({"values": [error]})
            else:
                current[str(key)] = value
        instance.set_secret_values(current)
        instance.save(update_fields=["secrets", "updated_at"])
        definition.secrets_changed(instance, sorted(str(k) for k in values))
        # the names only, never the values
        audit.record(
            "instance.secrets_updated", request=request, instance=instance, target=instance, details={"keys": sorted(values)}
        )
        if definition.setup_steps:
            try:
                enqueue_setup(instance, user=request.user)
            except ApiError as exc:
                log.info("setup of %s not queued: %s", instance.pk, exc)
        return Response(self._answer(instance, definition))


class SecretRevealView(APIView):
    """``POST /api/instances/<id>/secrets/<key>/reveal``: the value of a secret the game marks ``reveal``
    (a password players type in game: Palworld's admin password, Project Zomboid's admin login). Only
    for users who may change the secret anyway (``instance.configure``); audited with the key only."""

    @extend_schema(request=None, responses={200: dict})
    def post(self, request: Request, pk: str, key: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.configure")
        definition = games.for_instance(instance)
        spec = next((s for s in definition.secret_specs_for(instance) if s.key == key), None)
        if spec is None or not spec.reveal:
            raise ApiError("This secret cannot be shown", code="not_revealable", status_code=404)
        value = instance.secret_values.get(key)
        if not value:
            raise ApiError(f"{spec.label} is not set", code="not_set", status_code=404)
        audit.record("instance.secret_revealed", request=request, instance=instance, target=instance, details={"key": key})
        response = Response({"key": key, "value": value})
        response["Cache-Control"] = "no-store"
        return response


class SetupView(APIView):
    """``POST /api/instances/<id>/setup`` ``{"step": id?}``: run the pending setup steps (B7)."""

    @extend_schema(request=dict, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        from .game_handlers import enqueue_setup

        instance = get_instance_for(request.user, pk, "instance.configure")
        definition = games.for_instance(instance)
        if not definition.setup_steps:
            raise ConflictError(f"{definition.label} servers have no setup steps", code="unsupported_by_game")
        step = request.data.get("step") if isinstance(request.data, dict) else None
        if step is not None and not any(s.id == step for s in definition.setup_steps):
            raise ValidationError({"step": ["Unknown setup step"]})
        job = enqueue_setup(instance, user=request.user, step=step)
        if job is None:
            raise ConflictError("Nothing to set up now (a step waits for something only you can provide)", code="nothing_to_do")
        audit.record("instance.setup", request=request, instance=instance, target=instance, details={"step": step})
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


# --- saves ------------------------------------------------------------------------------------------
def _saves(instance: Instance, capability: str) -> tuple[Any, Any]:
    definition = _other_game(instance)
    definition.require(instance, capability)
    if definition.saves is None:
        raise ConflictError(f"{definition.label} servers have no save files the panel manages", code="unsupported_by_game")
    return definition, definition.saves


class SavesView(APIView):
    """``GET /api/instances/<id>/saves``: the save files, the current one and what was set aside (B8)."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from django.conf import settings

        instance = get_instance_for(request.user, pk, "instance.view")
        definition = _other_game(instance)
        if not ({"world_reset", "world_import_export"} & definition.capabilities(instance)) or definition.saves is None:
            raise ConflictError(f"{definition.label} servers have no save files the panel manages", code="unsupported_by_game")
        data = definition.saves.list(AgentClient(instance.node), instance)
        perms = request.user.instance_permissions(instance.pk)
        data.update(
            running=instance.status in RUNNING,
            can_replace={"instance.files.write", "instance.restore"} <= perms,
            can_export="instance.files.read" in perms,
            pre_restore_backup_default=bool(SystemSetting.get("pre_restore_backup")),
            max_upload_bytes=settings.MCPANEL_MAX_UPLOAD_BYTES or None,
        )
        return Response(data)


class SavesResetSerializer(serializers.Serializer):
    confirm_name = serializers.CharField(max_length=80)
    seed = serializers.IntegerField(min_value=0, max_value=4294967295, required=False, allow_null=True, default=None)
    settings = serializers.DictField(child=serializers.JSONField(allow_null=True), required=False, default=dict)
    backup = serializers.BooleanField(required=False, allow_null=True, default=None)


def _replace_checks(request: Request, pk: str, capability: str) -> tuple[Instance, Any, Any]:
    instance = get_instance_for(request.user, pk, "instance.restore")
    require(request.user, "instance.files.write", instance)
    definition, saves = _saves(instance, capability)
    if instance.archived_at:
        raise ConflictError("Unarchive the server first", code="archived")
    return instance, definition, saves


class SavesResetView(APIView):
    """``POST /api/instances/<id>/saves/reset``: a new map with the chosen seed (job ``saves_reset``)."""

    parser_classes = [JSONParser]

    @extend_schema(request=SavesResetSerializer, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance, definition, _saves_model = _replace_checks(request, pk, "world_reset")
        ser = SavesResetSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if data["confirm_name"] != instance.name:
            raise ApiError("Type the server name exactly to confirm resetting its world", code="confirmation_required")
        reset_config = getattr(_saves_model, "reset_config", None)
        cfg = reset_config(instance) if reset_config is not None else definition.config_file("map-gen-settings")
        settings_values: dict[str, Any] = {}
        if data["settings"]:
            if cfg is None:
                raise ValidationError({"settings": ["This game has no map settings"]})
            try:
                settings_values = codecs.validate_values(cfg, {k: v for k, v in data["settings"].items() if k != "seed"})
            except ValueError as exc:
                raise ValidationError({"settings": [str(exc)]}) from exc
        backup = data["backup"] if data["backup"] is not None else bool(SystemSetting.get("pre_restore_backup"))
        job = enqueue(
            "saves_reset",
            title=f"Reset world of {instance.name}",
            instance=instance,
            user=request.user,
            params={"seed": data["seed"], "settings": settings_values, "backup": backup},
        )
        audit.record(
            "instance.world_reset",
            request=request,
            instance=instance,
            target=instance,
            details={"game": definition.id, "settings": sorted(settings_values), "backup": backup},
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


def _looks_like_save(upload: Any, extension: str) -> str | None:
    """Why an uploaded file is not a save of the game (None: it looks like one)."""
    name = str(getattr(upload, "name", "") or "")
    if not name.lower().endswith(extension):
        return f"Upload a {extension} save file"
    if extension != ".zip":
        return None
    try:
        upload.seek(0)
        with zipfile.ZipFile(upload) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return "The file is not a zip archive"
    finally:
        upload.seek(0)
    if not any(n.rsplit("/", 1)[-1] in ("level.dat0", "level.dat", "level-init.dat") for n in names):
        return "The zip does not look like a Factorio save (no level.dat0 / level-init.dat in it)"
    return None


class SavesImportView(APIView):
    """``POST /api/instances/<id>/saves/import`` (multipart ``file``, ``confirm_name``, ``backup``):
    the uploaded save becomes the server's save (job ``saves_import``)."""

    parser_classes = [MultiPartParser]

    @extend_schema(request=None, responses={202: JobSerializer})
    def post(self, request: Request, pk: str) -> Response:
        instance, definition, saves = _replace_checks(request, pk, "world_import_export")
        if request.data.get("confirm_name") != instance.name:
            raise ApiError("Type the server name exactly to confirm replacing its save", code="confirmation_required")
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": ["Choose a save file"]})
        validate = getattr(saves, "validate_upload", None)
        if reason := (validate(upload) if validate is not None else _looks_like_save(upload, saves.extension)):
            raise ValidationError({"file": [reason]})
        raw_backup = request.data.get("backup")
        backup = bool(SystemSetting.get("pre_restore_backup")) if raw_backup in (None, "") else str(raw_backup).lower() == "true"
        # what the job needs to know about the upload (a zipped world folder: where the world is in it)
        upload_params = getattr(saves, "upload_params", None)
        extra = upload_params(upload) if upload_params is not None else {}
        client = AgentClient(instance.node)
        path = f"{IMPORT_ARCHIVE_PREFIX}{_secrets.token_hex(6)}{saves.extension}"
        client.upload(instance.id, path, upload.chunks(256 * 1024), overwrite=False)
        try:
            job = enqueue(
                "saves_import",
                title=f"Import a save into {instance.name}",
                instance=instance,
                user=request.user,
                params={"path": path, "backup": backup, "name": str(upload.name)[:120], **extra},
            )
        except ApiError:
            from .handlers import drop_import_archive

            drop_import_archive(client, instance, path)
            raise
        audit.record(
            "instance.world_import",
            request=request,
            instance=instance,
            target=instance,
            details={"game": definition.id, "file": str(upload.name)[:120], "size": upload.size, "backup": backup},
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


EXPORT_SAVE_WAIT = 60.0


class SavesExportView(APIView):
    """``GET /api/instances/<id>/saves/export``: the current save as a download. A running server is
    asked to save first (users who may send commands or take backups), and the download waits until
    the file was written."""

    @extend_schema(responses={(200, "application/zip"): OpenApiTypes.BINARY})
    def get(self, request: Request, pk: str) -> Any:
        from . import world

        instance = get_instance_for(request.user, pk, "instance.files.read")
        definition, saves = _saves(instance, "world_import_export")
        client = AgentClient(instance.node)
        own_export = getattr(saves, "export", None)
        filename = None
        if own_export is not None:
            # a game that saves through its API and names its saves itself (Satisfactory)
            live = instance.status in ("running", "unhealthy") and world.may_flush(request.user, instance)
            path, filename, saved = own_export(client, instance, save_first=live)
        else:
            # the save the server loads and saves into (Factorio: the newest one, e.g. an autosave)
            export_path = getattr(saves, "export_path", None)
            path = export_path(client, instance) if export_path is not None else saves.current_path(instance)
            saved = False
        if (
            own_export is None
            and instance.status in ("running", "unhealthy")
            and definition.flush_command
            and world.may_flush(request.user, instance)
        ):
            try:
                before = client.stat_file(instance.id, path).get("modified")
            except ApiError:
                before = None
            try:
                client.command(instance.id, definition.flush_command, timeout=60)
                deadline = time.monotonic() + EXPORT_SAVE_WAIT
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                    now = client.stat_file(instance.id, path).get("modified")
                    if now and now != before:
                        time.sleep(1.0)  # the zip is renamed into place when complete; let it settle
                        saved = True
                        break
            except ApiError:
                pass  # not running after all: download what is on disk
        if getattr(saves, "export_endpoint", "download") == "archive":
            # a world that is a folder (Palworld, Valheim, Project Zomboid, Rust): a zip of it, built by
            # the agent, without the game's own rolling copies and without files that hold a secret
            from . import secret_files

            secret_files.require_zippable(instance, [path], request.user)
            excludes = sorted(
                {*saves.export_excludes(instance, path), *secret_files.zip_excludes(instance, [path], request.user)}
            )
            if excludes and not client.supports("archive_excludes"):
                raise ConflictError(
                    "The node's agent is too old to build this zip; update the agent first", code="agent_outdated"
                )
            chunks, _length, upstream = client.download(
                instance.id, path, endpoint="archive", **({"exclude": excludes} if excludes else {})
            )
        else:
            chunks, _length, upstream = client.download(instance.id, path)
        response = AgentStreamingResponse(chunks, upstream, content_type=getattr(saves, "content_type", "application/zip"))
        response["Content-Disposition"] = content_disposition(filename or f"{instance.name}-{path.rsplit('/', 1)[-1]}")
        response["X-Content-Type-Options"] = "nosniff"
        audit.record(
            "files.download",
            request=request,
            instance=instance,
            target=instance,
            details={"path": path, "saved_first": saved},
        )
        return response
