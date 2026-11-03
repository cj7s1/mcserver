from __future__ import annotations

import ipaddress
import re
from typing import Any

from rest_framework import serializers

from apps.core.models import SystemSetting

from . import properties
from .models import Instance, Schedule, ServerType
from .spec import MANAGED_ENV, restart_required

ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
RESTART_POLICIES = ("unless-stopped", "on-failure", "no")
JVM_PRESETS = ("none", "aikar", "meowice")


def validate_env(value: dict[str, Any]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise serializers.ValidationError("Must be an object of NAME: value pairs")
    if len(value) > 100:
        raise serializers.ValidationError("Too many variables")
    out = {}
    for k, v in value.items():
        if not ENV_KEY.match(str(k)):
            raise serializers.ValidationError(f"Invalid variable name {k!r}")
        if str(k).upper() in MANAGED_ENV:
            raise serializers.ValidationError(f"{k} is managed by MCPanel; use the dedicated setting instead")
        if not isinstance(v, (str, int, float, bool)) or len(str(v)) > 8192 or "\x00" in str(v):
            raise serializers.ValidationError(f"Invalid value for {k}")
        out[str(k)] = str(v).lower() if isinstance(v, bool) else str(v)
    return out


def validate_extra_ports(value: list[Any]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 10:
        raise serializers.ValidationError("Provide at most 10 extra port mappings")
    out = []
    for item in value:
        try:
            cport = int(item["container_port"])
            hport = int(item["host_port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise serializers.ValidationError("Each mapping needs container_port and host_port") from exc
        proto = item.get("protocol", "tcp")
        if proto not in ("tcp", "udp"):
            raise serializers.ValidationError("protocol must be tcp or udp")
        if not (1 <= cport <= 65535 and 1024 <= hport <= 65535):
            raise serializers.ValidationError("Ports out of range (host ports must be 1024-65535)")
        if cport == 25575:
            raise serializers.ValidationError("Publishing the RCON port is not allowed; the panel proxies RCON securely")
        if cport == 25565 and proto == "tcp":
            raise serializers.ValidationError("The game port is configured separately")
        host_ip = item.get("host_ip") or ""
        if host_ip:
            ipaddress.ip_address(host_ip)
        out.append(
            {
                "container_port": cport,
                "host_port": hport,
                "protocol": proto,
                "host_ip": host_ip,
                "label": str(item.get("label", ""))[:40],
            }
        )
    return out


class InstanceSerializer(serializers.ModelSerializer):
    restart_required = serializers.SerializerMethodField()
    content_kind = serializers.CharField(read_only=True)
    is_modpack = serializers.BooleanField(read_only=True)
    container_memory_mb = serializers.IntegerField(read_only=True)
    loader = serializers.CharField(source="effective_loader", read_only=True)
    connection = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    active_job = serializers.SerializerMethodField()
    node_name = serializers.CharField(source="node.name", read_only=True)

    class Meta:
        model = Instance
        fields = [
            "id",
            "name",
            "description",
            "icon_url",
            "group",
            "node",
            "node_name",
            "edition",
            "server_type",
            "minecraft_version",
            "loader_version",
            "loader",
            "java_version",
            "image",
            "source",
            "content_kind",
            "is_modpack",
            "modpack_provider",
            "modpack_project_id",
            "modpack_slug",
            "modpack_name",
            "modpack_version_id",
            "modpack_version_name",
            "modpack_loader",
            "memory_mb",
            "memory_overhead_mb",
            "container_memory_mb",
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
            "desired_state",
            "status",
            "status_detail",
            "container_state",
            "health",
            "last_exit_code",
            "oom_killed",
            "started_at",
            "ready_at",
            "installed_at",
            "status_updated_at",
            "restart_required",
            "active_job",
            "archived_at",
            "created_at",
            "updated_at",
            "connection",
            "permissions",
        ]
        read_only_fields = [
            f
            for f in fields
            if f
            not in (
                "name",
                "description",
                "icon_url",
                "group",
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
            )
        ]

    def get_restart_required(self, obj: Instance) -> bool:
        try:
            return restart_required(obj)
        except Exception:
            return False

    def get_connection(self, obj: Instance) -> dict[str, Any]:
        return connection_info(obj)

    def get_permissions(self, obj: Instance) -> list[str]:
        request = self.context.get("request")
        if not request:
            return []
        cache = self.context.setdefault("_perm_cache", {})
        if obj.pk not in cache:
            cache[obj.pk] = sorted(request.user.instance_permissions(obj.pk))
        return cache[obj.pk]

    def get_active_job(self, obj: Instance) -> dict[str, Any] | None:
        job = obj.active_job
        if job is None or not job.is_active:
            return None
        return {
            "id": str(job.id),
            "kind": job.kind,
            "title": job.title,
            "progress": job.progress,
            "message": job.message,
            "status": job.status,
        }

    # --- validation of editable fields --------------------------------------------------
    def validate_env(self, value: Any) -> dict[str, str]:
        return validate_env(value)

    def validate_extra_ports(self, value: Any) -> list[dict[str, Any]]:
        return validate_extra_ports(value)

    def validate_memory_mb(self, value: int) -> int:
        if not 512 <= value <= 262144:
            raise serializers.ValidationError("Memory must be between 512 MB and 256 GB")
        return value

    def validate_restart_policy(self, value: str) -> str:
        if value not in RESTART_POLICIES:
            raise serializers.ValidationError(f"Must be one of {', '.join(RESTART_POLICIES)}")
        return value

    def validate_jvm_flags_preset(self, value: str) -> str:
        if value not in JVM_PRESETS:
            raise serializers.ValidationError(f"Must be one of {', '.join(JVM_PRESETS)}")
        return value

    def validate_jvm_args(self, value: str) -> str:
        if "\n" in value.strip() or "\x00" in value:
            raise serializers.ValidationError("JVM arguments must be on a single line")
        for arg in value.split():
            if not arg.startswith("-"):
                raise serializers.ValidationError(f"'{arg}' does not look like a JVM option (must start with '-')")
            if arg.startswith(("-Xmx", "-Xms")):
                raise serializers.ValidationError("Set heap size with the memory setting, not -Xmx/-Xms")
        return value

    def validate_stop_timeout(self, value: int) -> int:
        if not 15 <= value <= 900:
            raise serializers.ValidationError("Stop timeout must be between 15 and 900 seconds")
        return value

    def validate_java_version(self, value: int) -> int:
        if value not in (8, 11, 16, 17, 21, 25):
            raise serializers.ValidationError("Supported Java versions: 8, 11, 16, 17, 21, 25")
        return value

    def validate_cpu_limit(self, value: float | None) -> float | None:
        if value is not None and not 0.1 <= value <= 256:
            raise serializers.ValidationError("CPU limit must be between 0.1 and 256 cores")
        return value

    def validate_network(self, value: str) -> str:
        if value and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$", value):
            raise serializers.ValidationError("Invalid Docker network name")
        return value


def connection_info(instance: Instance) -> dict[str, Any]:
    node = instance.node
    public = SystemSetting.get("public_address") or node.public_address
    lan = SystemSetting.get("lan_address") or node.lan_address
    bind = instance.host_ip
    port = instance.game_port
    suffix = "" if port == 25565 else f":{port}"
    addresses = []
    if bind not in ("0.0.0.0", "::"):  # noqa: S104
        addresses.append({"kind": "bind", "label": "Bound address", "address": f"{bind}{suffix}"})
    if lan:
        addresses.append({"kind": "lan", "label": "LAN", "address": f"{lan}{suffix}"})
    if public:
        addresses.append({"kind": "public", "label": "Public (requires router port forwarding)", "address": f"{public}{suffix}"})
    return {
        "host_port": port,
        "container_port": 25565,
        "bind_address": bind,
        "addresses": addresses,
        "notes": [
            "The panel maps the host port to the container; it does not configure router port forwarding or DNS.",
            "Players outside your network can only connect if you forward this TCP port on your router.",
        ],
    }


class CreateInstanceSerializer(serializers.Serializer):
    MODES = ("standard", "modpack")

    mode = serializers.ChoiceField(choices=MODES, default="standard")
    name = serializers.CharField(max_length=80)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    group = serializers.CharField(max_length=60, required=False, allow_blank=True)
    icon_url = serializers.URLField(required=False, allow_blank=True, max_length=500)
    accept_eula = serializers.BooleanField()
    # standard
    server_type = serializers.ChoiceField(
        choices=[c for c in ServerType.values if c not in ("MODRINTH", "AUTO_CURSEFORGE", "FTBA", "CUSTOM")], required=False
    )
    minecraft_version = serializers.CharField(max_length=40, required=False)
    loader_version = serializers.CharField(max_length=60, required=False, allow_blank=True)
    # modpack
    modpack_provider = serializers.ChoiceField(choices=["modrinth", "curseforge", "ftb"], required=False)
    modpack_project_id = serializers.CharField(max_length=64, required=False)
    modpack_version_id = serializers.CharField(max_length=64, required=False)
    # resources / runtime
    java_version = serializers.IntegerField(required=False)
    memory_mb = serializers.IntegerField(min_value=512, max_value=262144, required=False)
    jvm_flags_preset = serializers.ChoiceField(choices=JVM_PRESETS, default="aikar")
    jvm_args = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    env = serializers.JSONField(required=False)
    # networking
    game_port = serializers.IntegerField(min_value=1024, max_value=65535, required=False)
    host_ip = serializers.IPAddressField(required=False)
    # initial server.properties values
    server_properties = serializers.DictField(child=serializers.JSONField(), required=False)
    start = serializers.BooleanField(default=True)

    def validate_accept_eula(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError("You must accept the Minecraft EULA (https://aka.ms/MinecraftEULA)")
        return value

    def validate_env(self, value: Any) -> dict[str, str]:
        return validate_env(value or {})

    def validate_jvm_args(self, value: str) -> str:
        return InstanceSerializer().validate_jvm_args(value)

    def validate_java_version(self, value: int) -> int:
        return InstanceSerializer().validate_java_version(value)

    def validate_server_properties(self, value: dict[str, Any]) -> dict[str, str]:
        try:
            validated = properties.validate_changes(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return {k: v for k, v in validated.items() if v is not None}

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["mode"] == "standard":
            if not attrs.get("server_type"):
                raise serializers.ValidationError({"server_type": ["Choose a server type"]})
            if not attrs.get("minecraft_version"):
                raise serializers.ValidationError({"minecraft_version": ["Choose a Minecraft version"]})
        else:
            if not attrs.get("modpack_provider") or not attrs.get("modpack_project_id"):
                raise serializers.ValidationError({"modpack_project_id": ["Choose a modpack"]})
            if not attrs.get("modpack_version_id"):
                raise serializers.ValidationError({"modpack_version_id": ["Choose a modpack release (releases are pinned)"]})
        return attrs


class ScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Schedule
        fields = [
            "id",
            "name",
            "action",
            "cron",
            "payload",
            "enabled",
            "only_when_running",
            "last_run_at",
            "next_run_at",
            "last_result",
            "created_at",
        ]
        read_only_fields = ["last_run_at", "next_run_at", "last_result", "created_at"]

    def validate_action(self, value: str) -> str:
        if value not in Schedule.ACTIONS:
            raise serializers.ValidationError(f"Must be one of {', '.join(Schedule.ACTIONS)}")
        return value

    def validate_cron(self, value: str) -> str:
        from .scheduling import validate_cron

        try:
            return validate_cron(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        action = attrs.get("action") or getattr(self.instance, "action", None)
        payload = attrs.get("payload", getattr(self.instance, "payload", {})) or {}
        if action == "command":
            cmd = str(payload.get("command", "")).strip()
            if not cmd or len(cmd) > 1400 or "\n" in cmd:
                raise serializers.ValidationError({"payload": ["A single-line command is required"]})
            payload["command"] = cmd.lstrip("/")
            attrs["payload"] = payload
        return attrs
