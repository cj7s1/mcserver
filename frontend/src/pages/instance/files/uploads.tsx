/** Multipart uploads with progress, conflict handling and a drag-and-drop zone. */
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, UploadCloud, X, XCircle } from "lucide-react";
import { useRef, useState, type DragEvent, type ReactNode } from "react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { ApiError, errorMessage, uploadWithProgress } from "@/lib/api";
import { BATCH_SIZE, batchLabel, chunk, type BatchProgress } from "@/lib/batches";
import { formatBytes } from "@/lib/format";
import { qk } from "@/lib/queries";
import { cn } from "@/lib/utils";

interface UploadItem {
  id: number;
  dir: string;
  names: string[];
  bytes: number;
  progress: number;
  status: "uploading" | "done" | "error";
  error?: string;
  /** Set while more than 100 files go up in several requests (the panel takes 100 per request). */
  batch?: BatchProgress;
  /** Files written before a later batch failed. */
  uploaded?: number;
  /** Files the server could not store (the others were written): name and reason. */
  failures?: UploadFailure[];
}

/** One entry of the upload answer's `failed` list (201 with `uploaded` and `failed`). */
interface UploadFailure {
  name: string;
  path?: string;
  code?: string;
  message: string;
}

interface UploadAnswer {
  uploaded: { path: string; size: number }[];
  failed?: UploadFailure[];
}

interface Conflict {
  dir: string;
  files: File[];
  /** Names known to exist already; empty when only the server reported the clash. */
  existing: string[];
}

let uploadSeq = 0;

export function useFileUploads(instanceId: string) {
  const qc = useQueryClient();
  const [items, setItems] = useState<UploadItem[]>([]);
  const [conflict, setConflict] = useState<Conflict | null>(null);

  const patch = (id: number, p: Partial<UploadItem>) => setItems((list) => list.map((i) => (i.id === id ? { ...i, ...p } : i)));
  const dismiss = (id: number) => setItems((list) => list.filter((i) => i.id !== id));

  const run = async (dir: string, files: File[], overwrite: boolean) => {
    if (!files.length) return;
    const id = ++uploadSeq;
    const bytes = files.reduce((n, f) => n + f.size, 0);
    const batches = chunk(files, BATCH_SIZE);
    setItems((list) => [...list, { id, dir, names: files.map((f) => f.name), bytes, progress: 0, status: "uploading" }]);
    let done = 0;
    let doneBytes = 0;
    let written = 0;
    const failures: UploadFailure[] = [];
    try {
      for (let b = 0; b < batches.length; b++) {
        const part = batches[b];
        const partBytes = part.reduce((n, f) => n + f.size, 0);
        const batch: BatchProgress = { batch: b + 1, batches: batches.length, done, total: files.length };
        patch(id, { batch: batches.length > 1 ? batch : undefined });
        const form = new FormData();
        for (const f of part) form.append("file", f, f.name);
        try {
          const res = await uploadWithProgress<UploadAnswer>(
            `/instances/${instanceId}/files/upload`,
            form,
            { path: dir, overwrite },
            (fraction) => patch(id, { progress: bytes ? (doneBytes + fraction * partBytes) / bytes : fraction }),
          );
          written += res?.uploaded?.length ?? part.length;
          failures.push(...(res?.failed ?? []));
        } catch (err) {
          if (err instanceof ApiError && err.status === 409 && err.code === "exists" && !overwrite && !failures.length) {
            // Ask about the files not written yet (this batch and the later ones).
            dismiss(id);
            if (written) toast.success(`Uploaded ${written} of ${files.length} files`);
            setConflict({ dir, files: files.slice(done), existing: [] });
            return;
          }
          const prefix = batches.length > 1 ? `Batch ${b + 1} of ${batches.length} failed` : "";
          const rest =
            batches.length > 1 ? `; ${written} of ${files.length} files were uploaded, the other ${files.length - written} were not` : "";
          patch(id, {
            status: "error",
            error: prefix ? `${prefix}: ${errorMessage(err)}${rest}` : errorMessage(err),
            uploaded: written,
            failures: failures.length ? failures : undefined,
          });
          return;
        }
        done += part.length;
        doneBytes += partBytes;
      }
      // Files that exist already are offered for overwriting; every other failure is listed.
      const clashes = overwrite ? [] : failures.filter((f) => f.code === "exists");
      const other = failures.filter((f) => !clashes.includes(f));
      if (other.length) {
        patch(id, {
          status: "error",
          progress: 1,
          batch: undefined,
          uploaded: written,
          failures: other,
          error: `${written} of ${files.length} files uploaded; ${other.length} could not be stored:`,
        });
      } else {
        patch(id, { status: "done", progress: 1, batch: undefined });
        setTimeout(() => dismiss(id), 4000);
      }
      if (written === files.length) toast.success(files.length === 1 ? `Uploaded ${files[0].name}` : `Uploaded ${files.length} files`);
      else if (written) toast.success(`Uploaded ${written} of ${files.length} files`);
      if (clashes.length) {
        const names = new Set(clashes.map((f) => f.name));
        setConflict({ dir, files: files.filter((f) => names.has(f.name)), existing: [...names] });
      }
    } finally {
      qc.invalidateQueries({ queryKey: qk.files(instanceId, dir) });
    }
  };

  /** Start an upload; asks first when names clash with entries already listed in `dir`. */
  const start = (dir: string, files: File[], existingNames: Set<string>) => {
    if (!files.length) return;
    const clashes = files.filter((f) => existingNames.has(f.name)).map((f) => f.name);
    if (clashes.length) setConflict({ dir, files, existing: clashes });
    else void run(dir, files, false);
  };

  const resolve = (mode: "overwrite" | "skip" | "cancel") => {
    const c = conflict;
    setConflict(null);
    if (!c || mode === "cancel") return;
    if (mode === "overwrite") void run(c.dir, c.files, true);
    else void run(c.dir, c.files.filter((f) => !c.existing.includes(f.name)), false);
  };

  return { items, dismiss, start, conflict, resolve };
}

