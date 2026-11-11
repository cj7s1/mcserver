/**
 * Query hooks for the modpack browser, catalog and port endpoints used by the
 * create-server wizard and the modpacks page.
 *
 * Endpoints (backend/apps/modpacks/views.py, backend/apps/catalog/views.py, backend/apps/core/views.py):
 *   GET /modpacks/providers                                   -> { providers: ProviderInfo[] }
 *   GET /modpacks/{p}/search                                  -> SearchResult<Project>
 *   GET /modpacks/{p}/projects/{id}                           -> Project (+ project_type, source_url, ...)
 *   GET /modpacks/{p}/projects/{id}/releases                  -> { releases: Release[] }
 *   GET /modpacks/{p}/projects/{id}/releases/{vid}            -> Release (with changelog)
 *   GET /modpacks/{p}/projects/{id}/releases/{vid}/resolve    -> ResolvedModpack
 *   GET /catalog/games                                        -> GameInfo[] (panels with game definitions)
 *   GET /catalog/server-types | versions | loaders | java
 *   GET /system/ports                                         -> { range, in_use[], suggested }
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import type {
  GameInfo,
  JavaInfo,
  Project,
  Provider,
  ProviderInfo,
  Release,
  ResolvedModpack,
  SearchResult,
  ServerTypeInfo,
  VersionEntry,
} from "@/lib/types";

export type SortKey = "relevance" | "downloads" | "follows" | "newest" | "updated";

/** Project detail as returned by the provider adapters (superset of the shared Project type). */
export interface ProjectDetailData extends Project {
  project_type?: string;
  source_url?: string | null;
  issues_url?: string | null;
}

export interface ReleaseData extends Release {
  status?: string;
  is_server_pack?: boolean;
  distribution_allowed?: boolean;
}

export interface ResolvedModpackData extends ResolvedModpack {
  file?: { filename: string; url: string | null; size: number; sha1?: string | null } | null;
}

export interface CatalogVersions {
  server_type: string;
  latest_release: string;
  versions: (VersionEntry & { java_minimum?: number | null })[];
}

export interface LoaderEntry {
  version: string;
  stable: boolean;
  recommended?: boolean;
  latest?: boolean;
  channel?: string;
  time?: string;
}

export interface PortsInfo {
  range: [number, number];
  in_use: { port: number; used_by: string }[];
  suggested: number | null;
}

export interface SearchParams {
  q: string;
  loaders: string[];
  gameVersion: string;
  sort: SortKey;
  offset: number;
  limit: number;
}

export const mpk = {
  providers: ["modpacks", "providers"] as const,
  search: (provider: string, params: SearchParams) => ["modpacks", provider, "search", params] as const,
  project: (provider: string, id: string) => ["modpacks", provider, "project", id] as const,
  releases: (provider: string, id: string, prerelease: boolean) =>
    ["modpacks", provider, "project", id, "releases", { prerelease }] as const,
  release: (provider: string, id: string, vid: string) => ["modpacks", provider, "project", id, "release", vid] as const,
  resolve: (provider: string, id: string, vid: string) => ["modpacks", provider, "project", id, "resolve", vid] as const,
  games: ["catalog", "games"] as const,
  serverTypes: ["catalog", "server-types"] as const,
  versions: (st: string, snapshots: boolean) => ["catalog", "versions", st, { snapshots }] as const,
  loaders: (st: string, mc: string) => ["catalog", "loaders", st, mc] as const,
  java: (mc: string, st: string) => ["catalog", "java", mc, st] as const,
  ports: ["system", "ports"] as const,
};

/** Do not hammer upstream providers with retries on deterministic errors (4xx, missing keys). */
function retryTransient(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  if (error instanceof ApiError && error.code === "curseforge_key_missing") return false;
  return failureCount < 1;
}

export function useProviders() {
  return useQuery({
    queryKey: mpk.providers,
    queryFn: async () => (await api.get<{ providers: ProviderInfo[] }>("/modpacks/providers")).providers,
    staleTime: 5 * 60_000,
  });
}

export function useModpackSearch(provider: Provider, params: SearchParams, enabled = true) {
  return useQuery({
    queryKey: mpk.search(provider, params),
    queryFn: ({ signal }) =>
      api.get<SearchResult<Project>>(
        `/modpacks/${provider}/search`,
        {
          q: params.q,
          type: "modpack",
          loaders: params.loaders.join(","),
          game_version: params.gameVersion,
          sort: params.sort,
          offset: params.offset,
          limit: params.limit,
        },
        signal,
      ),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    retry: retryTransient,
  });
}

