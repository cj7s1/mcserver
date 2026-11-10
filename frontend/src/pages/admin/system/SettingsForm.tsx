import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, ExternalLink, KeyRound, Loader2, Save, Trash2, TriangleAlert, Undo2 } from "lucide-react";
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Switch } from "@/components/ui/switch";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatMemoryMb } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { SystemSettings } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AdminCallout, AdminFieldError, AdminHint } from "../admin-shared";
import { isIpAddress, unreadableSecrets, type AgentPolicy } from "./system-types";

type Values = SystemSettings["values"];
type Channel = "stable" | "latest" | "pinned";

interface FormState {
  channel: Channel;
  pinned: string;
  default_memory_mb: string;
  memory_reserve_mb: string;
  port_range_start: string;
  port_range_end: string;
  default_bind_ip: string;
  public_address: string;
  lan_address: string;
  timezone: string;
  pre_restore_backup: boolean;
  pre_update_backup: boolean;
  keep_last: string;
  keep_daily: string;
  keep_weekly: string;
  keep_monthly: string;
}

const RETENTION_KEYS = ["keep_last", "keep_daily", "keep_weekly", "keep_monthly"] as const;
const ADVANCED_FIELDS = ["pinned", "port_range_start", "port_range_end", "default_bind_ip"];

function normRetention(r: Values["backup_default_retention"] | undefined) {
  return {
    keep_last: r?.keep_last ?? 10,
    keep_daily: r?.keep_daily ?? 7,
    keep_weekly: r?.keep_weekly ?? 4,
    keep_monthly: r?.keep_monthly ?? 0,
  };
}

function toForm(v: Values): FormState {
  const ch = v.runtime_image_channel || "stable";
  const channel: Channel = ch === "latest" ? "latest" : ch.startsWith("pinned:") ? "pinned" : "stable";
  const r = normRetention(v.backup_default_retention);
  return {
    channel,
    pinned: channel === "pinned" ? ch.slice("pinned:".length) : "",
    default_memory_mb: String(v.default_memory_mb ?? 4096),
    memory_reserve_mb: String(v.memory_reserve_mb ?? DEFAULT_RESERVE_MB),
    port_range_start: v.port_range_start == null ? "" : String(v.port_range_start),
    port_range_end: v.port_range_end == null ? "" : String(v.port_range_end),
    default_bind_ip: v.default_bind_ip ?? "0.0.0.0",
    public_address: v.public_address ?? "",
    lan_address: v.lan_address ?? "",
    timezone: v.timezone ?? "",
    pre_restore_backup: v.pre_restore_backup ?? true,
    pre_update_backup: v.pre_update_backup ?? true,
    keep_last: String(r.keep_last),
    keep_daily: String(r.keep_daily),
    keep_weekly: String(r.keep_weekly),
    keep_monthly: String(r.keep_monthly),
  };
}

const isIp = isIpAddress;
/** backend apps/instances/capacity.py DEFAULT_RESERVE_MB and the allowed range of the setting */
const DEFAULT_RESERVE_MB = 4096;
const MAX_RESERVE_MB = 65536;
const HOST_RE = /^[A-Za-z0-9.-]+$/;
const TAG_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$/;

function parseIntStrict(s: string): number | null {
  const t = s.trim();
  if (!/^\d+$/.test(t)) return null;
  return Number(t);
}

function getTimezones(): string[] {
  try {
    const list = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
    return list.includes("UTC") ? list : ["UTC", ...list];
  } catch {
    return [];
  }
}

