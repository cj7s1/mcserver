import type { Schedule } from "@/lib/types";

/** The backend also knows "snapshot" (btrfs filesystem snapshots); lib/types predates it. */
export type ScheduleAction = Schedule["action"] | "snapshot";

/** Default number of scheduled snapshots a snapshot schedule keeps (backend scheduling.execute). */
export const DEFAULT_SNAPSHOT_KEEP = 24;
export const MAX_SNAPSHOT_KEEP = 1000;

export const ACTION_INFO: Record<string, { label: string; description: string }> = {
  restart: { label: "Restart", description: "Gracefully stops the server and starts it again." },
  start: { label: "Start", description: "Starts the server if it is stopped." },
  stop: { label: "Stop", description: "Stops the server gracefully (saves the world first)." },
  backup: {
    label: "Backup",
    description: "Creates a scheduled backup. The retention policy on the Backups page prunes old ones afterwards.",
  },
  command: { label: "Command", description: "Runs a console command through RCON, e.g. “say Restart in 5 minutes”." },
  snapshot: {
    label: "Snapshot",
    description:
      "Takes an instant btrfs snapshot of the server folder (world saving is paused for a moment if the server is running), then deletes the oldest scheduled snapshots beyond the keep count.",
  },
};

/** Actions where "only when running" changes behaviour (mirrors backend scheduling.execute). */
export const RUNNING_ONLY_ACTIONS = ["restart", "command", "stop"];

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
    ...template,
  };
}

/** The keep count stored in a snapshot schedule's payload, if valid. */
export function snapshotKeep(s?: Schedule | null): number | null {
  const v = s?.payload?.keep;
  const n = typeof v === "number" ? v : typeof v === "string" && /^\d+$/.test(v) ? Number(v) : NaN;
  return Number.isInteger(n) && n > 0 ? n : null;
}
