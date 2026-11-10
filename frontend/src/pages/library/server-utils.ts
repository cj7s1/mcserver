/** Helpers shared by the dashboard and the server library (src/pages/Dashboard.tsx, ServersPage.tsx). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { toast } from "sonner";
import { api, errorMessage } from "@/lib/api";
import { loaderLabel, variantLabel } from "@/lib/games";
import { sendPower, type PowerOutcome } from "@/lib/power";
import { qk, useInvalidateInstance } from "@/lib/queries";
import { RESTART_REQUIRED_NOTE } from "@/lib/restart-required";
import type { Instance, InstanceStatus, Job } from "@/lib/types";

export type PowerAction = "start" | "stop" | "restart";

/** Statuses where the container is up or coming up (same rule as InstanceLayout's power controls). */
export const RUNNING_LIKE: readonly InstanceStatus[] = ["running", "starting", "installing", "unhealthy", "restarting"];

export function isRunningLike(i: Pick<Instance, "status">): boolean {
  return RUNNING_LIKE.includes(i.status);
}

export interface CardNote {
  text: string;
  tone: "error" | "muted" | "warning";
}

/**
 * The one line a server card shows below its facts while no job runs: why the server is down
 * (e.g. "Stopped because the host was shutting down; it is started again automatically"), or
 * that a restart is due. A running server's detail stays in the badge's tooltip.
 */
export function cardNote(i: Pick<Instance, "status" | "status_detail" | "restart_required">): CardNote | null {
  if (i.status === "failed" && i.status_detail) return { text: i.status_detail, tone: "error" };
  if (i.status === "stopped" && i.status_detail) return { text: i.status_detail, tone: "muted" };
  if (i.restart_required) return { text: RESTART_REQUIRED_NOTE, tone: "warning" };
  return null;
}

export type StatusBucket = "running" | "busy" | "stopped" | "failed";

/** Coarse status grouping used for filters and summary counts. */
export function statusBucket(s: InstanceStatus): StatusBucket {
  switch (s) {
    case "running":
    case "unhealthy":
      return "running";
    case "starting":
    case "stopping":
    case "restarting":
    case "installing":
    case "updating":
    case "backing_up":
    case "restoring":
      return "busy";
    case "failed":
    case "unknown":
      return "failed";
    default:
      return "stopped";
  }
}

const STATUS_RANK: Record<InstanceStatus, number> = {
  running: 0,
  unhealthy: 1,
  starting: 2,
  restarting: 2,
  stopping: 2,
  installing: 2,
  updating: 2,
  backing_up: 2,
  restoring: 2,
  failed: 3,
  unknown: 4,
  stopped: 5,
  created: 6,
  archived: 7,
};

export function statusRank(s: InstanceStatus): number {
  return STATUS_RANK[s] ?? 9;
}

/** Variant (server type) and loader labels live in lib/games.ts; these names stay for existing callers. */
export const serverTypeLabel = variantLabel;
export { loaderLabel };

export function loaderText(i: Instance): string {
  return `${loaderLabel(i.loader)}${i.loader_version ? ` ${i.loader_version}` : ""}`;
}

export function portText(i: Instance): string {
  return `${i.game_port}${i.extra_ports?.length ? ` +${i.extra_ports.length}` : ""}`;
}

/** Text matched by the library search box. */
export function searchHaystack(i: Instance): string {
  return [
    i.name,
    i.minecraft_version,
    i.server_type,
    serverTypeLabel(i.server_type),
    i.loader,
    i.loader_version,
    i.modpack_name,
    i.modpack_version_name,
    i.group,
    i.description,
  ]
    .join(" ")
    .toLowerCase();
}

/** Memory reserved by the container (heap + overhead), in MB. */
export function reservedMemoryMb(i: Instance): number {
  return i.container_memory_mb || i.memory_mb;
}

export interface MemoryCommitment {
  /** Container limits of the servers that are up or coming up (they hold their whole heap). */
  runningMb: number;
  /** Container limits of every non-archived server, as if all ran. */
  allMb: number;
}

/**
 * Memory promised to servers: the Dashboard's "Memory reserved" bar, the create wizard and the
 * Resources tab use this one computation. `nodeId` limits it to one host (the host memory figure
 * comes from that node's agent); `excludeId` leaves out the server being edited.
 */
export function memoryCommitment(
  instances: readonly Instance[],
  { nodeId, excludeId }: { nodeId?: number | null; excludeId?: string } = {},
): MemoryCommitment {
  const pool = instances.filter((i) => !i.archived_at && (nodeId == null || i.node === nodeId) && i.id !== excludeId);
  return {
    runningMb: pool.filter(isRunningLike).reduce((sum, i) => sum + reservedMemoryMb(i), 0),
    allMb: pool.reduce((sum, i) => sum + reservedMemoryMb(i), 0),
  };
}

const POWER_VERBS: Record<PowerAction, string> = { start: "Start", stop: "Stop", restart: "Restart" };

/**
 * POST /instances/{id}/power with toast feedback; one hook instance per server row/card. Goes
 * through `sendPower`, so a start refused for lack of memory asks "Start anyway?" like every
 * other start button.
 */
export function useServerPower(instance: Pick<Instance, "id" | "name">) {
  const invalidate = useInvalidateInstance();
  return useMutation<PowerOutcome<Job>, unknown, PowerAction>({
    mutationFn: (action: PowerAction) => sendPower<Job>(instance, { action }),
    onSuccess: (outcome, action) => {
      if (outcome.status === "cancelled") return;
      toast.success(`${POWER_VERBS[action]} requested for ${instance.name}`);
      invalidate(instance.id);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
}

/** POST /instances/{id}/unarchive (instance.delete). Returns the updated instance. */
export function useUnarchive(instance: Pick<Instance, "id" | "name">) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<Instance>(`/instances/${instance.id}/unarchive`),
    onSuccess: () => {
      toast.success(`${instance.name} restored from the archive`);
      qc.invalidateQueries({ queryKey: qk.instances });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
}

/** useState persisted in localStorage (per-browser UI preference). Storage failures are ignored. */
export function useStoredState<T>(key: string, initial: T, validate?: (v: unknown) => v is T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null) return initial;
      const parsed: unknown = JSON.parse(raw);
      return !validate || validate(parsed) ? (parsed as T) : initial;
    } catch {
      return initial;
    }
  });
  const update = useCallback(
    (next: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const v = typeof next === "function" ? (next as (p: T) => T)(prev) : next;
        try {
          localStorage.setItem(key, JSON.stringify(v));
        } catch {
          /* storage unavailable */
        }
        return v;
      });
    },
    [key],
  );
  return [value, update] as const;
}
