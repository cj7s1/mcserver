/** "Replace world…": choose an archive (upload or a file already in the server folder), then confirm. */
import { FileArchive, Globe, UploadCloud } from "lucide-react";
import { useId, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { api, errorMessage, uploadWithProgress } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import type { Instance, Job, WorldInfo } from "@/lib/types";
import { ARCHIVE_ACCEPT, isArchiveName, replacedFolders } from "./world-utils";

const RUNNING = ["running", "starting", "unhealthy", "restarting", "installing"];

type Source = "upload" | "server";

export function ReplaceWorldDialog({
  instance,
  world,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  world: WorldInfo;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const ids = { file: useId(), path: useId(), safety: useId(), del: useId() };
  const [step, setStep] = useState<"pick" | "confirm">("pick");
  const [source, setSource] = useState<Source>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [path, setPath] = useState("");
  const [deleteAfter, setDeleteAfter] = useState(false);
  const [safety, setSafety] = useState(world.pre_restore_backup_default);
  const [progress, setProgress] = useState<number | null>(null);
  const running = RUNNING.includes(instance.status);
  const suffixes = world.archive_suffixes?.length ? world.archive_suffixes : undefined;

  const limit = world.max_upload_bytes;
  const fileError = !file
    ? null
    : !isArchiveName(file.name, suffixes)
      ? "Choose a .zip, .tar, .tar.gz or .tar.xz archive."
      : limit && file.size > limit
        ? `This file is larger than the panel's upload limit (${formatBytes(limit)}). Copy it into the server folder another way (for example through the Unraid share) and choose “From the server folder”.`
        : null;
  const pathValue = path.trim().replace(/^\/+/, "");
  const pathError = pathValue && !isArchiveName(pathValue, suffixes) ? "The path must end in .zip, .tar, .tar.gz or .tar.xz." : null;
  const ready = source === "upload" ? !!file && !fileError : !!pathValue && !pathError;
  const replaced = replacedFolders(world);

  const close = () => {
    if (progress !== null) return; // uploading: keep the dialog until the request finishes
    onOpenChange(false);
  };

  const submit = async () => {
    try {
      let job: Job;
      if (source === "upload" && file) {
        const form = new FormData();
        form.append("file", file, file.name);
        form.append("confirm", "true");
        form.append("stop_if_running", String(running));
        form.append("pre_restore_backup", String(safety));
        setProgress(0);
        job = await uploadWithProgress<Job>(`/instances/${instance.id}/world/replace`, form, undefined, setProgress);
      } else {
        job = await api.post<Job>(`/instances/${instance.id}/world/replace`, {
          archive: pathValue,
          confirm: true,
          stop_if_running: running,
          pre_restore_backup: safety,
          delete_archive: deleteAfter,
        });
      }
      setProgress(null);
      toast.success("Replacing the world…");
      onStarted(job);
      onOpenChange(false);
    } catch (err) {
      setProgress(null);
      toast.error(errorMessage(err));
      throw err; // keeps the confirmation open
    }
  };

  const pick = (e: FormEvent) => {
    e.preventDefault();
    if (ready) setStep("confirm");
  };

  if (step === "confirm") {
    const label = source === "upload" && file ? `${file.name} (${formatBytes(file.size)})` : pathValue;
    return (
      <ConfirmDialog
        open={open}
        onOpenChange={(o) => !o && close()}
        title="Replace the world?"
        destructive
        confirmLabel={progress === null ? "Replace world" : progress < 1 ? `Uploading ${Math.round(progress * 100)}%…` : "Processing…"}
        // without a safety backup the current world is gone for good, so ask for the server name
        confirmText={safety ? undefined : instance.name}
        description={
          <div className="grid gap-3 text-sm">
            <p className="flex min-w-0 items-center gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-xs">
              <FileArchive className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
              <span className="min-w-0 font-medium break-all text-foreground">{label}</span>
            </p>
            <ol className="grid list-decimal gap-1.5 pl-5 text-muted-foreground">
              <li>{running ? "The server is stopped (players are disconnected)." : "The server stays stopped meanwhile."}</li>
              <li>
                {safety
                  ? "A safety backup of the server folder is taken first (“Before restore”)."
                  : "No safety backup is taken: the current world cannot be recovered afterwards."}
              </li>
              <li>
                {source === "upload" ? "The archive is uploaded and extracted" : "The archive is extracted"}; the world in it
                is found by its <code className="font-mono">level.dat</code> (other files in the archive are ignored).
              </li>
              <li>
                The current world{" "}
                {replaced.length ? (
                  <>
                    (<span className="font-mono text-foreground">{replaced.join(", ")}</span>)
                  </>
                ) : null}{" "}
                is <strong className="text-foreground">replaced</strong> by it as{" "}
                <span className="font-mono text-foreground">{world.level_name}</span>. The old folders are deleted only once the
                new world is in place, and put back if anything fails.
              </li>
              <li>{running ? "The server is started again." : "The server is left stopped."}</li>
            </ol>
            {world.layout === "bukkit" && (
              <p className="text-xs text-muted-foreground">
                Paper-style server: the nether and the end are taken from <code className="font-mono">*_nether</code> /{" "}
                <code className="font-mono">*_the_end</code> folders in the archive. A singleplayer world brings them inside
                its folder; Paper moves them out on the next start.
              </p>
            )}
          </div>
        }
        onConfirm={submit}
      >
        {progress !== null && (
          <div className="grid gap-1.5" aria-live="polite">
            <Progress value={progress * 100} className="h-1.5" aria-label="Upload progress" />
            <p className="text-xs text-muted-foreground">
              {progress < 1
                ? `Uploading ${formatBytes((file?.size ?? 0) * progress)} of ${formatBytes(file?.size ?? 0)}. Keep this page open.`
                : "Upload complete; handing the archive to the node…"}
            </p>
          </div>
        )}
      </ConfirmDialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && close()}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <form onSubmit={pick} className="grid gap-4">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Globe className="size-4 text-muted-foreground" aria-hidden /> Replace world
            </DialogTitle>
            <DialogDescription>
              Use a singleplayer save (a folder from <code className="font-mono">.minecraft/saves</code>), a world from
              another server or a whole server archive, packed as .zip, .tar.gz or .tar.xz. Java Edition only.
            </DialogDescription>
          </DialogHeader>

          <ToggleGroup
            type="single"
            variant="outline"
            value={source}
            onValueChange={(v) => v && setSource(v as Source)}
            aria-label="Where the world archive is"
            className="w-full justify-start"
          >
            <ToggleGroupItem value="upload" className="flex-1">
              Upload a file
            </ToggleGroupItem>
            <ToggleGroupItem value="server" className="flex-1">
              From the server folder
            </ToggleGroupItem>
          </ToggleGroup>

          {source === "upload" ? (
            <div className="grid gap-1.5">
              <Label htmlFor={ids.file}>World archive</Label>
              <Input
                id={ids.file}
                type="file"
                accept={ARCHIVE_ACCEPT}
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                aria-invalid={!!fileError}
                aria-describedby={`${ids.file}-hint`}
              />
              <p id={`${ids.file}-hint`} className={fileError ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                {fileError ??
                  (file
                    ? `${file.name} · ${formatBytes(file.size)}`
                    : `Up to ${limit ? formatBytes(limit) : "the panel's upload limit"}. Larger worlds: copy the archive into the server folder and choose “From the server folder”.`)}
              </p>
            </div>
          ) : (
            <div className="grid gap-2">
              <div className="grid gap-1.5">
                <Label htmlFor={ids.path}>Path in the server folder</Label>
                <Input
                  id={ids.path}
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  placeholder="e.g. uploads/my-world.zip"
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={!!pathError}
                  aria-describedby={`${ids.path}-hint`}
                />
                <p id={`${ids.path}-hint`} className={pathError ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                  {pathError ?? "Relative to the server folder. Upload it on the Files page or copy it there (for example via the Unraid share)."}
                </p>
              </div>
              <label htmlFor={ids.del} className="flex cursor-pointer items-center gap-2 text-sm">
                <Checkbox id={ids.del} checked={deleteAfter} onCheckedChange={(v) => setDeleteAfter(v === true)} />
                Delete the archive after the world was replaced
              </label>
            </div>
          )}

          <label htmlFor={ids.safety} className="flex cursor-pointer items-start gap-2.5 rounded-lg border p-3 text-sm">
            <Checkbox id={ids.safety} checked={safety} onCheckedChange={(v) => setSafety(v === true)} className="mt-0.5" />
            <span className="grid gap-0.5">
              <span className="font-medium">Take a safety backup first (recommended)</span>
              <span className="text-xs text-muted-foreground">
                Lets you go back to the current world by restoring the “Before restore” backup.
              </span>
            </span>
          </label>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" disabled={!ready}>
              <UploadCloud /> Continue
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
