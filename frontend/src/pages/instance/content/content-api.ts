/**
 * Types and query hooks for the mods/plugins manager
 * (backend/apps/content/{views,service}.py, mounted at /api/instances/<id>/content/).
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { api, errorMessage } from "@/lib/api";
import { qk, useInvalidateInstance } from "@/lib/queries";
import type { Instance } from "@/lib/types";

// --- response shapes ----------------------------------------------------------------------
export interface ContentProjectSummary {
  id: string | null;
  slug?: string | null;
  title?: string | null;
  icon_url?: string | null;
  url?: string | null;
  server_side?: string | null;
  client_side?: string | null;
}

export interface ContentDependency {
  project_id: string | null;
  version_id: string | null;
  type: "required" | "optional" | "incompatible";
}

export interface ContentVersion {
  id: string;
  project_id: string | null;
  name: string | null;
  version_number: string | null;
  version_type: string | null;
  loaders: string[];
  game_versions: string[];
  published: string | null;
  downloads: number | null;
  file: { filename: string | null; size: number | null } | null;
  dependencies: ContentDependency[];
}

export interface ContentFile {
  filename: string;
  enabled: boolean;
  size: number;
  modified: number | null;
  sha1: string;
  /** modrinth = identified by hash; manual = not on Modrinth; unknown = identification failed */
  source: "modrinth" | "manual" | "unknown";
  project: ContentProjectSummary | null;
  version: ContentVersion | null;
  compatibility: { loader: boolean; minecraft: boolean | null } | null;
}

export interface ContentInventory {
  kind: "mods" | "plugins";
  directory: string;
  platform: string;
  loaders: string[];
  minecraft_version: string | null;
  is_modpack: boolean;
  restart_required_for_changes: boolean;
  identification: { available: boolean; error: string | null };
  warnings: string[];
  summary: { total: number; enabled: number; disabled: number; identified: number; manual: number };
  files: ContentFile[];
}

export interface ContentUpdate {
  filename: string;
  project: ContentProjectSummary;
  current: ContentVersion;
  latest: ContentVersion;
}

export interface ContentUpdates {
  minecraft_version: string | null;
  loaders: string[];
  checked: number;
  unidentified: number;
  updates: ContentUpdate[];
  warnings: string[];
}

export interface ContentSearchHit {
  id: string;
  slug: string | null;
  title: string | null;
  description: string | null;
  author: string | null;
  icon_url: string | null;
  downloads: number | null;
  follows: number | null;
  categories: string[];
  loaders: string[];
  game_versions: string[];
  updated: string | null;
  server_side: string | null;
  client_side: string | null;
  url: string | null;
  license: string | null;
  installed: boolean;
}

export interface ContentSearchResponse {
  total: number;
  offset: number;
  limit: number;
  results: ContentSearchHit[];
  kind: "mods" | "plugins";
  project_type: string;
  loaders: string[];
  minecraft_version: string | null;
  warnings: string[];
}

export interface ProjectVersionsResponse {
  project: {
    id: string;
    slug: string | null;
    title: string | null;
    description: string | null;
    icon_url: string | null;
    url: string | null;
    server_side: string | null;
    client_side: string | null;
    loaders: string[];
    license: string | null;
  };
  installed: boolean | null;
  minecraft_version: string | null;
  loaders: string[];
  versions: ContentVersion[];
}

export interface MissingDependency {
  project_id: string | null;
  version_id: string | null;
  file_name: string | null;
  required_by: string;
  /** not_installed | disabled | not_on_modrinth | dependency_limit | <error code> */
  reason: string;
  message: string | null;
  title?: string | null;
  slug?: string | null;
}

export interface InstalledEntry {
  project_id: string;
  title: string | null;
  version_id: string;
  version_number: string | null;
  version_type: string | null;
  filename: string;
  size: number | null;
  dependency: boolean;
  required_by: string | null;
}

export interface InstallResponse {
  installed: InstalledEntry[];
  missing_dependencies: MissingDependency[];
  warnings: string[];
  restart_required: boolean;
}

export interface UpdateResultEntry {
  filename: string;
  status: "updated" | "up_to_date" | "skipped" | "failed";
  reason?: string;
  message?: string;
  new_filename?: string;
  from_version?: ContentVersion;
  to_version?: ContentVersion;
  warning?: string;
}

export interface UpdateResponse {
  results: UpdateResultEntry[];
  updated: number;
  failed: number;
  missing_dependencies: MissingDependency[];
  restart_required: boolean;
}

export interface ToggleResponse {
  filename: string;
  previous_filename: string;
  enabled: boolean;
  changed: boolean;
  restart_required: boolean;
}

export interface DeleteResponse {
  deleted: string;
  restart_required: boolean;
}

