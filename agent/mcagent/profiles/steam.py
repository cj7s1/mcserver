"""Shared pieces of the profiles of the six Steam-era games (Palworld, Valheim, Project Zomboid,
HKMP, CS2, Rust): value patterns, the allow-list policy, defaults for 1:1 port variables and the
control secret.

Value patterns are regular expressions the whole value must match (``RuntimeProfile.env_values``).
They are deliberately narrow: several of these images put variables into shell lines (CS2's
``entry.sh`` runs its start line through ``eval``; Rust's start line and Palworld's/PZ's ``sed``
calls expand them unquoted), so a value never contains quotes, ``$``, backticks, backslashes, ``;``,
``|``, ``&``, ``<``/``>`` or control characters unless the probe showed the image only uses it quoted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..errors import AgentError

if TYPE_CHECKING:
    from ..schemas import InstanceSpec
    from .base import EnvPolicy

BOOL = r"true|false"
BOOL_ANY_CASE = r"[Tt]rue|[Ff]alse|TRUE|FALSE"
ZERO_ONE = r"0|1"
# a port 100-65535 (1:1 ports: the container port is the host port the panel chose)
PORT = r"[1-9][0-9]{2,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]"
# IANA time zone names (Europe/Berlin, America/Argentina/Buenos_Aires, Etc/GMT+5, UTC): no "..", no "/"
# at either end (an image links /usr/share/zoneinfo/$TZ)
TZ = r"[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9_+-]{1,32}){0,2}"
# passwords and tokens that end up in a URL path, a shell line or a sed expression
ALNUM_SECRET = r"[A-Za-z0-9]{16,64}"  # noqa: S105 - a value pattern
# Docker's stop timeout while a server has not become ready in its run (``RuntimeProfile.not_ready_stop_timeout``)
NOT_READY_STOP_TIMEOUT = 5


def allow_list(label: str, allowed: frozenset[str], why: str) -> EnvPolicy:
    """Only the variables the panel sets (``allowed``, compared in upper case); every other one is
    refused with ``why``."""

    def policy(key: str) -> str | None:
        if key.upper() in allowed:
            return None
        return f"Environment variable {key} is not allowed for {label} servers: {why}"

    return policy


def defaults(spec: InstanceSpec, values: Mapping[str, str]) -> dict[str, str]:
    """``values`` for the variables the spec does not set itself (port variables: the agent then
    knows the port the server listens on, and the spec's own value keeps its pattern check)."""
    return {key: value for key, value in values.items() if key not in spec.env}


def control_secret(spec: InstanceSpec, key: str) -> dict[str, str]:
    """``{key: spec.rcon_password}`` when the spec does not set ``key`` itself: the password the
    agent's driver uses (CS2's ``CS2_RCONPW``, Rust's ``RUST_RCON_PASSWORD``, ...) is then the
    panel's control secret. The injected value must match the variable's pattern like a spec's value
    (``docker_ops.build_create_kwargs`` checks it)."""
    return {} if key in spec.env else {key: spec.rcon_password}


def memory_gb(spec: InstanceSpec) -> int:
    return max(1, int(spec.memory_limit_mb) // 1024)


def refuse(message: str) -> AgentError:
    return AgentError(message, "env_not_allowed", 400)


__all__ = [
    "ALNUM_SECRET",
    "BOOL",
    "BOOL_ANY_CASE",
    "NOT_READY_STOP_TIMEOUT",
    "PORT",
    "TZ",
    "ZERO_ONE",
    "allow_list",
    "control_secret",
    "defaults",
    "memory_gb",
    "refuse",
]