export function useModpackProject(provider: Provider, projectId: string | null | undefined) {
  return useQuery({
    queryKey: mpk.project(provider, projectId ?? ""),
    queryFn: ({ signal }) =>
      api.get<ProjectDetailData>(`/modpacks/${provider}/projects/${encodeURIComponent(projectId!)}`, undefined, signal),
    enabled: !!projectId,
    staleTime: 5 * 60_000,
    retry: retryTransient,
  });
}

export function useModpackReleases(provider: Provider, projectId: string | null | undefined, includePrerelease: boolean) {
  return useQuery({
    queryKey: mpk.releases(provider, projectId ?? "", includePrerelease),
    queryFn: async ({ signal }) =>
      (
        await api.get<{ releases: ReleaseData[] }>(
          `/modpacks/${provider}/projects/${encodeURIComponent(projectId!)}/releases`,
          { include_prerelease: includePrerelease ? "true" : "false" },
          signal,
        )
      ).releases,
    enabled: !!projectId,
    staleTime: 2 * 60_000,
    retry: retryTransient,
  });
}

export function useReleaseDetail(provider: Provider, projectId: string, versionId: string, enabled: boolean) {
  return useQuery({
    queryKey: mpk.release(provider, projectId, versionId),
    queryFn: ({ signal }) =>
      api.get<ReleaseData>(
        `/modpacks/${provider}/projects/${encodeURIComponent(projectId)}/releases/${encodeURIComponent(versionId)}`,
        undefined,
        signal,
      ),
    enabled,
    staleTime: 10 * 60_000,
    retry: retryTransient,
  });
}

export function useResolveRelease(provider: Provider, projectId: string | null | undefined, versionId: string | null | undefined) {
  return useQuery({
    queryKey: mpk.resolve(provider, projectId ?? "", versionId ?? ""),
    queryFn: ({ signal }) =>
      api.get<ResolvedModpackData>(
        `/modpacks/${provider}/projects/${encodeURIComponent(projectId!)}/releases/${encodeURIComponent(versionId!)}/resolve`,
        undefined,
        signal,
      ),
    enabled: !!projectId && !!versionId,
    staleTime: 5 * 60_000,
    retry: retryTransient,
  });
}

/**
 * The games this panel can run (GET /catalog/games). An older panel has no such endpoint: the
 * answer is then an empty list, and the wizard is the Minecraft wizard as before.
 */
export function useGames(enabled = true) {
  return useQuery({
    queryKey: mpk.games,
    enabled,
    queryFn: async () => {
      try {
        const res = await api.get<GameInfo[] | { games: GameInfo[] }>("/catalog/games");
        return Array.isArray(res) ? res : (res?.games ?? []);
      } catch (e) {
        if (e instanceof ApiError && (e.status === 404 || e.status === 405)) return [];
        throw e;
      }
    },
    staleTime: 60 * 60_000,
    retry: retryTransient,
  });
}

export function useServerTypes() {
  return useQuery({
    queryKey: mpk.serverTypes,
    queryFn: async () => (await api.get<{ server_types: ServerTypeInfo[] }>("/catalog/server-types")).server_types,
    staleTime: 60 * 60_000,
  });
}

export function useGameVersions(serverType: string | null | undefined, snapshots = false) {
  return useQuery({
    queryKey: mpk.versions(serverType ?? "", snapshots),
    queryFn: ({ signal }) =>
      api.get<CatalogVersions>("/catalog/versions", { server_type: serverType!, snapshots: snapshots ? "true" : "false" }, signal),
    enabled: !!serverType,
    staleTime: 10 * 60_000,
    retry: retryTransient,
  });
}

export function useLoaderVersions(serverType: string | null | undefined, minecraftVersion: string | null | undefined) {
  return useQuery({
    queryKey: mpk.loaders(serverType ?? "", minecraftVersion ?? ""),
    queryFn: async ({ signal }) =>
      (
        await api.get<{ versions: LoaderEntry[] }>(
          "/catalog/loaders",
          { server_type: serverType!, minecraft_version: minecraftVersion! },
          signal,
        )
      ).versions,
    enabled: !!serverType && !!minecraftVersion && serverType !== "VANILLA",
    staleTime: 10 * 60_000,
    retry: retryTransient,
  });
}

export function useJavaInfo(minecraftVersion: string | null | undefined, serverType: string | null | undefined) {
  return useQuery({
    queryKey: mpk.java(minecraftVersion ?? "", serverType ?? ""),
    queryFn: ({ signal }) =>
      api.get<JavaInfo>("/catalog/java", { minecraft_version: minecraftVersion!, server_type: serverType ?? "" }, signal),
    enabled: !!minecraftVersion,
    staleTime: 60 * 60_000,
    retry: retryTransient,
  });
}

export function usePorts() {
  return useQuery({
    queryKey: mpk.ports,
    queryFn: () => api.get<PortsInfo>("/system/ports"),
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
}
