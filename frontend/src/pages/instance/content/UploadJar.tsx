/** Upload .jar files into the server's mods/ or plugins/ folder (one request per file). */
import { CheckCircle2, FileUp, Loader2, ShieldAlert, X, XCircle } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { ConfirmAction } from "./ConfirmAction";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { ApiError, errorMessage, uploadWithProgress } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";
import { MAX_UPLOAD_BYTES, contentPath, useInvalidateContent, type UploadResponse } from "./content-api";
import { reportFromUpload } from "./content-report";
import type { ContentEnv } from "./content-ui";

interface UploadItem {
  id: number;
  file: File;
  status: "queued" | "uploading" | "done" | "conflict" | "error";
  progress: number;
  error?: string;
  result?: UploadResponse;
}

let seq = 0;

function validate(file: File): string | null {
  if (!file.name.toLowerCase().endsWith(".jar")) return "Only .jar files can be uploaded here";
  if (file.size === 0) return "The file is empty";
  if (file.size > MAX_UPLOAD_BYTES) return `Jar files may be at most ${formatBytes(MAX_UPLOAD_BYTES, 0)}`;
  return null;
}

export function UploadJar({ env }: { env: ContentEnv }) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const invalidate = useInvalidateContent(env.instance.id);
  const disabled = !!env.locked;

  const patch = (id: number, p: Partial<UploadItem>) => setItems((list) => list.map((i) => (i.id === id ? { ...i, ...p } : i)));

  const uploadOne = async (item: UploadItem, overwrite: boolean) => {
    patch(item.id, { status: "uploading", progress: 0, error: undefined });
    const form = new FormData();
    form.append("file", item.file, item.file.name);
    try {
      const res = await uploadWithProgress<UploadResponse>(
        contentPath(env.instance.id, "upload"),
        form,
        overwrite ? { overwrite: "true" } : undefined,
        (f) => patch(item.id, { progress: f }),
      );
      patch(item.id, { status: "done", progress: 1, result: res });
      const report = reportFromUpload(res);
      env.notify(report.title, res.restart_required);
      if (res.warnings.length) env.showReport(report);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.code === "file_exists" && !overwrite) {
        patch(item.id, { status: "conflict", error: errorMessage(err) });
      } else {
        patch(item.id, { status: "error", error: errorMessage(err) });
      }
    } finally {
      void invalidate();
    }
  };

  const addFiles = async (files: File[]) => {
    if (!files.length || disabled) return;
    const fresh: UploadItem[] = files.map((file) => {
      const problem = validate(file);
      return { id: ++seq, file, status: problem ? "error" : "queued", progress: 0, error: problem ?? undefined };
    });
    setItems((list) => [...fresh, ...list]);
    // sequential: the server inspects and stores each jar before answering
    for (const item of fresh) {
      if (item.status === "queued") await uploadOne(item, false);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    void addFiles(Array.from(e.dataTransfer.files));
  };

  return (
    <div className="grid grid-cols-1 gap-4">
      <div className="flex gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
        <div>
          <p className="font-medium">Only upload {env.noun.many} from sources you trust</p>
          <p className="mt-1 text-muted-foreground">
            {env.noun.Many} are untrusted third-party code. They run with the full privileges of the server process inside its
            container: they can read and change every file of this server (worlds, configs, other {env.noun.many}) and make network
            connections. Files from Modrinth are at least hash-verified; uploaded jars are not checked for malware.
          </p>
        </div>
      </div>

      <div
        onDragOver={(e) => {
          if (disabled) return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center gap-3 rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors",
          dragging ? "border-primary bg-primary/5" : "border-border",
          disabled && "opacity-60",
        )}
      >
        <div className="rounded-full bg-muted p-3 text-muted-foreground">
          <FileUp className="size-5" aria-hidden />
        </div>
        <div>
          <p className="font-medium">Drop .jar files here</p>
          <p className="mt-1 text-sm text-muted-foreground">
            Saved to <span className="font-mono">/{env.directory}</span> · up to {formatBytes(MAX_UPLOAD_BYTES, 0)} each · the panel
            checks that each jar targets {env.platform}
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={() => input.current?.click()} disabled={disabled}>
          Choose files…
        </Button>
        <input
          ref={input}
          type="file"
          accept=".jar,application/java-archive"
          multiple
          className="sr-only"
          tabIndex={-1}
          aria-hidden
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            void addFiles(files);
          }}
        />
        {disabled && <p className="text-xs text-muted-foreground">{env.locked}</p>}
      </div>

      {items.length > 0 && (
        <ul className="grid gap-2" aria-live="polite" aria-label="Uploads">
          {items.map((u) => (
            <li key={u.id} className="rounded-lg border bg-muted/30 px-3 py-2">
              <div className="flex items-center gap-2 text-sm">
                {u.status === "done" ? (
                  <CheckCircle2 className="size-4 shrink-0 text-emerald-500" aria-hidden />
                ) : u.status === "error" || u.status === "conflict" ? (
                  <XCircle className={cn("size-4 shrink-0", u.status === "error" ? "text-destructive" : "text-amber-500")} aria-hidden />
                ) : (
                  <Loader2 className="size-4 shrink-0 animate-spin text-primary" aria-hidden />
                )}
                <span className="min-w-0 truncate font-mono text-xs">{u.file.name}</span>
                <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">
                  {u.status === "uploading" ? `${Math.round(u.progress * 100)}%` : u.status === "queued" ? "waiting" : ""} {formatBytes(u.file.size)}
                </span>
                {u.status !== "uploading" && u.status !== "queued" && (
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-label={`Dismiss ${u.file.name}`}
                    onClick={() => setItems((list) => list.filter((i) => i.id !== u.id))}
                  >
                    <X />
                  </Button>
                )}
              </div>
              {u.status === "uploading" && (
                <>
                  <Progress value={u.progress * 100} className="mt-2 h-1.5" aria-label={`Upload progress for ${u.file.name}`} />
                  {u.progress >= 1 && <p className="mt-1 text-xs text-muted-foreground">Transferred — inspecting and saving the jar…</p>}
                </>
              )}
              {u.status === "done" && u.result && (
                <p className="mt-1 text-xs text-muted-foreground">
                  {u.result.identified
                    ? `Identified on Modrinth as ${u.result.identified.project.title ?? u.result.identified.project.id} ${u.result.identified.version.version_number ?? ""}`
                    : "Not found on Modrinth — listed as a manual file"}
                  {u.result.detected_platforms.length ? ` · targets ${u.result.detected_platforms.join(", ")}` : ""}
                </p>
              )}
              {u.status === "done" &&
                u.result?.warnings.map((w) => (
                  <p key={w} className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                    {w}
                  </p>
                ))}
              {u.error && <p className={cn("mt-1 text-xs", u.status === "conflict" ? "text-amber-700 dark:text-amber-300" : "text-destructive")}>{u.error}</p>}
              {u.status === "conflict" && (
                <div className="mt-2 flex gap-2">
                  <ConfirmAction
                    trigger={
                      <Button size="xs" variant="destructive">
                        Replace existing
                      </Button>
                    }
                    title={`Replace ${u.file.name}?`}
                    description={`The existing ${u.file.name} in /${env.directory} is overwritten permanently with the uploaded file.`}
                    confirmLabel="Replace"
                    destructive
                    onConfirm={() => {
                      void uploadOne(u, true);
                    }}
                  />
                  <Button size="xs" variant="ghost" onClick={() => setItems((list) => list.filter((i) => i.id !== u.id))}>
                    Keep existing
                  </Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
