/** Instance backups: restic backups, restore, file browser, retention, integrity checks, btrfs snapshots and world import/export. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Archive, Camera, Clock, HardDrive, KeyRound, Lock, Pin, Plus, ShieldCheck } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/use-auth";
import { api, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { Backup } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useInstanceContext } from "./InstanceLayout";
import { CreateBackupDialog, EditBackupDialog, VerifyDialog } from "./backups/BackupDialogs";
import { BackupTable, type BackupAction } from "./backups/BackupTable";
import { BrowseBackupDialog } from "./backups/BrowseBackupDialog";
import { DeleteBackupDialog, RestoreAsNewDialog, RestoreDialog } from "./backups/RestoreDialogs";
import { RetentionPolicyForm } from "./backups/RetentionPolicyForm";
import { SnapshotsSection } from "./backups/SnapshotsSection";
import { TrackedJobBanner } from "./backups/TrackedJobBanner";
import { useBackups, useTrackedJob, type BackupsResponse } from "./backups/backup-utils";
import { isSnapshotJob } from "./backups/snapshot-utils";
import { WorldSection } from "./backups/WorldSection";
import { isWorldJob } from "./backups/world-utils";

type DialogState = { type: BackupAction | "create" | "verify"; backup?: Backup; key: number } | null;

function Tile({ icon, label, value, sub, tone }: { icon: ReactNode; label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="min-w-0 rounded-xl border bg-card p-3">
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        {label}
      </p>
      <p className={cn("mt-1 truncate text-sm font-semibold", tone)}>{value}</p>
      {sub && <p className="mt-0.5 truncate text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

function Summary({ backups }: { backups: Backup[] }) {
  const ok = backups.filter((b) => b.status === "succeeded");
  const failed = backups.filter((b) => b.status === "failed").length;
  const latest = ok[0];
  const pinned = ok.filter((b) => b.pinned).length;
  const verified = ok.filter((b) => b.verified_at).sort((a, b) => (b.verified_at ?? "").localeCompare(a.verified_at ?? ""))[0];
  const iconCls = "size-3.5";
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Tile
        icon={<Archive className={iconCls} aria-hidden />}
        label="Backups"
        value={`${ok.length} restorable`}
        sub={[pinned ? `${pinned} pinned` : null, failed ? `${failed} failed` : null].filter(Boolean).join(" · ") || "none pinned"}
      />
      <Tile
        icon={<Clock className={iconCls} aria-hidden />}
        label="Latest backup"
        value={latest ? relativeTime(latest.created_at) : "Never"}
        sub={latest ? latest.name : "Create one to protect this server"}
        tone={latest ? undefined : "text-amber-700 dark:text-amber-300"}
      />
      <Tile
        icon={<HardDrive className={iconCls} aria-hidden />}
        label="Latest size"
        value={latest ? formatBytes(latest.total_bytes) : "—"}
        sub={latest ? `+${formatBytes(latest.data_added_bytes)} new data stored` : "source size of the newest backup"}
      />
      <Tile
        icon={<ShieldCheck className={iconCls} aria-hidden />}
        label="Integrity"
        value={!verified ? "Not verified" : verified.verify_ok ? "Check passed" : "Check failed"}
        sub={verified ? formatDateTime(verified.verified_at) : "Run “Verify integrity” periodically"}
        tone={!verified ? undefined : verified.verify_ok ? "text-emerald-700 dark:text-emerald-300" : "text-destructive"}
      />
    </div>
  );
}

export default function Backups() {
  const { instance, can } = useInstanceContext();
  const auth = useAuth();
  const qc = useQueryClient();
  const perms = {
    backup: can("instance.backup"),
    restore: can("instance.restore"),
    files: can("instance.files.read"),
    filesWrite: can("instance.files.write"),
    createInstance: auth.can("instance.create"),
  };
  const busy = !!instance.active_job;
  const tracked = useTrackedJob(instance);
  const query = useBackups(instance.id, busy || (!!tracked.jobId && !tracked.finished));
  const [dialog, setDialog] = useState<DialogState>(null);
  const open = (type: NonNullable<DialogState>["type"], backup?: Backup) => setDialog({ type, backup, key: Date.now() });
  const close = () => setDialog(null);

  const pin = useMutation({
    mutationFn: (b: Backup) => api.patch<Backup>(`/instances/${instance.id}/backups/${b.id}`, { pinned: !b.pinned }),
    onSuccess: (b) => {
      qc.setQueryData<BackupsResponse>(qk.backups(instance.id), (old) =>
        old ? { ...old, backups: old.backups.map((x) => (x.id === b.id ? b : x)) } : old,
      );
      toast.success(b.pinned ? `Pinned “${b.name}”: retention will never remove it` : `Unpinned “${b.name}”`);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  if (!perms.backup) {
    return (
      <EmptyState
        icon={<Lock className="size-5" />}
        title="No access to backups"
        description="You need the “Create backups” permission on this server to view its backups."
      />
    );
  }

  const backups = query.data?.backups ?? [];
  const restorable = backups.filter((b) => b.status === "succeeded").length;
  const onStarted = tracked.track;
  // jobs are exclusive per instance, so one tracker serves every section; show it where it belongs
  const trackedKind = tracked.job?.kind ?? instance.active_job?.kind;
  const snapshotJob = !!tracked.jobId && isSnapshotJob(trackedKind);
  const worldJob = !!tracked.jobId && isWorldJob(trackedKind);

  return (
    <div className="grid min-w-0 gap-4">
      {query.data && <Summary backups={backups} />}

      <Section
        title="Backups"
        description="Incremental, deduplicated and encrypted snapshots of the whole server folder."
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => open("verify")} disabled={busy || restorable === 0}>
              <ShieldCheck /> <span className="hidden sm:inline">Verify integrity</span>
              <span className="sm:hidden">Verify</span>
            </Button>
            <Button size="sm" onClick={() => open("create")} disabled={busy}>
              <Plus /> Create backup
            </Button>
          </>
        }
        className="min-w-0 [&>div:last-child]:p-0"
      >
        {busy && (!tracked.jobId || snapshotJob || worldJob) && (
          <p className="border-b px-4 py-2 text-xs text-muted-foreground">
            Another operation is running ({instance.active_job?.title}). Backup actions are available when it finishes.
          </p>
        )}
        {!snapshotJob && !worldJob && <TrackedJobBanner tracked={tracked} />}
        {query.isLoading ? (
          <LoadingState label="Loading backups…" />
        ) : query.error ? (
          <div className="p-4">
            <ErrorState error={query.error} onRetry={() => query.refetch()} title="Could not load backups" />
          </div>
        ) : backups.length === 0 ? (
          <div className="p-4">
            <EmptyState
              icon={<Archive className="size-5" />}
              title="No backups yet"
              description="Create a first backup now, and add a backup schedule so it happens automatically."
              action={
                <Button size="sm" onClick={() => open("create")} disabled={busy}>
                  <Plus /> Create backup
                </Button>
              }
            />
          </div>
        ) : (
          <BackupTable
            backups={backups}
            perms={perms}
            busy={busy}
            pinPending={pin.isPending ? (pin.variables?.id ?? null) : null}
            onTogglePin={(b) => pin.mutate(b)}
            onAction={(type, b) => open(type, b)}
          />
        )}
      </Section>

      {query.data && (
        <Section
          title="Retention policy"
          description="Decides which automatic and manual backups are pruned to save space."
        >
          <RetentionPolicyForm
            key={JSON.stringify(query.data.policy)}
            instanceId={instance.id}
            policy={query.data.policy}
            backups={backups}
            canEdit={perms.restore}
          />
        </Section>
      )}

      <SnapshotsSection
        instance={instance}
        perms={{ backup: perms.backup, restore: perms.restore }}
        tracked={tracked}
        showTracked={snapshotJob}
      />

      {perms.files && (
        <WorldSection
          instance={instance}
          canReplacePerm={perms.restore && perms.filesWrite}
          tracked={tracked}
          showTracked={worldJob}
        />
      )}

      <section className="grid gap-3 rounded-xl border bg-muted/30 p-4 text-xs text-muted-foreground md:grid-cols-2">
        <div className="grid content-start gap-1.5">
          <p className="flex items-center gap-1.5 font-medium text-foreground">
            <Archive className="size-3.5" aria-hidden /> How backups work
          </p>
          <p>
            Backups use <strong className="text-foreground">restic</strong>. Each server has its own repository in the host's backup
            directory. Unchanged files are stored only once (deduplicated), so frequent backups stay small, and everything is
            encrypted at rest.
          </p>
          <p>
            <Pin className="mr-1 inline size-3" aria-hidden />
            Pin a backup to keep it forever. “Before restore” and “Before update” safety backups are created automatically
            (also before a world is replaced).
          </p>
        </div>
        <div className="grid content-start gap-1.5">
          <p className="flex items-center gap-1.5 font-medium text-foreground">
            <KeyRound className="size-3.5" aria-hidden /> Keep the repository password safe
          </p>
          <p>
            The encryption password belongs to the host agent (<code className="font-mono">AGENT_RESTIC_PASSWORD</code> or{" "}
            <code className="font-mono">AGENT_RESTIC_PASSWORD_FILE</code>; generated in the agent's state directory if unset).
            Without it, no backup can be restored. Store a copy off the host, and copy the backup directory elsewhere for
            off-site protection.
          </p>
          <p>
            <Camera className="mr-1 inline size-3" aria-hidden />
            On btrfs, instant snapshots (above) complement backups for quick rollbacks; they stay on the same disk. ZFS snapshots
            are not supported yet.
          </p>
        </div>
      </section>

      {dialog?.type === "create" && (
        <CreateBackupDialog key={dialog.key} instance={instance} open onOpenChange={(o) => !o && close()} onStarted={onStarted} />
      )}
      {dialog?.type === "verify" && (
        <VerifyDialog
          key={dialog.key}
          instance={instance}
          backupCount={restorable}
          open
          onOpenChange={(o) => !o && close()}
          onStarted={onStarted}
        />
      )}
      {dialog?.type === "edit" && dialog.backup && (
        <EditBackupDialog key={dialog.key} instanceId={instance.id} backup={dialog.backup} open onOpenChange={(o) => !o && close()} />
      )}
      {dialog?.type === "restore" && dialog.backup && (
        <RestoreDialog
          key={dialog.key}
          instance={instance}
          backup={dialog.backup}
          open
          onOpenChange={(o) => !o && close()}
          onStarted={onStarted}
        />
      )}
      {dialog?.type === "restore-new" && dialog.backup && (
        <RestoreAsNewDialog key={dialog.key} instance={instance} backup={dialog.backup} open onOpenChange={(o) => !o && close()} />
      )}
      {dialog?.type === "delete" && dialog.backup && (
        <DeleteBackupDialog
          key={dialog.key}
          instanceId={instance.id}
          backup={dialog.backup}
          open
          onOpenChange={(o) => !o && close()}
          onStarted={(job) => {
            if (job) onStarted(job);
            else qc.invalidateQueries({ queryKey: qk.backups(instance.id) });
          }}
        />
      )}
      {dialog?.type === "browse" && dialog.backup && (
        <BrowseBackupDialog
          key={dialog.key}
          instance={instance}
          backup={dialog.backup}
          canRestore={perms.restore}
          open
          onOpenChange={(o) => !o && close()}
          onStarted={onStarted}
        />
      )}
    </div>
  );
}
