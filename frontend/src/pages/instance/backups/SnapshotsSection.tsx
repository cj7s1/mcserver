/** Snapshots section of the Backups page: native btrfs snapshots of the server folder. */
import { Camera, CalendarClock, Info, Layers, Lock, RotateCcw, Trash2, Zap } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Instance } from "@/lib/types";
import { ConsistencyBadge } from "./BackupTable";
import type { TrackedJob } from "./backup-utils";
import { SnapshotKindBadge } from "./SnapshotBits";
import { CreateSnapshotDialog, DeleteSnapshotDialog, EnableSnapshotsDialog, RollbackSnapshotDialog } from "./SnapshotDialogs";
import {
  snapshotState,
  snapshotTime,
  snapshotTitle,
  useSnapshots,
  type Snapshot,
  type SnapshotCapability,
} from "./snapshot-utils";
import { TrackedJobBanner } from "./TrackedJobBanner";

type DialogState =
  | { type: "create" | "enable"; key: number }
  | { type: "rollback" | "delete"; snapshot: Snapshot; key: number }
  | null;

function Notice({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border bg-muted/40 p-3 text-xs text-muted-foreground sm:flex-row sm:items-start">
      <Info className="hidden size-4 shrink-0 sm:mt-0.5 sm:block" aria-hidden />
      <div className="grid min-w-0 flex-1 gap-1">
        <p className="font-medium text-foreground">{title}</p>
        {children}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

function Unavailable({ cap, nodeName }: { cap: SnapshotCapability; nodeName: string }) {
  return (
    <Notice title="Snapshots are not available for this server">
      <p className="[overflow-wrap:anywhere]">
        {cap.reason || "The node cannot take filesystem snapshots."}
        {cap.fstype && !cap.reason?.includes(cap.fstype) ? ` (filesystem: ${cap.fstype})` : ""}
      </p>
      <p>
        Snapshots need the server data directory of node <strong className="text-foreground">{nodeName}</strong> on a btrfs
        filesystem (for example an Unraid btrfs pool) and btrfs-progs in the agent image. ZFS is not supported yet. The restic
        backups above work on every filesystem.
      </p>
    </Notice>
  );
}

function Explainer({ instanceId }: { instanceId: string }) {
  return (
    <ul className="grid list-disc gap-1 border-t px-4 py-3 pl-8 text-xs text-muted-foreground">
      <li>
        Snapshots are <strong className="text-foreground">instant</strong> read-only copies of the server folder. They share
        unchanged data with the live files, so they start almost empty and grow as the world changes.
      </li>
      <li>
        They live <strong className="text-foreground">on the same disk</strong> as the server: a failed disk or lost pool takes
        them with it. They are not a replacement for backups; keep restic backups for disaster recovery.
      </li>
      <li>Rolling back takes a safety snapshot of the current state first, so a rollback can itself be undone.</li>
      <li>
        <CalendarClock className="mr-1 inline size-3" aria-hidden />
        Automate them with a <em>Snapshot</em> action on the{" "}
        <Link to={`/servers/${instanceId}/schedules`} className="font-medium text-primary hover:underline">
          Schedules
        </Link>{" "}
        page; each run keeps only the newest scheduled snapshots.
      </li>
    </ul>
  );
}

function SnapshotTable({
  snapshots,
  canRestore,
  busy,
  onAction,
}: {
  snapshots: Snapshot[];
  canRestore: boolean;
  busy: boolean;
  onAction: (type: "rollback" | "delete", s: Snapshot) => void;
}) {
  return (
    <Table className="text-xs">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="min-w-36 sm:min-w-44">Snapshot</TableHead>
          <TableHead className="hidden sm:table-cell">Type</TableHead>
          <TableHead className="hidden md:table-cell">Created</TableHead>
          <TableHead className="text-right">
            <span className="sr-only">Actions</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {snapshots.map((s) => {
          const at = snapshotTime(s);
          const title = snapshotTitle(s);
          return (
            <TableRow key={s.name}>
              <TableCell className="max-w-80 min-w-36 whitespace-normal sm:min-w-44">
                <p className="text-sm font-medium [overflow-wrap:anywhere] sm:truncate" title={title}>
                  {title}
                </p>
                {s.label.trim() && (
                  <code className="block truncate font-mono text-[10px] text-muted-foreground" title={s.name}>
                    {s.name}
                  </code>
                )}
                <div className="mt-1 flex flex-wrap items-center gap-1.5 text-muted-foreground sm:hidden">
                  <SnapshotKindBadge kind={s.kind} />
                  {s.consistent !== undefined && <ConsistencyBadge consistent={s.consistent} />}
                </div>
                {at && (
                  <p className="mt-0.5 text-muted-foreground md:hidden" title={formatDateTime(at)}>
                    {relativeTime(at)}
                  </p>
                )}
              </TableCell>
              <TableCell className="hidden sm:table-cell">
                <div className="flex flex-wrap items-center gap-1.5">
                  <SnapshotKindBadge kind={s.kind} />
                  {s.consistent !== undefined && <ConsistencyBadge consistent={s.consistent} />}
                </div>
              </TableCell>
              <TableCell className="hidden whitespace-nowrap md:table-cell">
                {at ? (
                  <>
                    <div>{relativeTime(at)}</div>
                    <div className="text-muted-foreground">{formatDateTime(at)}</div>
                  </>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="text-right">
                {canRestore ? (
                  <div className="flex items-center justify-end gap-1">
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={busy}
                      onClick={() => onAction("rollback", s)}
                      aria-label={`Roll back to snapshot ${title}`}
                    >
                      <RotateCcw /> <span className="hidden sm:inline">Roll back</span>
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      disabled={busy}
                      onClick={() => onAction("delete", s)}
                      aria-label={`Delete snapshot ${title}`}
                      title="Delete snapshot"
                    >
                      <Trash2 />
                    </Button>
                  </div>
                ) : null}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

export function SnapshotsSection({
  instance,
  perms,
  tracked,
  showTracked,
}: {
  instance: Instance;
  perms: { backup: boolean; restore: boolean };
  tracked: TrackedJob;
  /** the tracked job is a snapshot job: show its progress here instead of in the backups list */
  showTracked: boolean;
}) {
  const busy = !!instance.active_job;
  const query = useSnapshots(instance.id, { busy: busy || (!!tracked.jobId && !tracked.finished) });
  const [dialog, setDialog] = useState<DialogState>(null);
  const close = () => setDialog(null);
  const cap = query.data?.capability;
  const state = snapshotState(cap);
  const snapshots = query.data?.snapshots ?? [];
  const onStarted = tracked.track;

  return (
    <Section
      title={
        <span className="flex items-center gap-1.5">
          Snapshots
          {state === "ready" && snapshots.length > 0 && (
            <span className="text-xs font-normal text-muted-foreground tabular-nums">· {snapshots.length}</span>
          )}
        </span>
      }
      description="Instant btrfs snapshots of the server folder for quick rollbacks. Stored on the same disk as the server."
      actions={
        state === "ready" &&
        perms.backup && (
          <Button size="sm" onClick={() => setDialog({ type: "create", key: Date.now() })} disabled={busy}>
            <Camera /> Take snapshot
          </Button>
        )
      }
      className="min-w-0 [&>div:last-child]:p-0"
    >
      {showTracked && <TrackedJobBanner tracked={tracked} />}
      {busy && !showTracked && state !== "unavailable" && (
        <p className="border-b px-4 py-2 text-xs text-muted-foreground">
          Another operation is running ({instance.active_job?.title}). Snapshot actions are available when it finishes.
        </p>
      )}

      {query.isLoading ? (
        <LoadingState label="Checking snapshot support…" />
      ) : query.error ? (
        <div className="p-4">
          <ErrorState error={query.error} onRetry={() => query.refetch()} title="Could not load snapshots" />
        </div>
      ) : !cap ? null : state === "unavailable" ? (
        <div className="p-4">
          <Unavailable cap={cap} nodeName={instance.node_name} />
        </div>
      ) : state === "convert" ? (
        <>
          <div className="p-4">
            <Notice
              title="Snapshots are supported here but not enabled for this server yet"
              action={
                perms.restore ? (
                  <Button size="sm" onClick={() => setDialog({ type: "enable", key: Date.now() })} disabled={busy}>
                    <Zap /> Enable snapshots
                  </Button>
                ) : undefined
              }
            >
              <p>
                This server's folder is a plain directory on a btrfs filesystem. Enabling snapshots turns it into a btrfs
                subvolume: the server is stopped briefly and its files are copied with reflinks (shared, not duplicated), then
                it is started again if it was running.
              </p>
              {!perms.restore && (
                <p className="flex items-center gap-1.5">
                  <Lock className="size-3" aria-hidden /> Enabling snapshots requires the “Restore backups” permission.
                </p>
              )}
            </Notice>
          </div>
          <Explainer instanceId={instance.id} />
        </>
      ) : (
        <>
          {snapshots.length === 0 ? (
            <div className="p-4">
              <EmptyState
                icon={<Layers className="size-5" />}
                title="No snapshots yet"
                description="Take one before risky changes (new mods, big builds) to be able to roll back in seconds."
                action={
                  perms.backup && (
                    <Button size="sm" onClick={() => setDialog({ type: "create", key: Date.now() })} disabled={busy}>
                      <Camera /> Take snapshot
                    </Button>
                  )
                }
              />
            </div>
          ) : (
            <SnapshotTable
              snapshots={snapshots}
              canRestore={perms.restore}
              busy={busy}
              onAction={(type, snapshot) => setDialog({ type, snapshot, key: Date.now() })}
            />
          )}
          <Explainer instanceId={instance.id} />
        </>
      )}

      {dialog?.type === "create" && (
        <CreateSnapshotDialog key={dialog.key} instance={instance} open onOpenChange={(o) => !o && close()} onStarted={onStarted} />
      )}
      {dialog?.type === "enable" && (
        <EnableSnapshotsDialog key={dialog.key} instance={instance} open onOpenChange={(o) => !o && close()} onStarted={onStarted} />
      )}
      {dialog?.type === "rollback" && (
        <RollbackSnapshotDialog
          key={dialog.key}
          instance={instance}
          snapshot={dialog.snapshot}
          open
          onOpenChange={(o) => !o && close()}
          onStarted={onStarted}
        />
      )}
      {dialog?.type === "delete" && (
        <DeleteSnapshotDialog
          key={dialog.key}
          instance={instance}
          snapshot={dialog.snapshot}
          open
          onOpenChange={(o) => !o && close()}
          onStarted={onStarted}
        />
      )}
    </Section>
  );
}
