/** Helpers shared by the dashboard and the server library (src/pages/Dashboard.tsx, ServersPage.tsx). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { toast } from "sonner";
import { api, errorMessage } from "@/lib/api";
import { qk, useInvalidateInstance } from "@/lib/queries";
import type { Instance, InstanceStatus, Job } from "@/lib/types";

export type PowerAction = "start" | "stop" | "restart";

/** Statuses where the container is up or coming up (same rule as InstanceLayout's power controls). */
export const RUNNING_LIKE: readonly InstanceStatus[] = ["running", "starting", "installing", "unhealthy", "restarting"];

export function isRunningLike(i: Pick<Instance, "status">): boolean {
  return RUNNING_LIKE.includes(i.status);
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

const TYPE_LABELS: Record<string, string> = {
  VANILLA: "Vanilla",
  PAPER: "Paper",
  PURPUR: "Purpur",
  FOLIA: "Folia",
  FABRIC: "Fabric",
  QUILT: "Quilt",
  FORGE: "Forge",
  NEOFORGE: "NeoForge",
  MODRINTH: "Modrinth modpack",
  AUTO_CURSEFORGE: "CurseForge modpack",
  FTBA: "FTB modpack",
  CUSTOM: "Custom",
};

export function serverTypeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type.charAt(0) + type.slice(1).toLowerCase();
}

export function loaderLabel(loader: string): string {
  if (!loader) return "—";
  const known = TYPE_LABELS[loader.toUpperCase()];
  if (known && !known.endsWith("modpack")) return known;
  return loader.charAt(0).toUpperCase() + loader.slice(1);
}

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

const POWER_VERBS: Record<PowerAction, string> = { start: "Start", stop: "Stop", restart: "Restart" };

/** POST /instances/{id}/power with toast feedback; one hook instance per server row/card. */
export function useServerPower(instance: Pick<Instance, "id" | "name">) {
  const invalidate = useInvalidateInstance();
  return useMutation({
    mutationFn: (action: PowerAction) => api.post<Job | { status: unknown }>(`/instances/${instance.id}/power`, { action }),
    onSuccess: (_data, action) => {
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
