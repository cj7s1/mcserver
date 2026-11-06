/** Create / edit dialog for an instance schedule (POST /instances/{id}/schedules, PATCH .../schedules/{sid}). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CalendarClock, Info, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api, ApiError, errorMessage } from "@/lib/api";
import { describeWarning, WARN_MESSAGE_MAX, WARN_MINUTES, warnMessageError, warnOptionLabel } from "@/lib/power-warning";
import { qk } from "@/lib/queries";
import type { PermissionCode, Schedule } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CRON_PRESETS, cronError, describeCron, normalizeCron } from "./cron";
import { snapshotState, useSnapshots } from "../backups/snapshot-utils";
import {
  ACTION_INFO,
  actionBlockedReason,
  DEFAULT_SNAPSHOT_KEEP,
  draftFrom,
  editBlockedReason,
  MAX_SNAPSHOT_KEEP,
  RUNNING_ONLY_ACTIONS,
  SCHEDULE_ACTIONS,
  SNAPSHOT_KEEP_PERMISSION,
  WARN_ACTIONS,
  warnPayload,
  type ScheduleAction,
  type ScheduleDraft,
} from "./schedule-meta";

type FieldKey = "name" | "cron" | "command" | "backupName" | "keep" | "warnMessage";

function validate(d: ScheduleDraft, canKeep: boolean): Partial<Record<FieldKey, string>> {
  const e: Partial<Record<FieldKey, string>> = {};
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
  if (d.action === "snapshot" && canKeep) {
    const n = Number(d.keep.trim());
    if (!/^\d+$/.test(d.keep.trim()) || !Number.isInteger(n) || n < 1 || n > MAX_SNAPSHOT_KEEP)
      e.keep = `Enter a whole number from 1 to ${MAX_SNAPSHOT_KEEP}`;
  }
  if (WARN_ACTIONS.includes(d.action) && d.warnMinutes > 0) {
    const w = warnMessageError(d.warnMessage);
    if (w) e.warnMessage = w;
  }
  return e;
}

export function ScheduleDialog({
  instanceId,
  schedule,
  template,
  can,
  panelTz,
  open,
  onOpenChange,
}: {
  instanceId: string;
  schedule?: Schedule | null;
  template?: Partial<ScheduleDraft>;
  /** the user's permissions on this server: only actions they may run themselves are offered */
  can: (code: PermissionCode) => boolean;
  panelTz: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const canBackup = can("instance.backup");
  const canKeep = can(SNAPSHOT_KEEP_PERMISSION);
  const [draft, setDraft] = useState<ScheduleDraft>(() => {
    const d = draftFrom(schedule, template);
    if (schedule || !actionBlockedReason(d.action, can)) return d;
    // a new schedule starts with the first action this user may schedule
    const first = SCHEDULE_ACTIONS.find((a) => !actionBlockedReason(a, can));
    return first ? { ...d, action: first } : d;
  });
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
      } else if (draft.action === "snapshot") {
        // without instance.restore the keep count is left as it is (new schedules: the default)
        payload = canKeep ? { ...keep, keep: Number(draft.keep.trim()) } : keep;
      }
      else if (WARN_ACTIONS.includes(draft.action)) payload = warnPayload(keep, draft);
      else payload = keep;
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

  const errors = validate(draft, canKeep);
  const blocked = editing
    ? editBlockedReason({ action: draft.action, payload: schedule!.action === draft.action ? schedule!.payload : {} }, can)
    : actionBlockedReason(draft.action, can);
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
    if (Object.keys(errors).length || blocked) return;
    save.mutate();
  };
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
                {SCHEDULE_ACTIONS.map((a) => {
                  const reason = actionBlockedReason(a, can);
                  return (
                    <SelectItem key={a} value={a} disabled={!!reason}>
                      {ACTION_INFO[a].label}
                      {reason ? ` (${reason})` : ""}
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{ACTION_INFO[draft.action]?.description}</p>
          </div>

          {WARN_ACTIONS.includes(draft.action) && (
            <div className="grid gap-3 rounded-lg border p-3">
              <div className="grid gap-1.5">
                <Label htmlFor="sched-warn">Warn players in game</Label>
                <Select value={String(draft.warnMinutes)} onValueChange={(v) => set("warnMinutes", Number(v))}>
                  <SelectTrigger id="sched-warn" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {WARN_MINUTES.map((m) => (
                      <SelectItem key={m} value={String(m)}>
                        {warnOptionLabel(m)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {draft.warnMinutes > 0
                    ? `The countdown starts at the scheduled time. ${describeWarning(draft.warnMinutes)} Without players online it runs at once.`
                    : `The ${draft.action} happens at the scheduled time without a warning.`}
                </p>
              </div>
              {draft.warnMinutes > 0 && (
                <div className="grid gap-1.5">
                  <Label htmlFor="sched-warn-msg">Message (optional)</Label>
                  <Input
                    id="sched-warn-msg"
                    value={draft.warnMessage}
                    maxLength={WARN_MESSAGE_MAX}
                    placeholder="e.g. Daily restart"
                    onChange={(e) => set("warnMessage", e.target.value.replace(/[\r\n]/g, " "))}
                    aria-invalid={!!err("warnMessage", "payload") || undefined}
                    aria-describedby="sched-warn-msg-help"
                    autoComplete="off"
                  />
                  <p
                    id="sched-warn-msg-help"
                    className={cn("text-xs", err("warnMessage", "payload") ? "text-destructive" : "text-muted-foreground")}
                  >
                    {err("warnMessage", "payload") ?? "Shown after each warning. Plain text, one line."}
                  </p>
                </div>
              )}
            </div>
          )}

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

          {draft.action === "snapshot" && (
            <SnapshotFields
              instanceId={instanceId}
              canBackup={canBackup}
              canKeep={canKeep}
              keep={draft.keep}
              error={err("keep", "payload")}
              onKeep={(v) => set("keep", v)}
              onNavigate={() => onOpenChange(false)}
            />
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
          {blocked && (
            <p className="text-sm text-muted-foreground" role="status">
              {blocked.charAt(0).toUpperCase() + blocked.slice(1)}
              {blocked.endsWith(".") ? "" : "."}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || !!blocked}>
              {save.isPending && <Loader2 className="animate-spin" />}
              {editing ? "Save changes" : "Create schedule"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Keep-count input plus a live check that snapshots are usable on this server. */
function SnapshotFields({
  instanceId,
  canBackup,
  canKeep,
  keep,
  error,
  onKeep,
  onNavigate,
}: {
  instanceId: string;
  canBackup: boolean;
  canKeep: boolean;
  keep: string;
  error?: string;
  onKeep: (v: string) => void;
  onNavigate: () => void;
}) {
  // GET /snapshots needs the backup permission; without it the readiness check is skipped
  const snaps = useSnapshots(instanceId, { enabled: canBackup });
  const state = snapshotState(snaps.data?.capability);
  const reason = snaps.data?.capability.reason;
  return (
    <div className="grid gap-3">
      <div className="grid gap-1.5">
        <Label htmlFor="sched-keep">Scheduled snapshots to keep</Label>
        <Input
          id="sched-keep"
          type="number"
          inputMode="numeric"
          min={1}
          max={MAX_SNAPSHOT_KEEP}
          step={1}
          className="w-32 tabular-nums"
          value={keep}
          placeholder={String(DEFAULT_SNAPSHOT_KEEP)}
          onChange={(e) => onKeep(e.target.value)}
          disabled={!canKeep}
          aria-invalid={!!error || undefined}
          aria-describedby="sched-keep-help"
        />
        <p id="sched-keep-help" className={cn("text-xs", error ? "text-destructive" : "text-muted-foreground")}>
          {!canKeep
            ? `Older scheduled snapshots are deleted after each run, so choosing how many are kept needs the “Restore backups” permission. New schedules keep the default ${DEFAULT_SNAPSHOT_KEEP}.`
            : error ??
            `After each run only the newest ${/^\d+$/.test(keep.trim()) && Number(keep) > 0 ? Number(keep) : "N"} scheduled snapshots are kept; older scheduled ones are deleted. Manual and “before rollback” snapshots are never removed automatically. Default ${DEFAULT_SNAPSHOT_KEEP} (e.g. one day of hourly snapshots).`}
        </p>
      </div>

      {state && state !== "ready" ? (
        <div
          role="status"
          className="flex gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-xs text-amber-800 dark:text-amber-200"
        >
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <p className="[overflow-wrap:anywhere]">
            {state === "unavailable"
              ? `Snapshots are not available for this server${reason ? `: ${reason}` : ""}. Runs of this schedule will fail.`
              : "Snapshots are not enabled for this server yet, so runs will fail until they are."}{" "}
            <Link to={`/servers/${instanceId}/backups`} onClick={onNavigate} className="font-medium underline underline-offset-2">
              {state === "convert" ? "Enable them on the Backups page" : "Backups page"}
            </Link>
          </p>
        </div>
      ) : (
        <p className="flex gap-2 rounded-md border bg-muted/40 px-2.5 py-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>
            Snapshots are instant and space-efficient but live on the same disk as the server; they complement backups, they
            don't replace them.
            {!canBackup && " Snapshot support on this server could not be checked (needs the backup permission)."}
          </span>
        </p>
      )}
    </div>
  );
}
