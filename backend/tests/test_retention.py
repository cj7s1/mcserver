"""backups.retention.select_to_delete (restic-style keep policies, pinned + safety backups)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apps.backups.retention import select_to_delete

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # a Tuesday


@dataclass(eq=False)
class B:
    created_at: datetime
    kind: str = "manual"
    pinned: bool = False
    label: str = ""

    def __repr__(self) -> str:
        return self.label or self.created_at.isoformat()


def policy(**kw):
    base = {"keep_last": 0, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0, "keep_safety_days": 14, "now": NOW}
    base.update(kw)
    return base


def test_empty():
    assert select_to_delete([], **policy(keep_last=3)) == []


def test_keep_last_keeps_newest_n_and_returns_rest_newest_first():
    backups = [B(NOW - timedelta(hours=h)) for h in range(10)]
    doomed = select_to_delete(list(reversed(backups)), **policy(keep_last=4))
    assert doomed == backups[4:]


def test_keep_daily_keeps_newest_per_day_for_n_days_with_backups():
    # two backups per day for 10 days
    backups = [B(NOW - timedelta(days=d, hours=h)) for d in range(10) for h in (0, 6)]
    doomed = select_to_delete(backups, **policy(keep_daily=7))
    kept = [b for b in backups if b not in doomed]
    assert kept == [b for b in backups if b.created_at.hour == 12][:7]
    assert len(doomed) == 20 - 7


def test_keep_daily_counts_days_that_have_backups_not_calendar_days():
    backups = [B(NOW - timedelta(days=d)) for d in (0, 4, 9, 20)]
    doomed = select_to_delete(backups, **policy(keep_daily=3))
    assert doomed == [backups[3]]


def test_keep_weekly_uses_iso_weeks():
    # NOW is Tue 2026-11-03 (ISO week 45). Mon 2026-11-02 is the same week; Sun 2026-11-01 is week 44.
    tue = B(NOW, label="tue")
    mon = B(NOW - timedelta(days=1), label="mon")
    sun = B(NOW - timedelta(days=2), label="sun")
    older = B(NOW - timedelta(days=9), label="older")
    doomed = select_to_delete([tue, mon, sun, older], **policy(keep_weekly=2))
    assert doomed == [mon, older]


def test_keep_monthly():
    backups = [B(datetime(2026, m, d, tzinfo=UTC)) for m in (8, 9, 10, 11) for d in (1, 2)]
    doomed = select_to_delete(backups, **policy(keep_monthly=3, now=datetime(2026, 11, 3, tzinfo=UTC)))
    kept = sorted((b for b in backups if b not in doomed), key=lambda b: b.created_at)
    assert [b.created_at.date().isoformat() for b in kept] == ["2026-09-02", "2026-10-02", "2026-11-02"]


def test_policies_are_a_union():
    backups = [B(NOW - timedelta(days=d)) for d in range(40)]
    doomed = select_to_delete(backups, **policy(keep_last=2, keep_daily=5, keep_weekly=3))
    kept = [b for b in backups if b not in doomed]
    # keep_daily covers the last 5 days (and keep_last=2); keep_weekly adds the newest backup of
    # ISO week 43 (Sun 2026-10-25); weeks 45 and 44 are already represented.
    assert kept == [*backups[0:5], backups[9]]


def test_pinned_backups_are_never_deleted():
    old_pinned = B(NOW - timedelta(days=400), pinned=True, label="pinned")
    backups = [B(NOW - timedelta(days=d)) for d in range(5)] + [old_pinned]
    doomed = select_to_delete(backups, **policy(keep_last=1))
    assert old_pinned not in doomed
    assert len(doomed) == 4


def test_pinned_backups_do_not_count_towards_keep_last():
    pinned = B(NOW, pinned=True)
    regular = [B(NOW - timedelta(hours=h)) for h in range(1, 4)]
    doomed = select_to_delete([pinned, *regular], **policy(keep_last=2))
    assert doomed == regular[2:]


def test_safety_backups_kept_for_keep_safety_days_then_deleted():
    fresh = B(NOW - timedelta(days=3), kind="pre_restore", label="fresh")
    edge = B(NOW - timedelta(days=14), kind="pre_update", label="edge")
    stale = B(NOW - timedelta(days=15), kind="pre_restore", label="stale")
    regular = B(NOW, label="regular")
    doomed = select_to_delete([fresh, edge, stale, regular], **policy(keep_last=10, keep_safety_days=14))
    assert doomed == [stale]


def test_safety_backups_do_not_displace_regular_backups():
    safety = [B(NOW - timedelta(minutes=m), kind="pre_restore") for m in range(5)]
    regular = [B(NOW - timedelta(days=d + 1)) for d in range(3)]
    doomed = select_to_delete(safety + regular, **policy(keep_last=3))
    assert doomed == []


def test_zero_policy_deletes_all_regular_but_keeps_pinned():
    backups = [B(NOW - timedelta(days=d)) for d in range(3)] + [B(NOW, pinned=True)]
    doomed = select_to_delete(backups, **policy())
    assert len(doomed) == 3
    assert all(not b.pinned for b in doomed)
