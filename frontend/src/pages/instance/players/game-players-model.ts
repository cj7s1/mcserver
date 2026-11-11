/** Lists, labels and name rules of the non-Minecraft players page (GamePlayers.tsx). */
import type { PlayersResponse } from "@/lib/types";
import type { PlayerAction } from "./player-utils";

export type ListKey = "whitelist" | "ops" | "bans";

const DEFAULT_LABELS: Record<ListKey, string> = { whitelist: "Whitelist", ops: "Admins", bans: "Bans" };
export const ACTIONS: Record<ListKey, { add: PlayerAction; remove: PlayerAction; addLabel: string; removeLabel: string }> = {
  whitelist: { add: "whitelist_add", remove: "whitelist_remove", addLabel: "Add", removeLabel: "Remove" },
  ops: { add: "op", remove: "deop", addLabel: "Promote", removeLabel: "Demote" },
  bans: { add: "ban", remove: "pardon", addLabel: "Ban", removeLabel: "Unban" },
};

/** The lists' labels: what the API says, else the catalog's, else the defaults. */
export function listLabels(
  data: Pick<PlayersResponse, "labels"> | undefined,
  catalog: Partial<Record<ListKey, string>> | undefined,
): Record<ListKey, string> {
  return { ...DEFAULT_LABELS, ...(catalog ?? {}), ...(data?.labels ?? {}) };
}

/** An entry of one of the lists as the page shows it. */
export interface GameListEntry {
  /** What actions name the player by: a name, an id (Rust: SteamID64) or an IP address. */
  name: string;
  /** The player's name when `name` is an id (Rust's owners and bans). */
  display_name?: string;
  reason?: string;
  /** An IP ban (HKMP): lifted with the address. */
  ip?: boolean;
}

/** The entries of a list; the ban list also holds the IP bans (HKMP), which are lifted by their address. */
export function listEntries(data: Pick<PlayersResponse, ListKey | "ip_bans">, list: ListKey): GameListEntry[] {
  const out: GameListEntry[] = (data[list] ?? []).map((e) => ({
    name: e.name,
    display_name: e.display_name && e.display_name !== e.name ? e.display_name : undefined,
    reason: e.reason,
  }));
  if (list === "bans") for (const b of data.ip_bans ?? []) if (b.ip) out.push({ name: b.ip, reason: b.reason, ip: true });
  return out;
}

/** Why a typed player name is invalid for the game (pattern from the catalog; default: 1-60 safe characters). */
export function gamePlayerNameError(raw: string, pattern?: string): string | null {
  const name = raw.trim();
  if (!name) return "Enter a player name";
  let re: RegExp;
  try {
    re = new RegExp(`^(?:${pattern || "[A-Za-z0-9_.-]{1,60}"})$`);
  } catch {
    re = /^[A-Za-z0-9_.-]{1,60}$/;
  }
  return re.test(name) ? null : "Not a valid player name for this game";
}