export function UploadList({ items, onDismiss }: { items: UploadItem[]; onDismiss: (id: number) => void }) {
  if (!items.length) return null;
  return (
    <ul className="grid min-w-0 grid-cols-1 gap-2" aria-live="polite" aria-label="Uploads">
      {items.map((u) => (
        <li key={u.id} className="min-w-0 rounded-lg border bg-muted/30 px-3 py-2">
          <div className="flex items-center gap-2 text-sm">
            {u.status === "done" ? (
              <CheckCircle2 className="size-4 shrink-0 text-emerald-500" aria-hidden />
            ) : u.status === "error" ? (
              <XCircle className="size-4 shrink-0 text-destructive" aria-hidden />
            ) : (
              <Loader2 className="size-4 shrink-0 animate-spin text-primary" aria-hidden />
            )}
            <span className="min-w-0 truncate" title={u.names.join(", ")}>
              {u.names.length === 1 ? u.names[0] : `${u.names.length} files`}{" "}
              <span className="text-muted-foreground">→ /{u.dir}</span>
            </span>
            <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">
              {u.status === "uploading" ? `${Math.round(u.progress * 100)}% of ${formatBytes(u.bytes)}` : formatBytes(u.bytes)}
            </span>
            {u.status !== "uploading" && (
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={`Dismiss ${u.names.length === 1 ? u.names[0] : `${u.names.length} files`}`}
                onClick={() => onDismiss(u.id)}
              >
                <X />
              </Button>
            )}
          </div>
          {u.status === "uploading" && (
            <Progress value={u.progress * 100} className="mt-2 h-1.5" aria-label={`Upload progress for ${u.names.join(", ")}`} />
          )}
          {u.status === "uploading" && u.batch && (
            <p className="mt-1 text-xs text-muted-foreground">Sending {batchLabel(u.batch)}</p>
          )}
          {u.status === "uploading" && u.progress >= 1 && (
            <p className="mt-1 text-xs text-muted-foreground">Transferred — writing files on the server…</p>
          )}
          {u.error && <p className="mt-1 text-xs text-destructive">{u.error}</p>}
          {u.failures && u.failures.length > 0 && (
            <ul className="mt-1 max-h-32 overflow-auto text-xs text-destructive" aria-label="Files that were not uploaded">
              {u.failures.map((f) => (
                <li key={f.path ?? f.name} className="truncate" title={`${f.name}: ${f.message}`}>
                  <span className="font-mono">{f.name}</span>: {f.message}
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ul>
  );
}

export function UploadConflictDialog({
  conflict,
  onResolve,
}: {
  conflict: Conflict | null;
  onResolve: (mode: "overwrite" | "skip" | "cancel") => void;
}) {
  const allClash = !!conflict && conflict.existing.length > 0 && conflict.existing.length === conflict.files.length;
  return (
    <AlertDialog open={!!conflict} onOpenChange={(o) => !o && onResolve("cancel")}>
      <AlertDialogContent>
        <AlertDialogHeader className="place-items-start text-left">
          <AlertDialogTitle>Replace existing files?</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="grid gap-2">
              {conflict?.existing.length ? (
                <>
                  <p>
                    {conflict.existing.length === 1 ? "This file already exists" : `${conflict.existing.length} files already exist`} in{" "}
                    <span className="font-mono">/{conflict.dir}</span>:
                  </p>
                  <ul className="max-h-32 overflow-auto rounded border bg-muted/40 px-2 py-1 font-mono text-xs">
                    {conflict.existing.map((n) => (
                      <li key={n} className="truncate">
                        {n}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p>
                  A file with the same name already exists in <span className="font-mono">/{conflict?.dir}</span>. Overwriting
                  replaces it permanently.
                </p>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          {!!conflict?.existing.length && !allClash && (
            <Button variant="outline" onClick={() => onResolve("skip")}>
              Skip existing
            </Button>
          )}
          <Button variant="destructive" onClick={() => onResolve("overwrite")}>
            Overwrite
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Wraps the listing; shows an overlay while files are dragged over it. */
export function DropZone({
  enabled,
  target,
  onFiles,
  children,
  className,
}: {
  enabled: boolean;
  target: string;
  onFiles: (files: File[]) => void;
  children: ReactNode;
  className?: string;
}) {
  const [over, setOver] = useState(false);
  const depth = useRef(0);
  const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer.types).includes("Files");

  if (!enabled) return <div className={className}>{children}</div>;
  return (
    <div
      className={cn("relative", className)}
      onDragEnter={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current += 1;
        setOver(true);
      }}
      onDragOver={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(e) => {
        if (!hasFiles(e)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setOver(false);
      }}
      onDrop={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current = 0;
        setOver(false);
        const items = Array.from(e.dataTransfer.items ?? []);
        const files: File[] = [];
        let skippedDirs = 0;
        if (items.length) {
          for (const item of items) {
            if (item.kind !== "file") continue;
            const entry = item.webkitGetAsEntry?.();
            if (entry?.isDirectory) {
              skippedDirs++;
              continue;
            }
            const f = item.getAsFile();
            if (f) files.push(f);
          }
        } else {
          files.push(...Array.from(e.dataTransfer.files));
        }
        if (skippedDirs) toast.error("Folders cannot be uploaded directly. Upload a .zip and extract it, or upload the files inside.");
        if (files.length) onFiles(files);
      }}
    >
      {children}
      {over && (
        <div
          className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-primary bg-background/85 text-sm"
          aria-hidden
        >
          <UploadCloud className="size-8 text-primary" />
          <p className="font-medium">Drop to upload</p>
          <p className="font-mono text-xs text-muted-foreground">to /{target}</p>
        </div>
      )}
    </div>
  );
}
