from __future__ import annotations

from typing import Any

from django.db import models

from .crypto import EncryptedTextField


class SystemSetting(models.Model):
    """Key/value panel settings editable from the System Settings screen.

    Secrets (provider API keys) go in ``secret`` which is encrypted at rest and
    never returned by the API.
    """

    key = models.CharField(max_length=100, primary_key=True)
    value = models.JSONField(default=None, null=True, blank=True)
    secret = EncryptedTextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    DEFAULTS: dict[str, Any] = {
        "runtime_image_repository": "itzg/minecraft-server",
        "runtime_image_channel": "stable",  # stable | latest | pinned:<version>
        "default_memory_mb": 4096,
        "port_range_start": None,  # falls back to settings.MCPANEL_PORT_RANGE_START
        "port_range_end": None,
        "default_bind_ip": "0.0.0.0",  # noqa: S104 - default for game ports (configurable)
        "backup_default_retention": {"keep_last": 10, "keep_daily": 7, "keep_weekly": 4},
        "pre_restore_backup": True,
        "pre_update_backup": True,
        "public_address": "",
        "lan_address": "",
        "timezone": "",
    }

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        row = cls.objects.filter(key=key).first()
        if row is not None and row.value is not None:
            return row.value
        if default is not None:
            return default
        return cls.DEFAULTS.get(key)

    @classmethod
    def set(cls, key: str, value: Any) -> None:
        cls.objects.update_or_create(key=key, defaults={"value": value})

    @classmethod
    def get_secret(cls, key: str) -> str:
        row = cls.objects.filter(key=key).first()
        return row.secret if row and row.secret else ""

    @classmethod
    def set_secret(cls, key: str, value: str) -> None:
        cls.objects.update_or_create(key=key, defaults={"secret": value})
