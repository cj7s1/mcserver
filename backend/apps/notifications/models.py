from __future__ import annotations

from django.db import models

from apps.core.crypto import EncryptedTextField

EVENTS = {
    "instance.failed": "Server failed or crashed",
    "instance.crash_loop_stopped": "Server stopped after repeated crashes",
    "instance.running": "Server became ready",
    "instance.stopped": "Server stopped",
    "job.failed": "Any background job failed",
    "backup.failed": "Backup failed",
    "backup.succeeded": "Backup completed",
    "modpack.update_available": "Modpack update available (once per new release)",
}


class NotificationChannel(models.Model):
    KINDS = (("discord", "Discord webhook"), ("webhook", "Generic JSON webhook"))

    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=20, choices=KINDS)
    url = EncryptedTextField()
    events = models.JSONField(default=list)
    instance = models.ForeignKey(
        "instances.Instance",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notification_channels",
        help_text="Empty = all servers",
    )
    enabled = models.BooleanField(default=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
