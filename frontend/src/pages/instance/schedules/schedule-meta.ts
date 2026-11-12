import { isWarnMinutes } from "@/lib/power-warning";
import type { PermissionCode, Schedule } from "@/lib/types";

/** The backend also knows "snapshot" (btrfs/ZFS filesystem snapshots); lib/types predates it. */
export type ScheduleAction = Schedule["action"] | "snapshot";

/** Default number of scheduled snapshots a snapshot schedule keeps (backend scheduling.execute). */
export const DEFAULT_SNAPSHOT_KEEP = 24;
export const MAX_SNAPSHOT_KEEP = 1000;
/** Warning period preselected for new restart/stop schedules. */
export const DEFAULT_WARN_MINUTES = 5;

export const ACTION_INFO: Record<string, { label: string; description: string }> = {
  restart: {
    label: "Restart",
    description: "Gracefully stops the server and starts it again, optionally after warning online players in chat.",
  },
  start: { label: "Start", description: "Starts the server if it is stopped." },
  stop: {
    label: "Stop",
    description: "Stops the server gracefully (saves the world first), optionally after warning online players in chat.",
  },
  backup: {
    label: "Backup",
    description: "Creates a scheduled backup. The retention policy on the Backups page prunes old ones afterwards.",
  },
  command: { label: "Command", description: "Runs a console command through RCON, e.g. “say Restart in 5 minutes”." },
  snapshot: {
    label: "Snapshot",
    description:
      "Takes an instant filesystem snapshot (btrfs or ZFS) of the server folder (world saving is paused for a moment if the server is running), then deletes the oldest scheduled snapshots beyond the keep count.",
  },
};

/** Every schedule action, in the order the dialog offers them. */
export const SCHEDULE_ACTIONS: ScheduleAction[] = ["restart", "start", "stop", "backup", "snapshot", "command"];

/**
 * The actions a server's schedules can have: "command" only for a game with a console (capability
 * `console_commands`; the backend refuses a command schedule for Palworld, Valheim or Satisfactory). An
 * existing schedule keeps its own action in the list (`current`), so its dialog can show it.
 */
export function scheduleActions(hasConsole: boolean, current?: ScheduleAction): ScheduleAction[] {
  return SCHEDULE_ACTIONS.filter((a) => a !== "command" || hasConsole || a === current);
}

/**
 * Permission a schedule action needs: a schedule runs it with the panel's authority, so creating or
 * editing one needs the action's own permission (backend apps/instances/views.py
 * SCHEDULE_ACTION_PERMISSIONS; instance.schedules alone only allows viewing, pausing and deleting).
 */
export const ACTION_PERMISSIONS: Record<ScheduleAction, PermissionCode> = {
  restart: "instance.control",
  start: "instance.control",
  stop: "instance.control",
  backup: "instance.backup",
  snapshot: "instance.backup",
  command: "instance.command",
};

/** Setting a snapshot schedule's keep count deletes older snapshots, so it also needs this. */
export const SNAPSHOT_KEEP_PERMISSION: PermissionCode = "instance.restore";

const PERMISSION_LABELS: Partial<Record<PermissionCode, string>> = {
  "instance.control": "Start/stop/restart",
  "instance.backup": "Create backups",
  "instance.command": "Send commands",
  "instance.restore": "Restore backups",
  "instance.console": "View console",
};

/**
 * Whether the panel hid this command schedule's command (and last result) from the user: it sends them as
 * null to users without console access on the server (D-13).
 */
export function commandHidden(s: { action: ScheduleAction | string; payload: Schedule["payload"] }): boolean {
  return s.action === "command" && s.payload?.command === null;
}

/** What the schedules page shows for a hidden command or result. */
export const HIDDEN_FOR_CONSOLE = "hidden (needs console access)";

type Can = (code: PermissionCode) => boolean;

/** Why `action` cannot be chosen, or null when the user may schedule it. */
export function actionBlockedReason(action: ScheduleAction, can: Can): string | null {
  const code = ACTION_PERMISSIONS[action] ?? "instance.configure";
  if (!can(code)) return `needs the “${PERMISSION_LABELS[code] ?? code}” permission`;
  // every snapshot schedule prunes older scheduled snapshots (backend: needs instance.restore too)
  if (action === "snapshot" && !can(SNAPSHOT_KEEP_PERMISSION))
    return `needs the “${PERMISSION_LABELS[SNAPSHOT_KEEP_PERMISSION]}” permission (it deletes older snapshots)`;
  return null;
}

