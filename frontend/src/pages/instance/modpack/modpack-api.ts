/**
 * Types, queries and helpers for the instance Modpack tab.
 *
 * Endpoints (backend/apps/modpacks/views.py, backend/apps/modpacks/handlers.py):
 *   GET  /instances/{id}/modpack[?include_prerelease=true] -> InstanceModpack
 *   GET  /instances/{id}/modpack/plan/{version_id}          -> UpdatePlan        (instance.content)
 *   POST /instances/{id}/modpack/update   {version_id, backup, allow_minecraft_change, start} -> Job (202)
 *   POST /instances/{id}/modpack/rollback {backup_id, confirm: true}                         -> Job (202)
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api, ApiError } from "@/lib/api";
import { toDate } from "@/lib/format";
import { qk, useInvalidateInstance, useJob } from "@/lib/queries";
import type { Instance, Job, Release } from "@/lib/types";

/** The pin fields the backend records before/after an update (apps/modpacks/handlers.py PIN_FIELDS). */
export interface ModpackPin {
  modpack_version_id: string;
  modpack_version_name: string;
  minecraft_version: string;
  modpack_loader: string;
  loader_version: string;
  java_version: number;
  image: string;
}

export interface ModpackRelease extends Release {
  status?: string;
}

export interface ModpackHistoryEntry {
  from: Partial<ModpackPin> | null;
  to: Partial<ModpackPin> | null;
  job: string;
  backup: string | null;
}

export interface RollbackPoint {
  backup_id: string;
  name: string;
  created_at: string;
  before: Partial<ModpackPin> | null;
}

export interface InstanceModpack {
  provider: string;
  project_id: string;
  name: string;
  /** The full release when it is in the list, otherwise only id + version_number. */
  current: Partial<ModpackRelease> & { id: string; version_number: string };
  releases: ModpackRelease[];
  updates_available: number;
  history: ModpackHistoryEntry[];
  rollback_points: RollbackPoint[];
}

export interface UpdatePlan {
  current: ModpackPin;
  target: ModpackPin;
  minecraft_changed: boolean;
  loader_changed: boolean;
  same_release: boolean;
  warnings: string[];
}

export type Relation = "current" | "newer" | "older";

export const modpackKey = (id: string, prerelease: boolean) => ["instances", id, "modpack", { prerelease }] as const;
export const modpackPrefix = (id: string) => ["instances", id, "modpack"] as const;

/** Do not retry deterministic errors (not a modpack, provider key missing, unknown project). */
function retryTransient(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && (error.status < 500 || error.code === "curseforge_key_missing")) return false;
  return failureCount < 1;
}

export function useInstanceModpack(instanceId: string, includePrerelease: boolean, enabled: boolean) {
  return useQuery({
    queryKey: modpackKey(instanceId, includePrerelease),
    queryFn: ({ signal }) =>
      api.get<InstanceModpack>(
        `/instances/${instanceId}/modpack`,
        { include_prerelease: includePrerelease ? "true" : undefined },
        signal,
      ),
    enabled,
    staleTime: 60_000,
    retry: retryTransient,
  });
}

/** Same key and shape as the Overview's job list, so both share one cache entry. */
export function useInstanceJobList(instanceId: string) {
  return useQuery({
    queryKey: qk.instanceJobs(instanceId),
    queryFn: () => api.get<Job[]>(`/instances/${instanceId}/jobs`),
    staleTime: 15_000,
  });
}

/**
 * Where a release sits relative to the pinned one. Releases come newest first from the
 * provider adapters; when the pinned release is not in the list (e.g. a beta while betas are
 * hidden), fall back to comparing publish dates.
 */
export function relationOf(
  release: ModpackRelease,
  index: number,
  currentIndex: number,
  current: InstanceModpack["current"] | undefined,
  pinnedId: string,
): Relation | null {
  if (release.id === pinnedId) return "current";
  if (currentIndex >= 0) return index < currentIndex ? "newer" : "older";
  const a = toDate(release.published ?? null);
  const b = toDate(current?.published ?? null);
  if (!a || !b) return null;
  return a.getTime() > b.getTime() ? "newer" : "older";
}

