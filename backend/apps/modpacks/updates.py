"""Periodic "modpack update available" checks.

For every (non-archived) modpack server the provider is asked for releases that match the
pinned Minecraft version and mod loader; the newest one is recorded in ``ModpackUpdateCheck``
and announced once through the ``modpack.update_available`` notification event.

Gentle with provider APIs: the beat task runs hourly but only checks servers whose last
attempt is older than ``CHECK_INTERVAL`` (6 h), at most ``MAX_PER_TICK`` per tick, one
Celery task per server staggered ``STAGGER_SECONDS`` apart and ordered by project so servers
running the same pack hit the adapters' short-lived release cache. Provider calls use the
adapters' 20 s timeout. Failures are recorded on the check, never raised.

CurseForge without an API key cannot list files, so such servers are recorded as
"update check unavailable" instead of guessing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core.errors import ApiError
from apps.instances.models import MODPACK_TYPES, Instance

from .models import ModpackUpdateCheck, UpdateCheckStatus
from .providers import curseforge_key, get_provider

log = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(hours=6)
STAGGER_SECONDS = 20
MAX_PER_TICK = 120
MANUAL_COOLDOWN = timedelta(seconds=30)
KEYLESS_CURSEFORGE = "Update check unavailable without a CurseForge API key"
NO_PROJECT = "The modpack project is not recorded yet (it is recorded after the first successful install)"
STABLE_TYPES = (None, "release")


class CheckUnavailable(Exception):
    pass


# --- pure helpers ---------------------------------------------------------------------
def published_at(value: Any) -> datetime | None:
    """Normalise provider dates (ISO strings for Modrinth/CurseForge, Unix seconds for FTB)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = parse_datetime(value)
        except ValueError:
            return None
        if parsed is not None and timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, UTC)
        return parsed
    return None


def is_compatible(release: dict[str, Any], minecraft_version: str, loader: str) -> bool:
    """Same Minecraft version and (when both sides declare one) the same mod loader."""
    if minecraft_version and minecraft_version.upper() != "LATEST":
        if minecraft_version not in (release.get("game_versions") or []):
            return False
    loaders = [str(lo).lower() for lo in release.get("loaders") or []]
    return not (loader and loaders and loader.lower() not in loaders)


