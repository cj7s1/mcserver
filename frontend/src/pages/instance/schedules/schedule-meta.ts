import type { Schedule } from "@/lib/types";

/** The backend also knows "snapshot" (filesystem snapshots, not available yet). */
export type ScheduleAction = Schedule["action"] | "snapshot";

export const ACTION_INFO: Record<string, { label: string; description: string }> = {
  restart: { label: "Restart", description: "Gracefully stops the server and starts it again." },
  start: { label: "Start", description: "Starts the server if it is stopped." },
  stop: { label: "Stop", description: "Stops the server gracefully (saves the world first)." },
  backup: {
    label: "Backup",
    description: "Creates a scheduled backup. The retention policy on the Backups page prunes old ones afterwards.",
  },
  command: { label: "Command", description: "Runs a console command through RCON, e.g. “say Restart in 5 minutes”." },
  snapshot: { label: "Snapshot", description: "Filesystem snapshot (not available yet)." },
};

/** Actions where "only when running" changes behaviour (mirrors backend scheduling.execute). */
export const RUNNING_ONLY_ACTIONS = ["restart", "command", "stop"];

export interface ScheduleDraft {
  name: string;
  action: ScheduleAction;
  cron: string;
  command: string;
  backupName: string;
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
    enabled: s?.enabled ?? true,
    only_when_running: s?.only_when_running ?? true,
    ...template,
  };
}
