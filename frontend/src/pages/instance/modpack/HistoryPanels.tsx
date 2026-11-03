/** Update history and rollback points (pre-update backups that remember the previous release). */
import { ArrowRight, History, RotateCcw, Undo2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Instance, Job, JobStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { loaderLabel } from "@/pages/modpacks/modpackMeta";
import { pinLabel, type ModpackHistoryEntry, type ModpackPin, type RollbackPoint } from "./modpack-api";

const JOB_STATUS: Record<JobStatus, { label: string; className: string }> = {
  queued: { label: "Queued", className: "bg-muted text-muted-foreground" },
  running: { label: "Running", className: "bg-sky-500/10 text-sky-700 dark:text-sky-300" },
  succeeded: { label: "Succeeded", className: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" },
  failed: { label: "Failed", className: "bg-red-500/10 text-red-700 dark:text-red-300" },
  cancelled: { label: "Cancelled", className: "bg-muted text-muted-foreground" },
};

function pinDetail(p: Partial<ModpackPin> | null | undefined): string {
  if (!p) return "";
  return [
    p.minecraft_version ? `MC ${p.minecraft_version}` : "",
    p.modpack_loader ? [loaderLabel(p.modpack_loader), p.loader_version].filter(Boolean).join(" ") : "",
    p.java_version ? `Java ${p.java_version}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

export function UpdateHistory({ history, jobs, rollbackIds }: { history: ModpackHistoryEntry[]; jobs: Job[] | undefined; rollbackIds: Set<string> }) {
  if (!history.length) {
    return (
      <EmptyState
        icon={<History className="size-5" />}
        title="No release changes yet"
        description="Updates and release switches made from this page are listed here."
        className="py-8"
      />
    );
  }
  const byId = new Map((jobs ?? []).map((j) => [j.id, j]));
  return (
    <ol className="grid gap-2" aria-label="Release changes, newest first">
      {[...history].reverse().map((h, i) => {
        const job = byId.get(h.job);
        const mcChanged = !!h.from?.minecraft_version && !!h.to?.minecraft_version && h.from.minecraft_version !== h.to.minecraft_version;
        const status = job ? JOB_STATUS[job.status] : null;
        return (
          <li key={`${h.job}-${i}`} className="grid gap-1 rounded-lg border p-2.5">
            <div className="flex flex-wrap items-center gap-1.5 text-sm">
              <span className="break-all text-muted-foreground">{pinLabel(h.from)}</span>
              <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-label="to" />
              <span className="font-medium break-all">{pinLabel(h.to)}</span>
              {status && (
                <span className={cn("inline-flex h-5 items-center rounded-md px-1.5 text-[11px] font-medium", status.className)}>
                  {status.label}
                </span>
              )}
              {mcChanged && (
                <span className="inline-flex h-5 items-center rounded-md bg-amber-500/10 px-1.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                  MC {h.from?.minecraft_version} → {h.to?.minecraft_version}
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
              {job && (
                <span title={formatDateTime(job.created_at)}>
                  {relativeTime(job.created_at)}
                  {job.user_name ? ` by ${job.user_name}` : ""}
                </span>
              )}
              {pinDetail(h.to) && <span>{pinDetail(h.to)}</span>}
              <span>
                {h.backup
                  ? rollbackIds.has(h.backup)
                    ? "Backup taken (rollback point available)"
                    : "Backup taken (no longer available)"
                  : "No backup taken"}
              </span>
            </div>
            {job?.status === "failed" && job.error && <p className="text-xs break-words text-destructive">{job.error}</p>}
          </li>
        );
      })}
    </ol>
  );
}

export function RollbackPoints({
  instance,
  points,
  canRollback,
  canViewBackups,
  locked,
  onStarted,
}: {
  instance: Instance;
  points: RollbackPoint[];
  canRollback: boolean;
  canViewBackups: boolean;
  locked: boolean;
  onStarted: (job: Job) => void;
}) {
  const [target, setTarget] = useState<RollbackPoint | null>(null);
  if (!points.length) {
    return (
      <EmptyState
        icon={<Undo2 className="size-5" />}
        title="No rollback points"
        description="Updating with “Create a backup first” records the previous release in that backup, so you can return to it here."
        className="py-8"
      />
    );
  }
  const live = ["running", "starting", "unhealthy", "restarting", "stopping"].includes(instance.status);
  return (
    <>
      <ul className="grid gap-2" aria-label="Rollback points">
        {points.map((p) => (
          <li key={p.backup_id} className="flex flex-col gap-2 rounded-lg border p-2.5 sm:flex-row sm:items-center">
            <div className="grid min-w-0 flex-1 gap-0.5">
              <p className="truncate text-sm font-medium" title={p.name}>
                {p.name}
              </p>
              <p className="text-[11px] text-muted-foreground">
                <span title={formatDateTime(p.created_at)}>{relativeTime(p.created_at)}</span>
                {" · "}returns to <span className="font-medium text-foreground">{pinLabel(p.before)}</span>
                {pinDetail(p.before) && ` (${pinDetail(p.before)})`}
              </p>
            </div>
            {canRollback && (
              <Button
                type="button"
                size="xs"
                variant="outline"
                className="self-start sm:self-center"
                disabled={locked}
                onClick={() => setTarget(p)}
                aria-label={`Roll back to ${pinLabel(p.before)} using backup ${p.name}`}
              >
                <RotateCcw aria-hidden /> Roll back
              </Button>
            )}
          </li>
        ))}
      </ul>
      {!canRollback && (
        <p className="mt-2 text-xs text-muted-foreground">Rolling back needs the “Manage mods/plugins” and “Restore backups” permissions on this server.</p>
      )}
      {canViewBackups && (
        <p className="mt-2 text-xs text-muted-foreground">
          These are “Before update” backups. They are also listed on the{" "}
          <Link to={`/servers/${instance.id}/backups`} className="font-medium text-primary hover:underline">
            Backups tab
          </Link>
          , where retention can remove them after the safety period unless pinned.
        </p>
      )}
      {target && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setTarget(null)}
          title={`Roll back to ${pinLabel(target.before)}?`}
          confirmLabel="Roll back"
          destructive
          description={
            <div className="grid gap-2 text-sm">
              <p>
                Restores the backup “<span className="font-medium text-foreground">{target.name}</span>” from{" "}
                {formatDateTime(target.created_at)} and pins the server to {pinLabel(target.before)} again.
              </p>
              <ol className="list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
                <li>The server is stopped{live ? " (players are disconnected)" : " if it is running"}.</li>
                <li>A “Before restore” safety backup of the current state is created first.</li>
                <li>All server files are replaced with the backup, including worlds and configs.</li>
                <li>The container is recreated with the previous release and started again if it was running.</li>
              </ol>
              <p className="font-medium text-destructive">
                World progress made since {relativeTime(target.created_at)} is replaced by the backup (it stays in the safety backup).
              </p>
            </div>
          }
          onConfirm={async () => {
            try {
              const job = await api.post<Job>(`/instances/${instance.id}/modpack/rollback`, { backup_id: target.backup_id, confirm: true });
              onStarted(job);
              toast.success(`Rolling back to ${pinLabel(target.before)}`);
            } catch (err) {
              toast.error(errorMessage(err));
              throw err;
            }
          }}
        />
      )}
    </>
  );
}
