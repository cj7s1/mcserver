/** Shared types, queries and small UI pieces for the instance Backups page. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { qk, useInvalidateInstance, useJob } from "@/lib/queries";
import type { Backup, Instance, Job, RetentionPolicy } from "@/lib/types";
import { SNAPSHOT_JOB_KINDS, snapshotsKey } from "./snapshot-utils";
import { WORLD_JOB_KINDS } from "./world-utils";

export interface BackupsResponse {
  backups: Backup[];
  policy: RetentionPolicy;
}

export interface BackupFileEntry {
  name: string;
  path: string;
  type: "file" | "directory" | "symlink" | string;
  size: number;
  modified: string | null;
}

export interface BackupFilesResponse {
  path: string;
  entries: BackupFileEntry[];
  /** The folder holds more entries than one listing returns (B-6): only these are shown. */
  truncated?: boolean;
}

export interface RestoreAsNewResponse {
  instance: Instance;
  job: Job;
}

export interface PortsResponse {
  range: [number, number];
  in_use: { port: number; used_by: string }[];
  suggested: number | null;
}

/**
 * Job kinds that change the backup or snapshot lists (used to pick up jobs started elsewhere,
 * e.g. by a schedule).
 */
export const BACKUP_JOB_KINDS = [
  "backup",
  "backup_delete",
  "backup_verify",
  "restore",
  "restore_new",
  "restore_path",
  ...SNAPSHOT_JOB_KINDS,
  ...WORLD_JOB_KINDS,
];

/** What the empty Backups page suggests: a schedule only to a server that has none. */
export function emptyBackupsHint(hasBackupSchedule: boolean): string {
  return hasBackupSchedule
    ? "Create a first backup now. This server's backup schedule takes the next ones automatically."
    : "Create a first backup now, and add a backup schedule so it happens automatically.";
}

/** restic excludes always applied by the backend (backend/apps/backups/handlers.py DEFAULT_EXCLUDES). */
export const BUILTIN_EXCLUDES = ["logs/*.log.gz", "crash-reports", ".cache", "cache", "debug", "*.tmp", "modernfix/classCache*"];

export const KIND_INFO: Record<Backup["kind"], { label: string; className: string; hint: string }> = {
  manual: {
    label: "Manual",
    className: "bg-muted text-foreground",
    hint: "Created by a user from this page.",
  },
  scheduled: {
    label: "Scheduled",
    className: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
    hint: "Created by a backup schedule.",
  },
  pre_restore: {
    label: "Before restore",
    className: "bg-violet-500/10 text-violet-700 dark:text-violet-300",
    hint: "Safety backup taken automatically before a restore, a world replacement or a world reset changed the server files.",
  },
  pre_update: {
    label: "Before update",
    className: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
    hint: "Safety backup taken automatically before a software or modpack update.",
  },
};

export function useBackups(instanceId: string, busy: boolean) {
  return useQuery({
    queryKey: qk.backups(instanceId),
    queryFn: () => api.get<BackupsResponse>(`/instances/${instanceId}/backups`),
    refetchInterval: (q) => {
      const d = q.state.data as BackupsResponse | undefined;
      return busy || d?.backups.some((b) => b.status === "running") ? 3000 : 30_000;
    },
  });
}

/** Whether the backup lives in another node's repository (the server was moved since it was made). */
export function onOtherNode(b: Pick<Backup, "on_current_node">): boolean {
  return b.on_current_node === false;
}

/** Whether the backup can still be read: false when the node holding it was deleted. */
export function backupReadable(b: Pick<Backup, "on_current_node" | "node_id">): boolean {
  return !(onOtherNode(b) && b.node_id == null);
}

/** Why a backup on another node cannot be restored over this server. */
export function otherNodeHint(b: Pick<Backup, "node_name" | "node_id">): string {
  if (b.node_id == null)
    return `This backup was stored on ${b.node_name ? `node “${b.node_name}”, which` : "a node that"} no longer exists; it can no longer be read.`;
  const where = b.node_name ? `node “${b.node_name}”` : "another node";
  return `Stored in the backup repository on ${where}: this server was moved since. It can be browsed, downloaded and restored as a new server (created on that node), but not restored over this server.`;
}

/**
 * Ports in use on `node` (GET /system/ports?node=<id>): pages about an existing server pass that
 * server's node, so a server on another node is checked against that node's ports, not the default
 * node's. The key starts with ["system", "ports"], so invalidating that prefix refreshes every node.
 */
export function usePorts(node: number, enabled = true) {
  return useQuery({
    queryKey: ["system", "ports", { node }],
    queryFn: () => api.get<PortsResponse>("/system/ports", { node }),
    enabled,
    staleTime: 10_000,
  });
}

const FINISHED = ["succeeded", "failed", "cancelled"];

/**
 * Tracks one job started from the Backups page (jobs are exclusive per instance, so one slot is
 * enough). Also adopts a backup-related job already running on the instance (e.g. a scheduled
 * backup) so the page refreshes and reports when it finishes.
 */
export function useTrackedJob(instance: Instance) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const [jobId, setJobId] = useState<string | null>(null);
  const [adopted, setAdopted] = useState<string | null>(null);
  const active = instance.active_job;
  const candidate = active && BACKUP_JOB_KINDS.includes(active.kind) ? active.id : null;
  if (candidate && candidate !== adopted) {
    // adjust state during render (React-endorsed pattern for deriving from props)
    setAdopted(candidate);
    if (!jobId || jobId !== candidate) setJobId(candidate);
  }
  const { data: job } = useJob(jobId);
  const handled = useRef<string | null>(null);

  useEffect(() => {
    if (!job || !FINISHED.includes(job.status) || handled.current === job.id) return;
    handled.current = job.id;
    qc.invalidateQueries({ queryKey: qk.backups(instance.id) });
    qc.invalidateQueries({ queryKey: snapshotsKey(instance.id) });
    qc.invalidateQueries({ queryKey: qk.world(instance.id) });
    qc.invalidateQueries({ queryKey: qk.saves(instance.id) });
    invalidate(instance.id);
    if (job.status === "succeeded") toast.success(`${job.title}: done`);
    else if (job.status === "failed") toast.error(`${job.title} failed${job.error ? `: ${job.error}` : ""}`);
    else toast.message(`${job.title} was cancelled`);
  }, [job, qc, invalidate, instance.id]);

  return {
    jobId,
    job,
    finished: !!job && FINISHED.includes(job.status),
    track: (j: Job | null | undefined) => {
      if (j?.id) {
        handled.current = null;
        setJobId(j.id);
        qc.setQueryData(qk.job(j.id), j);
      }
    },
    dismiss: () => setJobId(null),
  };
}

export type TrackedJob = ReturnType<typeof useTrackedJob>;
