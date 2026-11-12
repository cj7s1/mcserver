/**
 * System → Discord (B3): the bot's settings and live state. The token is write-only (the page only knows
 * whether one is set); roles and the announcement channel are picked from the Discord server's own lists
 * once the bot has connected, typed as ids before that.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bot, ExternalLink, KeyRound, Loader2, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api, ApiError, errorMessage } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AdminFieldError, AdminHint } from "../admin-shared";
import {
  describeState,
  DISCORD_DOCS_URL,
  discordKey,
  idsError,
  nextTryText,
  parseIds,
  SNOWFLAKE,
  useDiscordSettings,
  type DiscordSettings,
} from "./discord-model";

const NO_CHANNEL = "__none__";

const TONES = {
  ok: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  busy: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  off: "bg-muted text-muted-foreground",
  error: "bg-destructive/15 text-destructive",
} as const;

function StateLine({ data }: { data: DiscordSettings }) {
  const { label, tone } = describeState(data.status);
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm" aria-live="polite">
      <span className={cn("rounded-full px-2.5 py-0.5 text-xs font-medium", TONES[tone])} role="status" aria-label="Bot state">
        {label}
      </span>
      {nextTryText(data.status) && <span className="text-xs text-muted-foreground">{nextTryText(data.status)}</span>}
      {data.status.updated_at ? <span className="text-xs text-muted-foreground">reported {relativeTime(data.status.updated_at * 1000)}</span> : null}
      {data.status.invite_url && (
        <a href={data.status.invite_url} target="_blank" rel="noopener noreferrer" className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
          Invite the bot to your Discord server <ExternalLink className="size-3" aria-hidden />
        </a>
      )}
    </div>
  );
}

function SetupSteps() {
  return (
    <details className="rounded-lg border px-3 py-2 text-sm">
      <summary className="cursor-pointer font-medium">How to set it up (once)</summary>
      <ol className="mt-2 grid list-decimal gap-1 pl-5 text-muted-foreground">
        <li>
          Create an application at{" "}
          <a href="https://discord.com/developers/applications" target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
            discord.com/developers
          </a>
          ; under Bot, reset the token and copy it. Leave every privileged intent off.
        </li>
        <li>In Discord, turn on Developer Mode, right-click your Discord server and copy its server ID.</li>
        <li>Paste the token and the server ID below, choose who may start and stop, switch the bot on and save.</li>
        <li>When it says connected, open “Invite the bot” and add it to your Discord server.</li>
        <li>Turn on “Available in Discord” in each server's Settings → General.</li>
      </ol>
      <a href={DISCORD_DOCS_URL} target="_blank" rel="noopener noreferrer" className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
        The full guide <ExternalLink className="size-3" aria-hidden />
      </a>
    </details>
  );
}

interface Draft {
  enabled: boolean;
  guildId: string;
  roles: string[];
  rolesText: string;
  channelId: string;
  token: string;
}

function toDraft(d: DiscordSettings): Draft {
  return { enabled: d.enabled, guildId: d.guild_id, roles: d.roles, rolesText: d.roles.join(", "), channelId: d.channel_id, token: "" };
}

function DiscordForm({ data }: { data: DiscordSettings }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Draft>(() => toDraft(data));
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const pickRoles = data.status.guild_roles.length > 0;
  const pickChannel = data.status.guild_channels.length > 0;
  const roles = pickRoles ? draft.roles : parseIds(draft.rolesText);

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<DiscordSettings>("/system/discord", body),
    onSuccess: (answer, body) => {
      qc.setQueryData(discordKey, answer);
      setDraft((d) => ({ ...d, token: "", enabled: answer.enabled }));
      toast.success("token" in body && body.token === "" ? "Bot token removed" : "Discord settings saved");
    },
    onError: (err) => {
      if (!(err instanceof ApiError && err.code === "validation_error")) toast.error(errorMessage(err));
    },
  });
  const server = save.error instanceof ApiError ? save.error.fieldErrors : {};
  const errors: Record<string, string> = {};
  if (draft.guildId.trim() && !SNOWFLAKE.test(draft.guildId.trim())) errors.guild_id = "A Discord server id is 15 to 22 digits";
  const rolesErr = idsError(roles);
  if (rolesErr) errors.roles = rolesErr;
  if (draft.channelId && !SNOWFLAKE.test(draft.channelId)) errors.channel_id = "A channel id is 15 to 22 digits";
  const err = (k: string) => errors[k] ?? server[k];

  const body: Record<string, unknown> = {};
  if (draft.enabled !== data.enabled) body.enabled = draft.enabled;
  if (draft.guildId.trim() !== data.guild_id) body.guild_id = draft.guildId.trim();
  if (JSON.stringify([...roles].sort()) !== JSON.stringify([...data.roles].sort())) body.roles = roles;
  if (draft.channelId !== data.channel_id) body.channel_id = draft.channelId;
  if (draft.token.trim()) body.token = draft.token.trim();
  const dirty = Object.keys(body).length > 0;
  const valid = Object.keys(errors).length === 0;

  return (
    <form
      className="grid gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (dirty && valid) save.mutate(body);
      }}
      noValidate
    >
      <div className="flex items-start justify-between gap-4">
        <div className="grid gap-0.5">
          <Label htmlFor="discord-enabled">Bot on</Label>
          <AdminHint>It runs only when it is on and has a token and a Discord server id.</AdminHint>
        </div>
        <Switch id="discord-enabled" checked={draft.enabled} onCheckedChange={(v) => set({ enabled: v })} />
      </div>

      <div className="grid gap-1.5">
        <Label htmlFor="discord-token" className="flex items-center gap-1.5">
          <KeyRound className="size-3.5 text-muted-foreground" aria-hidden /> Bot token
          <span className={cn("text-xs font-normal", data.token_set ? "text-emerald-700 dark:text-emerald-400" : "text-muted-foreground")}>
            · {data.token_set ? "set" : "not set"}
          </span>
        </Label>
        <div className="flex gap-2">
          <Input
            id="discord-token"
            type="password"
            autoComplete="off"
            value={draft.token}
            placeholder={data.token_set ? "Type a new token to replace it" : "Paste the bot token"}
            onChange={(e) => set({ token: e.target.value })}
            aria-invalid={!!server.token || undefined}
          />
          {data.token_set && (
            <Button type="button" variant="outline" size="sm" disabled={save.isPending} onClick={() => save.mutate({ token: "" })}>
              <Trash2 /> Clear
            </Button>
          )}
        </div>
        <AdminHint>Never shown again once saved.</AdminHint>
        <AdminFieldError id="discord-token-err" message={server.token} />
      </div>

      <div className="grid gap-1.5">
        <Label htmlFor="discord-guild">Discord server id</Label>
        <Input
          id="discord-guild"
          inputMode="numeric"
          value={draft.guildId}
          onChange={(e) => set({ guildId: e.target.value })}
          aria-invalid={!!err("guild_id") || undefined}
          className="font-mono"
        />
        <AdminHint>The bot answers in this Discord server only.</AdminHint>
        <AdminFieldError id="discord-guild-err" message={err("guild_id")} />
      </div>

      <fieldset className="grid gap-1.5">
        <legend className="text-sm font-medium">Roles that may start and stop servers</legend>
        {pickRoles ? (
          <div className="grid gap-1.5 sm:grid-cols-2">
            {data.status.guild_roles.map((r) => {
              const id = `discord-role-${r.id}`;
              const checked = draft.roles.includes(r.id);
              return (
                <div key={r.id} className="flex items-center gap-2">
                  <Checkbox
                    id={id}
                    checked={checked}
                    onCheckedChange={(v) => set({ roles: v === true ? [...draft.roles, r.id] : draft.roles.filter((x) => x !== r.id) })}
                  />
                  <Label htmlFor={id} className="font-normal">
                    {r.name}
                  </Label>
                </div>
              );
            })}
          </div>
        ) : (
          <Input
            aria-label="Role ids"
            value={draft.rolesText}
            placeholder="Role ids, separated by commas"
            onChange={(e) => set({ rolesText: e.target.value })}
            aria-invalid={!!err("roles") || undefined}
            className="font-mono"
          />
        )}
        <AdminHint>
          Everyone in the Discord server may list servers and get the join text; only these roles may start and stop (a server can
          name its own roles). Without any role nobody can.
          {!pickRoles && " Once the bot is connected you can pick them from a list."}
        </AdminHint>
        <AdminFieldError id="discord-roles-err" message={err("roles")} />
      </fieldset>

      <div className="grid gap-1.5">
        <Label htmlFor="discord-channel">Announcement channel (optional)</Label>
        {pickChannel ? (
          <Select value={draft.channelId || NO_CHANNEL} onValueChange={(v) => set({ channelId: v === NO_CHANNEL ? "" : v })}>
            <SelectTrigger id="discord-channel" className="w-full sm:w-72">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NO_CHANNEL}>None</SelectItem>
              {data.status.guild_channels.map((c) => (
                <SelectItem key={c.id} value={c.id}>
                  #{c.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <Input
            id="discord-channel"
            inputMode="numeric"
            value={draft.channelId}
            placeholder="Channel id, or empty for none"
            onChange={(e) => set({ channelId: e.target.value.trim() })}
            aria-invalid={!!err("channel_id") || undefined}
            className="font-mono"
          />
        )}
        <AdminHint>Starts and stops are also posted there.</AdminHint>
        <AdminFieldError id="discord-channel-err" message={err("channel_id")} />
      </div>

      <div className="flex justify-end">
        <Button type="submit" size="sm" disabled={!dirty || !valid || save.isPending}>
          {save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save
        </Button>
      </div>
    </form>
  );
}

export function DiscordSection() {
  const query = useDiscordSettings();
  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <Bot className="size-4 text-muted-foreground" aria-hidden /> Discord
        </span>
      }
      description="Friends list, start and stop servers and get the join text with slash commands in your Discord server. The bot only connects out to Discord: nothing is opened."
    >
      {query.isLoading ? (
        <LoadingState label="Loading the Discord settings…" className="py-6" />
      ) : query.error && !query.data ? (
        <ErrorState error={query.error} title="Couldn't load the Discord settings" onRetry={() => query.refetch()} />
      ) : query.data ? (
        <div className="grid gap-4">
          <StateLine data={query.data} />
          <p className="text-xs text-muted-foreground">
            The bot runs on its own: when it is off, cannot connect or fails, the panel, its servers and their schedules keep working
            as usual. Only the slash commands in Discord are unavailable meanwhile.
          </p>
          <SetupSteps />
          <DiscordForm key={`${query.data.enabled}|${query.data.guild_id}|${query.data.roles.join()}|${query.data.channel_id}`} data={query.data} />
        </div>
      ) : null}
    </Section>
  );
}
