/**
 * The Share card's data (B1): `GET /api/instances/<id>/share` (backend apps/instances/share.py).
 *
 * The plain answer never carries the join password, only `password_set`. The password itself (and an invite
 * text with it) is fetched on demand (`?with_password=true`, users who may configure the server) by the copy
 * buttons and dropped right after it was copied: it never enters the query cache.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export type ProtectionKind = "password" | "whitelist" | "accounts" | "none";
/** `online_mode`: Minecraft only, whether players need a Minecraft account (`online-mode`). */
export type ChecklistId = "running" | "public_address" | "protection" | "online_mode" | "ports";

export interface ShareChecklistItem {
  id: ChecklistId | string;
  /** true pass, false fail, null unknown */
  ok: boolean | null;
  label: string;
  hint: string;
}

/** The answer as the backend sends it. */
export interface ShareAnswer {
  game: string;
  game_label: string;
  address: { host: string | null; source: "panel" | "node" | null; note: string };
  connect: string | null;
  ports: { port: number; protocol: "tcp" | "udp"; name: string }[];
  protection: { kind: ProtectionKind; on: boolean | null; label: string };
  steps: string[];
  requirements: string[];
  open_warning: string;
  invite_text: string;
  invite_has_password: boolean;
  checklist: ShareChecklistItem[];
  /** A join password is set (true), none (false), cannot be read now (null); false for games without one. */
  password_set?: boolean | null;
  /** Only in the `with_password=true` answer: "" none set, null unknown or not a password game. */
  password?: string | null;
}

export const shareKey = (instanceId: string) => ["instances", instanceId, "share"] as const;

export function useShareCard(instanceId: string, enabled = true) {
  return useQuery({
    queryKey: shareKey(instanceId),
    queryFn: () => api.get<ShareAnswer>(`/instances/${instanceId}/share`),
    enabled,
    staleTime: 15_000,
    retry: false,
  });
}

/** The join password and the invite text with it, read once for a copy (never cached). */
export async function fetchWithPassword(instanceId: string): Promise<{ password: string; inviteText: string }> {
  const answer = await api.get<ShareAnswer>(`/instances/${instanceId}/share`, { with_password: "true" });
  return { password: typeof answer.password === "string" ? answer.password : "", inviteText: answer.invite_text };
}
