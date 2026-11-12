"""Periodic housekeeping of the accounts app."""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

# failed sign-ins carry a user name the client chose: kept this long, then removed (D-3)
LOGIN_FAILED_RETENTION = timedelta(days=90)


@shared_task(name="apps.accounts.tasks.prune_login_failures", ignore_result=True)
def prune_login_failures() -> int:
    """Delete ``auth.login_failed`` audit rows older than LOGIN_FAILED_RETENTION; returns how many."""
    from apps.audit.models import AuditEvent

    deleted, _ = AuditEvent.objects.filter(
        action="auth.login_failed", created_at__lt=timezone.now() - LOGIN_FAILED_RETENTION
    ).delete()
    return deleted