export function projectUrl(i: Instance): string | null {
  const ref = i.modpack_slug || i.modpack_project_id;
  switch (i.modpack_provider) {
    case "modrinth":
      return ref ? `https://modrinth.com/modpack/${encodeURIComponent(ref)}` : null;
    case "curseforge":
      return i.modpack_slug ? `https://www.curseforge.com/minecraft/modpacks/${encodeURIComponent(i.modpack_slug)}` : null;
    case "ftb":
      return i.modpack_project_id
        ? `https://www.feed-the-beast.com/modpacks/${encodeURIComponent(i.modpack_project_id)}${i.modpack_slug ? `-${encodeURIComponent(i.modpack_slug)}` : ""}`
        : null;
    default:
      return null;
  }
}

/** Provider page of one release (FTB has no per-release pages). */
export function releaseUrl(i: Instance, versionId: string): string | null {
  const base = projectUrl(i);
  if (!base || !versionId) return null;
  if (i.modpack_provider === "modrinth") return `${base}/version/${encodeURIComponent(versionId)}`;
  if (i.modpack_provider === "curseforge") return `${base}/files/${encodeURIComponent(versionId)}`;
  return null;
}

export function pinLabel(p: Partial<ModpackPin> | null | undefined): string {
  if (!p) return "unknown release";
  return p.modpack_version_name || p.modpack_version_id || "unknown release";
}

export function lockReason(instance: Instance): string | null {
  if (instance.archived_at) return "This server is archived. Unarchive it before changing its modpack release.";
  if (instance.active_job) return `Another operation is running (${instance.active_job.title}). Release changes are available when it finishes.`;
  return null;
}

const MODPACK_JOB_KINDS = ["modpack_update", "modpack_rollback"];
const FINISHED = ["succeeded", "failed", "cancelled"];

/**
 * Tracks the update/rollback job started from this page, and adopts one already running on the
 * instance (e.g. started in another tab) so the page refreshes when it finishes.
 */
export function useModpackJob(instance: Instance) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const [jobId, setJobId] = useState<string | null>(null);
  const [adopted, setAdopted] = useState<string | null>(null);
  const active = instance.active_job;
  const candidate = active && MODPACK_JOB_KINDS.includes(active.kind) ? active.id : null;
  if (candidate && candidate !== adopted) {
    // derive from props during render (React-endorsed pattern)
    setAdopted(candidate);
    if (jobId !== candidate) setJobId(candidate);
  }
  const jobQuery = useJob(jobId);
  const job = jobQuery.data;
  const handled = useRef<string | null>(null);

  useEffect(() => {
    if (!job || !FINISHED.includes(job.status) || handled.current === job.id) return;
    handled.current = job.id;
    qc.invalidateQueries({ queryKey: modpackPrefix(instance.id) });
    qc.invalidateQueries({ queryKey: qk.backups(instance.id) });
    qc.invalidateQueries({ queryKey: qk.instanceJobs(instance.id) });
    invalidate(instance.id);
    if (job.status === "succeeded") toast.success(`${job.title}: done`);
    else if (job.status === "failed") toast.error(`${job.title} failed${job.error ? `: ${job.error}` : ""}`);
    else toast.message(`${job.title} was cancelled`);
  }, [job, qc, invalidate, instance.id]);

  return {
    jobId,
    job,
    finished: jobQuery.isError || (!!job && FINISHED.includes(job.status)),
    // an unreadable job (e.g. deleted) must not lock the page
    running: !!jobId && !jobQuery.isError && (!job || !FINISHED.includes(job.status)),
    track: (j: Job) => {
      handled.current = null;
      setJobId(j.id);
    },
    dismiss: () => setJobId(null),
  };
}
