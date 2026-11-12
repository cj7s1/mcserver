/** The Discord bot's settings and state (B3): `GET` / `PATCH /api/system/discord` (permission `system.settings`). */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export type DiscordState = "disabled" | "no_token" | "no_guild" | "not_running" | "connecting" | "connected" | "error" | "idle";

export interface DiscordNamed {
  id: string;
  name: string;
}

export interface DiscordSettings {
  enabled: boolean;
  guild_id: string;
  roles: string[];
  channel_id: string;
  /** The token is write-only: only whether one is stored. */
  token_set: boolean;
  status: {
    state: DiscordState | string;
    connected: boolean;
    last_error: string;
    updated_at: number | null;
    bot_name: string;
    guild: DiscordNamed | null;
    guild_roles: DiscordNamed[];
    guild_channels: DiscordNamed[];
    application_id: string | null;
    invite_url: string | null;
    /** With `state: "error"`: when the bot tries again (epoch seconds); null otherwise. */
    retry_at?: number | null;
  };
}

export const discordKey = ["system", "discord"] as const;
export const DISCORD_DOCS_URL = "https://github.com/cj7s1/mcserver/blob/main/docs/FRIENDS.md#3-the-discord-bot";

/** A Discord id (a "snowflake"): 15 to 22 digits. */
export const SNOWFLAKE = /^[0-9]{15,22}$/;

/** The settings and state, read again every few seconds while the page shows them (the bot connects within seconds). */
export function useDiscordSettings(enabled = true) {
  return useQuery({
    queryKey: discordKey,
    queryFn: () => api.get<DiscordSettings>("/system/discord"),
    enabled,
    refetchInterval: 5_000,
    retry: false,
  });
}

/** Ids typed into a field: split at commas and spaces. */
export function parseIds(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean))];
}

/** Why typed ids are not valid Discord ids, or null. */
export function idsError(ids: string[]): string | null {
  const bad = ids.find((id) => !SNOWFLAKE.test(id));
  return bad ? `“${bad}” is not a Discord id (15 to 22 digits)` : null;
}

/** "next try at 14:32" for a bot in error with a retry time, else null. */
export function nextTryText(s: DiscordSettings["status"]): string | null {
  if (s.state !== "error" || typeof s.retry_at !== "number") return null;
  return `next try at ${new Date(s.retry_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
}

/** The state in words for the page. */
export function describeState(s: DiscordSettings["status"]): { label: string; tone: "ok" | "busy" | "off" | "error" } {
  switch (s.state) {
    case "connected":
      return { label: `Connected${s.bot_name ? ` as ${s.bot_name}` : ""}${s.guild ? ` in ${s.guild.name}` : ""}`, tone: "ok" };
    case "connecting":
      return { label: "Connecting to Discord…", tone: "busy" };
    case "error":
      return { label: s.last_error ? `Error: ${s.last_error}` : "Error", tone: "error" };
    case "disabled":
      return { label: "Off", tone: "off" };
    case "no_token":
      return { label: "Needs the bot token", tone: "off" };
    case "no_guild":
      return { label: "Needs the Discord server id", tone: "off" };
    case "idle":
      return { label: "Idle: waiting for its next try", tone: "off" };
    default:
      return { label: "Not running yet: the bot reports within seconds after it starts", tone: "busy" };
  }
}
