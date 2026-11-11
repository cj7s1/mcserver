"""Config file codecs: read a game's configuration file into flat values and write changes back.

Every codec keeps what it does not change (comments, order, unknown keys), so editing one setting
through the form never rewrites the rest of the file. Values are addressed by flat keys:

* ``properties`` (Java properties, Minecraft's server.properties): the property name.
* ``json``: dotted paths into nested objects (``visibility.public``); lists are values.
* ``ini``: ``section.key`` (keys before the first section: ``key``).
* ``kv``: ``key=value`` lines (Terraria's serverconfig.txt), ``#`` comments kept.

``validate_values`` checks values against a ConfigFile's field schema (apps/games/base.py) and
converts them to the field's type.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .base import ConfigFile

MASK = "********"
MAX_TEXT = 4096
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Codec(Protocol):
    name: str

    def parse(self, text: str) -> dict[str, Any]: ...

    def update(self, text: str, changes: dict[str, Any]) -> str: ...


# --- JSON ---------------------------------------------------------------------------------------
class JsonCodec:
    """Nested objects as dotted keys. A change of ``None`` removes the key."""

    name = "json"

    def load(self, text: str) -> dict[str, Any]:
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"The file is not valid JSON ({exc}); fix it in the file manager") from exc
        if not isinstance(data, dict):
            raise ValueError("The file does not hold a JSON object; fix it in the file manager")
        return data

    def parse(self, text: str) -> dict[str, Any]:
        out: dict[str, Any] = {}

        def walk(prefix: str, obj: dict[str, Any]) -> None:
            for key, value in obj.items():
                if not isinstance(key, str) or key.startswith("_comment"):
                    continue
                path = f"{prefix}{key}"
                if isinstance(value, dict):
                    walk(path + ".", value)
                else:
                    out[path] = value

        walk("", self.load(text))
        return out

    def update(self, text: str, changes: dict[str, Any]) -> str:
        data = self.load(text)
        for path, value in changes.items():
            parts = path.split(".")
            node = data
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    if value is None:
                        break
                    child = {}
                    node[part] = child
                node = child
            else:
                if value is None:
                    node.pop(parts[-1], None)
                else:
                    node[parts[-1]] = value
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    def render(self, values: dict[str, Any]) -> str:
        return self.update("", values)


# --- key=value ----------------------------------------------------------------------------------
_KV_LINE = re.compile(r"^\s*([^#;=\s][^=]*?)\s*=(.*)$")


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


class KeyValueCodec:
    """``key=value`` lines; ``#`` comments and unknown lines are kept, new keys are appended."""

    name = "kv"

    def parse(self, text: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for line in text.splitlines():
            m = _KV_LINE.match(line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(1).strip()] = m.group(2).strip()
        return out

    def update(self, text: str, changes: dict[str, Any]) -> str:
        lines = text.splitlines()
        pending = dict(changes)
        out: list[str] = []
        for line in lines:
            m = _KV_LINE.match(line)
            if m and not line.lstrip().startswith("#") and m.group(1).strip() in pending:
                value = pending.pop(m.group(1).strip())
                if value is not None:
                    out.append(f"{m.group(1).strip()}={_as_text(value)}")
                continue
            out.append(line)
        for key, value in pending.items():
            if value is not None:
                out.append(f"{key}={_as_text(value)}")
        return "\n".join(out) + "\n"

    def render(self, values: dict[str, Any]) -> str:
        return self.update("", values)


# --- INI ----------------------------------------------------------------------------------------
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


class IniCodec:
    """``[section]`` blocks of ``key = value``; keys as ``section.key``. Comments (``;``/``#``) and the
    order are kept; a new key goes to the end of its section (a new section to the end of the file)."""

    name = "ini"

    def parse(self, text: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        section = ""
        for line in text.splitlines():
            if m := _SECTION.match(line):
                section = m.group(1).strip()
                continue
            stripped = line.lstrip()
            if stripped.startswith(("#", ";")):
                continue
            m = _KV_LINE.match(line)
            if m:
                key = m.group(1).strip()
                out[f"{section}.{key}" if section else key] = m.group(2).strip()
        return out

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        if "." in path:
            section, key = path.split(".", 1)
            return section, key
        return "", path

    def update(self, text: str, changes: dict[str, Any]) -> str:
        pending: dict[tuple[str, str], Any] = {self._split(k): v for k, v in changes.items()}
        lines = text.splitlines()
        out: list[str] = []
        section = ""

        def flush(sec: str) -> None:
            for (s, key), value in list(pending.items()):
                if s == sec:
                    if value is not None:
                        out.append(f"{key} = {_as_text(value)}")
                    del pending[(s, key)]

        for line in lines:
            if m := _SECTION.match(line):
                # trailing blank lines of the section stay after the keys added to it
                blanks = []
                while out and not out[-1].strip():
                    blanks.append(out.pop())
                flush(section)
                out.extend(blanks)
                section = m.group(1).strip()
                out.append(line)
                continue
            m = _KV_LINE.match(line)
            if m and not line.lstrip().startswith(("#", ";")) and (section, m.group(1).strip()) in pending:
                value = pending.pop((section, m.group(1).strip()))
                if value is not None:
                    out.append(f"{m.group(1).strip()} = {_as_text(value)}")
                continue
            out.append(line)
        flush(section)
        for sec in sorted({s for s, _ in pending}):
            if not any(v is not None for (s, _), v in pending.items() if s == sec):
                continue
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{sec}]")
            flush(sec)
        return "\n".join(out) + "\n"

    def render(self, values: dict[str, Any]) -> str:
        return self.update("", values)


# --- Java properties (Minecraft) -----------------------------------------------------------------
class PropertiesCodec:
    """Wraps apps/instances/properties.py (Minecraft's server.properties, ISO-8859-1 safe)."""

    name = "java-properties"

    def parse(self, text: str) -> dict[str, Any]:
        from apps.instances import properties

        return dict(properties.parse(text))

    def update(self, text: str, changes: dict[str, Any]) -> str:
        from apps.instances import properties

        return properties.update(text, {k: (None if v is None else _as_text(v)) for k, v in changes.items()})

    def render(self, values: dict[str, Any]) -> str:
        from apps.instances import properties

        return properties.render({k: _as_text(v) for k, v in values.items()})


CODECS: dict[str, Any] = {
    "json": JsonCodec(),
    "kv": KeyValueCodec(),
    "ini": IniCodec(),
    "java-properties": PropertiesCodec(),
    "properties": PropertiesCodec(),
}


def get(name: str) -> Any:
    try:
        return CODECS[name]
    except KeyError:
        raise ValueError(f"Unknown config codec {name!r}") from None


# --- values against the schema --------------------------------------------------------------------
def coerce(field: dict[str, Any], value: Any, *, text_codec: bool = False) -> Any:
    """``value`` as the field's type (ValueError with a message for the user otherwise). With
    ``text_codec`` (ini, kv, properties) the result is what the file holds: text."""
    label = field.get("label") or field["key"]
    kind = field.get("type", "string")
    if value is None or (value == "" and field.get("nullable")):
        if not field.get("nullable") and kind != "string":
            raise ValueError(f"{label} needs a value")
        return None if field.get("nullable") else ""
    if kind == "bool":
        if isinstance(value, bool):
            result: Any = value
        elif str(value).strip().lower() in ("true", "false"):
            result = str(value).strip().lower() == "true"
        else:
            raise ValueError(f"{label} must be true or false")
    elif kind == "int":
        if isinstance(value, bool):
            raise ValueError(f"{label} must be a whole number")
        try:
            result = int(str(value).strip()) if not isinstance(value, int) else value
        except ValueError as exc:
            raise ValueError(f"{label} must be a whole number") from exc
        _bounds(field, label, result)
    elif kind == "number":
        if isinstance(value, bool):
            raise ValueError(f"{label} must be a number")
        try:
            result = float(value) if not isinstance(value, (int, float)) else value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a number") from exc
        _bounds(field, label, result)
    elif kind == "enum":
        options = [str(o) for o in field.get("options") or []]
        if str(value) not in options:
            raise ValueError(f"{label} must be one of {', '.join(options)}")
        result = str(value)
    elif kind == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(value) > 500:
            raise ValueError(f"{label} must be a list of texts")
        for item in value:
            _text(label, item, text_codec)
        result = list(value)
    else:
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError(f"{label} must be text")
        result = _text(label, str(value), text_codec)
        # a value that reaches a container's environment (Counter-Strike 2's start line is eval'd by its
        # image): the field's pattern, the same one the agent's profile checks
        if field.get("pattern") and not re.fullmatch(str(field["pattern"]), result):
            raise ValueError(f"{label}: {field.get('pattern_help') or 'contains characters that are not allowed'}")
    if text_codec and result is not None:
        return _as_text(result)
    return result


def _bounds(field: dict[str, Any], label: str, value: float) -> None:
    if "min" in field and value < field["min"]:
        raise ValueError(f"{label} must be at least {field['min']}")
    if "max" in field and value > field["max"]:
        raise ValueError(f"{label} must be at most {field['max']}")


def _text(label: str, value: str, single_line: bool) -> str:
    if len(value) > MAX_TEXT:
        raise ValueError(f"{label} is too long")
    if _CONTROL.search(value) or (single_line and ("\n" in value or "\r" in value)):
        raise ValueError(f"{label} contains characters that are not allowed")
    return value


def validate_values(cfg: ConfigFile, values: dict[str, Any]) -> dict[str, Any]:
    """Schema keys only, managed keys refused, each value converted to its field's type."""
    text_codec = cfg.codec != "json"
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key in cfg.managed_keys:
            raise ValueError(f"'{key}' is managed by MCPanel")
        field = cfg.field(str(key))
        if field is None:
            raise ValueError(f"'{key}' is not a setting of {cfg.label or cfg.path}; edit the file in the file manager")
        out[str(key)] = coerce(field, value, text_codec=text_codec)
    return out


def masked(cfg: ConfigFile, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """``values`` with every set secret (secret_keys, or fields marked ``secret``) as MASK."""
    secret = set(cfg.secret_keys) | {f["key"] for f in cfg.fields if f.get("secret")}
    shown = dict(values)
    redacted = []
    for key in sorted(secret):
        if shown.get(key) not in (None, ""):
            shown[key] = MASK
            redacted.append(key)
    return shown, redacted
