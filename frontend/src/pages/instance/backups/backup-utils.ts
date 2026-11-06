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
    hint: "Safety backup taken automatically before a restore or a world replacement changed the server files.",
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

export function usePorts(enabled = true) {
  return useQuery({
    queryKey: ["system", "ports"],
    queryFn: () => api.get<PortsResponse>("/system/ports"),
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
