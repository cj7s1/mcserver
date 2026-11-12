"""The Discord bot's service account (review F-15 of 2026-11-12).

The account is found by the id kept in the system setting ``discord_service_user_id``, never by its name alone:
a person's account that happens to be called "discord-bot" (made before this version) is never taken over (the
bot then uses "discord-bot-2", ...). The names ``discord-bot`` and ``discord-bot-<n>`` are reserved: creating,
inviting or renaming an account to one of them is refused, and the service account itself cannot be changed or
given a sign-in link.
"""

from __future__ import annotations

import re
from typing import Any

from rest_framework import serializers

SERVICE_ACCOUNT_KEY = "discord_service_user_id"
SERVICE_USERNAME = "discord-bot"
RESERVED_USERNAME = re.compile(r"^discord-bot(-\d+)?$", re.IGNORECASE)
RESERVED_MESSAGE = "This name is reserved for the panel's Discord bot"


def service_account_id() -> int | None:
    from apps.core.models import SystemSetting

    value = SystemSetting.get(SERVICE_ACCOUNT_KEY)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def is_service_account(user: Any) -> bool:
    return user is not None and getattr(user, "pk", None) is not None and user.pk == service_account_id()


def validate_username(value: str, current: str | None = None) -> str:
    """Refuse a reserved name (keeping an account's own unchanged name is fine)."""
    if RESERVED_USERNAME.match(str(value or "")) and value != current:
        raise serializers.ValidationError(RESERVED_MESSAGE)
    return value


def service_account() -> Any:
    """The account the Discord bot acts as (the audit log's actor): inactive, without a usable password."""
    from django.db import transaction

    from apps.accounts.models import User
    from apps.core.models import SystemSetting

    user = User.objects.filter(pk=service_account_id()).first() if service_account_id() else None
    if user is None:
        with transaction.atomic():
            taken = {
                u.lower()
                for u in User.objects.filter(username__iregex=r"^discord-bot(-[0-9]+)?$").values_list("username", flat=True)
            }
            name = next(n for n in (SERVICE_USERNAME, *(f"{SERVICE_USERNAME}-{i}" for i in range(2, 1000))) if n not in taken)
            user = User(username=name, is_active=False)
            user.set_unusable_password()
            user.save()
            SystemSetting.set(SERVICE_ACCOUNT_KEY, user.pk)
    elif user.is_active or user.has_usable_password():
        user.set_unusable_password()
        user.is_active = False
        user.save(update_fields=["password", "is_active"])
    return user