/** Validate the form and produce the full set of values the backend expects. */
function buildValues(f: FormState, envRange: [number, number], policy?: AgentPolicy) {
  const errors: Record<string, string> = {};
  const values: Partial<Values> = {};

  if (f.channel === "pinned") {
    const tag = f.pinned.trim();
    if (!tag) errors.pinned = "Enter the image release to pin, e.g. 2025.6.2";
    else if (!TAG_RE.test(tag)) errors.pinned = "Only letters, digits, dots, dashes and underscores";
    values.runtime_image_channel = `pinned:${tag}`;
  } else {
    values.runtime_image_channel = f.channel;
  }

  const maxMem = policy?.max_memory_mb ?? 65536;
  const mem = parseIntStrict(f.default_memory_mb);
  if (mem === null) errors.default_memory_mb = "Enter a whole number of MB";
  else if (mem < 512) errors.default_memory_mb = "At least 512 MB";
  else if (mem > maxMem) errors.default_memory_mb = `At most ${formatMemoryMb(maxMem)} (agent limit)`;
  else values.default_memory_mb = mem;

  const reserve = parseIntStrict(f.memory_reserve_mb);
  if (reserve === null) errors.memory_reserve_mb = "Enter a whole number of MB (0 for none)";
  else if (reserve > MAX_RESERVE_MB) errors.memory_reserve_mb = `At most ${formatMemoryMb(MAX_RESERVE_MB)}`;
  else values.memory_reserve_mb = reserve;

  const port = (key: "port_range_start" | "port_range_end") => {
    if (!f[key].trim()) return null;
    const n = parseIntStrict(f[key]);
    if (n === null || n < 1024 || n > 65535) {
      errors[key] = "A port between 1024 and 65535";
      return undefined;
    }
    return n;
  };
  const start = port("port_range_start");
  const end = port("port_range_end");
  if (start !== undefined) values.port_range_start = start;
  if (end !== undefined) values.port_range_end = end;
  if (start !== undefined && end !== undefined) {
    const effStart = start ?? envRange[0];
    const effEnd = end ?? envRange[1];
    if (effStart > effEnd) errors.port_range_end = `The range end must be at least ${effStart}`;
  }

  const bind = f.default_bind_ip.trim();
  if (!bind) errors.default_bind_ip = "Required. Use 0.0.0.0 for all interfaces";
  else if (!isIp(bind)) errors.default_bind_ip = "Enter an IPv4 or IPv6 address, without a port (e.g. 0.0.0.0 or 192.168.1.10)";
  else values.default_bind_ip = bind;

  for (const key of ["public_address", "lan_address"] as const) {
    const v = f[key].trim();
    if (v && !isIp(v) && !HOST_RE.test(v)) errors[key] = "Hostname or IP only, without scheme or port";
    else values[key] = v;
  }

  values.timezone = f.timezone.trim();
  values.pre_restore_backup = f.pre_restore_backup;
  values.pre_update_backup = f.pre_update_backup;

  const retention: Record<string, number> = {};
  for (const k of RETENTION_KEYS) {
    const n = parseIntStrict(f[k]);
    if (n === null || n > 1000) errors[k] = "0–1000";
    else retention[k] = n;
  }
  if (!errors.keep_last && retention.keep_last < 1) errors.keep_last = "Keep at least 1";
  values.backup_default_retention = retention;

  return { values, errors };
}

function diffValues(next: Partial<Values>, prev: Values): Partial<Values> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(next)) {
    const old =
      k === "backup_default_retention"
        ? normRetention(prev.backup_default_retention)
        : (prev as unknown as Record<string, unknown>)[k];
    if (JSON.stringify(v) !== JSON.stringify(old)) out[k] = v;
  }
  return out as Partial<Values>;
}

function FieldGroup({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <fieldset className="grid gap-3 border-t pt-4 first:border-t-0 first:pt-0">
      <div>
        <legend className="text-sm font-semibold">{title}</legend>
        {description && <AdminHint className="mt-0.5">{description}</AdminHint>}
      </div>
      {children}
    </fieldset>
  );
}

