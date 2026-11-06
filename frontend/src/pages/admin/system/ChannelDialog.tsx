/** Add / edit a notification channel (POST /notifications/channels, PATCH /notifications/channels/{id}). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleX, Eye, EyeOff, Loader2 } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ApiError, api, errorMessage } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AdminCallout, AdminFieldError, AdminHint } from "../admin-shared";
import {
  channelPayload,
  channelsKey,
  channelToForm,
  EMPTY_CHANNEL_FORM,
  KIND_META,
  validateChannelForm,
  type ChannelFormState,
  type ChannelKind,
  type NotificationChannel,
  type NotificationEvent,
} from "./notifications-model";

const ALL_SERVERS = "__all__";
const FIELD_KEYS = ["name", "kind", "url", "events", "instance", "enabled"];

/** Move focus (and the dialog's scroll position) to the first field with an error. */
function focusFirstError(errs: Record<string, string>) {
  const key = FIELD_KEYS.find((k) => errs[k]);
  if (!key) return;
  const selector: Record<string, string> = {
    name: "#nc-name",
    kind: '[id^="nc-kind-"]',
    url: "#nc-url",
    events: '[id^="nc-ev-"]',
    instance: "#nc-instance",
    enabled: "#nc-enabled",
  };
  requestAnimationFrame(() => document.querySelector<HTMLElement>(selector[key])?.focus());
}

export function ChannelDialog({
  open,
  onOpenChange,
  channel,
  events,
  servers,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  channel?: NotificationChannel;
  events: readonly NotificationEvent[];
  servers: readonly Instance[];
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <ChannelDialogBody
          key={channel?.id ?? "new"}
          channel={channel}
          events={events}
          servers={servers}
          onClose={() => onOpenChange(false)}
        />
      )}
    </Dialog>
  );
}

