/** Create, verify and edit dialogs for the Backups page. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Archive, Loader2, Pencil, ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Backup, Instance, Job } from "@/lib/types";
import type { BackupsResponse } from "./backup-utils";

type Consistency = "auto" | "require" | "none";

const CONSISTENCY: { value: Consistency; label: string; description: string }[] = [
  {
    value: "auto",
    label: "Automatic (recommended)",
    description:
      "If the server is running, world saving is paused (save-off), the world is flushed to disk (save-all flush), the files are backed up and saving is turned back on. If pausing fails, the backup continues and is marked crash-consistent.",
  },
  {
    value: "require",
    label: "Require a consistent backup",
    description: "Like automatic, but the backup fails instead of continuing if world saving cannot be paused (for example, RCON is unreachable).",
  },
  {
    value: "none",
    label: "Don't pause saving",
    description: "Copy the files as they are, without touching the running server. Fastest, but the result is only crash-consistent while the server is running.",
  },
];

const RUNNING = ["running", "starting", "unhealthy"];

export function CreateBackupDialog({
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
  const [name, setName] = useState("");
  const [consistency, setConsistency] = useState<Consistency>("auto");
  const create = useMutation({
    mutationFn: () =>
      api.post<Job>(`/instances/${instance.id}/backups`, { name: name.trim() || undefined, consistency }),
    onSuccess: (job) => {
      toast.success("Backup started");
      onStarted(job);
      onOpenChange(false);
      setName("");
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const running = RUNNING.includes(instance.status);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !create.isPending && onOpenChange(o)}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Archive className="size-4 text-muted-foreground" aria-hidden /> Create backup
            </DialogTitle>
            <DialogDescription>
              Backs up the whole server folder (world, configs, mods/plugins). Only changed data is stored thanks to
              deduplication. The server is currently <strong>{running ? "running" : "not running"}</strong>.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-1.5">
            <Label htmlFor="backup-name">Name (optional)</Label>
            <Input
              id="backup-name"
              value={name}
              maxLength={120}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Before installing new mods"
              autoFocus
            />
            <p className="text-xs text-muted-foreground">Leave empty to use the date and time.</p>
          </div>
          <fieldset className="grid gap-2">
            <legend className="mb-2 text-sm font-medium">World consistency</legend>
            <RadioGroup value={consistency} onValueChange={(v) => setConsistency(v as Consistency)} className="gap-2">
              {CONSISTENCY.map((c) => (
                <label
                  key={c.value}
                  htmlFor={`consistency-${c.value}`}
                  className="flex cursor-pointer gap-3 rounded-lg border p-3 has-data-checked:border-primary/50 has-data-checked:bg-primary/5"
                >
                  <RadioGroupItem id={`consistency-${c.value}`} value={c.value} className="mt-0.5" />
                  <span className="grid gap-0.5">
                    <span className="text-sm font-medium">{c.label}</span>
                    <span className="text-xs text-muted-foreground">{c.description}</span>
                  </span>
                </label>
              ))}
            </RadioGroup>
            {!running && (
              <p className="text-xs text-muted-foreground">The server is stopped, so every option produces a consistent backup.</p>
            )}
          </fieldset>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || !!instance.active_job}>
              {create.isPending ? <Loader2 className="animate-spin" /> : <Archive />} Start backup
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const SUBSETS = ["1%", "5%", "10%", "25%", "50%", "100%"];

export function VerifyDialog({
  instance,
  backupCount,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  backupCount: number;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const [subset, setSubset] = useState("10%");
  const verify = useMutation({
    mutationFn: () => api.post<Job>(`/instances/${instance.id}/backups/verify`, { read_data_subset: subset }),
    onSuccess: (job) => {
      toast.success("Integrity check started");
      onStarted(job);
      onOpenChange(false);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  return (
    <Dialog open={open} onOpenChange={(o) => !verify.isPending && onOpenChange(o)}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-muted-foreground" aria-hidden /> Verify backup integrity
          </DialogTitle>
          <DialogDescription>
            Runs <code className="font-mono">restic check</code> on this server's backup repository: it validates the repository
            structure and re-reads a sample of the stored data to detect corruption or disk errors. The result is recorded on all{" "}
            {backupCount} completed backup{backupCount === 1 ? "" : "s"}, because they share one deduplicated repository.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-1.5">
          <Label htmlFor="verify-subset">Data to re-read</Label>
          <Select value={subset} onValueChange={setSubset}>
            <SelectTrigger id="verify-subset" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SUBSETS.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                  {s === "10%" ? " (recommended)" : s === "100%" ? " (slowest, reads everything)" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">
            Higher values find more problems but take longer and read more from disk. The server keeps running.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={verify.isPending}>
            Cancel
          </Button>
          <Button onClick={() => verify.mutate()} disabled={verify.isPending || !!instance.active_job}>
            {verify.isPending ? <Loader2 className="animate-spin" /> : <ShieldCheck />} Start check
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function EditBackupDialog({
  instanceId,
  backup,
  open,
  onOpenChange,
}: {
  instanceId: string;
  backup: Backup;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(backup.name);
  const [notes, setNotes] = useState(backup.notes);
  const save = useMutation({
    mutationFn: () => api.patch<Backup>(`/instances/${instanceId}/backups/${backup.id}`, { name: name.trim(), notes }),
    onSuccess: (b) => {
      qc.setQueryData<BackupsResponse>(qk.backups(instanceId), (old) =>
        old ? { ...old, backups: old.backups.map((x) => (x.id === b.id ? b : x)) } : old,
      );
      toast.success("Backup details saved");
      onOpenChange(false);
    },
    onError: (err) => {
      if (!(err instanceof ApiError && err.code === "validation_error")) toast.error(errorMessage(err));
    },
  });
  const fe = save.error instanceof ApiError ? save.error.fieldErrors : {};
  const nameError = !name.trim() ? "A name is required" : fe.name;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    save.mutate();
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !save.isPending && onOpenChange(o)}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-md">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Pencil className="size-4 text-muted-foreground" aria-hidden /> Edit backup
            </DialogTitle>
            <DialogDescription>Rename the backup or keep notes about what it contains.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-1.5">
            <Label htmlFor="edit-backup-name">Name</Label>
            <Input
              id="edit-backup-name"
              value={name}
              maxLength={120}
              onChange={(e) => setName(e.target.value)}
              aria-invalid={!!nameError || undefined}
            />
            {nameError && <p className="text-xs text-destructive">{nameError}</p>}
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="edit-backup-notes">Notes</Label>
            <Textarea
              id="edit-backup-notes"
              value={notes}
              maxLength={2000}
              rows={4}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="e.g. Last state before the 1.21 update"
              aria-invalid={!!fe.notes || undefined}
            />
            {fe.notes ? (
              <p className="text-xs text-destructive">{fe.notes}</p>
            ) : (
              <p className="text-right text-xs text-muted-foreground tabular-nums">{notes.length}/2000</p>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || !name.trim()}>
              {save.isPending && <Loader2 className="animate-spin" />} Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