function SwitchRow({
  id,
  label,
  hint,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  hint: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="grid gap-0.5">
        <Label htmlFor={id}>{label}</Label>
        <AdminHint>{hint}</AdminHint>
      </div>
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

export function SettingsForm({ settings, policy }: { settings: SystemSettings; policy?: AgentPolicy }) {
  const qc = useQueryClient();
  const initial = useMemo(() => toForm(settings.values), [settings.values]);
  const [form, setForm] = useState<FormState>(initial);
  const [cfKey, setCfKey] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const timezones = useMemo(() => getTimezones(), []);
  const envRange = settings.env.port_range_default;
  const cfConfigured = !!settings.secrets_configured.curseforge_api_key;
  const cfUnreadable = !!settings.secrets_unreadable?.curseforge_api_key;
  const unreadable = unreadableSecrets(settings);

  const built = buildValues(form, envRange, policy);
  const changes = diffValues(built.values, settings.values);
  const formChanged = JSON.stringify(form) !== JSON.stringify(initial);
  const dirty = formChanged || Object.keys(changes).length > 0 || cfKey.trim() !== "";

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    if (errors[key]) setErrors((e) => ({ ...e, [key]: "" }));
  };

  const save = useMutation({
    mutationFn: (body: { values: Partial<Values>; secrets?: Record<string, string> }) =>
      api.patch<SystemSettings>("/system/settings", body),
    onSuccess: (data) => {
      const { warnings, ...stored } = data;
      qc.setQueryData(qk.settings, stored);
      qc.invalidateQueries({ queryKey: [...qk.system, "ports"] });
      setCfKey("");
      toast.success("Settings saved");
      for (const w of warnings ?? []) toast.warning(w, { duration: 10_000 });
    },
    onError: (err) => {
      // CurseForge refused the key: nothing was saved; the message belongs to the key field
      if (err instanceof ApiError && err.code === "invalid_curseforge_key") setErrors((e) => ({ ...e, cf_key: err.message }));
      toast.error(errorMessage(err));
    },
  });

  const clearKey = useMutation({
    mutationFn: () => api.patch<SystemSettings>("/system/settings", { values: {}, secrets: { curseforge_api_key: "" } }),
    onSuccess: (data) => {
      qc.setQueryData<SystemSettings>(qk.settings, (old) =>
        old ? { ...old, secrets_configured: data.secrets_configured, secrets_unreadable: data.secrets_unreadable } : data,
      );
      if (data.secrets_configured.curseforge_api_key)
        toast.info("Removed the stored key. A key is still provided by the CURSEFORGE_API_KEY environment variable.");
      else toast.success("CurseForge API key removed");
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    setErrors(built.errors);
    if (Object.values(built.errors).some(Boolean)) {
      if (Object.keys(built.errors).some((k) => ADVANCED_FIELDS.includes(k))) setAdvancedOpen(true);
      toast.error("Fix the highlighted fields first");
      return;
    }
    const body: { values: Partial<Values>; secrets?: Record<string, string> } = { values: changes };
    if (cfKey.trim()) body.secrets = { curseforge_api_key: cfKey.trim() };
    save.mutate(body);
  }

  function discard() {
    setForm(initial);
    setCfKey("");
    setErrors({});
  }

  const err = (k: string) => errors[k] || undefined;
  const memNum = parseIntStrict(form.default_memory_mb);
  const tzUnknown = form.timezone.trim() !== "" && timezones.length > 0 && !timezones.includes(form.timezone.trim());

  return (
    <Section title="Panel settings" description="Panel-wide defaults. Changes apply to new servers or the next container recreation.">
      <form onSubmit={submit} className="grid gap-5" noValidate>
        {unreadable.length > 0 && (
          <AdminCallout tone="warning" icon={<TriangleAlert />} title="Some stored secrets can't be read">
            <p>
              {unreadable.join(", ")}: stored, but encrypted with a different <span className="font-mono">MCPANEL_SECRET_KEY</span>,
              so the panel treats {unreadable.length === 1 ? "it" : "them"} as missing. Re-enter{" "}
              {unreadable.length === 1 ? "it" : "them"} below and save to replace the unreadable value. If the secret key was
              changed by mistake, put the previous key into <span className="font-mono">MCPANEL_SECRET_KEY_FALLBACKS</span> and
              restart the panel instead; the stored value then works again.
            </p>
          </AdminCallout>
        )}
        <FieldGroup title="New servers">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid content-start gap-1.5">
              <Label htmlFor="set-mem">Default memory (MB)</Label>
              <Input
                id="set-mem"
                inputMode="numeric"
                value={form.default_memory_mb}
                onChange={(e) => set("default_memory_mb", e.target.value)}
                aria-invalid={!!err("default_memory_mb")}
                aria-describedby="set-mem-hint set-mem-err"
              />
              <AdminHint>
                <span id="set-mem-hint">
                  {memNum ? `${formatMemoryMb(memNum)} heap for new servers. ` : ""}Modpack servers start at 6 GB.
                </span>
              </AdminHint>
              <AdminFieldError id="set-mem-err" message={err("default_memory_mb")} />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="set-reserve">Memory kept free for the host (MB)</Label>
              <Input
                id="set-reserve"
                inputMode="numeric"
                value={form.memory_reserve_mb}
                onChange={(e) => set("memory_reserve_mb", e.target.value)}
                aria-invalid={!!err("memory_reserve_mb")}
                aria-describedby="set-reserve-hint set-reserve-err"
              />
              <AdminHint>
                <span id="set-reserve-hint">
                  A server start is refused (unless confirmed) when the memory of the running servers plus this reserve would exceed
                  the host's RAM. Default {formatMemoryMb(DEFAULT_RESERVE_MB)}.
                </span>
              </AdminHint>
              <AdminFieldError id="set-reserve-err" message={err("memory_reserve_mb")} />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="set-tz">Time zone</Label>
              <Input
                id="set-tz"
                list="set-tz-list"
                value={form.timezone}
                placeholder="Panel default (TZ)"
                onChange={(e) => set("timezone", e.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-describedby="set-tz-hint"
              />
              <datalist id="set-tz-list">
                {timezones.map((t) => (
                  <option key={t} value={t} />
                ))}
              </datalist>
              <AdminHint>
                <span id="set-tz-hint">
                  {tzUnknown ? (
                    <span className="text-amber-700 dark:text-amber-300">
                      Not a time zone this browser recognises. Check the spelling, e.g. Europe/Berlin.
                    </span>
                  ) : (
                    "Passed to servers as TZ (log timestamps, in-game schedules). Blank uses the panel's TZ."
                  )}
                </span>
              </AdminHint>
            </div>
          </div>
        </FieldGroup>

        <FieldGroup
          title="Connection addresses"
          description="Only used to show players how to connect (on each server's overview). They don't change networking, DNS or router port forwarding."
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid content-start gap-1.5">
              <Label htmlFor="set-public">Public address</Label>
              <Input
                id="set-public"
                value={form.public_address}
                placeholder="mc.example.com"
                onChange={(e) => set("public_address", e.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={!!err("public_address")}
                aria-describedby="set-public-hint set-public-err"
              />
              <AdminHint>
                <span id="set-public-hint">
                  Your public hostname or IP. Players outside your network also need a router port forward.
                </span>
              </AdminHint>
              <AdminFieldError id="set-public-err" message={err("public_address")} />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="set-lan">LAN address</Label>
              <Input
                id="set-lan"
                value={form.lan_address}
                placeholder="192.168.1.20"
                onChange={(e) => set("lan_address", e.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={!!err("lan_address")}
                aria-describedby="set-lan-hint set-lan-err"
              />
              <AdminHint>
                <span id="set-lan-hint">The Docker host's address on your local network.</span>
              </AdminHint>
              <AdminFieldError id="set-lan-err" message={err("lan_address")} />
            </div>
          </div>
          <AdminHint>Blank fields fall back to the node's addresses (MCPANEL_PUBLIC_HOSTNAME / MCPANEL_LAN_ADDRESS).</AdminHint>
        </FieldGroup>

        <FieldGroup title="Backups">
          <div className="grid gap-3">
            <SwitchRow
              id="set-pre-restore"
              label="Safety backup before restores"
              hint="Default for backing up the current files before a restore overwrites them."
              checked={form.pre_restore_backup}
              onChange={(v) => set("pre_restore_backup", v)}
            />
            <SwitchRow
              id="set-pre-update"
              label="Safety backup before updates"
              hint="Default for backing up before modpack updates, so a bad update can be rolled back."
              checked={form.pre_update_backup}
              onChange={(v) => set("pre_update_backup", v)}
            />
          </div>
          <div className="grid content-start gap-1.5">
            <p className="text-sm font-medium" id="set-retention-label">
              Default retention
            </p>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" role="group" aria-labelledby="set-retention-label">
              {(
                [
                  ["keep_last", "Latest"],
                  ["keep_daily", "Daily"],
                  ["keep_weekly", "Weekly"],
                  ["keep_monthly", "Monthly"],
                ] as const
              ).map(([k, label]) => (
                <div key={k} className="grid content-start gap-1">
                  <Label htmlFor={`set-${k}`} className="text-xs font-normal text-muted-foreground">
                    {label}
                  </Label>
                  <Input
                    id={`set-${k}`}
                    inputMode="numeric"
                    value={form[k]}
                    onChange={(e) => set(k, e.target.value)}
                    aria-invalid={!!err(k)}
                    aria-describedby={`set-${k}-err`}
                  />
                  <AdminFieldError id={`set-${k}-err`} message={err(k)} />
                </div>
              ))}
            </div>
            <AdminHint>
              Copied into a server's backup policy when it is first used. Existing servers keep their own policy (Backups
              tab).
            </AdminHint>
          </div>
        </FieldGroup>

        <FieldGroup
          title="Integrations"
          description={
            <>
              A CurseForge API key enables CurseForge modpack search and CurseForge server installs. Get one from the{" "}
              <a
                href="https://console.curseforge.com/"
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-0.5 text-primary underline underline-offset-3"
              >
                CurseForge console <ExternalLink className="size-3" aria-hidden />
              </a>
              .
            </>
          }
        >
          <div className="grid content-start gap-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <Label htmlFor="set-cf-key">CurseForge API key</Label>
              {cfConfigured ? (
                <Badge className="bg-emerald-500/15 text-emerald-700 dark:text-emerald-300">
                  <KeyRound /> Configured
                </Badge>
              ) : cfUnreadable ? (
                <Badge className="bg-amber-500/15 text-amber-800 dark:text-amber-200">
                  <TriangleAlert /> Unreadable: re-enter it
                </Badge>
              ) : (
                <Badge variant="outline">Not configured</Badge>
              )}
            </div>
            <div className="flex gap-2">
              <Input
                id="set-cf-key"
                type="password"
                name="curseforge-api-key"
                autoComplete="new-password"
                spellCheck={false}
                value={cfKey}
                placeholder={
                  cfConfigured
                    ? "Enter a new key to replace the current one"
                    : cfUnreadable
                      ? "Paste the key again to replace the unreadable one"
                      : "Paste your API key"
                }
                onChange={(e) => {
                  setCfKey(e.target.value);
                  if (errors.cf_key) setErrors((x) => ({ ...x, cf_key: "" }));
                }}
                maxLength={500}
                className="font-mono"
                aria-invalid={!!err("cf_key")}
                aria-describedby="set-cf-hint set-cf-err"
              />
              {(cfConfigured || cfUnreadable) && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setConfirmClear(true)}
                  disabled={clearKey.isPending}
                  aria-label="Remove the stored CurseForge API key"
                >
                  {clearKey.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
                  Clear
                </Button>
              )}
            </div>
            <AdminHint>
              <span id="set-cf-hint">
                Checked with CurseForge before it is saved. Stored encrypted; the key is never shown again after saving.
              </span>
            </AdminHint>
            <AdminFieldError id="set-cf-err" message={err("cf_key")} />
          </div>
        </FieldGroup>

        <details
          className="group rounded-lg border"
          open={advancedOpen}
          onToggle={(e) => setAdvancedOpen(e.currentTarget.open)}
        >
          <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2.5 text-sm font-medium select-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-4 text-muted-foreground transition-transform group-open:rotate-90" aria-hidden />
            Advanced
            <span className="font-normal text-muted-foreground">runtime image, port range, bind address</span>
          </summary>
          <div className="grid gap-5 border-t p-3">
            <div className="grid gap-2">
              <p className="text-sm font-medium" id="set-channel-label">
                Runtime image channel
              </p>
              <RadioGroup
                value={form.channel}
                onValueChange={(v) => set("channel", v as Channel)}
                aria-labelledby="set-channel-label"
                className="gap-2"
              >
                {(
                  [
                    ["stable", "Stable", "Recommended. Tested itzg/minecraft-server releases, tag stable-java21."],
                    ["latest", "Latest", "Newest image build, tag java21. Picks up fixes sooner but can change without notice."],
                    ["pinned", "Pinned", "A fixed image release, tag <version>-java21. Nothing changes until you change it."],
                  ] as const
                ).map(([value, label, hint]) => (
                  <label
                    key={value}
                    htmlFor={`set-channel-${value}`}
                    className={cn(
                      "flex cursor-pointer items-start gap-3 rounded-lg border p-2.5 hover:bg-muted/40",
                      form.channel === value && "border-primary/50 bg-primary/5",
                    )}
                  >
                    <RadioGroupItem id={`set-channel-${value}`} value={value} className="mt-0.5" />
                    <span className="grid gap-0.5">
                      <span className="text-sm font-medium">{label}</span>
                      <span className="text-xs text-muted-foreground">{hint}</span>
                    </span>
                  </label>
                ))}
              </RadioGroup>
              {form.channel === "pinned" && (
                <div className="grid content-start gap-1.5 sm:max-w-xs">
                  <Label htmlFor="set-pinned">Image release</Label>
                  <Input
                    id="set-pinned"
                    value={form.pinned}
                    placeholder="2025.6.2"
                    onChange={(e) => set("pinned", e.target.value)}
                    className="font-mono"
                    spellCheck={false}
                    autoComplete="off"
                    aria-invalid={!!err("pinned")}
                    aria-describedby="set-pinned-hint set-pinned-err"
                  />
                  <AdminHint>
                    <span id="set-pinned-hint">
                      A release from{" "}
                      <a
                        href="https://github.com/itzg/docker-minecraft-server/releases"
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-primary underline underline-offset-3"
                      >
                        itzg/docker-minecraft-server
                      </a>
                      .
                    </span>
                  </AdminHint>
                  <AdminFieldError id="set-pinned-err" message={err("pinned")} />
                </div>
              )}
              <AdminHint>
                The Java version in the tag follows each server's Minecraft version. Applies to existing servers too: a
                running server shows "restart required", a stopped one gets it at its next start, and each start looks for a
                newer build of the tag.
              </AdminHint>
            </div>

            <div className="grid content-start gap-1.5">
              <p className="text-sm font-medium" id="set-ports-label">
                Port allocation range
              </p>
              <div className="grid grid-cols-2 gap-3 sm:max-w-sm" role="group" aria-labelledby="set-ports-label">
                <div className="grid content-start gap-1">
                  <Label htmlFor="set-port-start" className="text-xs font-normal text-muted-foreground">
                    First port
                  </Label>
                  <Input
                    id="set-port-start"
                    inputMode="numeric"
                    value={form.port_range_start}
                    placeholder={String(envRange[0])}
                    onChange={(e) => set("port_range_start", e.target.value)}
                    aria-invalid={!!err("port_range_start")}
                    aria-describedby="set-port-start-err"
                  />
                  <AdminFieldError id="set-port-start-err" message={err("port_range_start")} />
                </div>
                <div className="grid content-start gap-1">
                  <Label htmlFor="set-port-end" className="text-xs font-normal text-muted-foreground">
                    Last port
                  </Label>
                  <Input
                    id="set-port-end"
                    inputMode="numeric"
                    value={form.port_range_end}
                    placeholder={String(envRange[1])}
                    onChange={(e) => set("port_range_end", e.target.value)}
                    aria-invalid={!!err("port_range_end")}
                    aria-describedby="set-port-end-err"
                  />
                  <AdminFieldError id="set-port-end-err" message={err("port_range_end")} />
                </div>
              </div>
              <AdminHint>
                New servers get the first free host port in this range. Blank uses the deployment default ({envRange[0]}–
                {envRange[1]}).
                {policy?.port_min !== undefined &&
                  policy.port_max !== undefined &&
                  ` The agent accepts ports ${policy.port_min}–${policy.port_max}.`}
              </AdminHint>
            </div>

            <div className="grid content-start gap-1.5 sm:max-w-sm">
              <Label htmlFor="set-bind">Default bind address</Label>
              <Input
                id="set-bind"
                value={form.default_bind_ip}
                onChange={(e) => set("default_bind_ip", e.target.value)}
                className="font-mono"
                spellCheck={false}
                autoComplete="off"
                aria-invalid={!!err("default_bind_ip")}
                aria-describedby="set-bind-hint set-bind-err"
              />
              <AdminHint>
                <span id="set-bind-hint">
                  Host interface new servers publish their game port on. 0.0.0.0 means all interfaces; a LAN IP keeps servers
                  off other networks. Existing servers keep their own setting (Network tab).
                </span>
              </AdminHint>
              <AdminFieldError id="set-bind-err" message={err("default_bind_ip")} />
            </div>
          </div>
        </details>

        <div
          className={cn(
            "flex flex-wrap items-center justify-end gap-2 border-t pt-4",
            dirty && "sticky bottom-4 z-20 rounded-lg border bg-popover p-2.5 shadow-lg",
          )}
        >
          <span className="mr-auto text-xs text-muted-foreground" aria-live="polite">
            {dirty ? "You have unsaved changes" : "All changes saved"}
          </span>
          <Button type="button" variant="outline" size="sm" onClick={discard} disabled={!dirty || save.isPending}>
            <Undo2 /> Discard
          </Button>
          <Button type="submit" size="sm" disabled={!dirty || save.isPending}>
            {save.isPending ? <Loader2 className="animate-spin" /> : <Save />}
            Save settings
          </Button>
        </div>
      </form>

      <ConfirmDialog
        open={confirmClear}
        onOpenChange={setConfirmClear}
        title="Remove the CurseForge API key?"
        description="CurseForge modpack search, installs and updates won't work until a key is added again."
        confirmLabel="Remove key"
        destructive
        onConfirm={async () => {
          try {
            await clearKey.mutateAsync();
          } catch {
            /* toast shown by the mutation */
          }
        }}
      />
    </Section>
  );
}
