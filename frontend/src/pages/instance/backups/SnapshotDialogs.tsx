/** Take / enable / roll back / delete dialogs for btrfs and ZFS snapshots on the Backups page. */
import { useMutation } from "@tanstack/react-query";
import { Camera, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Instance, Job } from "@/lib/types";
import { SnapshotKindBadge } from "./SnapshotBits";
import { snapshotTime, snapshotTitle, type Snapshot } from "./snapshot-utils";

const RUNNING = ["running", "starting", "unhealthy", "restarting"];

type Consistency = "auto" | "require" | "none";

const CONSISTENCY: { value: Consistency; label: string; description: string }[] = [
  {
    value: "auto",
    label: "Automatic (recommended)",
    description:
      "If the server is running, world saving is paused and flushed to disk (save-off / save-all flush) for the moment the snapshot is taken, then turned back on. If pausing fails, the snapshot is still taken and marked crash-consistent.",
  },
  {
    value: "require",
    label: "Require a consistent snapshot",
    description: "Like automatic, but fails instead of continuing if world saving cannot be paused (for example, RCON is unreachable).",
  },
  {
    value: "none",
    label: "Don't pause saving",
    description: "Snapshot the files as they are without touching the running server. Only crash-consistent while the server is running.",
  },
];

function SnapshotSummary({ snapshot }: { snapshot: Snapshot }) {
  const at = snapshotTime(snapshot);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-muted/40 px-3 py-2 text-xs">
      <span className="min-w-0 font-medium [overflow-wrap:anywhere] text-foreground">{snapshotTitle(snapshot)}</span>
      <SnapshotKindBadge kind={snapshot.kind} />
      {at && (
        <span className="text-muted-foreground" title={formatDateTime(at)}>
          {relativeTime(at)}
        </span>
      )}
    </div>
  );
}

