/**
 * Shapes and client-side validation for System → Notifications.
 * Mirrors backend/apps/notifications/{views,service,models}.py; the backend re-validates everything.
 */

export type ChannelKind = "discord" | "webhook";

/** GET /notifications/channels item. The URL is write-only; only `url_hint` ("https://host/…") is returned. */
export interface NotificationChannel {
  id: number;
  name: string;
  kind: ChannelKind;
  url_hint: string;
  events: string[];
  /** Server id, or null for all servers. */
  instance: string | null;
  enabled: boolean;
  last_sent_at: string | null;
  last_error: string;
  created_at: string;
}

export interface NotificationEvent {
  id: string;
  label: string;
}

export const channelsKey = ["notifications", "channels"] as const;
export const eventsKey = ["notifications", "events"] as const;

export const KIND_META: Record<ChannelKind, { label: string; short: string; placeholder: string; hint: string }> = {
  discord: {
    label: "Discord webhook",
    short: "Discord",
    placeholder: "https://discord.com/api/webhooks/…",
    hint: "Discord: channel settings → Integrations → Webhooks → Copy webhook URL.",
  },
  webhook: {
    label: "Generic JSON webhook",
    short: "Webhook",
    placeholder: "https://example.org/hooks/mcpanel",
    hint: "Receives a JSON POST with event, title, message, data and sent_at.",
  },
};

const DISCORD_HOSTS = ["discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"];

/** Same rules as backend service.validate_url. Returns an error message or null. */
export function webhookUrlError(kind: ChannelKind, value: string): string | null {
  const v = value.trim();
  if (!v) return "Enter the webhook URL";
  if (v.length > 1000) return "At most 1000 characters";
  let u: URL;
  try {
    u = new URL(v);
  } catch {
    return kind === "discord" ? "Discord webhooks look like https://discord.com/api/webhooks/..." : "Enter a full http(s) URL";
  }
  if ((u.protocol !== "https:" && u.protocol !== "http:") || !u.hostname) return "Enter a full http(s) URL";
  if (kind === "discord") {
    if (u.protocol !== "https:" || !DISCORD_HOSTS.includes(u.hostname.toLowerCase()) || !u.pathname.startsWith("/api/webhooks/"))
      return "Discord webhooks look like https://discord.com/api/webhooks/...";
  }
  return null;
}

export interface ChannelFormState {
  name: string;
  kind: ChannelKind;
  url: string;
  events: string[];
  /** "" = all servers */
  instance: string;
  enabled: boolean;
}

export const EMPTY_CHANNEL_FORM: ChannelFormState = {
  name: "",
  kind: "discord",
  url: "",
  events: ["instance.failed", "instance.crash_loop_stopped", "backup.failed"],
  instance: "",
  enabled: true,
};

export function channelToForm(ch: NotificationChannel): ChannelFormState {
  return {
    name: ch.name,
    kind: ch.kind,
    url: "",
    events: [...ch.events],
    instance: ch.instance ?? "",
    enabled: ch.enabled,
  };
}

/** Validate the add/edit form. On edit a blank URL keeps the stored one, unless the kind changes. */
export function validateChannelForm(
  f: ChannelFormState,
  existing?: NotificationChannel,
  knownEvents?: readonly string[],
): Record<string, string> {
  const errors: Record<string, string> = {};
  const name = f.name.trim();
  if (!name) errors.name = "Enter a name";
  else if (name.length > 80) errors.name = "At most 80 characters";

  const kindChanged = !!existing && existing.kind !== f.kind;
  if (!existing || f.url.trim() || kindChanged) {
    const e = webhookUrlError(f.kind, f.url);
    if (e) errors.url = kindChanged && !f.url.trim() ? `Enter the ${KIND_META[f.kind].label} URL for the new kind` : e;
  }

  if (!f.events.length) errors.events = "Choose at least one event";
  else if (knownEvents && f.events.some((e) => !knownEvents.includes(e))) errors.events = "Unknown event selected";
  return errors;
}

/** POST body (add) or PATCH body with only the changed fields (edit). */
export function channelPayload(f: ChannelFormState, existing?: NotificationChannel): Record<string, unknown> {
  const next: Record<string, unknown> = {
    name: f.name.trim(),
    kind: f.kind,
    events: [...new Set(f.events)].sort(),
    instance: f.instance || null,
    enabled: f.enabled,
  };
  if (!existing) return { ...next, url: f.url.trim() };
  const out: Record<string, unknown> = {};
  const prev: Record<string, unknown> = {
    name: existing.name,
    kind: existing.kind,
    events: [...existing.events].sort(),
    instance: existing.instance,
    enabled: existing.enabled,
  };
  for (const [k, v] of Object.entries(next)) if (JSON.stringify(v) !== JSON.stringify(prev[k])) out[k] = v;
  if (f.url.trim()) out.url = f.url.trim();
  return out;
}

/** Labels for a channel's events, in the server's order; unknown ids are shown as-is. */
export function eventLabels(ids: readonly string[], events: readonly NotificationEvent[] | undefined): string[] {
  if (!events?.length) return [...ids];
  const order = new Map(events.map((e, i) => [e.id, i] as const));
  return [...ids]
    .sort((a, b) => (order.get(a) ?? 999) - (order.get(b) ?? 999))
    .map((id) => events.find((e) => e.id === id)?.label ?? id);
}
