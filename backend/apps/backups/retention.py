"""Retention selection (pure function, unit-tested).

Mirrors restic's keep-last/daily/weekly/monthly semantics but runs in the panel so
pinned and safety backups are honoured and decisions are auditable.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Protocol


class BackupLike(Protocol):
    created_at: datetime
    pinned: bool
    kind: str


def select_to_delete(
    backups: Iterable[Any],
    *,
    keep_last: int,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
    keep_safety_days: int,
    now: datetime,
) -> list[Any]:
    ordered = sorted(backups, key=lambda b: b.created_at, reverse=True)
    keep: set[int] = set()
    regular = []
    for b in ordered:
        if b.pinned:
            keep.add(id(b))
        elif b.kind in ("pre_restore", "pre_update"):
            if now - b.created_at <= timedelta(days=keep_safety_days):
                keep.add(id(b))
        else:
            regular.append(b)
    for b in regular[:keep_last]:
        keep.add(id(b))

    def bucket(count: int, key_fn: Any) -> None:
        seen: set[Any] = set()
        for b in regular:
            k = key_fn(b.created_at)
            if k in seen:
                continue
            if len(seen) >= count:
                break
            seen.add(k)
            keep.add(id(b))

    if keep_daily:
        bucket(keep_daily, lambda d: d.date())
    if keep_weekly:
        bucket(keep_weekly, lambda d: tuple(d.isocalendar())[:2])
    if keep_monthly:
        bucket(keep_monthly, lambda d: (d.year, d.month))
    return [b for b in ordered if id(b) not in keep]
