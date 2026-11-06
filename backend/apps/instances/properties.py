"""server.properties parsing/serialisation that preserves comments and ordering,
plus a typed schema for the specialised editor."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# java.util.Properties semantics (Properties.load / LineReader / loadConvert), which is how
# Minecraft and itzg's mc-image-helper read server.properties: only \n, \r and \r\n end a
# line; " ", \t and \f are whitespace; a line ending in an odd number of backslashes
# continues on the next one (whose leading whitespace is skipped); "#" or "!" as the first
# non-blank character starts a comment (comments never continue); the key ends at the first
# unescaped "=", ":" or whitespace; keys and values are unescaped (\uXXXX, \t, \n, \r, \f,
# any other \x is x). Parsing differently from Java would let a crafted file or value hide a
# key from the managed-key guard (e.g. "rcon\.password" or "server\u002dport").
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_WS = " \t\f"
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
                    pass  # Java rejects the whole file here; stay lenient for display
            out.append({"n": "\n", "t": "\t", "r": "\r", "f": "\f"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_char(ch: str) -> str:
    special = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\f": "\\f"}
    if ch in special:
        return special[ch]
    # other control characters and Unicode line/paragraph separators (U+0085, U+2028, U+2029):
    # Java does not treat them as line ends, but editors and other tools do
    if unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
        return f"\\u{ord(ch):04x}"
    return ch


def _escape_value(value: str) -> str:
    escaped = "".join(_escape_char(ch) for ch in value)
    # Java strips leading whitespace from values; keep it by escaping
    return "\\" + escaped if escaped[:1] == " " else escaped


def _escape_key(key: str) -> str:
    return "".join("\\" + ch if ch in " =:#!" else _escape_char(ch) for ch in key)


def _format(key: str, value: str) -> str:
    return f"{_escape_key(key)}={_escape_value(value)}"


def _physical_lines(text: str) -> list[str]:
    lines = _LINE_BREAK.split(text)
    if lines and lines[-1] == "":
        lines.pop()  # the text ended with a line break
    return lines


def _continues(line: str) -> bool:
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _logical_lines(lines: list[str]) -> list[tuple[int, int, str | None]]:
    """(first, end) physical line indices of each logical line with its text (continuations
    joined), or None for blank and comment lines. Like Java's LineReader, blank and comment
    lines are skipped while the logical line is still empty, even right after a line that
    held only a continuation backslash."""
    out: list[tuple[int, int, str | None]] = []
    buf = ""
    start: int | None = None  # first physical line of the logical line being read
    for i, raw in enumerate(lines):
        line = raw.lstrip(_WS)
        if not buf:
            if not line or line[0] in "#!":
                if start is None:
                    out.append((i, i + 1, None))
                continue
            if start is None:
                start = i
        buf += line
        if _continues(buf):
            buf = buf[:-1]  # the backslash at the end of a line is not part of it
            if start is None:
                start = i
            continue
        out.append((start if start is not None else i, i + 1, buf))
        buf, start = "", None
    if start is not None:  # continuation at end of file: Java drops the backslash
        out.append((start, len(lines), buf or None))
    return out


def _split(logical: str) -> tuple[str, str]:
    """Raw (still escaped) key and value of a logical line, exactly as Properties.load splits it."""
    key_len, value_start, has_sep, backslash = 0, len(logical), False, False
    while key_len < len(logical):
        ch = logical[key_len]
        if ch in "=:" and not backslash:
            value_start, has_sep = key_len + 1, True
            break
        if ch in _WS and not backslash:
            value_start = key_len + 1
            break
        backslash = ch == "\\" and not backslash
        key_len += 1
    while value_start < len(logical):
        ch = logical[value_start]
        if ch not in _WS:
            if not has_sep and ch in "=:":
                has_sep = True
            else:
                break
        value_start += 1
    return logical[:key_len], logical[value_start:]


def _key_of(logical: str) -> str:
    return _unescape(_split(logical)[0])


def parse(text: str) -> dict[str, str]:
    """Keys and values as java.util.Properties (and so Minecraft) reads them."""
    result: dict[str, str] = {}
    for _start, _end, logical in _logical_lines(_physical_lines(text)):
        if logical is not None:
            raw_key, raw_value = _split(logical)
            result[_unescape(raw_key)] = _unescape(raw_value)
    return result


def update(text: str, changes: dict[str, str | None]) -> str:
    """Apply changes (None deletes) while keeping comments, order and unknown keys."""
    lines = _physical_lines(text)
    groups = _logical_lines(lines)
    seen: set[str] = set()
    out: list[str] = []
    for index, (start, end, logical) in enumerate(groups):
        key = _key_of(logical) if logical is not None else None
        if key is not None and key in changes:
            value = changes[key]
            # Java Properties (and parse()) use the *last* occurrence of a duplicated key, so
            # write the new value at the first occurrence and drop any later duplicates.
            if value is not None and key not in seen:
                out.append(_format(key, str(value)))
            seen.add(key)
            continue
        chunk = lines[start:end]
        if index == len(groups) - 1 and logical is not None and end == len(lines) and _continues(chunk[-1]):
            # a dangling continuation at the end of the file would swallow appended keys;
            # Java ignores that backslash at EOF anyway
            chunk = [*chunk[:-1], chunk[-1][:-1]]
        out.extend(chunk)
    for key, value in changes.items():
        if value is not None and key not in seen:
            out.append(_format(key, str(value)))
    return "\n".join(out) + "\n"


def render(values: dict[str, Any]) -> str:
    lines = ["#Minecraft server properties", "#Initial values written by MCPanel"]
    for key, value in values.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(_format(key, str(value)))
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
        # Property names are plain words (letters, digits, ".", "_", "-"): no escapes, separators,
        # whitespace or line breaks, so the name the guard below sees is exactly the key Java reads.
        if not isinstance(key, str) or len(key) > 100 or not _KEY_RE.fullmatch(key):
            raise ValueError(f"Invalid property name {key!r}")
        if _unescape(key).lower() in MANAGED_KEYS:
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