export function CreateSnapshotDialog({
  instance,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const [label, setLabel] = useState("");
  const [consistency, setConsistency] = useState<Consistency>("auto");
  const running = RUNNING.includes(instance.status);
  const create = useMutation({
    mutationFn: () => api.post<Job>(`/instances/${instance.id}/snapshots`, { label: label.trim(), consistency }),
    onSuccess: (job) => {
      toast.success("Snapshot started");
      onStarted(job);
      onOpenChange(false);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  const chosen = CONSISTENCY.find((c) => c.value === consistency);
  return (
    <Dialog open={open} onOpenChange={(o) => !create.isPending && onOpenChange(o)}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Camera className="size-4 text-muted-foreground" aria-hidden /> Take snapshot
            </DialogTitle>
            <DialogDescription>
              Freezes the current state of the whole server folder instantly. It shares unchanged data with the live files, so
              it takes almost no space at first. The server is currently <strong>{running ? "running" : "not running"}</strong>.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-1.5">
            <Label htmlFor="snapshot-label">Label (optional)</Label>
            <Input
              id="snapshot-label"
              value={label}
              maxLength={120}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. Before the boss fight"
              autoFocus
            />
            <p className="text-xs text-muted-foreground">
              The snapshot is named after the current UTC time; letters and digits of the label are appended to the name.
            </p>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="snapshot-consistency">World consistency</Label>
            <Select value={consistency} onValueChange={(v) => setConsistency(v as Consistency)}>
              <SelectTrigger id="snapshot-consistency" className="w-full" aria-describedby="snapshot-consistency-help">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CONSISTENCY.map((c) => (
                  <SelectItem key={c.value} value={c.value}>
                    {c.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p id="snapshot-consistency-help" className="text-xs text-muted-foreground">
              {running ? chosen?.description : "The server is stopped, so every option produces a consistent snapshot."}
            </p>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !!instance.active_job}>
              {create.isPending ? <Loader2 className="animate-spin" /> : <Camera />} Take snapshot
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function EnableSnapshotsDialog({
  instance,
  provider,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  /** "btrfs" | "zfs" from the snapshot capability */
  provider: string | null;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const running = RUNNING.includes(instance.status);
  const convert = useMutation({
    mutationFn: () => api.post<Job>(`/instances/${instance.id}/snapshots/convert`),
    onSuccess: (job) => {
      toast.success("Enabling snapshots");
      onStarted(job);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Enable snapshots for “${instance.name}”?`}
      confirmLabel="Enable snapshots"
      description={
        <ol className="grid list-decimal gap-1.5 pl-5 text-sm text-muted-foreground">
          <li>
            {running
              ? "The server is stopped briefly (players are disconnected)."
              : "The server stays stopped during the conversion."}
          </li>
          {provider === "zfs" ? (
            <li>
              The server folder is copied into a new <strong className="text-foreground">ZFS dataset</strong> of its own. This
              needs free space for a full copy while it runs (unless the pool supports block cloning) and takes longer for big
              worlds.
            </li>
          ) : (
            <li>
              The server folder is copied into a new btrfs subvolume using <strong className="text-foreground">reflinks</strong>{" "}
              (copy-on-write): file data is shared instead of duplicated, so it is fast and needs almost no extra space.
            </li>
          )}
          <li>The original folder is swapped out and removed only after the copy succeeded. Files and settings stay the same.</li>
          <li>{running ? "The server is started again." : "The server is left stopped."}</li>
        </ol>
      }
      onConfirm={async () => {
        try {
          await convert.mutateAsync();
        } catch {
          /* reported by onError */
        }
      }}
    />
  );
}

export function RollbackSnapshotDialog({
  instance,
  snapshot,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  snapshot: Snapshot;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const running = RUNNING.includes(instance.status);
  const at = snapshotTime(snapshot);
  const rollback = useMutation({
    mutationFn: () =>
      api.post<Job>(`/instances/${instance.id}/snapshots/${encodeURIComponent(snapshot.name)}`, { confirm: true }),
    onSuccess: (job) => {
      toast.success("Rollback started");
      onStarted(job);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Roll back the server to this snapshot?"
      destructive
      confirmLabel="Roll back"
      description={
        <div className="grid gap-3 text-sm">
          <SnapshotSummary snapshot={snapshot} />
          <ol className="grid list-decimal gap-1.5 pl-5 text-muted-foreground">
            <li>{running ? "The server is stopped (players are disconnected)." : "The server stays stopped while rolling back."}</li>
            <li>A safety snapshot of the current state is taken automatically (“Before rollback”).</li>
            <li>
              The <strong className="text-foreground">entire server folder is replaced</strong> with the snapshot. Changes made
              {at ? ` since ${formatDateTime(at)}` : " after the snapshot"} are lost, but kept in the safety snapshot.
            </li>
            <li>{running ? "The server is started again." : "The server is left stopped."}</li>
          </ol>
          {snapshot.consistent === false && (
            <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-800 dark:text-amber-200">
              This snapshot is crash-consistent: it was taken while the world was being written. It usually works fine, but the
              newest chunks may be incomplete.
            </p>
          )}
        </div>
      }
      onConfirm={async () => {
        try {
          await rollback.mutateAsync();
        } catch {
          /* reported by onError */
        }
      }}
    />
  );
}

export function DeleteSnapshotDialog({
  instance,
  snapshot,
  provider,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  snapshot: Snapshot;
  provider: string | null;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const remove = useMutation({
    mutationFn: () => api.delete<Job>(`/instances/${instance.id}/snapshots/${encodeURIComponent(snapshot.name)}`),
    onSuccess: (job) => {
      toast.success("Deleting snapshot");
      onStarted(job);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Delete this snapshot?"
      destructive
      confirmLabel="Delete snapshot"
      description={
        <div className="grid gap-3 text-sm">
          <SnapshotSummary snapshot={snapshot} />
          <p className="text-muted-foreground">
            The snapshot is removed permanently and cannot be rolled back to anymore. The live server is not affected. Disk
            space is freed only for data that no other snapshot or the live files still share.
          </p>
          {provider === "zfs" && (
            <p className="text-xs text-muted-foreground">
              ZFS refuses to delete a snapshot that snapshots kept from before a rollback are based on; the error names them,
              delete those first.
            </p>
          )}
        </div>
      }
      onConfirm={async () => {
        try {
          await remove.mutateAsync();
        } catch {
          /* reported by onError */
        }
      }}
    />
  );
}
