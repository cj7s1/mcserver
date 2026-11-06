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
  /**
   * modrinth = identified by hash; hangar / spigot = installed from Hangar / SpigotMC by the panel;
   * manual = not on Modrinth; unknown = identification failed
   */
  source: "modrinth" | "hangar" | "spigot" | "manual" | "unknown";
  /** Set when source is "hangar" (recorded at install time; Hangar has no hash lookup). */
  hangar?: { slug: string; version: string };
  /** Set when source is "spigot": recorded at install time, and the file still has the recorded hash. */
  spigot?: { resource_id: number; title: string | null; version: string | null; url: string | null };
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
  summary: {
    total: number;
    enabled: number;
    disabled: number;
    identified: number;
    manual: number;
    hangar?: number;
    spigot?: number;
  };
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

// --- Hangar (hangar.papermc.io, Paper-family plugin servers only) ---------------------------
export interface HangarSearchHit {
  id: string;
  slug: string;
  title: string | null;
  description: string | null;
  author: string | null;
  icon_url: string | null;
  downloads: number | null;
  /** Hangar stars. */
  follows?: number | null;
  categories?: string[];
  game_versions: string[];
  updated: string | null;
  url: string | null;
  installed: boolean;
}

export interface HangarSearchResponse {
  total: number;
  offset: number;
  limit: number;
  results: HangarSearchHit[];
}

export interface HangarDependency {
  name: string | null;
  type: "required" | "optional";
  external_url: string | null;
}

export interface HangarVersion {
  name: string;
  version_type: string | null;
  game_versions: string[];
  published: string | null;
  file: { filename: string | null; size: number | null; url: string | null; sha256: string | null; external_url: string | null } | null;
  dependencies: HangarDependency[];
  installable: boolean;
  reason?: string;
}

export interface HangarVersionsResponse {
  versions: HangarVersion[];
  minecraft_version: string | null;
}

export interface HangarInstallResponse {
  installed: { filename: string; slug: string; version: string; size: number | null };
  required_dependencies: HangarDependency[];
  notes: string[];
}

/** Hangar search pages are fixed at 20 results by the backend. */
export const HANGAR_PAGE_SIZE = 20;

// --- SpigotMC via Spiget (api.spiget.org, Paper-family plugin servers only) -----------------
/** Why a resource cannot be installed automatically. */
export type SpigotUnavailableReason = "premium" | "external" | "not_a_jar";

export interface SpigotResource {
  id: number;
  title: string;
  /** One-line summary (plain text). */
  description: string | null;
  icon_url: string | null;
  downloads: number | null;
  likes: number | null;
  rating: { average: number | null; count: number | null } | null;
  /** Minecraft versions the author says were tested, for the resource as a whole (e.g. "1.21"). */
  tested_versions: string[];
  updated: string | null;
  released: string | null;
  premium: boolean;
  price: number | null;
  currency: string | null;
  external: boolean;
  file_type: string | null;
  file_size: string | null;
  external_url: string | null;
  source_url: string | null;
  url: string;
  installable: boolean;
  reason: SpigotUnavailableReason | null;
  reason_message: string | null;
  /** Whether tested_versions covers this server's Minecraft version; null when the author lists none. */
  tested: boolean | null;
  installed: boolean;
}

export interface SpigotSearchResponse {
  query: string;
  page: number;
  /** Number of result pages Spiget reports (0 when nothing matched). */
  pages: number;
  size: number;
  results: SpigotResource[];
  minecraft_version: string | null;
}

export interface SpigotVersion {
  id: number;
  name: string;
  released: string | null;
  downloads: number | null;
  /** Download page on SpigotMC (opens in the browser; the panel cannot download it). */
  url: string;
}

/** Codes the install/update endpoints accept in `confirm`. */
export type SpigotWarningCode = "untested_minecraft_version" | "mirror_outdated" | "mirror_unknown";

export interface SpigotWarning {
  code: SpigotWarningCode;
  message: string;
}

export interface SpigotMirror {
  available: boolean;
  error?: string;
  size?: number | null;
  sha1?: string | null;
  uploaded_at?: string | null;
  /** current = stored after SpigotMC's latest release; outdated = before it; unknown = cannot tell */
  freshness?: "current" | "outdated" | "unknown";
}

export interface SpigotInstalledFile {
  filename: string;
  enabled: boolean;
  sha1: string;
  version: string | null;
  update_available: boolean;
}

