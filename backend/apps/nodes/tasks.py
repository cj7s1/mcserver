"""Periodic checks of the nodes (A-7 of the security review of 2026-11-11).

``check_disk_space``: the free space of every node's server storage (``storage.instances`` of the agent's
GET /v1/info) and backup storage (``storage.backups``); below the system setting ``low_disk_space_gb`` (0 = off)
a ``node.low_disk_space`` notification goes out once, and again only after the space was above the threshold
in between or a day later. A full disk stops game saves and backups (and a compromised or runaway server can
fill it), so the owner should hear of it before that.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.core.cache import cache

from apps.core.errors import ApiError
from apps.core.models import SystemSetting

log = logging.getLogger(__name__)
GB = 1024**3
REMIND_SECONDS = 24 * 3600
STORAGES = {"instances": "server storage", "backups": "backup storage"}


def _low_key(node: Any, which: str) -> str:
    return f"low-disk-notified:{node.pk}:{which}"


def check_node(node: Any, threshold_gb: int) -> list[str]:
    """The storages of ``node`` that were reported low now (notified); [] when none or the agent cannot be asked."""
    from apps.nodes.client import AgentClient
    from apps.notifications.service import notify

    try:
        info = AgentClient(node, timeout=15).info()
    except ApiError as exc:
        log.debug("disk space of node %s not checked: %s", node.name, exc)
        return []
    storage = info.get("storage") if isinstance(info, dict) and isinstance(info.get("storage"), dict) else {}
    notified = []
    seen: set[tuple[Any, Any]] = set()
    for which, label in STORAGES.items():
        entry = storage.get(which)
        if not isinstance(entry, dict):
            continue
        free, total = entry.get("free_bytes"), entry.get("total_bytes")
        if not isinstance(free, int) or isinstance(free, bool):
            continue
        same = (entry.get("source"), entry.get("mount_point"))
        key = _low_key(node, which)
        if free >= threshold_gb * GB:
            cache.delete(key)  # recovered: the next drop is told again
            continue
        if cache.get(key) or (same != (None, None) and same in seen):
            continue  # told already (or the same filesystem as the server storage)
        seen.add(same)
        cache.set(key, True, REMIND_SECONDS)
        free_gb = free / GB
        total_text = f" of {total / GB:.0f} GB" if isinstance(total, int) and total else ""
        notify(
            "node.low_disk_space",
            f"Low disk space on node {node.name}",
            f"The {label} of node {node.name} ({entry.get('path') or which}) has {free_gb:.1f} GB free{total_text}, "
            f"below the warning level of {threshold_gb} GB. Servers cannot save and backups fail when it is full: "
            "free space (old backups, snapshots, servers you no longer need) or raise the warning level in Settings.",
            data={"node": node.name, "storage": which, "free_bytes": free, "total_bytes": total, "threshold_gb": threshold_gb},
        )
        notified.append(which)
    return notified


@shared_task(name="apps.nodes.tasks.check_disk_space", ignore_result=True)
def check_disk_space() -> int:
    """Check every node; returns how many low storages were notified."""
    from apps.nodes.models import Node

    try:
        threshold = int(SystemSetting.get("low_disk_space_gb") or 0)
    except (TypeError, ValueError):
        threshold = 0
    if threshold <= 0:
        return 0
    count = 0
    for node in Node.objects.all():
        try:
            count += len(check_node(node, threshold))
        except Exception:  # one node must not stop the check of the others
            log.exception("disk space check of node %s failed", node.pk)
    return count
