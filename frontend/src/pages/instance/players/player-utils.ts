/** Types, validation and hooks for Minecraft player management (backend/apps/players). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { api, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";

/** Entries of whitelist.json, ops.json, banned-players.json and banned-ips.json. */
export interface McListEntry {
  uuid?: string;
  name?: string;
  level?: number;
  bypassesPlayerLimit?: boolean;
  created?: string;
  source?: string;
  expires?: string;
  reason?: string;
  ip?: string;
}

export interface McOnline {
  online?: number | null;
  max?: number | null;
  players?: string[];
  raw?: string;
  error?: string;
}

export interface McPlayers {
  running: boolean;
  whitelist: McListEntry[];
  ops: McListEntry[];
  bans: McListEntry[];
  ip_bans: McListEntry[];
  known_players: { name: string; uuid: string; expiresOn?: string }[];
  online?: McOnline;
}

export type PlayerAction = "whitelist_add" | "whitelist_remove" | "op" | "deop" | "ban" | "pardon" | "kick";

export interface PlayerActionResult {
  via: "rcon" | "file";
  output: string;
}

const NAME_RE = /^[A-Za-z0-9_]{1,16}$/;

export function playerNameError(raw: string): string | null {
  const name = raw.trim();
  if (!name) return "Enter a player name";
  if (name.length > 16) return "Player names are at most 16 characters";
  if (!NAME_RE.test(name)) return "Only letters, digits and underscores are allowed";
  return null;
}

export const sameName = (a: string | undefined, b: string | undefined) => !!a && !!b && a.toLowerCase() === b.toLowerCase();

/** Minecraft writes dates as "2024-05-01 12:34:56 +0000". */
export function parseMcDate(value: string | undefined): Date | null {
  if (!value || value === "forever") return null;
  const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([+-]\d{2})(\d{2})$/.exec(value.trim());
  const d = new Date(m ? `${m[1]}T${m[2]}${m[3]}:${m[4]}` : value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatMcDate(value: string | undefined): string {
  if (!value) return "—";
  if (value === "forever") return "Never";
  const d = parseMcDate(value);
  return d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : value;
}

export function usePlayers(instanceId: string, poll: boolean) {
  return useQuery({
    queryKey: qk.players(instanceId),
    queryFn: () => api.get<McPlayers>(`/instances/${instanceId}/players`),
    refetchInterval: poll ? 15_000 : false,
    retry: false,
  });
}

const LABELS: Record<PlayerAction, (n: string) => string> = {
  whitelist_add: (n) => `Added ${n} to the whitelist`,
  whitelist_remove: (n) => `Removed ${n} from the whitelist`,
  op: (n) => `Made ${n} an operator`,
  deop: (n) => `Removed operator status from ${n}`,
  ban: (n) => `Banned ${n}`,
  pardon: (n) => `Unbanned ${n}`,
  kick: (n) => `Kicked ${n}`,
};

/** Replies (vanilla/Paper wording) that mean the command did not change anything. */
const UNCHANGED =
  /^(nothing changed|that player does not exist|no player was found|could not|unknown or incomplete|unknown command|player is (already|not)\b|\S+ is already in )/i;

export function usePlayerAction(instanceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { action: PlayerAction; name: string; reason?: string }) =>
      api.post<PlayerActionResult>(`/instances/${instanceId}/players`, {
        action: v.action,
        name: v.name.trim(),
        ...(v.reason?.trim() ? { reason: v.reason.trim() } : {}),
      }),
    onSuccess: (res, v) => {
      const output = (res.output ?? "").trim();
      if (output && UNCHANGED.test(output)) {
        toast.warning(output, { description: res.via === "rcon" ? "Reply from the server" : undefined });
      } else {
        toast.success(LABELS[v.action](v.name.trim()), {
          description: res.via === "rcon" ? output || "Applied live through RCON" : output,
        });
      }
      qc.invalidateQueries({ queryKey: qk.players(instanceId) });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
}

// --- avatars (third-party image service; opt-in) ------------------------------------------
const AVATAR_KEY = "mcpanel.players.show-heads";

export function useAvatarPreference(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(() => {
    try {
      return localStorage.getItem(AVATAR_KEY) === "1";
    } catch {
      return false;
    }
  });
  const set = (v: boolean) => {
    setOn(v);
    try {
      localStorage.setItem(AVATAR_KEY, v ? "1" : "0");
    } catch {
      /* preference just won't persist */
    }
  };
  return [on, set];
}

export function avatarUrl(entry: { uuid?: string; name?: string }): string | null {
  const ref = entry.uuid ? entry.uuid.replace(/-/g, "") : entry.name;
  return ref ? `https://mc-heads.net/avatar/${encodeURIComponent(ref)}/32` : null;
}