export interface SpigotDetails {
  resource: SpigotResource & { author: string | null; description_text: string };
  versions: SpigotVersion[];
  latest_version: SpigotVersion | null;
  mirror: SpigotMirror | null;
  warnings: SpigotWarning[];
  installed_files: SpigotInstalledFile[];
  minecraft_version: string | null;
}

export interface SpigotInstallResponse {
  installed: {
    filename: string;
    resource_id: number;
    title: string;
    version: string | null;
    latest_version: string | null;
    mirror: "current" | "outdated" | "unknown";
    size: number | null;
    sha256: string;
  };
  warnings: string[];
  notes: string[];
  restart_required: boolean;
}

export interface SpigotUpdateResponse {
  status: "updated" | "up_to_date";
  filename: string;
  new_filename?: string;
  from_version?: string | null;
  to_version?: string | null;
  version?: string | null;
  latest_version?: string | null;
  size?: number | null;
  sha256?: string;
  warnings: string[];
  restart_required: boolean;
}

/** Spiget search pages are fixed at 20 results by the backend. */
export const SPIGOT_PAGE_SIZE = 20;

/** "1.8 … 1.21, 26.1, 26.2" — the author's tested versions, shortened. */
export function testedSummary(res: Pick<SpigotResource, "tested_versions">): string {
  const t = res.tested_versions;
  if (!t.length) return "no versions listed";
  return t.length > 4 ? `${t[0]} … ${t.slice(-3).join(", ")}` : t.join(", ");
}

// --- keys and paths ---------------------------------------------------------------------
export const contentKeys = {
  inventory: (id: string) => [...qk.content(id), "inventory"] as const,
  updates: (id: string) => [...qk.content(id), "updates"] as const,
  search: (id: string, params: { q: string; offset: number; sort: SearchSort }) => [...qk.content(id), "search", params] as const,
  versions: (id: string, projectId: string) => [...qk.content(id), "versions", projectId] as const,
  hangarSearch: (id: string, params: { q: string; offset: number }) => [...qk.content(id), "hangar", "search", params] as const,
  hangarVersions: (id: string, slug: string) => [...qk.content(id), "hangar", "versions", slug] as const,
  spigotSearch: (id: string, params: { q: string; page: number }) => [...qk.content(id), "spigot", "search", params] as const,
  spigotResource: (id: string, resourceId: number) => [...qk.content(id), "spigot", "resource", resourceId] as const,
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

export function useHangarSearch(instanceId: string, params: { q: string; offset: number }, enabled = true) {
  return useQuery({
    queryKey: contentKeys.hangarSearch(instanceId, params),
    queryFn: ({ signal }) =>
      api.get<HangarSearchResponse>(contentPath(instanceId, "hangar/search"), { q: params.q, offset: params.offset }, signal),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useHangarVersions(instanceId: string, slug: string | null) {
  return useQuery({
    queryKey: contentKeys.hangarVersions(instanceId, slug ?? ""),
    queryFn: () => api.get<HangarVersionsResponse>(contentPath(instanceId, `hangar/projects/${encodeURIComponent(slug ?? "")}/versions`)),
    enabled: !!slug,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useSpigotSearch(instanceId: string, params: { q: string; page: number }, enabled = true) {
  return useQuery({
    queryKey: contentKeys.spigotSearch(instanceId, params),
    queryFn: ({ signal }) =>
      api.get<SpigotSearchResponse>(contentPath(instanceId, "spigot/search"), { q: params.q, page: params.page }, signal),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useSpigotResource(instanceId: string, resourceId: number | null) {
  return useQuery({
    queryKey: contentKeys.spigotResource(instanceId, resourceId ?? 0),
    queryFn: () => api.get<SpigotDetails>(contentPath(instanceId, `spigot/resources/${resourceId}`)),
    enabled: resourceId != null,
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
  return file.project?.title || file.hangar?.slug || file.spigot?.title || file.filename.replace(/\.jar(\.disabled)?$/, "");
}

export function modrinthUrl(project: { url?: string | null; slug?: string | null; id?: string | null } | null | undefined) {
  if (!project) return null;
  if (project.url) return project.url;
  const ref = project.slug || project.id;
  return ref ? `https://modrinth.com/project/${ref}` : null;
}

/** Only http(s) links from provider data are rendered as links (never javascript: or data: URLs). */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const u = new URL(value);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
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
