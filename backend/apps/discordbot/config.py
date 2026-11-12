"""The Discord bot's settings (B3 of the feature plan of 2026-11-12), kept in the system settings table.

* ``discord_enabled`` (bool), ``discord_guild_id`` (the one Discord server the bot answers in),
  ``discord_roles`` (role ids that may start and stop servers unless a server names its own),
  ``discord_channel_id`` (optional: where starts and stops are announced);
* the bot token: a secret (``SystemSetting.set_secret``, encrypted like the other secrets), write-only in the API,
  never logged, never in audit details or error texts.

Per server (``Instance``): ``discord_available`` (default off: the bot neither lists nor accepts it),
``discord_roles`` (own roles; empty = the default roles), ``discord_share_password`` (``/join`` shows the join
password).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from apps.core.models import SystemSetting

TOKEN_KEY = "discord_bot_token"
SNOWFLAKE = re.compile(r"^[0-9]{15,22}$")  # Discord ids


@dataclass(frozen=True)
class BotSettings:
    enabled: bool = False
    guild_id: str = ""
    roles: tuple[str, ...] = ()
    channel_id: str = ""
    token: str = field(default="", repr=False)

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.guild_id)

    def fingerprint(self) -> str:
        """What a running bot must reconnect for when it changes (the token only as a hash)."""
        token = hashlib.sha256(self.token.encode()).hexdigest()[:16] if self.token else ""
        return f"{self.enabled}:{self.guild_id}:{token}"


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if SNOWFLAKE.match(str(v)))


def load() -> BotSettings:
    token = SystemSetting.get_secret(TOKEN_KEY)
    return BotSettings(
        enabled=bool(SystemSetting.get("discord_enabled") or False),
        guild_id=str(SystemSetting.get("discord_guild_id") or ""),
        roles=_ids(SystemSetting.get("discord_roles") or []),
        channel_id=str(SystemSetting.get("discord_channel_id") or ""),
        token=str(token or ""),  # (an unreadable secret is falsy: no token)
    )


def invite_url(application_id: str | None, *, announcements: bool) -> str | None:
    """The link that adds the bot to a Discord server: the ``bot`` and ``applications.commands`` scopes; the
    permissions View Channel + Send Messages (3072) only when it announces in a channel, else none."""
    if not application_id or not SNOWFLAKE.match(str(application_id)):
        return None
    permissions = 3072 if announcements else 0
    return (
        f"https://discord.com/oauth2/authorize?client_id={application_id}&scope=bot+applications.commands"
        f"&permissions={permissions}"
    )
