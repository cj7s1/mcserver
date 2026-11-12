"""What the bot process tells the panel about itself (in the shared cache), and how the panel wakes it.

The bot process writes ``status()`` every minute and on every change; the web process reads it for the settings
page. A status older than STALE_SECONDS means the process is not running (disabled, or it stopped).

``wake()`` touches the file the supervisor watches (``MCPANEL_DISCORD_WAKE``): it starts the idle bot process at
once instead of at its next periodic try (backend/container/supervise.py).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from django.core.cache import cache

STATUS_KEY = "discordbot:status"
STALE_SECONDS = 150


def write_status(**fields: Any) -> None:
    current = cache.get(STATUS_KEY) or {}
    current.update(fields)
    current["updated_at"] = time.time()
    cache.set(STATUS_KEY, current, 24 * 3600)


def read_status() -> dict[str, Any]:
    data = dict(cache.get(STATUS_KEY) or {})
    if data and time.time() - float(data.get("updated_at") or 0) > STALE_SECONDS:
        data["stale"] = True
    return data


def clear_status() -> None:
    cache.delete(STATUS_KEY)


def wake_path() -> Path | None:
    raw = os.environ.get("MCPANEL_DISCORD_WAKE", "")
    return Path(raw) if raw else None


def wake() -> bool:
    """Ask the supervisor to (re)start the bot process now. False outside the panel container."""
    path = wake_path()
    if path is None:
        return False
    try:
        path.touch()
        os.utime(path)
    except OSError:
        return False
    return True
