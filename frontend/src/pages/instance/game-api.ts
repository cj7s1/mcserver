/**
 * API hooks of the non-Minecraft game features (backend contract B3-B10): the catalog entry of an
 * instance's game, its version catalog, game settings (config files), secrets, setup, saves.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { gameId } from "@/lib/games";
import { qk, useInvalidateInstance } from "@/lib/queries";
import type {
  ConfigFilesResponse,
  ConfigPatchResult,
  ConfigValue,
  GameInfo,
  GameVersionsResponse,
  Instance,
  Job,
  SavesInfo,
  SecretsResponse,
} from "@/lib/types";
import { useGames } from "../modpacks/modpackQueries";

/** The catalog entry of an instance's game (undefined while loading, for an older panel, or an unknown game). */
export function useGameInfo(instance: Pick<Instance, "game"> | string | null | undefined): GameInfo | undefined {
  const id = typeof instance === "string" ? instance : instance ? gameId(instance) : null;
  const games = useGames(!!id);
  return id ? games.data?.find((g) => g.id === id) : undefined;
}

/** GET /catalog/games/<id>/versions (only for games with has_versions). */
export function useGameVersionCatalog(game: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: qk.gameVersions(game ?? ""),
    queryFn: ({ signal }) => api.get<GameVersionsResponse>(`/catalog/games/${encodeURIComponent(game!)}/versions`, undefined, signal),
    enabled: !!game && enabled,
    staleTime: 10 * 60_000,
    retry: 1,
  });
}

export function useConfigFiles(instanceId: string, enabled = true) {
  return useQuery({
    queryKey: qk.gameConfig(instanceId),
    queryFn: () => api.get<ConfigFilesResponse>(`/instances/${instanceId}/config`),
    enabled,
    retry: false,
  });
}

export function usePatchConfigFile(instanceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ file, values }: { file: string; values: Record<string, ConfigValue> }) =>
      api.patch<ConfigPatchResult>(`/instances/${instanceId}/config/${encodeURIComponent(file)}`, { values }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.gameConfig(instanceId) }),
  });
}

export function useSecrets(instanceId: string, enabled = true) {
  return useQuery({
    queryKey: qk.secrets(instanceId),
    queryFn: () => api.get<SecretsResponse>(`/instances/${instanceId}/secrets`),
    enabled,
    retry: false,
  });
}

export function usePutSecrets(instanceId: string) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  return useMutation({
    mutationFn: (values: Record<string, string | null>) => api.put<SecretsResponse>(`/instances/${instanceId}/secrets`, { values }),
    onSuccess: (data) => {
      qc.setQueryData(qk.secrets(instanceId), data);
      invalidate(instanceId);
    },
  });
}

/** POST /instances/{id}/setup: queue the `setup` job (optionally for one step). */
export function useRunSetup(instanceId: string) {
  const invalidate = useInvalidateInstance();
  return useMutation({
    mutationFn: (step?: string) => api.post<Job>(`/instances/${instanceId}/setup`, step ? { step } : {}),
    onSuccess: () => invalidate(instanceId),
  });
}

export function useSaves(instanceId: string, enabled = true) {
  return useQuery({
    queryKey: qk.saves(instanceId),
    queryFn: () => api.get<SavesInfo>(`/instances/${instanceId}/saves`),
    enabled,
    retry: false,
  });
}