def pick_latest(
    releases: list[dict[str, Any]], *, pinned_id: str, minecraft_version: str, loader: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(newest compatible release, the pinned release if listed). Releases are newest first.

    Pre-releases are only considered when the pinned release is one itself.
    """
    pinned = next((r for r in releases if str(r.get("id")) == str(pinned_id)), None)
    stable_only = pinned is None or pinned.get("version_type") in STABLE_TYPES
    for r in releases:
        if stable_only and r.get("version_type") not in STABLE_TYPES:
            continue
        if is_compatible(r, minecraft_version, loader):
            return r, pinned
    return None, pinned


def is_newer(latest: dict[str, Any], pinned_id: str, pinned_published: datetime | None) -> bool:
    if str(latest.get("id")) == str(pinned_id):
        return False
    latest_published = published_at(latest.get("published"))
    if latest_published is None or pinned_published is None:
        # the newest compatible release is not the installed one and dates cannot be compared
        return True
    return latest_published > pinned_published


# --- provider access --------------------------------------------------------------------
def _releases(instance: Instance) -> list[dict[str, Any]]:
    if not instance.modpack_provider or not instance.modpack_project_id:
        raise CheckUnavailable(NO_PROJECT)
    if instance.modpack_provider == "curseforge":
        if not curseforge_key():
            raise CheckUnavailable(KEYLESS_CURSEFORGE)
        mc = instance.minecraft_version
        # CurseForge pages at 50 files: filter by Minecraft version on the server side
        if mc and mc.upper() != "LATEST":
            return get_provider("curseforge").releases(instance.modpack_project_id, game_versions=[mc])
        return get_provider("curseforge").releases(instance.modpack_project_id)
    # same (cached) call as the instance Modpack tab; filtered locally
    return get_provider(instance.modpack_provider).releases(instance.modpack_project_id)


def _pinned_published(instance: Instance, pinned: dict[str, Any] | None) -> datetime | None:
    if pinned is not None:
        return published_at(pinned.get("published"))
    if not instance.modpack_version_id:
        return None
    try:
        if instance.modpack_provider == "modrinth":
            return published_at(get_provider("modrinth").release(instance.modpack_version_id).get("published"))
        if instance.modpack_provider == "curseforge":
            rel = get_provider("curseforge").release(instance.modpack_project_id, instance.modpack_version_id)
            return published_at(rel.get("published"))
    except ApiError as exc:
        log.info("could not look up the pinned release of %s: %s", instance.pk, exc)
    return None


# --- the check --------------------------------------------------------------------------
def check_instance(instance: Instance) -> ModpackUpdateCheck:
    """Ask the provider for the newest compatible release and record it. Never raises."""
    now = timezone.now()
    outcome: dict[str, Any]
    try:
        releases = _releases(instance)
        latest, pinned = pick_latest(
            releases,
            pinned_id=instance.modpack_version_id,
            minecraft_version=instance.minecraft_version,
            loader=instance.modpack_loader,
        )
        newer = bool(latest) and is_newer(latest, instance.modpack_version_id, _pinned_published(instance, pinned))
        outcome = {"status": UpdateCheckStatus.OK, "latest": latest, "newer": newer}
    except CheckUnavailable as exc:
        outcome = {"status": UpdateCheckStatus.UNAVAILABLE, "detail": str(exc)}
    except ApiError as exc:
        outcome = {"status": UpdateCheckStatus.ERROR, "detail": str(exc.detail)}
    except Exception as exc:  # a background check must never break the worker
        log.warning("modpack update check for %s crashed", instance.pk, exc_info=True)
        outcome = {"status": UpdateCheckStatus.ERROR, "detail": f"Unexpected error ({type(exc).__name__})"}

    announce = None
    with transaction.atomic():
        check, _ = ModpackUpdateCheck.objects.select_for_update().get_or_create(instance=instance)
        check.checked_at = now
        check.status = outcome["status"]
        check.detail = str(outcome.get("detail") or "")[:500]
        if outcome["status"] == UpdateCheckStatus.OK:
            latest = outcome["latest"] or {}
            check.last_success_at = now
            check.checked_version_id = instance.modpack_version_id
            check.latest_version_id = str(latest.get("id") or "")[:64]
            check.latest_version_name = str(latest.get("version_number") or latest.get("name") or "")[:200]
            check.latest_published = published_at(latest.get("published"))
            check.latest_minecraft_version = str((latest.get("game_versions") or [""])[0] or "")[:40]
            check.update_available = bool(outcome["newer"])
            if check.update_available and check.latest_version_id != check.notified_version_id:
                check.notified_version_id = check.latest_version_id
                announce = check.latest_version_name or check.latest_version_id
        elif outcome["status"] == UpdateCheckStatus.UNAVAILABLE:
            # nothing known any more (e.g. the CurseForge key was removed)
            check.checked_version_id = check.latest_version_id = check.latest_version_name = ""
            check.latest_minecraft_version = ""
            check.latest_published = None
            check.update_available = False
        check.save()
    instance.modpack_update_check = check  # keep the reverse relation cache current for summary()
    if announce:
        _announce(instance, announce)
    return check


def _announce(instance: Instance, version: str) -> None:
    from apps.notifications.service import notify

    current = instance.modpack_version_name or instance.modpack_version_id or "the installed release"
    pack = instance.modpack_name or "the modpack"
    notify(
        "modpack.update_available",
        f"Update available for {instance.name}: {pack} {version}",
        f"{pack} {version} is available for Minecraft {instance.minecraft_version} (installed: {current}). "
        "Nothing changes until you update from the server's Modpack tab.",
        instance=instance,
        data={"status": "update available", "detail": f"{current} → {version}"},
    )


def due_instances(now: datetime) -> list[Any]:
    """Modpack servers whose last check attempt is older than CHECK_INTERVAL (oldest first)."""
    cutoff = now - CHECK_INTERVAL
    qs = (
        Instance.objects.filter(server_type__in=list(MODPACK_TYPES), archived_at__isnull=True)
        .filter(Q(modpack_update_check__isnull=True) | Q(modpack_update_check__checked_at__lt=cutoff))
        .order_by(F("modpack_update_check__checked_at").asc(nulls_first=True), "modpack_provider", "modpack_project_id")
    )
    picked = list(qs.values_list("pk", "modpack_provider", "modpack_project_id")[:MAX_PER_TICK])
    # neighbours of the same project share the provider adapters' release cache
    picked.sort(key=lambda row: (row[1], row[2]))
    return [row[0] for row in picked]


def summary(instance: Instance) -> dict[str, Any] | None:
    """API representation (instance serializer and modpack endpoint)."""
    if not instance.is_modpack:
        return None
    try:
        check = instance.modpack_update_check
    except ModpackUpdateCheck.DoesNotExist:
        check = None
    if check is None:
        return {
            "status": "pending",
            "detail": "Not checked yet",
            "checked_at": None,
            "last_success_at": None,
            "available": False,
            "stale": False,
            "latest": None,
        }
    latest = None
    if check.latest_version_id:
        latest = {
            "id": check.latest_version_id,
            "name": check.latest_version_name or check.latest_version_id,
            "published": check.latest_published.isoformat() if check.latest_published else None,
            "minecraft_version": check.latest_minecraft_version,
        }
    pin = instance.modpack_version_id
    up_to_date = latest is not None and latest["id"] == pin
    # the pin changed since the last successful check (update/rollback): unknown until re-checked
    stale = bool(check.last_success_at) and check.checked_version_id != pin and not up_to_date
    return {
        "status": check.status,
        "detail": check.detail,
        "checked_at": check.checked_at.isoformat() if check.checked_at else None,
        "last_success_at": check.last_success_at.isoformat() if check.last_success_at else None,
        "available": bool(latest) and check.update_available and not up_to_date and not stale,
        "stale": stale,
        "latest": latest,
    }
