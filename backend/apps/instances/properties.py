"""server.properties parsing/serialisation that preserves comments and ordering,
plus a typed schema for the specialised editor."""

from __future__ import annotations

from typing import Any


def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "u" and len(value) >= i + 6:
                try:
                    out.append(chr(int(value[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append({"n": "\n", "t": "\t", "r": "\r", "f": "\f"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _split(line: str) -> tuple[str, str] | None:
    stripped = line.lstrip()
    if not stripped or stripped[0] in "#!":
        return None
    for i, ch in enumerate(stripped):
        if ch in "=:" and (i == 0 or stripped[i - 1] != "\\"):
            return stripped[:i].strip(), stripped[i + 1 :].lstrip()
    return stripped.strip(), ""


def parse(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        kv = _split(line)
        if kv:
            result[kv[0]] = _unescape(kv[1])
    return result


def update(text: str, changes: dict[str, str | None]) -> str:
    """Apply changes (None deletes) while keeping comments, order and unknown keys."""
    lines = text.splitlines()
    remaining = dict(changes)
    out: list[str] = []
    for line in lines:
        kv = _split(line)
        if kv and kv[0] in remaining:
            value = remaining.pop(kv[0])
            if value is None:
                continue
            out.append(f"{kv[0]}={_escape_value(str(value))}")
        else:
            out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={_escape_value(str(value))}")
    return "\n".join(out) + "\n"


def render(values: dict[str, Any]) -> str:
    lines = ["#Minecraft server properties", "#Initial values written by MCPanel"]
    for key, value in values.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"{key}={_escape_value(str(value))}")
    return "\n".join(lines) + "\n"


# Keys the panel controls through the runtime (changing them in the file is reverted).
MANAGED_KEYS = {"server-port", "rcon.port", "enable-rcon", "rcon.password"}

# Typed schema for the friendly editor. Unknown keys still appear in the raw editor.
SCHEMA: list[dict[str, Any]] = [
    {"key": "motd", "type": "string", "label": "Message of the day", "group": "General", "restart": True},
    {"key": "max-players", "type": "int", "label": "Max players", "group": "General", "min": 1, "max": 10000, "restart": True},
    {
        "key": "difficulty",
        "type": "enum",
        "label": "Difficulty",
        "group": "Gameplay",
        "options": ["peaceful", "easy", "normal", "hard"],
        "restart": False,
    },
    {
        "key": "gamemode",
        "type": "enum",
        "label": "Default game mode",
        "group": "Gameplay",
        "options": ["survival", "creative", "adventure", "spectator"],
        "restart": True,
    },
    {"key": "force-gamemode", "type": "bool", "label": "Force game mode", "group": "Gameplay", "restart": True},
    {"key": "hardcore", "type": "bool", "label": "Hardcore", "group": "Gameplay", "restart": True},
    {"key": "pvp", "type": "bool", "label": "PvP", "group": "Gameplay", "restart": True},
    {"key": "allow-flight", "type": "bool", "label": "Allow flight", "group": "Gameplay", "restart": True},
    {"key": "allow-nether", "type": "bool", "label": "Allow the Nether", "group": "World", "restart": True},
    {
        "key": "spawn-protection",
        "type": "int",
        "label": "Spawn protection radius",
        "group": "World",
        "min": 0,
        "max": 1000,
        "restart": True,
    },
    {"key": "level-name", "type": "string", "label": "World folder name", "group": "World", "restart": True},
    {
        "key": "level-seed",
        "type": "string",
        "label": "World seed",
        "group": "World",
        "restart": True,
        "help": "Only used when a new world is generated.",
    },
    {
        "key": "level-type",
        "type": "string",
        "label": "World type",
        "group": "World",
        "restart": True,
        "help": "e.g. minecraft:normal, minecraft:flat, minecraft:large_biomes, minecraft:amplified",
    },
    {"key": "generate-structures", "type": "bool", "label": "Generate structures", "group": "World", "restart": True},
    {
        "key": "view-distance",
        "type": "int",
        "label": "View distance (chunks)",
        "group": "Performance",
        "min": 2,
        "max": 32,
        "restart": True,
    },
    {
        "key": "simulation-distance",
        "type": "int",
        "label": "Simulation distance (chunks)",
        "group": "Performance",
        "min": 2,
        "max": 32,
        "restart": True,
    },
    {
        "key": "max-tick-time",
        "type": "int",
        "label": "Watchdog max tick time (ms, -1 disables)",
        "group": "Performance",
        "min": -1,
        "restart": True,
    },
    {"key": "sync-chunk-writes", "type": "bool", "label": "Synchronous chunk writes", "group": "Performance", "restart": True},
    {
        "key": "online-mode",
        "type": "bool",
        "label": "Online mode (verify accounts with Mojang)",
        "group": "Security",
        "restart": True,
        "help": "Disabling allows unauthenticated (cracked) clients and impersonation. Keep enabled unless behind a proxy that authenticates.",
    },
    {"key": "white-list", "type": "bool", "label": "Whitelist enabled", "group": "Security", "restart": False},
    {
        "key": "enforce-whitelist",
        "type": "bool",
        "label": "Kick players not on the whitelist",
        "group": "Security",
        "restart": False,
    },
    {
        "key": "enforce-secure-profile",
        "type": "bool",
        "label": "Enforce secure chat profiles",
        "group": "Security",
        "restart": True,
    },
    {
        "key": "op-permission-level",
        "type": "int",
        "label": "Operator permission level",
        "group": "Security",
        "min": 1,
        "max": 4,
        "restart": True,
    },
    {
        "key": "player-idle-timeout",
        "type": "int",
        "label": "Idle kick timeout (minutes, 0 = off)",
        "group": "Players",
        "min": 0,
        "restart": True,
    },
    {"key": "enable-command-block", "type": "bool", "label": "Command blocks", "group": "Gameplay", "restart": True},
    {"key": "spawn-monsters", "type": "bool", "label": "Spawn monsters", "group": "Gameplay", "restart": True},
    {"key": "spawn-animals", "type": "bool", "label": "Spawn animals", "group": "Gameplay", "restart": True},
    {"key": "spawn-npcs", "type": "bool", "label": "Spawn villagers", "group": "Gameplay", "restart": True},
    {"key": "enable-status", "type": "bool", "label": "Show in server list (status)", "group": "Network", "restart": True},
    {"key": "hide-online-players", "type": "bool", "label": "Hide online player list", "group": "Network", "restart": True},
    {"key": "enable-query", "type": "bool", "label": "Enable GameSpy4 query", "group": "Network", "restart": True},
    {
        "key": "network-compression-threshold",
        "type": "int",
        "label": "Network compression threshold",
        "group": "Network",
        "min": -1,
        "restart": True,
    },
    {
        "key": "prevent-proxy-connections",
        "type": "bool",
        "label": "Prevent proxy/VPN connections",
        "group": "Network",
        "restart": True,
    },
    {"key": "resource-pack", "type": "string", "label": "Resource pack URL", "group": "Resource pack", "restart": True},
    {"key": "resource-pack-sha1", "type": "string", "label": "Resource pack SHA-1", "group": "Resource pack", "restart": True},
    {"key": "require-resource-pack", "type": "bool", "label": "Require resource pack", "group": "Resource pack", "restart": True},
]
SCHEMA_KEYS = {s["key"]: s for s in SCHEMA}


def validate_changes(changes: dict[str, Any]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key, value in changes.items():
        if not isinstance(key, str) or not key or any(c in key for c in "=:\n\r# ") or len(key) > 100:
            raise ValueError(f"Invalid property name {key!r}")
        if key in MANAGED_KEYS:
            raise ValueError(f"'{key}' is managed by MCPanel (change it in the Network settings instead)")
        if value is None:
            out[key] = None
            continue
        schema = SCHEMA_KEYS.get(key)
        if schema:
            if schema["type"] == "bool":
                if isinstance(value, bool):
                    value = "true" if value else "false"
                elif str(value).lower() not in ("true", "false"):
                    raise ValueError(f"{key} must be true or false")
                value = str(value).lower()
            elif schema["type"] == "int":
                try:
                    num = int(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a whole number") from exc
                if "min" in schema and num < schema["min"]:
                    raise ValueError(f"{key} must be at least {schema['min']}")
                if "max" in schema and num > schema["max"]:
                    raise ValueError(f"{key} must be at most {schema['max']}")
                value = str(num)
            elif schema["type"] == "enum" and str(value) not in schema["options"]:
                raise ValueError(f"{key} must be one of {', '.join(schema['options'])}")
        value = str(value)
        if len(value) > 4096:
            raise ValueError(f"{key} value is too long")
        out[key] = value
    return out
