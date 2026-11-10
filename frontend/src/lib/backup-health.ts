/**
 * "Is this server backed up?": the rule behind the notice on a server's Overview and Backups
 * pages and the indicator in the server lists (components/BackupNotice.tsx). It reads
 * last_backup_at and backup_schedules of the instance payload (InstanceSerializer).
 */
import { toDate } from "./format";
import type { Instance, PermissionCode } from "./types";

/** A newest successful backup older than this many days counts as out of date. */
export const BACKUP_STALE_DAYS = 7;

/** What the Backups page and GET /instances/{id}/backups need; without it last_backup_at is always null. */
export const BACKUP_VIEW_PERMISSION: PermissionCode = "instance.backup";

const DAY_MS = 24 * 60 * 60 * 1000;

export interface BackupWarning {
  /** "none": never backed up successfully; "stale": the newest backup is older than BACKUP_STALE_DAYS */
  kind: "none" | "stale";
  /** Headline of the notice, e.g. "No backup yet" or "Last backup 9 days ago". */
  title: string;
  /** Short form for the list indicator, e.g. "No backup yet" or "Backup 9 days old". */
  label: string;
  /** Whole days since the newest successful backup (null when there is none). */
  days: number | null;
  /** The server has at least one enabled backup schedule. */
  scheduled: boolean;
}

export type BackupHealthFields = Pick<Instance, "archived_at" | "permissions" | "last_backup_at" | "backup_schedules">;

/**
 * The backup warning to show for a server, or null when there is nothing to say: it has a
 * successful backup from the last BACKUP_STALE_DAYS days, it is archived, or the viewer may not
 * see its backups.
 */
export function backupWarning(instance: BackupHealthFields, now: number = Date.now()): BackupWarning | null {
  if (instance.archived_at) return null;
  if (!instance.permissions?.includes(BACKUP_VIEW_PERMISSION)) return null;
  // a payload without the field (not from InstanceSerializer): unknown, so say nothing
  if (instance.last_backup_at === undefined) return null;
  const scheduled = (instance.backup_schedules ?? 0) > 0;
  const last = toDate(instance.last_backup_at);
  if (!last) return { kind: "none", title: "No backup yet", label: "No backup yet", days: null, scheduled };
  const age = now - last.getTime();
  if (age <= BACKUP_STALE_DAYS * DAY_MS) return null;
  const days = Math.floor(age / DAY_MS);
  return { kind: "stale", title: `Last backup ${days} days ago`, label: `Backup ${days} days old`, days, scheduled };
}

/** What the notice says below its headline. */
export function backupWarningDetail(warning: BackupWarning): string {
  if (warning.kind === "none") {
    return warning.scheduled
      ? "This server has not been backed up yet. Its backup schedule takes the first backup on its next run; you can also create one now."
      : "This server has not been backed up yet and has no backup schedule, so a disk failure or a wrong delete would lose its world.";
  }
  return warning.scheduled
    ? `The newest successful backup is more than ${BACKUP_STALE_DAYS} days old although a backup schedule is enabled. Check the schedule's last result and the recent backup jobs.`
    : `The newest successful backup is more than ${BACKUP_STALE_DAYS} days old and no backup schedule is enabled.`;
}