/** Whether the schedule's payload sets its own keep count (backend: needs instance.restore). */
export function hasExplicitKeep(s?: Pick<Schedule, "payload"> | null): boolean {
  return s?.payload?.keep !== undefined && s?.payload?.keep !== null;
}

/**
 * Why an existing schedule cannot be edited by this user (null = editable). Mirrors the backend:
 * every edit except switching it off needs the action's permission, and a snapshot schedule with
 * its own keep count also needs instance.restore.
 */
export function editBlockedReason(s: { action: ScheduleAction | string; payload: Schedule["payload"] }, can: Can): string | null {
  const action = s.action as ScheduleAction;
  const label = ACTION_INFO[action]?.label.toLowerCase() ?? action;
  const blocked = actionBlockedReason(action, can);
  if (blocked) return `Editing ${label} schedules ${blocked}.`;
  // the command is not known here: an edit would have to type it anew and could replace it unseen
  if (commandHidden(s)) return `Its command is hidden from you; editing it needs the “${PERMISSION_LABELS["instance.console"]}” permission.`;
  if (action === "snapshot" && hasExplicitKeep(s) && !can(SNAPSHOT_KEEP_PERMISSION))
    return `This schedule sets its own keep count; editing it needs the “${PERMISSION_LABELS[SNAPSHOT_KEEP_PERMISSION]}” permission.`;
  return null;
}

/** Actions where "only when running" changes behaviour (mirrors backend scheduling.execute). */
export const RUNNING_ONLY_ACTIONS = ["restart", "command", "stop"];

/** Actions that can warn players in game first (payload warn_minutes / warn_message). */
export const WARN_ACTIONS = ["restart", "stop"];

export interface ScheduleDraft {
  name: string;
  action: ScheduleAction;
  cron: string;
  command: string;
  backupName: string;
  /** snapshot action: how many scheduled snapshots to keep (string while editing) */
  keep: string;
  enabled: boolean;
  only_when_running: boolean;
  /** restart/stop: in-game warning period in minutes (0 = none) and optional message */
  warnMinutes: number;
  warnMessage: string;
}

export function draftFrom(s?: Schedule | null, template?: Partial<ScheduleDraft>): ScheduleDraft {
  return {
    name: s?.name ?? "",
    action: (s?.action as ScheduleAction | undefined) ?? "restart",
    cron: s?.cron ?? "0 4 * * *",
    command: typeof s?.payload?.command === "string" ? s.payload.command : "",
    backupName: typeof s?.payload?.name === "string" ? s.payload.name : "",
    keep: String(snapshotKeep(s) ?? DEFAULT_SNAPSHOT_KEEP),
    enabled: s?.enabled ?? true,
    only_when_running: s?.only_when_running ?? true,
    // new schedules warn players by default; existing ones keep what they had (none before this option)
    warnMinutes: s ? (scheduleWarnMinutes(s) ?? 0) : DEFAULT_WARN_MINUTES,
    warnMessage: typeof s?.payload?.warn_message === "string" ? s.payload.warn_message : "",
    ...template,
  };
}

/** The keep count stored in a snapshot schedule's payload, if valid. */
export function snapshotKeep(s?: Schedule | null): number | null {
  const v = s?.payload?.keep;
  const n = typeof v === "number" ? v : typeof v === "string" && /^\d+$/.test(v) ? Number(v) : NaN;
  return Number.isInteger(n) && n > 0 ? n : null;
}

/** The warning period stored in a restart/stop schedule's payload, if valid. */
export function scheduleWarnMinutes(s?: Pick<Schedule, "payload"> | null): number | null {
  const v = s?.payload?.warn_minutes;
  const n = typeof v === "number" ? v : typeof v === "string" && /^\d+$/.test(v) ? Number(v) : NaN;
  return isWarnMinutes(n) ? n : null;
}

/** Payload of a restart/stop schedule: the warning keys, plus any other keys of an existing payload. */
export function warnPayload(
  keep: Record<string, unknown>,
  d: Pick<ScheduleDraft, "warnMinutes" | "warnMessage">,
): Record<string, unknown> {
  const payload: Record<string, unknown> = { ...keep, warn_minutes: isWarnMinutes(d.warnMinutes) ? d.warnMinutes : 0 };
  const message = d.warnMessage.trim();
  if (payload.warn_minutes && message) payload.warn_message = message;
  else delete payload.warn_message;
  return payload;
}
