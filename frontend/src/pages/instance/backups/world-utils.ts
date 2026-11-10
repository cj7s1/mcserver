/** Query, labels and small helpers for the World section of the Backups page (GET /instances/{id}/world). */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { WorldFolder, WorldInfo } from "@/lib/types";

/** Job kinds of the world section (backend apps/instances/handlers.py); a reset is started from Settings → Danger zone. */
export const WORLD_JOB_KINDS = ["world_replace", "world_reset"];

export const isWorldJob = (kind: string | null | undefined) => !!kind && WORLD_JOB_KINDS.includes(kind);

/** Archive types the backend accepts (apps/instances/views.py ARCHIVE_SUFFIXES). */
export const ARCHIVE_SUFFIXES = [".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz"];

export const ARCHIVE_ACCEPT = ARCHIVE_SUFFIXES.join(",");

export function isArchiveName(name: string, suffixes: readonly string[] = ARCHIVE_SUFFIXES): boolean {
  const lower = name.trim().toLowerCase();
  return suffixes.some((s) => lower.endsWith(s) && lower.length > s.length);
}

export function useWorld(instanceId: string, { enabled = true, busy = false }: { enabled?: boolean; busy?: boolean } = {}) {
  return useQuery({
    queryKey: qk.world(instanceId),
    queryFn: () => api.get<WorldInfo>(`/instances/${instanceId}/world`),
    enabled,
    // the agent walks every file of the world for the sizes: no polling while a job changes it
    // (the section refetches when the job ends), and only once a minute otherwise
    refetchInterval: busy ? false : 60_000,
    staleTime: 10_000,
  });
}

export const LEVEL_SOURCE_LABEL: Record<WorldInfo["level_name_source"], string> = {
  "server.properties": "level-name in server.properties",
  env: "LEVEL environment variable",
  default: "default (no level-name set)",
};

export const ROLE_LABEL: Record<WorldFolder["role"], string> = {
  overworld: "Overworld",
  nether: "Nether",
  end: "The End",
};

/** Human summary of where the nether and the end are stored. */
export function dimensionsSummary(world: Pick<WorldInfo, "layout" | "folders">): string {
  if (world.layout === "bukkit") {
    const present = world.folders.filter((f) => f.role !== "overworld" && f.exists).map((f) => ROLE_LABEL[f.role]);
    return present.length
      ? `${present.join(" and ")} in separate folders (Paper/Bukkit layout)`
      : "Nether and End are created in separate folders (Paper/Bukkit layout)";
  }
  const inside = world.folders.find((f) => f.role === "overworld")?.dimensions ?? [];
  return inside.length
    ? `${inside.map((d) => ROLE_LABEL[d]).join(" and ")} inside the world folder`
    : "Nether and End are stored inside the world folder";
}

/** The folders a replacement swaps out: every existing one for this layout. */
export function replacedFolders(world: Pick<WorldInfo, "folders">): string[] {
  return world.folders.filter((f) => f.exists).map((f) => f.path);
}