export interface UploadResponse {
  uploaded: { filename: string; size: number; sha1: string };
  detected_platforms: string[];
  identified: { project: ContentProjectSummary; version: ContentVersion } | null;
  warnings: string[];
  notice: string;
  restart_required: boolean;
}

export type SearchSort = "relevance" | "downloads" | "follows" | "newest" | "updated";

// --- keys and paths ---------------------------------------------------------------------
export const contentKeys = {
  inventory: (id: string) => [...qk.content(id), "inventory"] as const,
  updates: (id: string) => [...qk.content(id), "updates"] as const,
  search: (id: string, params: { q: string; offset: number; sort: SearchSort }) => [...qk.content(id), "search", params] as const,
  versions: (id: string, projectId: string) => [...qk.content(id), "versions", projectId] as const,
};

export const contentPath = (id: string, sub = "") => `/instances/${id}/content/${sub}`;

export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;
export const SEARCH_PAGE_SIZE = 20;

// --- hooks ------------------------------------------------------------------------------
export function useContentInventory(instanceId: string) {
  return useQuery({
    queryKey: contentKeys.inventory(instanceId),
    queryFn: () => api.get<ContentInventory>(contentPath(instanceId)),
    staleTime: 30_000,
    retry: false,
  });
}

/** Update checks query Modrinth for every file, so they only run when asked for. */
export function useContentUpdates(instanceId: string, enabled: boolean) {
  return useQuery({
    queryKey: contentKeys.updates(instanceId),
    queryFn: () => api.get<ContentUpdates>(contentPath(instanceId, "updates")),
    enabled,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useContentSearch(instanceId: string, params: { q: string; offset: number; sort: SearchSort }, enabled = true) {
  return useQuery({
    queryKey: contentKeys.search(instanceId, params),
    queryFn: ({ signal }) =>
      api.get<ContentSearchResponse>(
        contentPath(instanceId, "search"),
        { q: params.q, offset: params.offset, limit: SEARCH_PAGE_SIZE, sort: params.sort },
        signal,
      ),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useProjectVersions(instanceId: string, projectId: string | null) {
  return useQuery({
    queryKey: contentKeys.versions(instanceId, projectId ?? ""),
    queryFn: () => api.get<ProjectVersionsResponse>(contentPath(instanceId, `projects/${encodeURIComponent(projectId ?? "")}/versions`)),
    enabled: !!projectId,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

/** Invalidate the inventory and everything derived from it (installed flags, updates). */
export function useInvalidateContent(instanceId: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: qk.content(instanceId) });
}

// --- restart hint -----------------------------------------------------------------------
const RUNNING_LIKE = ["running", "starting", "unhealthy", "restarting"];

export function isRunningLike(instance: Instance): boolean {
  return RUNNING_LIKE.includes(instance.status);
}

/**
 * Remembers (per browser tab) that content changed while the server was running, keyed by the
 * server's start time, so the "restart to load changes" hint disappears after the next restart.
 */
export function useRestartHint(instance: Instance) {
  const storageKey = `mcpanel.content.pending-restart.${instance.id}`;
  const marker = instance.started_at ?? "running";
  const [stored, setStored] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(storageKey);
    } catch {
      return null;
    }
  });
  const pending = isRunningLike(instance) && stored === marker;
  const mark = () => {
    try {
      sessionStorage.setItem(storageKey, marker);
    } catch {
      /* storage unavailable: the hint just won't survive navigation */
    }
    setStored(marker);
  };
  const clear = () => {
    try {
      sessionStorage.removeItem(storageKey);
    } catch {
      /* ignore */
    }
    setStored(null);
  };
  return { pending, mark, clear };
}

export function useRestartServer(instance: Instance) {
  const invalidate = useInvalidateInstance();
  return useMutation({
    mutationFn: () => api.post(`/instances/${instance.id}/power`, { action: "restart" }),
    onSuccess: () => {
      toast.success("Restart requested");
      invalidate(instance.id);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
}

// --- small formatting helpers -----------------------------------------------------------
export function displayTitle(file: ContentFile): string {
  return file.project?.title || file.filename.replace(/\.jar(\.disabled)?$/, "");
}

export function modrinthUrl(project: { url?: string | null; slug?: string | null; id?: string | null } | null | undefined) {
  if (!project) return null;
  if (project.url) return project.url;
  const ref = project.slug || project.id;
  return ref ? `https://modrinth.com/project/${ref}` : null;
}

export function versionTypeClass(type: string | null | undefined): string {
  if (type === "beta") return "bg-amber-500/10 text-amber-700 dark:text-amber-300";
  if (type === "alpha") return "bg-red-500/10 text-red-700 dark:text-red-300";
  return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
}

export function nouns(kind: "mods" | "plugins") {
  return kind === "plugins"
    ? { one: "plugin", many: "plugins", One: "Plugin", Many: "Plugins" }
    : { one: "mod", many: "mods", One: "Mod", Many: "Mods" };
}
