/** Types, query and labels for native filesystem snapshots (GET /instances/{id}/snapshots). */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface SnapshotCapability {
  provider: string | null;
  available: boolean;
  fstype: string | null;
  reason: string | null;
  instance_is_subvolume: boolean;
}

export interface Snapshot {
  name: string;
  label: string;
  created_at: string | null;
  /** missing on snapshots whose metadata sidecar was lost */
  kind?: "manual" | "scheduled" | "pre_restore" | string;
  consistent?: boolean;
}

export interface SnapshotsResponse {
  capability: SnapshotCapability;
  snapshots: Snapshot[];
}

/** Job kinds that change the snapshot list (backend apps/backups/handlers.py). */
export const SNAPSHOT_JOB_KINDS = ["snapshot_create", "snapshot_restore", "snapshot_delete", "snapshot_convert"];

export const isSnapshotJob = (kind: string | null | undefined) => !!kind && SNAPSHOT_JOB_KINDS.includes(kind);

export const snapshotsKey = (instanceId: string) => ["instances", instanceId, "snapshots"] as const;

/** unavailable: node filesystem can't snapshot · convert: folder must become a subvolume first · ready */
export type SnapshotState = "unavailable" | "convert" | "ready";

export function snapshotState(cap: SnapshotCapability | undefined): SnapshotState | null {
  if (!cap) return null;
  if (!cap.available) return "unavailable";
  return cap.instance_is_subvolume ? "ready" : "convert";
}

export function useSnapshots(instanceId: string, { enabled = true, busy = false }: { enabled?: boolean; busy?: boolean } = {}) {
  return useQuery({
    queryKey: snapshotsKey(instanceId),
    queryFn: () => api.get<SnapshotsResponse>(`/instances/${instanceId}/snapshots`),
    enabled,
    refetchInterval: busy ? 3000 : 60_000,
    staleTime: 10_000,
  });
}

export const SNAPSHOT_KIND_INFO: Record<string, { label: string; className: string; hint: string }> = {
  manual: {
    label: "Manual",
    className: "bg-muted text-foreground",
    hint: "Taken by a user from this page. Never removed automatically.",
  },
  scheduled: {
    label: "Scheduled",
    className: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
    hint: "Taken by a snapshot schedule. Each run keeps only the newest scheduled snapshots (the schedule's keep count).",
  },
  pre_restore: {
    label: "Before rollback",
    className: "bg-violet-500/10 text-violet-700 dark:text-violet-300",
    hint: "Safety snapshot of the previous state, taken automatically before a rollback. Never removed automatically.",
  },
};

export const UNKNOWN_SNAPSHOT_KIND = {
  label: "Unknown",
  className: "bg-muted text-muted-foreground",
  hint: "The snapshot's metadata is missing, so its origin is unknown.",
};

/** The label the user gave, or the generated name when there is none. */
export function snapshotTitle(s: Snapshot): string {
  return s.label.trim() || s.name;
}

/**
 * Creation time from the metadata; falls back to the UTC timestamp the agent encodes in every
 * snapshot name (YYYYMMDDTHHMMSSZ[-slug]) when the metadata is missing.
 */
export function snapshotTime(s: Snapshot): string | null {
  if (s.created_at) return s.created_at;
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(s.name);
  return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z` : null;
}
