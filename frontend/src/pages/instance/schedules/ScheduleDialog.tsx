/** Create / edit dialog for an instance schedule (POST /instances/{id}/schedules, PATCH .../schedules/{sid}). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api, ApiError, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Schedule } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CRON_PRESETS, cronError, describeCron, normalizeCron } from "./cron";
import { ACTION_INFO, draftFrom, RUNNING_ONLY_ACTIONS, type ScheduleAction, type ScheduleDraft } from "./schedule-meta";

function validate(d: ScheduleDraft): Partial<Record<"name" | "cron" | "command" | "backupName", string>> {
  const e: Partial<Record<"name" | "cron" | "command" | "backupName", string>> = {};
  if (!d.name.trim()) e.name = "Give the schedule a name";
  else if (d.name.length > 80) e.name = "At most 80 characters";
  const c = cronError(d.cron);
  if (c) e.cron = c;
  if (d.action === "command") {
    const cmd = d.command.trim().replace(/^\/+/, "");
    if (!cmd) e.command = "Enter the command to run";
    else if (cmd.length > 1400) e.command = "At most 1400 characters";
  }
  if (d.action === "backup" && d.backupName.length > 120) e.backupName = "At most 120 characters";
  return e;
}

export function ScheduleDialog({
  instanceId,
  schedule,
  template,
  canBackup,
  panelTz,
  open,
  onOpenChange,
}: {
  instanceId: string;
  schedule?: Schedule | null;
  template?: Partial<ScheduleDraft>;
  canBackup: boolean;
  panelTz: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<ScheduleDraft>(() => draftFrom(schedule, template));
  const [touched, setTouched] = useState(false);
  const editing = !!schedule;

  const save = useMutation({
    mutationFn: () => {
      const keep = schedule && schedule.action === draft.action ? { ...schedule.payload } : {};
      let payload: Record<string, unknown> = {};
      if (draft.action === "command") payload = { ...keep, command: draft.command.trim().replace(/^\/+/, "") };
      else if (draft.action === "backup") {
        payload = { ...keep };
        if (draft.backupName.trim()) payload.name = draft.backupName.trim();
        else delete payload.name;
      } else payload = keep;
      const body = {
        name: draft.name.trim(),
        action: draft.action,
        cron: normalizeCron(draft.cron),
        payload,
        enabled: draft.enabled,
        only_when_running: draft.only_when_running,
      };
      return editing
        ? api.patch<Schedule>(`/instances/${instanceId}/schedules/${schedule!.id}`, body)
        : api.post<Schedule>(`/instances/${instanceId}/schedules`, body);
    },
    onSuccess: (s) => {
      toast.success(editing ? `Schedule “${s.name}” updated` : `Schedule “${s.name}” created`);
      qc.invalidateQueries({ queryKey: qk.schedules(instanceId) });
      onOpenChange(false);
    },
    onError: (err) => {
      if (!(err instanceof ApiError && err.code === "validation_error")) toast.error(errorMessage(err));
    },
  });

  const errors = validate(draft);
  const server = save.error instanceof ApiError ? save.error.fieldErrors : {};
  const err = (k: keyof typeof errors, serverKey: string = k) => (touched ? errors[k] : undefined) ?? server[serverKey];
  const description = describeCron(draft.cron);
  const set = <K extends keyof ScheduleDraft>(k: K, v: ScheduleDraft[K]) => {
    setDraft((d) => ({ ...d, [k]: v }));
    if (save.error) save.reset();
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (Object.keys(errors).length) return;
    save.mutate();
  };
  const actions: ScheduleAction[] = ["restart", "start", "stop", "backup", "command"];
  const generalError = server.non_field_errors ?? server.detail;

  return (
    <Dialog open={open} onOpenChange={(o) => !save.isPending && onOpenChange(o)}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <CalendarClock className="size-4 text-muted-foreground" aria-hidden />
              {editing ? "Edit schedule" : "New schedule"}
            </DialogTitle>
            <DialogDescription>
              Runs automatically on the panel server. Times use the panel's timezone{panelTz ? ` (${panelTz})` : ""}.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-1.5">
            <Label htmlFor="sched-name">Name</Label>
            <Input
              id="sched-name"
              value={draft.name}
              maxLength={80}
              placeholder="e.g. Nightly restart"
              onChange={(e) => set("name", e.target.value)}
              aria-invalid={!!err("name") || undefined}
              aria-describedby={err("name") ? "sched-name-err" : undefined}
              autoFocus
            />
            {err("name") && (
              <p id="sched-name-err" className="text-xs text-destructive">
                {err("name")}
              </p>
            )}
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="sched-action">Action</Label>
            <Select value={draft.action} onValueChange={(v) => set("action", v as ScheduleAction)}>
              <SelectTrigger id="sched-action" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {actions.map((a) => (
                  <SelectItem key={a} value={a} disabled={a === "backup" && !canBackup}>
                    {ACTION_INFO[a].label}
                    {a === "backup" && !canBackup ? " (needs backup permission)" : ""}
                  </SelectItem>
                ))}
                {(schedule?.action as ScheduleAction | undefined) === "snapshot" && (
                  <SelectItem value="snapshot" disabled>
                    Snapshot
                  </SelectItem>
                )}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{ACTION_INFO[draft.action]?.description}</p>
          </div>

          {draft.action === "command" && (
            <div className="grid gap-1.5">
              <Label htmlFor="sched-command">Command</Label>
              <div className="flex items-center rounded-lg border border-input focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 dark:bg-input/30">
                <span className="pl-2.5 font-mono text-sm text-muted-foreground" aria-hidden>
                  /
                </span>
                <input
                  id="sched-command"
                  className="h-8 min-w-0 flex-1 bg-transparent px-1 font-mono text-sm outline-none"
                  value={draft.command}
                  maxLength={1400}
                  placeholder="say Server restarts in 5 minutes"
                  onChange={(e) => set("command", e.target.value.replace(/\n/g, " "))}
                  aria-invalid={!!err("command", "payload") || undefined}
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
              {err("command", "payload") ? (
                <p className="text-xs text-destructive">{err("command", "payload")}</p>
              ) : (
                <p className="text-xs text-muted-foreground">Single line, without the leading slash. Output is stored as the last result.</p>
              )}
            </div>
          )}

          {draft.action === "backup" && (
            <div className="grid gap-1.5">
              <Label htmlFor="sched-backup-name">Backup name (optional)</Label>
              <Input
                id="sched-backup-name"
                value={draft.backupName}
                maxLength={120}
                placeholder={`Scheduled: ${draft.name || "…"}`}
                onChange={(e) => set("backupName", e.target.value)}
                aria-invalid={!!err("backupName") || undefined}
              />
              {err("backupName") && <p className="text-xs text-destructive">{err("backupName")}</p>}
            </div>
          )}

          <div className="grid gap-1.5">
            <Label htmlFor="sched-cron">Schedule (cron)</Label>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Cron presets">
              {CRON_PRESETS.map((p) => {
                const active = normalizeCron(draft.cron) === p.cron;
                return (
                  <Button
                    key={p.cron}
                    type="button"
                    size="xs"
                    variant={active ? "default" : "outline"}
                    aria-pressed={active}
                    onClick={() => set("cron", p.cron)}
                  >
                    {p.label}
                  </Button>
                );
              })}
            </div>
            <Input
              id="sched-cron"
              className="font-mono"
              value={draft.cron}
              onChange={(e) => set("cron", e.target.value)}
              placeholder="minute hour day month weekday"
              aria-invalid={!!err("cron") || undefined}
              aria-describedby="sched-cron-help"
              autoComplete="off"
              spellCheck={false}
            />
            <div id="sched-cron-help" className="text-xs" aria-live="polite">
              {err("cron") ? (
                <p className="text-destructive">{err("cron")}</p>
              ) : cronError(draft.cron) ? (
                <p className="text-muted-foreground">Format: minute hour day-of-month month day-of-week, e.g. 0 4 * * *</p>
              ) : (
                <p className={cn(description ? "text-foreground" : "text-muted-foreground")}>
                  {description ?? `Custom schedule: ${normalizeCron(draft.cron)}`}
                  {panelTz && <span className="text-muted-foreground"> · {panelTz}</span>}
                </p>
              )}
            </div>
          </div>

          <div className="grid gap-3 rounded-lg border p-3">
            <div className="flex items-start justify-between gap-4">
              <div className="grid gap-0.5">
                <Label htmlFor="sched-enabled">Enabled</Label>
                <p className="text-xs text-muted-foreground">Paused schedules are kept but never run.</p>
              </div>
              <Switch id="sched-enabled" checked={draft.enabled} onCheckedChange={(v) => set("enabled", v)} />
            </div>
            {RUNNING_ONLY_ACTIONS.includes(draft.action) && (
              <div className="flex items-start justify-between gap-4">
                <div className="grid gap-0.5">
                  <Label htmlFor="sched-running">Only when running</Label>
                  <p className="text-xs text-muted-foreground">
                    Skip this run if the server is stopped{draft.action === "restart" ? " (so a stopped server is not started by a restart)" : ""}.
                  </p>
                </div>
                <Switch
                  id="sched-running"
                  checked={draft.only_when_running}
                  onCheckedChange={(v) => set("only_when_running", v)}
                />
              </div>
            )}
          </div>

          {generalError && <p className="text-sm text-destructive">{generalError}</p>}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending && <Loader2 className="animate-spin" />}
              {editing ? "Save changes" : "Create schedule"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
