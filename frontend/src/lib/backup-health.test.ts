import { describe, expect, it } from "vitest";
import { BACKUP_STALE_DAYS, backupWarning, backupWarningDetail, type BackupHealthFields } from "./backup-health";
import type { PermissionCode } from "./types";

const NOW = Date.parse("2026-11-09T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;
const ago = (ms: number) => new Date(NOW - ms).toISOString();

function server(over: Partial<BackupHealthFields> = {}): BackupHealthFields {
  return {
    archived_at: null,
    permissions: ["instance.view", "instance.backup"],
    last_backup_at: null,
    backup_schedules: 0,
    ...over,
  };
}

describe("backupWarning", () => {
  it("says “No backup yet” for a server that was never backed up", () => {
    expect(backupWarning(server(), NOW)).toEqual({
      kind: "none",
      title: "No backup yet",
      label: "No backup yet",
      days: null,
      scheduled: false,
    });
    expect(backupWarning(server({ backup_schedules: 1 }), NOW)).toMatchObject({ kind: "none", scheduled: true });
  });

  it("stays quiet while the newest backup is at most 7 days old", () => {
    expect(BACKUP_STALE_DAYS).toBe(7);
    for (const age of [0, 60_000, DAY, 6 * DAY, 7 * DAY - 1, 7 * DAY]) {
      expect(backupWarning(server({ last_backup_at: ago(age) }), NOW)).toBeNull();
    }
    // a clock that runs a little behind the panel's must not invent a warning
    expect(backupWarning(server({ last_backup_at: ago(-5 * 60_000) }), NOW)).toBeNull();
  });

  it("warns once the newest backup is older than 7 days, in whole days", () => {
    expect(backupWarning(server({ last_backup_at: ago(7 * DAY + 1) }), NOW)).toMatchObject({
      kind: "stale",
      title: "Last backup 7 days ago",
      days: 7,
    });
    expect(backupWarning(server({ last_backup_at: ago(9 * DAY + 3 * 60 * 60 * 1000), backup_schedules: 2 }), NOW)).toEqual({
      kind: "stale",
      title: "Last backup 9 days ago",
      label: "Backup 9 days old",
      days: 9,
      scheduled: true,
    });
    expect(backupWarning(server({ last_backup_at: ago(400 * DAY) }), NOW)?.title).toBe("Last backup 400 days ago");
  });

  it("reads the panel's timestamps with a UTC offset", () => {
    // 2026-10-31T04:17 at -07:00 is 11:17 UTC: nine days and a bit before NOW
    expect(backupWarning(server({ last_backup_at: "2026-10-31T04:17:00-07:00" }), NOW)?.days).toBe(9);
  });

  it("shows nothing for archived servers", () => {
    expect(backupWarning(server({ archived_at: "2026-11-01T00:00:00Z" }), NOW)).toBeNull();
    expect(backupWarning(server({ archived_at: "2026-11-01T00:00:00Z", last_backup_at: ago(30 * DAY) }), NOW)).toBeNull();
  });

  it("shows nothing to users who cannot see the server's backups", () => {
    // without instance.backup the API always sends null, which must not read as “No backup yet”
    const lists: PermissionCode[][] = [[], ["instance.view"], ["instance.view", "instance.schedules", "instance.restore"]];
    for (const permissions of lists) {
      expect(backupWarning(server({ permissions }), NOW)).toBeNull();
      expect(backupWarning(server({ permissions, last_backup_at: ago(30 * DAY) }), NOW)).toBeNull();
    }
    expect(backupWarning(server({ permissions: ["instance.backup"] }), NOW)).not.toBeNull();
  });

  it("says nothing when the payload does not carry the field", () => {
    expect(backupWarning(server({ last_backup_at: undefined }), NOW)).toBeNull();
    // a missing schedule count reads as "no schedule"
    expect(backupWarning(server({ backup_schedules: undefined }), NOW)).toMatchObject({ kind: "none", scheduled: false });
  });
});

describe("backupWarningDetail", () => {
  const detail = (over: Partial<BackupHealthFields>) => backupWarningDetail(backupWarning(server(over), NOW)!);

  it("explains each case and whether a schedule is in place", () => {
    expect(detail({})).toMatch(/not been backed up yet and has no backup schedule/);
    expect(detail({ backup_schedules: 1 })).toMatch(/schedule takes the first backup on its next run/);
    expect(detail({ last_backup_at: ago(9 * DAY) })).toMatch(/more than 7 days old and no backup schedule is enabled/);
    expect(detail({ last_backup_at: ago(9 * DAY), backup_schedules: 1 })).toMatch(/although a backup schedule is enabled/);
  });
});
