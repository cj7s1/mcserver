"""``GET`` / ``PATCH /api/system/discord``: the bot's settings and state (B3; permission ``system.settings``)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.models import SystemSetting
from apps.core.permissions import require

from . import config, state


class DiscordSettingsSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    guild_id = serializers.RegexField(r"^([0-9]{15,22})?$", required=False, allow_blank=True)
    roles = serializers.ListField(child=serializers.RegexField(r"^[0-9]{15,22}$"), required=False, max_length=50)
    channel_id = serializers.RegexField(r"^([0-9]{15,22})?$", required=False, allow_blank=True)
    # write-only; "" removes it
    token = serializers.CharField(required=False, allow_blank=True, max_length=200, write_only=True, trim_whitespace=True)


def describe() -> dict[str, Any]:
    settings = config.load()
    live = state.read_status()
    if not settings.enabled:
        current = "disabled"
    elif not settings.token:
        current = "no_token"
    elif not settings.guild_id:
        current = "no_guild"
    elif live.get("stale"):
        # the process tries again every 5 minutes after an error (a refused token, no access to the guild)
        current = "error" if live.get("state") == "error" else "not_running"
    elif live.get("connected"):
        current = "connected"
    else:
        # "connecting", "error", "idle"; nothing yet: the process has not started (or runs outside the supervisor)
        current = str(live.get("state") or "not_running")
    application_id = str(live.get("application_id") or "")
    return {
        "enabled": settings.enabled,
        "guild_id": settings.guild_id,
        "roles": list(settings.roles),
        "channel_id": settings.channel_id,
        "token_set": bool(settings.token),
        "status": {
            "state": current,
            "connected": bool(live.get("connected")) and not live.get("stale"),
            "last_error": str(live.get("last_error") or ""),
            "updated_at": live.get("updated_at"),
            "bot_name": str(live.get("bot_name") or ""),
            "guild": live.get("guild"),
            "guild_roles": list(live.get("roles") or []),
            "guild_channels": list(live.get("channels") or []),
            "application_id": application_id or None,
            "invite_url": config.invite_url(application_id, announcements=bool(settings.channel_id)),
        },
    }


class DiscordSettingsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        require(request.user, "system.settings")
        return Response(describe())

    @extend_schema(request=DiscordSettingsSerializer, responses={200: dict})
    def patch(self, request: Request) -> Response:
        require(request.user, "system.settings")
        ser = DiscordSettingsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        changed: dict[str, Any] = {}
        for key, setting in (
            ("enabled", "discord_enabled"),
            ("guild_id", "discord_guild_id"),
            ("roles", "discord_roles"),
            ("channel_id", "discord_channel_id"),
        ):
            if key in data:
                SystemSetting.set(setting, data[key])
                changed[key] = data[key]
        if "token" in data:
            SystemSetting.set_secret(config.TOKEN_KEY, data["token"])
            changed["bot_credential"] = "set" if data["token"] else "removed"  # never the token itself
        audit.record("system.discord_updated", request=request, details=changed)
        if config.load().usable:
            state.wake()  # the supervisor starts the bot now instead of at its next try
        return Response(describe())
