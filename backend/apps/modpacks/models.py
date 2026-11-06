from __future__ import annotations

from django.db import models


class UpdateCheckStatus(models.TextChoices):
    OK = "ok", "Checked"
    UNAVAILABLE = "unavailable", "Check not possible"
    ERROR = "error", "Check failed"


class ModpackUpdateCheck(models.Model):
    """Latest provider release compatible with a modpack server's pin (apps/modpacks/updates.py).

    ``latest_*`` and ``update_available`` describe the last *successful* check and are only
    valid for ``checked_version_id`` (the pin at that time); a later failed check keeps them.
    """

    instance = models.OneToOneField(
        "instances.Instance", on_delete=models.CASCADE, primary_key=True, related_name="modpack_update_check"
    )
    status = models.CharField(max_length=12, choices=UpdateCheckStatus.choices, default=UpdateCheckStatus.OK)
    detail = models.CharField(max_length=500, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True, help_text="Last attempt")
    last_success_at = models.DateTimeField(null=True, blank=True)
    checked_version_id = models.CharField(max_length=64, blank=True, help_text="Pinned release at the last success")
    latest_version_id = models.CharField(max_length=64, blank=True)
    latest_version_name = models.CharField(max_length=200, blank=True)
    latest_published = models.DateTimeField(null=True, blank=True)
    latest_minecraft_version = models.CharField(max_length=40, blank=True)
    update_available = models.BooleanField(default=False)
    notified_version_id = models.CharField(max_length=64, blank=True, help_text="Release last announced")

    def __str__(self) -> str:
        return f"update check for {self.instance_id}: {self.status}"
