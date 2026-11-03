"""Audit logging helper. Never pass secrets in ``details``."""

from __future__ import annotations

import logging
from typing import Any

from .models import AuditEvent

logger = logging.getLogger("mcpanel.audit")

_REDACT_KEYS = {"password", "token", "secret", "api_key", "rcon_password", "new_password", "old_password"}


def _redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("[redacted]" if any(r in k.lower() for r in _REDACT_KEYS) else _redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v) for v in data]
    return data


def record(
    action: str,
    *,
    actor=None,  # type: ignore[no-untyped-def]
    request=None,  # type: ignore[no-untyped-def]
    instance=None,  # type: ignore[no-untyped-def]
    target=None,  # type: ignore[no-untyped-def]
    target_type: str = "",
    target_id: str = "",
    success: bool = True,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    if request is not None and actor is None and getattr(request, "user", None) is not None:
        actor = request.user if request.user.is_authenticated else None
    if target is not None:
        target_type = target_type or target._meta.model_name
        target_id = target_id or str(target.pk)
    ip = request.META.get("REMOTE_ADDR") if request is not None else None
    event = AuditEvent.objects.create(
        actor=actor,
        actor_username=getattr(actor, "username", "") or "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        instance=instance,
        ip_address=ip,
        success=success,
        details=_redact(details or {}),
    )
    logger.info("audit %s", action, extra={"actor": event.actor_username, "target": f"{target_type}:{target_id}"})
    return event
