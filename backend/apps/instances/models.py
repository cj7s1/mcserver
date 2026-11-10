from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.core.crypto import EncryptedTextField
from apps.core.utils import random_secret


class ServerType(models.TextChoices):
    VANILLA = "VANILLA", "Vanilla"
    PAPER = "PAPER", "Paper"
    PURPUR = "PURPUR", "Purpur"
    FOLIA = "FOLIA", "Folia"
    FABRIC = "FABRIC", "Fabric"
    QUILT = "QUILT", "Quilt"
    FORGE = "FORGE", "Forge"
    NEOFORGE = "NEOFORGE", "NeoForge"
    MODRINTH = "MODRINTH", "Modrinth modpack"
    AUTO_CURSEFORGE = "AUTO_CURSEFORGE", "CurseForge modpack"
    FTBA = "FTBA", "Feed The Beast modpack"
    CUSTOM = "CUSTOM", "Custom server jar"


PLUGIN_TYPES = {ServerType.PAPER, ServerType.PURPUR, ServerType.FOLIA}
MOD_TYPES = {ServerType.FABRIC, ServerType.QUILT, ServerType.FORGE, ServerType.NEOFORGE}
MODPACK_TYPES = {ServerType.MODRINTH, ServerType.AUTO_CURSEFORGE, ServerType.FTBA}


class Source(models.TextChoices):
    STANDARD = "standard", "Standard server"
    MODRINTH = "modrinth", "Modrinth modpack"
    CURSEFORGE = "curseforge", "CurseForge modpack"
    FTB = "ftb", "Feed The Beast modpack"
    SERVER_PACK = "server_pack", "Uploaded server pack"
    IMPORTED = "imported", "Imported server"
    CLONED = "cloned", "Cloned instance"
    RESTORED = "restored", "Restored from backup"


class DesiredState(models.TextChoices):
    RUNNING = "running", "Running"
    STOPPED = "stopped", "Stopped"


class Status(models.TextChoices):
    CREATED = "created", "Created"
    INSTALLING = "installing", "Installing"
    STARTING = "starting", "Starting"
    RUNNING = "running", "Running"
    UNHEALTHY = "unhealthy", "Unresponsive"
    STOPPING = "stopping", "Stopping"
    STOPPED = "stopped", "Stopped"
    RESTARTING = "restarting", "Restarting"
    UPDATING = "updating", "Updating"
    BACKING_UP = "backing_up", "Backing up"
    RESTORING = "restoring", "Restoring"
    FAILED = "failed", "Failed"
    UNKNOWN = "unknown", "Unknown"
    ARCHIVED = "archived", "Archived"


def _rcon_password() -> str:
    return random_secret(32)


