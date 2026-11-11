"""The runtime profiles this agent has, and how a spec or a container selects one.

Registered: Minecraft (``minecraft.py``) and Factorio (``factorio.py``). A spec without ``game`` (every panel
before multi-game support, and every Minecraft spec of a current one) is Minecraft; a container
without the ``io.mcpanel.game`` label is Minecraft as well, so existing containers keep their
labels and their spec-hash label unchanged.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

from ..errors import AgentError
from . import factorio, minecraft
from .base import AGENT_SIDE_HEALTH, IMPLEMENTED_HEALTH, Health, Mount, RuntimeProfile, UserModel

if TYPE_CHECKING:
    from ..config import Settings
    from ..schemas import InstanceSpec

__all__ = [
    "AGENT_SIDE_HEALTH",
    "DEFAULT_GAME",
    "LABEL_GAME",
    "LABEL_VARIANT",
    "Health",
    "Mount",
    "RuntimeProfile",
    "UserModel",
    "all_allowed_images",
    "allowed_images",
    "for_labels",
    "for_spec",
    "get",
    "info",
    "labels_for",
    "register",
]

DEFAULT_GAME = minecraft.GAME
LABEL_GAME = "io.mcpanel.game"
LABEL_VARIANT = "io.mcpanel.variant"
_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")

# (game, variant) -> profile; a variant of None is the profile for a spec without a variant.
PROFILES: dict[tuple[str, str | None], RuntimeProfile] = {}


def register(profile: RuntimeProfile) -> RuntimeProfile:
    """Add a profile (at import time; tests may add their own)."""
    if not _ID_RE.match(profile.game) or any(not _ID_RE.match(v) for v in profile.variants):
        raise ValueError(f"Invalid game or variant id in profile {profile.game!r}")
    from ..drivers import FACTORIES

    if profile.control not in FACTORIES:
        raise ValueError(f"Profile {profile.game}: unknown control driver {profile.control!r}")
    if profile.health.kind not in IMPLEMENTED_HEALTH:
        raise ValueError(
            f"Profile {profile.game}: health check kind {profile.health.kind!r} is not implemented"
        )
    if profile.health.kind == "cmd" and not profile.health.argv:
        raise ValueError(f"Profile {profile.game}: a cmd health check needs an argv")
    if profile.health.kind == "tcp" and not (profile.health.port and 1 <= profile.health.port <= 65535):
        raise ValueError(f"Profile {profile.game}: a tcp health check needs a port")
    patterns = list(profile.env_values.values()) + list(profile.log_redact_patterns)
    if profile.health.kind == "log":
        if not profile.health.pattern:
            raise ValueError(f"Profile {profile.game}: a log health check needs a pattern")
        patterns.append(profile.health.pattern)
    if profile.tag_pattern is not None:
        patterns.append(profile.tag_pattern)
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"Profile {profile.game}: invalid regular expression {pattern!r}: {exc}"
            ) from exc
    for path in (*profile.volume_tmpfs, *(m.container_path for m in profile.mounts)):
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"Profile {profile.game}: invalid container path {path!r}")
    for rel in profile.default_backup_excludes:
        if rel.startswith("/") or ".." in rel.split("/") or not rel:
            raise ValueError(f"Profile {profile.game}: invalid backup exclude {rel!r}")
    for mount in profile.mounts:
        parts = mount.subdir.split("/")
        if mount.subdir.startswith("/") or ".." in parts or not mount.container_path.startswith("/"):
            raise ValueError(f"Profile {profile.game}: invalid mount {mount}")
    if profile.user.kind == "drop_privileges_env" and not (profile.user.uid_env and profile.user.gid_env):
        raise ValueError(f"Profile {profile.game}: drop_privileges_env needs uid_env and gid_env")
    keys = [(profile.game, v) for v in profile.variants] or [(profile.game, None)]
    for key in keys:
        PROFILES[key] = profile
    return profile


register(minecraft.PROFILE)
register(factorio.PROFILE)


def get(game: str | None, variant: str | None = None) -> RuntimeProfile | None:
    return PROFILES.get((game or DEFAULT_GAME, variant or None))


def for_spec(spec: InstanceSpec) -> RuntimeProfile:
    """The profile a spec asks for; 400 ``unknown_game`` when this agent has none."""
    profile = get(spec.game, spec.variant)
    if profile is None:
        what = spec.game if spec.variant is None else f"{spec.game} ({spec.variant})"
        raise AgentError(f"This agent has no runtime profile for the game {what}", "unknown_game", 400)
    return profile


def for_labels(labels: dict[str, str] | None) -> RuntimeProfile | None:
    """The profile a container was created with (no game label: Minecraft); None when this agent
    does not know it (created by a newer agent, before a rollback)."""
    labels = labels or {}
    return get(labels.get(LABEL_GAME) or DEFAULT_GAME, labels.get(LABEL_VARIANT) or None)


def labels_for(spec: InstanceSpec) -> dict[str, str]:
    """``io.mcpanel.game`` / ``io.mcpanel.variant`` for a container of this spec: none for a
    Minecraft spec, so its labels stay what they always were."""
    if (spec.game or DEFAULT_GAME) == DEFAULT_GAME and spec.variant is None:
        return {}
    labels = {LABEL_GAME: spec.game}
    if spec.variant is not None:
        labels[LABEL_VARIANT] = spec.variant
    return labels


def _env_name(game: str) -> str:
    return f"AGENT_GAME_{game.upper().replace('-', '_')}_IMAGES"


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("["):
        loaded = json.loads(value)
        return [str(v).strip() for v in loaded if str(v).strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


def allowed_images(settings: Settings, profile: RuntimeProfile) -> list[str]:
    """The image repositories a spec of this profile may use.

    Minecraft: ``AGENT_ALLOWED_IMAGES`` (unchanged meaning). Another game: ``AGENT_GAME_<ID>_IMAGES``
    (CSV or a JSON list) when it is set, else the profile's own list.
    """
    if profile.game == DEFAULT_GAME:
        return list(settings.allowed_images)
    raw = os.environ.get(_env_name(profile.game))
    return list(profile.images) if raw is None else _parse_list(raw)


def all_profiles() -> list[RuntimeProfile]:
    seen: list[RuntimeProfile] = []
    for profile in PROFILES.values():
        if profile not in seen:
            seen.append(profile)
    return seen


def all_allowed_images(settings: Settings) -> set[str]:
    """Every repository any profile allows (superseded-image cleanup)."""
    out: set[str] = set()
    for profile in all_profiles():
        out.update(allowed_images(settings, profile))
    return out


def info(settings: Settings) -> dict[str, Any]:
    """``GET /v1/info`` → ``games``: {game: {variants, images, control, ports, never_publish,
    health, install_retry_exits}} (``ports``: the [container port, protocol] pairs a spec may
    publish; ``never_publish``: [port, protocol or null]; ``health``: the readiness probe kind)."""
    games: dict[str, Any] = {}
    for profile in all_profiles():
        entry = games.setdefault(
            profile.game,
            {
                "variants": [],
                "images": allowed_images(settings, profile),
                "control": profile.control,
                "ports": [[port, proto] for port, proto in profile.container_ports],
                "never_publish": [[port, proto] for port, proto in profile.never_publish],
                "health": profile.health.kind,
                "install_retry_exits": list(profile.install_retry_exits),
            },
        )
        for variant in profile.variants:
            if variant not in entry["variants"]:
                entry["variants"].append(variant)
    return games
