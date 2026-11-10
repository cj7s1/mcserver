import { formatDuration, toDate } from "@/lib/format";
import type { Job, JobStatus, PermissionCode } from "@/lib/types";

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

export function isJobActive(job: Pick<Job, "status">): boolean {
  return job.status === "queued" || job.status === "running";
}

/**
 * Permissions needed to mark a job of each kind as failed (backend apps/jobs/views.py
 * ABANDON_PERMISSIONS): releasing the lock while the agent may still be working (e.g. halfway
 * through a restore) needs the permission that starts that kind of job. Unknown kinds need
 * system.settings, which may always do it.
 */
export const ABANDON_PERMISSIONS: Record<string, readonly PermissionCode[]> = {
  start: ["instance.control"],
  stop: ["instance.control"],
  restart: ["instance.control"],
  install: ["instance.configure"],
  recreate: ["instance.configure"],
  software_update: ["instance.configure"],
  archive: ["instance.delete"],
  delete: ["instance.delete"],
  clone: ["instance.create", "instance.files.read"],
  extract_archive: ["instance.files.write"],
  migrate: ["system.nodes", "instance.configure"],
  backup: ["instance.backup"],
  backup_verify: ["instance.backup"],
  snapshot_create: ["instance.backup"],
  restore: ["instance.restore"],
  restore_new: ["instance.restore", "instance.create"],
  restore_path: ["instance.restore"],
  backup_delete: ["instance.restore"],
  snapshot_restore: ["instance.restore"],
  snapshot_delete: ["instance.restore"],
  snapshot_convert: ["instance.restore"],
  modpack_update: ["instance.content"],
  modpack_rollback: ["instance.content", "instance.restore"],
  world_replace: ["instance.restore", "instance.files.write"],
  world_reset: ["instance.restore", "instance.files.write"],
};

/**
 * Whether the current user may POST /jobs/{id}/abandon (backend JobAbandonView): the job must be
 * queued or running; global system.settings may always; otherwise the job needs a server and every
 * permission of ABANDON_PERMISSIONS for its kind: server-level codes on that server, panel-wide ones
 * (system.*, instance.create) in the global role.
 */
export function canAbandonJob(
  job: Pick<Job, "status" | "instance"> & { kind?: string },
  perms: { instance?: readonly string[] | null; global?: readonly string[] | null },
): boolean {
  if (!isJobActive(job)) return false;
  if (perms.global?.includes("system.settings")) return true;
  if (!job.instance) return false;
  const needed = ABANDON_PERMISSIONS[job.kind ?? ""] ?? ["system.settings"];
  return needed.every((code) => (PANEL_WIDE.has(code) || code.startsWith("system.") ? perms.global : perms.instance)?.includes(code) ?? false);
}

/** Codes with scope "system" that do not start with "system." (backend apps/accounts/permissions.py). */
const PANEL_WIDE = new Set<string>(["instance.create"]);
