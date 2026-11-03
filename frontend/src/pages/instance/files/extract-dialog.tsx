/** Extract an uploaded archive inside the server folder (POST /instances/{id}/files/extract -> job). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, PackageOpen } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { JobProgress } from "@/components/JobProgress";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ApiError, api, errorMessage } from "@/lib/api";
import { useInvalidateInstance, useJob } from "@/lib/queries";
import type { FileEntry, Instance, Job } from "@/lib/types";
import { normalizePath } from "./file-utils";

const LIVE = ["running", "starting", "unhealthy", "restarting", "stopping"];

function OptionSwitch({
  id,
  title,
  help,
  checked,
  onChange,
}: {
  id: string;
  title: string;
  help: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <Label htmlFor={id} className="grid gap-1 font-normal">
        <span className="font-medium">{title}</span>
        <span className="text-xs text-muted-foreground">{help}</span>
      </Label>
      <Switch id={id} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

export function ExtractDialog({ instance, entry, dir, onClose }: { instance: Instance; entry: FileEntry; dir: string; onClose: () => void }) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const [destination, setDestination] = useState(dir);
  const [strip, setStrip] = useState(true);
  const [overwrite, setOverwrite] = useState(false);
  const [deleteArchive, setDeleteArchive] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);
  const running = LIVE.includes(instance.status);

  const extract = useMutation({
    mutationFn: () =>
      api.post<Job>(`/instances/${instance.id}/files/extract`, {
        path: entry.path,
        destination: normalizePath(destination),
        strip: strip ? "auto" : "none",
        overwrite,
        delete_archive: deleteArchive,
      }),
    onSuccess: (j) => {
      setJobId(j.id);
      invalidate(instance.id);
    },
    onError: (err) => {
      // must_stop and other conflicts are shown inline; anything else also gets a toast
      if (!(err instanceof ApiError && err.status === 409)) toast.error(errorMessage(err));
    },
  });

  const refreshFiles = () => qc.invalidateQueries({ queryKey: filesPrefix(instance.id) });
  const status = job.data?.status;
  useEffect(() => {
    if (status === "succeeded") {
      toast.success(`Extracted ${entry.name}`);
      qc.invalidateQueries({ queryKey: filesPrefix(instance.id) });
    } else if (status === "failed") {
      qc.invalidateQueries({ queryKey: filesPrefix(instance.id) });
    }
  }, [status, entry.name, instance.id, qc]);

  const finished = status === "succeeded" || status === "failed";
  const close = () => {
    // the job keeps running after the dialog closes; refresh so the listing catches up
    if (jobId) refreshFiles();
    onClose();
  };
  const conflict = extract.error instanceof ApiError && extract.error.status === 409 ? extract.error : null;
  const target = normalizePath(destination);

  return (
    <Dialog open onOpenChange={(o) => !o && close()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <PackageOpen className="size-4 text-muted-foreground" aria-hidden /> Extract archive
          </DialogTitle>
          <DialogDescription>
            Unpack <span className="font-mono break-all">{entry.name}</span> on the server. Links, special files and paths that
            would escape the server folder are skipped.
          </DialogDescription>
        </DialogHeader>
        {jobId ? (
          <div className="grid gap-2">
            <JobProgress jobId={jobId} showLog={finished} />
            {!finished && (
              <p className="text-xs text-muted-foreground">You can close this dialog; extraction continues in the background.</p>
            )}
          </div>
        ) : (
          <div className="grid gap-4">
            {running && (
              <p
                className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
                role="note"
              >
                <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
                Stop the server first — archives can only be extracted while it is stopped.
              </p>
            )}
            {conflict && (
              <p className="flex gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive" role="alert">
                <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
                {conflict.display}
              </p>
            )}
            <div className="grid gap-2">
              <Label htmlFor="extract-destination">Extract into</Label>
              <Input
                id="extract-destination"
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                placeholder="(server root)"
                className="font-mono"
                autoComplete="off"
                spellCheck={false}
              />
              <p className="text-xs text-muted-foreground">
                Folder relative to the server folder (defaults to the current folder):{" "}
                <span className="font-mono break-all">/{target}</span>
              </p>
            </div>
            <OptionSwitch
              id="extract-strip"
              title="Remove a single top-level folder"
              help="When everything in the archive sits inside one folder, extract its contents directly (auto). Off keeps the archive's layout."
              checked={strip}
              onChange={setStrip}
            />
            <OptionSwitch
              id="extract-overwrite"
              title="Overwrite existing files"
              help="Off keeps files that already exist and skips those entries."
              checked={overwrite}
              onChange={setOverwrite}
            />
            <OptionSwitch
              id="extract-delete"
              title="Delete the archive afterwards"
              help="Only after a successful extraction."
              checked={deleteArchive}
              onChange={setDeleteArchive}
            />
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            {jobId ? "Close" : "Cancel"}
          </Button>
          {!jobId && (
            <Button onClick={() => extract.mutate()} disabled={running || extract.isPending}>
              {extract.isPending ? <Loader2 className="animate-spin" /> : <PackageOpen />} Extract
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function filesPrefix(instanceId: string) {
  return ["instances", instanceId, "files"] as const;
}