class Instance(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True, max_length=2000)
    icon_url = models.URLField(blank=True, max_length=500)
    group = models.CharField(max_length=60, blank=True, help_text="Library group/folder")
    node = models.ForeignKey("nodes.Node", on_delete=models.PROTECT, related_name="instances")

    # --- software --------------------------------------------------------------------
    edition = models.CharField(max_length=10, default="java")
    server_type = models.CharField(max_length=20, choices=ServerType.choices, default=ServerType.VANILLA)
    minecraft_version = models.CharField(max_length=40, default="LATEST")
    loader_version = models.CharField(max_length=60, blank=True, help_text="Blank = recommended/latest")
    java_version = models.PositiveSmallIntegerField(default=21)
    image = models.CharField(max_length=200)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.STANDARD)

    # --- modpack pinning ---------------------------------------------------------------
    modpack_provider = models.CharField(max_length=20, blank=True)
    modpack_project_id = models.CharField(max_length=64, blank=True)
    modpack_slug = models.CharField(max_length=128, blank=True)
    modpack_name = models.CharField(max_length=200, blank=True)
    modpack_version_id = models.CharField(max_length=64, blank=True)
    modpack_version_name = models.CharField(max_length=200, blank=True)
    modpack_loader = models.CharField(max_length=20, blank=True)
    modpack_meta = models.JSONField(default=dict, blank=True)

    # --- resources ---------------------------------------------------------------------
    memory_mb = models.PositiveIntegerField(default=4096, help_text="Java heap (-Xmx) in MB")
    memory_overhead_mb = models.PositiveIntegerField(default=0, help_text="Extra container memory above heap; 0 = auto")
    cpu_limit = models.FloatField(null=True, blank=True)
    jvm_flags_preset = models.CharField(max_length=20, default="aikar", help_text="none | aikar | meowice")
    jvm_args = models.TextField(blank=True, max_length=4000)
    env = models.JSONField(default=dict, blank=True, help_text="Advanced: extra runtime environment variables")
    restart_policy = models.CharField(max_length=20, default="unless-stopped")
    stop_timeout = models.PositiveIntegerField(default=90)

    # --- networking --------------------------------------------------------------------
    host_ip = models.GenericIPAddressField(default="0.0.0.0")  # noqa: S104 - user-configurable bind address
    game_port = models.PositiveIntegerField()
    extra_ports = models.JSONField(default=list, blank=True)
    network = models.CharField(max_length=100, blank=True, help_text="Blank = agent default instance network")
    rcon_password = EncryptedTextField(default=_rcon_password)

    # --- lifecycle / observed state ------------------------------------------------------
    desired_state = models.CharField(max_length=10, choices=DesiredState.choices, default=DesiredState.STOPPED)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED)
    status_detail = models.CharField(max_length=500, blank=True)
    container_state = models.CharField(max_length=20, blank=True)
    health = models.CharField(max_length=20, blank=True)
    container_spec_hash = models.CharField(max_length=32, blank=True)
    last_exit_code = models.IntegerField(null=True, blank=True)
    oom_killed = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    installed_at = models.DateTimeField(null=True, blank=True, help_text="First time the server reached readiness")
    status_updated_at = models.DateTimeField(null=True, blank=True)
    config_revision = models.PositiveIntegerField(default=1)
    applied_revision = models.PositiveIntegerField(default=0)
    active_job = models.ForeignKey("jobs.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Set by `manage.py host_shutdown` (the Unraid stopping_docker hook) on a running server it
    # stops because the host is about to stop Docker. desired_state stays "running"; after the
    # next start of the panel the scheduler tick starts these servers again, one per minute
    # (scheduling.resume_flagged_servers). An explicit start, stop, kill, archive or delete
    # clears it. resume_flagged_at is when it was set: nothing is resumed by a panel process
    # that was already running then (the host is still shutting down). db_default: after a
    # rollback to an older image the column is still there and must not break new rows.
    resume_after_boot = models.BooleanField(default=False, db_default=False)
    resume_flagged_at = models.DateTimeField(null=True, blank=True)

    archived_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["node", "game_port"], name="unique_game_port_per_node"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_modpack(self) -> bool:
        return self.server_type in MODPACK_TYPES

    @property
    def content_kind(self) -> str:
        """Which add-on ecosystem applies: 'plugins', 'mods' or 'none'."""
        if self.server_type in PLUGIN_TYPES:
            return "plugins"
        if self.server_type in MOD_TYPES or self.server_type in MODPACK_TYPES:
            return "mods"
        return "none"

    @property
    def effective_loader(self) -> str:
        if self.server_type in MODPACK_TYPES:
            return self.modpack_loader
        return self.server_type.lower()

    @property
    def container_memory_mb(self) -> int:
        # Off-heap memory (metaspace, threads, GC, direct buffers) grows with mod count: a
        # 311-mod Forge pack used ~1.3 GB above a 6 GB heap in testing, so leave generous room.
        if self.memory_overhead_mb:
            overhead = self.memory_overhead_mb
        elif self.content_kind == "mods":
            overhead = max(1536, int(self.memory_mb * 0.4))
        else:
            overhead = max(768, int(self.memory_mb * 0.3))
        return self.memory_mb + overhead


class Schedule(models.Model):
    """Cron-based automation (restart, backup, command, start/stop)."""

    ACTIONS = ("restart", "start", "stop", "backup", "snapshot", "command")

    instance = models.ForeignKey(Instance, on_delete=models.CASCADE, related_name="schedules")
    name = models.CharField(max_length=80)
    action = models.CharField(max_length=20)
    cron = models.CharField(max_length=100)
    payload = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    only_when_running = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["instance", "name"]
