import { formatDuration, toDate } from "@/lib/format";
import type { Job, JobStatus } from "@/lib/types";

export const JOB_STATUS: Record<JobStatus, { label: string; className: string }> = {
  queued: { label: "Queued", className: "text-muted-foreground" },
  running: { label: "Running", className: "text-sky-600 dark:text-sky-400" },
  succeeded: { label: "Succeeded", className: "text-emerald-600 dark:text-emerald-400" },
  failed: { label: "Failed", className: "text-destructive" },
  cancelled: { label: "Cancelled", className: "text-muted-foreground" },
};

export const JOB_STATUSES: JobStatus[] = ["queued", "running", "succeeded", "failed", "cancelled"];

export function jobStatusLabel(s: JobStatus): string {
  return JOB_STATUS[s]?.label ?? s;
}

/** Wall-clock run time: finished - started, or elapsed so far for a running job. */
export function jobDurationSeconds(job: Pick<Job, "started_at" | "finished_at">, now = Date.now()): number | null {
  const start = toDate(job.started_at);
  if (!start) return null;
  const end = toDate(job.finished_at)?.getTime() ?? now;
  return Math.max(0, (end - start.getTime()) / 1000);
}

export function jobDurationLabel(job: Pick<Job, "started_at" | "finished_at">, now = Date.now()): string {
  const s = jobDurationSeconds(job, now);
  return s === null ? "—" : formatDuration(s);
}
