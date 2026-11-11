/** Instance schedules: cron-based restarts, backups, snapshots and console commands. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  CalendarClock,
  Camera,
  Clock,
  Lock,
  Pencil,
  Play,
  Plus,
  RotateCw,
  Square,
  Terminal,
  Trash2,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { hasCapability } from "@/lib/games";
import { qk } from "@/lib/queries";
import type { PermissionCode, Schedule } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useInstanceContext } from "./InstanceLayout";
import { snapshotState, useSnapshots } from "./backups/snapshot-utils";
import { browserOffset, describeCron, offsetFromIso } from "./schedules/cron";
import {
  ACTION_INFO,
  actionBlockedReason,
  DEFAULT_SNAPSHOT_KEEP,
  editBlockedReason,
  RUNNING_ONLY_ACTIONS,
  scheduleActions,
  scheduleWarnMinutes,
  snapshotKeep,
  WARN_ACTIONS,
  type ScheduleDraft,
} from "./schedules/schedule-meta";
import { ScheduleDialog } from "./schedules/ScheduleDialog";

const ACTION_ICONS: Record<string, LucideIcon> = {
  restart: RotateCw,
  start: Play,
  stop: Square,
  backup: Archive,
  command: Terminal,
  snapshot: Camera,
};

const TEMPLATES: { label: string; draft: Partial<ScheduleDraft> & Pick<ScheduleDraft, "action">; needsSnapshots?: boolean }[] = [
  { label: "Nightly backup", draft: { name: "Nightly backup", action: "backup", cron: "0 4 * * *" } },
  {
    label: "Hourly snapshot",
    draft: { name: "Hourly snapshot", action: "snapshot", cron: "0 * * * *", keep: String(DEFAULT_SNAPSHOT_KEEP) },
    needsSnapshots: true,
  },
  {
    label: "Daily restart",
    draft: { name: "Daily restart", action: "restart", cron: "0 5 * * *", warnMinutes: 5, warnMessage: "Daily restart" },
  },
];

function resultTone(result: string): string {
  if (/^(Error|Unknown)/i.test(result)) return "text-destructive";
  if (/^Skipped/i.test(result)) return "text-amber-700 dark:text-amber-300";
  return "text-muted-foreground";
}

function payloadSummary(s: Schedule): string | null {
  if (s.action === "command" && typeof s.payload?.command === "string") return `/${s.payload.command}`;
  if (s.action === "backup") return typeof s.payload?.name === "string" && s.payload.name ? `Named “${s.payload.name}”` : null;
  if ((s.action as string) === "snapshot") {
    // backend: keep defaults to 24 when missing; 0 disables pruning
    if (s.payload?.keep === 0) return "Keeps all scheduled snapshots";
    const n = snapshotKeep(s) ?? DEFAULT_SNAPSHOT_KEEP;
    return `Keeps the newest ${n} scheduled snapshot${n === 1 ? "" : "s"}`;
  }
  if (WARN_ACTIONS.includes(s.action)) {
    const minutes = scheduleWarnMinutes(s) ?? 0;
    if (!minutes) return null;
    const message = typeof s.payload?.warn_message === "string" && s.payload.warn_message ? ` · “${s.payload.warn_message}”` : "";
    return `Warns players ${minutes} min before${message}`;
  }
  return null;
}

function ScheduleRow({
  schedule: s,
  canManage,
  can,
  onEdit,
  instanceId,
}: {
  schedule: Schedule;
  canManage: boolean;
  can: (code: PermissionCode) => boolean;
  onEdit: () => void;
  instanceId: string;
}) {
  const qc = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.patch<Schedule>(`/instances/${instanceId}/schedules/${s.id}`, { enabled }),
    onSuccess: (data) => {
      qc.setQueryData<Schedule[]>(qk.schedules(instanceId), (old) => old?.map((x) => (x.id === data.id ? data : x)));
      qc.invalidateQueries({ queryKey: qk.schedules(instanceId) });
      toast.success(`${data.name} ${data.enabled ? "enabled" : "paused"}`);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const remove = useMutation({
    mutationFn: () => api.delete(`/instances/${instanceId}/schedules/${s.id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.schedules(instanceId) });
      toast.success(`Schedule “${s.name}” deleted`);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const Icon = ACTION_ICONS[s.action] ?? CalendarClock;
  const description = describeCron(s.cron);
  const summary = payloadSummary(s);
  const enabled = toggle.isPending ? !!toggle.variables : s.enabled;
  // editing needs the permission of the schedule's action (and restore for a snapshot keep count)
  const lockedReason = editBlockedReason(s, can);
  const editable = canManage && !lockedReason;

  return (
    <li className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-2 px-4 py-3 md:grid-cols-[auto_minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_auto] md:items-center">
      <div className="pt-0.5 md:pt-0">
        <Switch
          checked={enabled}
          onCheckedChange={(v) => toggle.mutate(v)}
          disabled={!canManage || toggle.isPending}
          aria-label={`${s.enabled ? "Pause" : "Enable"} schedule ${s.name}`}
        />
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn("truncate font-medium", !s.enabled && "text-muted-foreground")}>{s.name}</span>
          <Badge variant="outline" className="gap-1">
            <Icon aria-hidden />
            {ACTION_INFO[s.action]?.label ?? s.action}
          </Badge>
          {!s.enabled && <Badge variant="secondary">Paused</Badge>}
        </div>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {description ?? "Custom schedule"} · <code className="font-mono">{s.cron}</code>
          {RUNNING_ONLY_ACTIONS.includes(s.action) && s.only_when_running && " · only when running"}
        </p>
        {summary && <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">{summary}</p>}
      </div>

      <div className="col-start-2 min-w-0 text-xs md:col-start-auto">
        <p className="text-muted-foreground">Next run</p>
        {!s.enabled ? (
          <p className="text-muted-foreground">—</p>
        ) : s.next_run_at ? (
          <p title={formatDateTime(s.next_run_at)}>
            {relativeTime(s.next_run_at)} <span className="text-muted-foreground">· {formatDateTime(s.next_run_at)}</span>
          </p>
        ) : (
          <p className="text-muted-foreground" title="The scheduler computes the next run within about a minute of saving.">
            Being scheduled…
          </p>
        )}
      </div>

      <div className="col-start-2 min-w-0 text-xs md:col-start-auto">
        <p className="text-muted-foreground">Last run</p>
        {s.last_run_at ? (
          <>
            <p title={formatDateTime(s.last_run_at)}>{relativeTime(s.last_run_at)}</p>
            {s.last_result && (
              <p className={cn("truncate", resultTone(s.last_result))} title={s.last_result}>
                {s.last_result}
              </p>
            )}
          </>
        ) : (
          <p className="text-muted-foreground">Never</p>
        )}
      </div>

      <div className="col-start-2 flex items-center gap-1 md:col-start-auto">
        {editable ? (
          <>
            <Button variant="ghost" size="icon-sm" onClick={onEdit} aria-label={`Edit schedule ${s.name}`}>
              <Pencil />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Delete schedule ${s.name}`}
              disabled={remove.isPending}
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2 />
            </Button>
            <ConfirmDialog
              open={confirmDelete}
              onOpenChange={setConfirmDelete}
              title={`Delete “${s.name}”?`}
              description="The schedule stops running immediately. Backups or jobs it already created are kept."
              confirmLabel="Delete schedule"
              destructive
              onConfirm={async () => {
                await remove.mutateAsync().catch(() => undefined);
              }}
            />
          </>
        ) : canManage ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-flex size-7 items-center justify-center text-muted-foreground" tabIndex={0}>
                <Lock className="size-3.5" aria-label="Locked" />
              </span>
            </TooltipTrigger>
            <TooltipContent>{lockedReason} You can still pause or resume it.</TooltipContent>
          </Tooltip>
        ) : null}
      </div>
    </li>
  );
}

export default function Schedules() {
  const { instance, can } = useInstanceContext();
  const canManage = can("instance.schedules");
  const canBackup = can("instance.backup");
  // console commands only for a game with a console (the backend refuses them for Palworld, Valheim, Satisfactory)
  const hasConsole = hasCapability(instance, "console_commands");
  // a schedule runs its action with the panel's authority: only actions the user may run are offered
  const canCreate = canManage && scheduleActions(hasConsole).some((a) => !actionBlockedReason(a, can));
  const [dialog, setDialog] = useState<{ key: number; schedule?: Schedule; template?: Partial<ScheduleDraft> } | null>(null);

  const query = useQuery({
    queryKey: qk.schedules(instance.id),
    queryFn: () => api.get<Schedule[]>(`/instances/${instance.id}/schedules`),
    // next_run_at is filled in by the scheduler tick (every minute) after a create/edit
    refetchInterval: (q) => {
      const data = q.state.data as Schedule[] | undefined;
      return data?.some((s) => s.enabled && !s.next_run_at) ? 10_000 : 60_000;
    },
  });
  const schedules = query.data ?? [];
  const panelTz = offsetFromIso(schedules.find((s) => s.next_run_at)?.next_run_at ?? instance.updated_at);
  const localTz = browserOffset();
  const openNew = (template?: Partial<ScheduleDraft>) => setDialog({ key: Date.now(), template });
  // only offer the snapshot template where snapshots actually work (GET /snapshots needs instance.backup)
  const snaps = useSnapshots(instance.id, { enabled: canManage && canBackup && query.data?.length === 0 });
  const snapshotsReady = snapshotState(snaps.data?.capability) === "ready";

  return (
    <div className="grid min-w-0 gap-4">
      <Section
        title="Schedules"
        description={
          <>
            Automate restarts, backups, snapshots{hasConsole ? " and console commands" : ""} with cron expressions. Times use the panel server's timezone
            {panelTz ? ` (${panelTz})` : ""}
            {panelTz && panelTz !== localTz ? `; run times below are shown in your browser's time (${localTz})` : ""}.
          </>
        }
        actions={
          canCreate && (
            <Button size="sm" onClick={() => openNew()}>
              <Plus /> New schedule
            </Button>
          )
        }
        className="[&>div:last-child]:p-0"
      >
        {query.isLoading ? (
          <LoadingState label="Loading schedules…" />
        ) : query.error ? (
          <div className="p-4">
            <ErrorState error={query.error} onRetry={() => query.refetch()} title="Could not load schedules" />
          </div>
        ) : schedules.length === 0 ? (
          <div className="p-4">
            <EmptyState
              icon={<CalendarClock className="size-5" />}
              title="No schedules yet"
              description={
                canCreate
                  ? "Start from a template or create your own. A nightly backup is a good first schedule."
                  : "No automation is configured for this server."
              }
              action={
                canCreate && (
                  <div className="flex flex-wrap justify-center gap-2">
                    {TEMPLATES.filter((t) => !actionBlockedReason(t.draft.action, can) && (!t.needsSnapshots || snapshotsReady)).map((t) => (
                      <Button key={t.label} size="sm" variant="outline" onClick={() => openNew(t.draft)}>
                        <Plus /> {t.label}
                      </Button>
                    ))}
                  </div>
                )
              }
            />
          </div>
        ) : (
          <ul className="divide-y" aria-label="Schedules">
            {schedules.map((s) => (
              <ScheduleRow
                key={s.id}
                schedule={s}
                canManage={canManage}
                can={can}
                instanceId={instance.id}
                onEdit={() => setDialog({ key: Date.now(), schedule: s })}
              />
            ))}
          </ul>
        )}
      </Section>

      <div className="grid gap-2 rounded-xl border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="flex items-center gap-1.5 font-medium text-foreground">
          <Clock className="size-3.5" aria-hidden /> How schedules run
        </p>
        <ul className="grid list-disc gap-1 pl-5">
          <li>
            The panel checks schedules once a minute. A run is skipped (and noted as the last result) if another operation
            such as an update or restore is in progress, or the server is archived.
          </li>
          <li>
            Cron format: <code className="font-mono">minute hour day-of-month month day-of-week</code>, e.g.{" "}
            <code className="font-mono">30 3 * * 1-5</code> runs at 03:30 on weekdays. Sunday is 0 or 7.
          </li>
          <li>Scheduled backups follow the retention policy configured on the Backups page.</li>
          <li>
            Restarts and stops with a warning start a countdown at the scheduled time: online players are told in chat, and the
            action follows after the chosen number of minutes (at once when nobody is online). The countdown can be cancelled
            from the server header.
          </li>
          <li>
            Snapshot schedules take instant btrfs or ZFS snapshots (where enabled on the Backups page) and keep only the newest scheduled
            snapshots, as set per schedule (default {DEFAULT_SNAPSHOT_KEEP}). Manual and “before rollback” snapshots are never
            pruned.
          </li>
        </ul>
        {!canManage ? (
          <p className="flex items-center gap-1.5">
            <Lock className="size-3.5" aria-hidden /> You can view schedules but need the “Manage schedules” permission to change
            them.
          </p>
        ) : (
          !canCreate && (
            <p className="flex items-center gap-1.5">
              <Lock className="size-3.5" aria-hidden /> You can pause and delete schedules. Creating or editing one also needs the
              permission of its action (start/stop/restart, backups or commands).
            </p>
          )
        )}
      </div>

      {dialog && (
        <ScheduleDialog
          key={dialog.key}
          instanceId={instance.id}
          schedule={dialog.schedule}
          template={dialog.template}
          can={can}
          panelTz={panelTz}
          open
          onOpenChange={(o) => !o && setDialog(null)}
          actions={scheduleActions(hasConsole, dialog.schedule?.action)}
        />
      )}
    </div>
  );
}