function ChannelDialogBody({
  channel,
  events,
  servers,
  onClose,
}: {
  channel?: NotificationChannel;
  events: readonly NotificationEvent[];
  servers: readonly Instance[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = !!channel;
  const known = events.map((e) => e.id);
  const [form, setForm] = useState<ChannelFormState>(() =>
    channel
      ? channelToForm(channel)
      : { ...EMPTY_CHANNEL_FORM, events: EMPTY_CHANNEL_FORM.events.filter((e) => known.includes(e)) },
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [showUrl, setShowUrl] = useState(false);

  const set = <K extends keyof ChannelFormState>(key: K, value: ChannelFormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setErrors((e) => {
      if (!e[key] && !(key === "kind" && e.url)) return e;
      const next = { ...e, [key]: "" };
      if (key === "kind") next.url = "";
      return next;
    });
  };

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      channel
        ? api.patch<NotificationChannel>(`/notifications/channels/${channel.id}`, body)
        : api.post<NotificationChannel>("/notifications/channels", body),
    onSuccess: (saved) => {
      qc.invalidateQueries({ queryKey: channelsKey });
      toast.success(editing ? `Channel “${saved.name}” saved` : `Channel “${saved.name}” added`);
      onClose();
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        const entries = Object.entries(err.fieldErrors);
        const onFields = Object.fromEntries(entries.filter(([k]) => FIELD_KEYS.includes(k)));
        const rest = entries.filter(([k]) => !FIELD_KEYS.includes(k)).map(([, v]) => v);
        setErrors(onFields);
        focusFirstError(onFields);
        if (rest.length) setFormError(rest.join(" "));
        else setFormError(Object.keys(onFields).length ? null : err.display);
        toast.error(Object.keys(onFields).length ? "Check the highlighted fields" : err.display);
      } else {
        setFormError(errorMessage(err));
        toast.error(errorMessage(err));
      }
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs = validateChannelForm(form, channel, known.length ? known : undefined);
    setErrors(errs);
    setFormError(null);
    if (Object.values(errs).some(Boolean)) {
      focusFirstError(errs);
      return;
    }
    const body = channelPayload(form, channel);
    if (editing && Object.keys(body).length === 0) {
      toast.info("Nothing changed");
      onClose();
      return;
    }
    save.mutate(body);
  }

  const toggleEvent = (id: string, on: boolean) =>
    set("events", on ? [...new Set([...form.events, id])] : form.events.filter((e) => e !== id));

  const sortedServers = [...servers].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  const unknownServer = !!form.instance && !servers.some((s) => s.id === form.instance);
  const kindChanged = editing && channel.kind !== form.kind;
  const busy = save.isPending;
  const err = (k: string) => errors[k] || undefined;

  return (
    <DialogContent
      className="max-h-[90svh] overflow-y-auto sm:max-w-lg"
      onInteractOutside={(e) => busy && e.preventDefault()}
      onEscapeKeyDown={(e) => busy && e.preventDefault()}
    >
      <form onSubmit={submit} className="grid min-w-0 gap-4" noValidate>
        <DialogHeader>
          <DialogTitle>{editing ? `Edit channel “${channel.name}”` : "Add a notification channel"}</DialogTitle>
          <DialogDescription>
            The worker posts selected events to a Discord channel or any HTTP endpoint that accepts JSON.
          </DialogDescription>
        </DialogHeader>

        <Field id="nc-name" label="Name" error={err("name")}>
          <Input
            id="nc-name"
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            maxLength={80}
            placeholder="Discord #server-alerts"
            autoComplete="off"
            aria-invalid={!!err("name") || undefined}
            aria-describedby="nc-name-err"
          />
        </Field>

        <div className="grid gap-2">
          <p className="text-sm font-medium" id="nc-kind-label">
            Kind
          </p>
          <RadioGroup
            value={form.kind}
            onValueChange={(v) => set("kind", v as ChannelKind)}
            aria-labelledby="nc-kind-label"
            className="grid gap-2 sm:grid-cols-2"
          >
            {(Object.keys(KIND_META) as ChannelKind[]).map((k) => (
              <label
                key={k}
                htmlFor={`nc-kind-${k}`}
                className={cn(
                  "flex cursor-pointer items-start gap-2.5 rounded-lg border p-2.5 hover:bg-muted/40",
                  form.kind === k && "border-primary/50 bg-primary/5",
                )}
              >
                <RadioGroupItem id={`nc-kind-${k}`} value={k} className="mt-0.5" />
                <span className="text-sm font-medium">{KIND_META[k].label}</span>
              </label>
            ))}
          </RadioGroup>
          <AdminFieldError id="nc-kind-err" message={err("kind")} />
        </div>

        <Field
          id="nc-url"
          label="Webhook URL"
          error={err("url")}
          hint={
            <>
              {KIND_META[form.kind].hint} The URL works like a password: it is stored encrypted and only its host is shown
              afterwards.
              {editing && !kindChanged && ` Leave blank to keep the current URL (${channel.url_hint || "stored"}).`}
              {kindChanged && " Changing the kind needs the matching URL."}
            </>
          }
        >
          <InputGroup>
            <InputGroupInput
              id="nc-url"
              type={showUrl ? "text" : "password"}
              value={form.url}
              onChange={(e) => set("url", e.target.value)}
              placeholder={editing && !kindChanged ? "Unchanged" : KIND_META[form.kind].placeholder}
              autoComplete="off"
              spellCheck={false}
              maxLength={1000}
              className="font-mono text-xs"
              aria-invalid={!!err("url") || undefined}
              aria-describedby="nc-url-hint nc-url-err"
            />
            <InputGroupAddon align="inline-end">
              <InputGroupButton
                size="icon-xs"
                aria-label={showUrl ? "Hide URL" : "Show URL"}
                aria-pressed={showUrl}
                onClick={() => setShowUrl((v) => !v)}
              >
                {showUrl ? <EyeOff /> : <Eye />}
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
        </Field>

        <fieldset className="grid min-w-0 gap-2" aria-describedby="nc-events-err">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <legend className="text-sm font-medium">Events</legend>
            {events.length > 0 && (
              <div className="flex gap-1">
                <Button type="button" variant="ghost" size="xs" onClick={() => set("events", known)}>
                  Select all
                </Button>
                <Button type="button" variant="ghost" size="xs" onClick={() => set("events", [])}>
                  Clear
                </Button>
              </div>
            )}
          </div>
          {events.length === 0 ? (
            <p className="text-xs text-muted-foreground">The event list couldn't be loaded.</p>
          ) : (
            <div className="grid gap-1.5 sm:grid-cols-2">
              {events.map((ev) => {
                const id = `nc-ev-${ev.id.replace(/\W/g, "-")}`;
                const checked = form.events.includes(ev.id);
                return (
                  <div
                    key={ev.id}
                    className={cn("flex items-start gap-2 rounded-md border px-2.5 py-2", checked && "border-primary/40 bg-primary/5")}
                  >
                    <Checkbox
                      id={id}
                      checked={checked}
                      onCheckedChange={(c) => toggleEvent(ev.id, c === true)}
                      className="mt-0.5"
                    />
                    <Label htmlFor={id} className="grid gap-0.5 text-[13px] leading-snug font-normal">
                      {ev.label}
                      <span className="font-mono text-[10px] text-muted-foreground">{ev.id}</span>
                    </Label>
                  </div>
                );
              })}
            </div>
          )}
          <AdminFieldError id="nc-events-err" message={err("events")} />
        </fieldset>

        <Field
          id="nc-instance"
          label="Servers"
          error={err("instance")}
          hint={
            form.instance
              ? "Only events from this server are sent."
              : "Events from every server are sent, including servers added later."
          }
        >
          <Select value={form.instance || ALL_SERVERS} onValueChange={(v) => set("instance", v === ALL_SERVERS ? "" : v)}>
            <SelectTrigger
              id="nc-instance"
              className="w-full"
              aria-invalid={!!err("instance") || undefined}
              aria-describedby="nc-instance-hint nc-instance-err"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper">
              <SelectItem value={ALL_SERVERS}>All servers</SelectItem>
              {sortedServers.length > 0 && <SelectSeparator />}
              {sortedServers.map((s) => (
                <SelectItem key={s.id} value={s.id}>
                  {s.name}
                  {s.archived_at && <span className="text-xs text-muted-foreground">archived</span>}
                </SelectItem>
              ))}
              {unknownServer && <SelectItem value={form.instance}>Unknown server</SelectItem>}
            </SelectContent>
          </Select>
        </Field>

        <div className="flex items-start justify-between gap-4">
          <div className="grid gap-0.5">
            <Label htmlFor="nc-enabled">Enabled</Label>
            <AdminHint>Disabled channels keep their settings but receive nothing, not even tests.</AdminHint>
          </div>
          <Switch id="nc-enabled" checked={form.enabled} onCheckedChange={(v) => set("enabled", v)} />
        </div>

        {formError && (
          <AdminCallout tone="danger" icon={<CircleX />}>
            {formError}
          </AdminCallout>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy && <Loader2 className="animate-spin" />}
            {editing ? "Save channel" : "Add channel"}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string;
  label: ReactNode;
  error?: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="grid min-w-0 content-start gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && (
        <AdminHint>
          <span id={`${id}-hint`}>{hint}</span>
        </AdminHint>
      )}
      <AdminFieldError id={`${id}-err`} message={error} />
    </div>
  );
}
