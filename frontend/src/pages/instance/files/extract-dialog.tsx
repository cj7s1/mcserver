/** Extract an uploaded archive inside the server folder (POST /instances/{id}/files/extract -> job). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Loader2, PackageOpen } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { JobProgress } from "@/components/JobProgress";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, errorMessage } from "@/lib/api";
import { useInvalidateInstance, useJob } from "@/lib/queries";
import type { FileEntry, Instance, Job } from "@/lib/types";
import { cn } from "@/lib/utils";
import { normalizePath } from "./file-utils";

const LIVE = ["running", "starting", "unhealthy", "restarting", "stopping"];

export function ExtractDialog({ instance, entry, dir, onClose }: { instance: Instance; entry: FileEntry; dir: string; onClose: () => void }) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const [destination, setDestination] = useState(dir);
  const [advanced, setAdvanced] = useState(false);
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
    onError: (err) => toast.error(errorMessage(err)),
  });

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
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <PackageOpen className="size-4 text-muted-foreground" aria-hidden /> Extract archive
          </DialogTitle>
          <DialogDescription>
            Unpack <span className="font-mono">{entry.name}</span> on the server. Links, special files and paths that would escape
            the server folder are skipped.
          </DialogDescription>
        </DialogHeader>
        {jobId ? (
          <JobProgress jobId={jobId} showLog={finished} />
        ) : (
          <div className="grid gap-4">
            {running && (
              <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
                Stop the server first — archives can only be extracted while it is stopped.
              </p>
            )}
            <div className="grid gap-2">
              <Label htmlFor="extract-destination">Destination folder</Label>
              <Input
                id="extract-destination"
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                placeholder="(server root)"
                className="font-mono"
                autoComplete="off"
                spellCheck={false}
              />
              <p className="text-xs text-muted-foreground">Relative to the server folder. Leave empty for the root.</p>
            </div>
            <div className="flex items-center justify-between gap-4">
              <Label htmlFor="extract-overwrite" className="grid gap-1 font-normal">
                <span className="font-medium">Overwrite existing files</span>
                <span className="text-xs text-muted-foreground">Off keeps files that already exist.</span>
              </Label>
              <Switch id="extract-overwrite" checked={overwrite} onCheckedChange={setOverwrite} />
            </div>
            <button
              type="button"
              className="flex items-center gap-1 justify-self-start rounded text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
              aria-expanded={advanced}
              aria-controls="extract-advanced"
              onClick={() => setAdvanced((v) => !v)}
            >
              <ChevronDown className={cn("size-3.5 transition-transform", advanced && "rotate-180")} aria-hidden /> Advanced
            </button>
            {advanced && (
              <div id="extract-advanced" className="grid gap-4 rounded-lg border bg-muted/30 p-3">
                <div className="flex items-center justify-between gap-4">
                  <Label htmlFor="extract-strip" className="grid gap-1 font-normal">
                    <span className="font-medium">Remove a single top-level folder</span>
                    <span className="text-xs text-muted-foreground">
                      When everything in the archive sits inside one folder, extract its contents directly.
                    </span>
                  </Label>
                  <Switch id="extract-strip" checked={strip} onCheckedChange={setStrip} />
                </div>
                <div className="flex items-center justify-between gap-4">
                  <Label htmlFor="extract-delete" className="grid gap-1 font-normal">
                    <span className="font-medium">Delete the archive afterwards</span>
                    <span className="text-xs text-muted-foreground">Only after a successful extraction.</span>
                  </Label>
                  <Switch id="extract-delete" checked={deleteArchive} onCheckedChange={setDeleteArchive} />
                </div>
              </div>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {jobId ? "Close" : "Cancel"}
          </Button>
          {!jobId && (
            <Button onClick={() => extract.mutate()} disabled={running || extract.isPending}>
              {extract.isPending && <Loader2 className="animate-spin" />} Extract
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
