/** TanStack Query keys and hooks shared across pages. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { AuthState, Instance, Job, Stats, SystemInfo } from "./types";

export const qk = {
  auth: ["auth"] as const,
  instances: ["instances"] as const,
  instance: (id: string) => ["instances", id] as const,
  stats: (id: string) => ["instances", id, "stats"] as const,
  instanceJobs: (id: string) => ["instances", id, "jobs"] as const,
  properties: (id: string) => ["instances", id, "properties"] as const,
  files: (id: string, path: string) => ["instances", id, "files", path] as const,
  backups: (id: string) => ["instances", id, "backups"] as const,
  world: (id: string) => ["instances", id, "world"] as const,
  schedules: (id: string) => ["instances", id, "schedules"] as const,
  players: (id: string) => ["instances", id, "players"] as const,
  content: (id: string) => ["instances", id, "content"] as const,
  access: (id: string) => ["instances", id, "access"] as const,
  jobs: ["jobs"] as const,
  job: (id: string) => ["jobs", id] as const,
  system: ["system"] as const,
  settings: ["system", "settings"] as const,
  users: ["users"] as const,
  roles: ["roles"] as const,
  audit: ["audit"] as const,
};

export function useAuthState() {
  return useQuery({ queryKey: qk.auth, queryFn: () => api.get<AuthState>("/auth/state"), staleTime: 60_000 });
}

export function useInstances(archived: "false" | "true" | "all" = "false") {
  return useQuery({
    queryKey: [...qk.instances, { archived }],
    queryFn: () => api.get<Instance[]>("/instances/", { archived }),
    refetchInterval: 10_000,
  });
}

export function useInstance(id: string | undefined) {
  return useQuery({
    queryKey: qk.instance(id ?? ""),
    queryFn: () => api.get<Instance>(`/instances/${id}`),
    enabled: !!id,
    refetchInterval: (q) => {
      const d = q.state.data as Instance | undefined;
      if (!d) return 5000;
      const transitional = ["installing", "starting", "stopping", "restarting", "updating", "backing_up", "restoring"];
      return transitional.includes(d.status) || d.active_job ? 2000 : 8000;
    },
  });
}

export function useStats(id: string | undefined, enabled = true) {
  return useQuery({
    queryKey: qk.stats(id ?? ""),
    queryFn: () => api.get<Stats>(`/instances/${id}/stats`),
    enabled: !!id && enabled,
    refetchInterval: 5000,
    retry: false,
  });
}

export function useJobs(params: { instance?: string; status?: string; limit?: number } = {}) {
  return useQuery({
    queryKey: [...qk.jobs, params],
    queryFn: () => api.get<Job[]>("/jobs", params),
    refetchInterval: 5000,
  });
}

export function useJob(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.job(id ?? ""),
    queryFn: () => api.get<Job>(`/jobs/${id}`),
    enabled: !!id,
    refetchInterval: (q) => {
      const d = q.state.data as Job | undefined;
      return d && (d.status === "succeeded" || d.status === "failed" || d.status === "cancelled") ? false : 1500;
    },
  });
}

export function useSystemInfo() {
  return useQuery({ queryKey: qk.system, queryFn: () => api.get<SystemInfo>("/system/info"), refetchInterval: 30_000 });
}

/** Invalidate everything related to an instance (after an action). */
export function useInvalidateInstance() {
  const qc = useQueryClient();
  return (id?: string) => {
    qc.invalidateQueries({ queryKey: qk.instances });
    if (id) qc.invalidateQueries({ queryKey: qk.instance(id) });
    qc.invalidateQueries({ queryKey: qk.jobs });
  };
}

export function hasPerm(perms: readonly string[] | undefined, code: string): boolean {
  return !!perms && perms.includes(code);
}
