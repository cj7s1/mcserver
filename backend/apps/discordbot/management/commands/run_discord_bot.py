"""Run the Discord bot (B3) while its settings say so; the panel container's supervisor runs this.

Exit status 78 means "nothing to do now" (the bot is off, has no token or guild, Discord refused the token, or
the settings changed): the supervisor keeps the process idle and tries again later, or at once when the panel's
settings page wakes it (backend/container/supervise.py). The token never appears in the output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Connect the Discord bot while it is enabled (exit 78 when there is nothing to do)."

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.discordbot import bot

        # discord.py logs the gateway's events at INFO; the panel keeps warnings only (no token is ever logged)
        logging.getLogger("discord").setLevel(logging.WARNING)
        code = asyncio.run(bot.run())
        raise SystemExit(code)
