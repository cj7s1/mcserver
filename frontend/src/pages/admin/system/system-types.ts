/** Local shapes for GET /system/info (agent part), GET /system/ports (backend/apps/core/views.py). */
import type { SystemInfo, SystemSettings } from "@/lib/types";

/** Names of the stored secrets (backend apps/core/views.py SECRETS). */
const SECRET_LABELS: Record<string, string> = { curseforge_api_key: "CurseForge API key" };

/** Labels of stored secrets the panel cannot decrypt (GET /system/settings secrets_unreadable). */
export function unreadableSecrets(settings: Pick<SystemSettings, "secrets_unreadable">): string[] {
  return Object.entries(settings.secrets_unreadable ?? {})
    .filter(([, unreadable]) => unreadable)
    .map(([key]) => SECRET_LABELS[key] ?? key);
}

export interface StorageVolume {
  path: string;
  fstype: string;
  mount_point: string | null;
  source?: string | null;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  snapshot: { provider: string | null; available: boolean; reason: string | null };
}

export interface AgentPolicy {
  allowed_images?: string[];
  networks?: string[];
  port_min?: number;
  port_max?: number;
  max_memory_mb?: number;
  runtime_uid?: number;
  runtime_gid?: number;
}

export interface DockerInfo {
  server_version?: string | null;
  api_version?: string | null;
  os?: string | null;
  kernel?: string | null;
  architecture?: string | null;
  cpus?: number | null;
  memory_bytes?: number | null;
  storage_driver?: string | null;
  cgroup_version?: string | null;
  containers_running?: number | null;
  name?: string | null;
}

export interface PortsInfo {
  range: [number, number];
  in_use: { port: number; used_by: string }[];
  suggested: number | null;
}

export type AgentInfo = SystemInfo["agent"];

export function agentDocker(agent: AgentInfo | undefined): DockerInfo {
  return (agent?.docker ?? {}) as DockerInfo;
}

export function agentPolicy(agent: AgentInfo | undefined): AgentPolicy | undefined {
  return agent?.policy as AgentPolicy | undefined;
}

export function agentStorage(agent: AgentInfo | undefined): Record<string, StorageVolume> {
  return (agent?.storage ?? {}) as Record<string, StorageVolume>;
}

/** "restic 0.19.1 compiled with go1.26.4 on linux/amd64" -> "0.19.1" */
export function resticVersion(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const m = /restic\s+v?([\d.]+)/i.exec(raw);
  return m ? m[1] : raw;
}
