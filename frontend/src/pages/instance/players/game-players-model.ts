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
