/**
 * The Share card's data (B1): `GET /api/instances/<id>/share` (backend apps/instances/share.py).
 *
 * The plain answer carries the join password for users who may configure the server. It never goes into the
 * query cache: the query keeps only whether a password is set; the password itself is fetched on demand
 * (`?with_password=true`) by the copy buttons and dropped right after it was copied.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export type ProtectionKind = "password" | "whitelist" | "accounts" | "none";
export type ChecklistId = "running" | "public_address" | "protection" | "ports";

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
  /** Only for users who may configure the server: "" none set, null unknown or not a password game. */
  password?: string | null;
}

/** What the page keeps: the answer without the password, plus whether this user may read one and whether one is set. */
export interface ShareCardData extends Omit<ShareAnswer, "password"> {
  /** The answer carried the password field: this user may copy the password (with_password). */
  passwordReadable: boolean;
  /** A join password is set (true), none (false), unknown (null). */
  passwordSet: boolean | null;
}

export const shareKey = (instanceId: string) => ["instances", instanceId, "share"] as const;

export function withoutPassword(answer: ShareAnswer): ShareCardData {
  const { password, ...rest } = answer;
  const readable = Object.prototype.hasOwnProperty.call(answer, "password");
  return {
    ...rest,
    passwordReadable: readable,
    passwordSet: !readable || password === null || password === undefined ? null : password !== "",
  };
}

export function useShareCard(instanceId: string, enabled = true) {
  return useQuery({
    queryKey: shareKey(instanceId),
    queryFn: async () => withoutPassword(await api.get<ShareAnswer>(`/instances/${instanceId}/share`)),
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
