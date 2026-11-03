from __future__ import annotations

from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """Append-only audit trail of security-relevant and state-changing actions."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    actor_username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=50, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    instance = models.ForeignKey(
        "instances.Instance", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    success = models.BooleanField(default=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["instance", "-created_at"])]
