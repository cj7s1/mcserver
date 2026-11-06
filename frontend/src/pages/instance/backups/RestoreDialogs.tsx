/** Restore in place, restore as a new server, and delete confirmations for a backup. */
import { useMutation } from "@tanstack/react-query";
import { CopyPlus, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import { useInvalidateInstance } from "@/lib/queries";
import type { Backup, Instance, Job } from "@/lib/types";
import { usePorts, type RestoreAsNewResponse } from "./backup-utils";
import { KindBadge } from "./KindBadge";

const RUNNING = ["running", "starting", "unhealthy", "restarting"];

function BackupSummary({ backup }: { backup: Backup }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-muted/40 px-3 py-2 text-xs">
      <span className="font-medium text-foreground">{backup.name}</span>
      <KindBadge kind={backup.kind} />
      <span className="text-muted-foreground" title={formatDateTime(backup.created_at)}>
        {relativeTime(backup.created_at)}
      </span>
      {backup.total_bytes !== null && <span className="text-muted-foreground">{formatBytes(backup.total_bytes)}</span>}
    </div>
  );
}

export function RestoreDialog({
  instance,
  backup,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  backup: Backup;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const [safety, setSafety] = useState(true);
  const running = RUNNING.includes(instance.status);
  const restore = useMutation({
    mutationFn: () =>
      api.post<Job>(`/instances/${instance.id}/backups/${backup.id}/restore`, { confirm: true, pre_restore_backup: safety }),
    onSuccess: (job) => {
      toast.success("Restore started");
      onStarted(job);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Restore this backup over the server?"
      destructive
      confirmLabel="Restore backup"
      // without a safety backup the current files are gone for good, so ask for the server name
      confirmText={safety ? undefined : instance.name}
      description={
        <div className="grid gap-3 text-sm">
          <BackupSummary backup={backup} />
          <ol className="grid list-decimal gap-1.5 pl-5 text-muted-foreground">
            <li>{running ? "The server is stopped (players are disconnected)." : "The server stays stopped while restoring."}</li>
            <li>{safety ? "A safety backup of the current files is taken first." : "No safety backup is taken."}</li>
            <li>
              The <strong className="text-foreground">entire server folder is replaced</strong> with the backup's contents.
              Changes made since {formatDateTime(backup.created_at)} are lost
              {safety ? " (but kept in the safety backup)" : ""}.
            </li>
            <li>{running ? "The server is started again." : "The server is left stopped."}</li>
          </ol>
          {!backup.consistent && (
            <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-800 dark:text-amber-200">
              This backup is crash-consistent: it was taken while the world was being written. It usually restores fine, but
              the newest chunks may be incomplete.
            </p>
          )}
        </div>
      }
      onConfirm={async () => {
        try {
          await restore.mutateAsync();
        } catch {
          /* reported by onError */
        }
      }}
    >
      <label htmlFor="restore-safety" className="flex cursor-pointer items-start gap-2.5 rounded-lg border p-3 text-sm">
        <Checkbox id="restore-safety" checked={safety} onCheckedChange={(v) => setSafety(v === true)} className="mt-0.5" />
        <span className="grid gap-0.5">
          <span className="font-medium">Take a safety backup first (recommended)</span>
          <span className="text-xs text-muted-foreground">
            Lets you undo the restore. Safety backups are kept for the period set in the retention policy.
          </span>
        </span>
      </label>
    </ConfirmDialog>
  );
}

export function DeleteBackupDialog({
  instanceId,
  backup,
  open,
  onOpenChange,
  onStarted,
}: {
  instanceId: string;
  backup: Backup;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job | null) => void;
}) {
  const remove = useMutation({
    mutationFn: () => api.delete<Job | undefined>(`/instances/${instanceId}/backups/${backup.id}`),
    onSuccess: (job) => {
      toast.success(job ? "Deleting backup…" : "Backup entry removed");
      onStarted(job ?? null);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const succeeded = backup.status === "succeeded";
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Delete this backup permanently?"
      destructive
      confirmLabel="Delete backup"
      confirmText="delete"
      description={
        <div className="grid gap-3 text-sm">
          <BackupSummary backup={backup} />
          <p className="text-muted-foreground">
            {succeeded
              ? "The snapshot is removed from the restic repository and data no other backup uses is pruned. This cannot be undone."
              : "This failed backup has no restorable data; only its entry is removed from the list."}
          </p>
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

export function RestoreAsNewDialog({
  instance,
  backup,
  open,
  onOpenChange,
}: {
  instance: Instance;
  backup: Backup;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const navigate = useNavigate();
  const invalidate = useInvalidateInstance();
  // the new server is created on the node whose repository holds the backup (it can be the
  // server's previous node after a move), so its port is checked there
  const targetNode = backup.node_id ?? instance.node;
  const elsewhere = backup.on_current_node === false && backup.node_id != null;
  const ports = usePorts(targetNode, open);
  const [name, setName] = useState(`${instance.name} (restored)`.slice(0, 80));
  const [port, setPort] = useState("");
  const restore = useMutation({
    mutationFn: () =>
      api.post<RestoreAsNewResponse>(`/instances/${instance.id}/backups/${backup.id}/restore-as-new`, {
        name: name.trim(),
        ...(port.trim() ? { game_port: Number(port) } : {}),
      }),
    onSuccess: (res) => {
      toast.success(`Restoring into “${res.instance.name}”`);
      invalidate(instance.id);
      invalidate(res.instance.id);
      onOpenChange(false);
      navigate(`/servers/${res.instance.id}`);
    },
    onError: (err) => {
      if (!(err instanceof ApiError && (err.code === "port_conflict" || err.code === "invalid_port"))) toast.error(errorMessage(err));
    },
  });

  const portNum = port.trim() ? Number(port) : null;
  const used = portNum !== null ? ports.data?.in_use.find((p) => p.port === portNum) : undefined;
  let portError: string | null = null;
  if (portNum !== null) {
    if (!Number.isInteger(portNum) || portNum < 1024 || portNum > 65535) portError = "Use a port between 1024 and 65535";
    else if (used) portError = `Port ${portNum} is already used by ${used.used_by}`;
  }
  if (!portError && restore.error instanceof ApiError && ["port_conflict", "invalid_port"].includes(restore.error.code))
    portError = restore.error.message;
  const nameError = !name.trim() ? "A name is required" : null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (nameError || portError) return;
    restore.mutate();
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !restore.isPending && onOpenChange(o)}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-md">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <CopyPlus className="size-4 text-muted-foreground" aria-hidden /> Restore as a new server
            </DialogTitle>
            <DialogDescription>
              Creates a separate server with the same software and settings, filled with this backup's files. “{instance.name}”
              is not changed. Good for checking an old world or recovering items.
              {elsewhere &&
                ` The backup is stored on node “${backup.node_name}” (this server was moved since), so the new server is created on that node.`}
            </DialogDescription>
          </DialogHeader>
          <BackupSummary backup={backup} />
          <div className="grid gap-1.5">
            <Label htmlFor="restore-new-name">New server name</Label>
            <Input
              id="restore-new-name"
              value={name}
              maxLength={80}
              onChange={(e) => setName(e.target.value)}
              aria-invalid={!!nameError || undefined}
              autoFocus
            />
            {nameError && <p className="text-xs text-destructive">{nameError}</p>}
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="restore-new-port">Game port (optional)</Label>
            <Input
              id="restore-new-port"
              inputMode="numeric"
              value={port}
              onChange={(e) => {
                setPort(e.target.value.replace(/[^\d]/g, ""));
                if (restore.error) restore.reset();
              }}
              placeholder={ports.data?.suggested ? `Automatic (next free: ${ports.data.suggested})` : "Automatic"}
              aria-invalid={!!portError || undefined}
            />
            {portError ? (
              <p className="text-xs text-destructive">{portError}</p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Leave empty to pick the next free port{ports.data ? ` in ${ports.data.range[0]}–${ports.data.range[1]}` : ""}. The
                new server is not started automatically.
              </p>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={restore.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={restore.isPending || !!nameError || !!portError || !!instance.active_job}>
              {restore.isPending ? <Loader2 className="animate-spin" /> : <CopyPlus />} Create & restore
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
