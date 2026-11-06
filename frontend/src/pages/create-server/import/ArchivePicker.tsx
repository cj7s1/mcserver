import { AlertTriangle, FileArchive, FileUp, X } from "lucide-react";
import { type DragEvent, useId, useRef, useState } from "react";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { FieldErrors, WizardState } from "../wizardModel";
import { ARCHIVE_ACCEPT, DEFAULT_UPLOAD_LIMIT_BYTES, validateImportArchive } from "./importModel";

/** File input + drop zone for the archive of the server folder (import mode, software step). */
export function ArchivePicker({
  state,
  patch,
  errors,
}: {
  state: WizardState;
  patch: (p: Partial<WizardState>) => void;
  errors: FieldErrors;
}) {
  const uid = useId();
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const file = state.importFile;
  // a chosen file is checked right away; "nothing chosen" only after Next was pressed
  const error = (file ? validateImportArchive(file) : null) ?? errors.importFile;
  const tooBig = !!file && file.size > DEFAULT_UPLOAD_LIMIT_BYTES;

  const pick = (files: FileList | File[] | null | undefined) => {
    const list = Array.from(files ?? []);
    if (list.length) patch({ importFile: list[0] });
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    pick(e.dataTransfer.files);
  };

  return (
    <Section
      title="Server archive"
      description="An archive of the whole server folder: the one with server.properties and the world folder."
    >
      <div className="grid gap-4">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          aria-describedby={error ? `${uid}-error` : `${uid}-hint`}
          className={cn(
            "rounded-xl border-2 border-dashed p-4 transition-colors",
            dragging ? "border-primary bg-primary/5" : error ? "border-destructive/50" : "border-border",
          )}
        >
          {file ? (
            <div className="flex flex-wrap items-center gap-3">
              <div className="grid size-10 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary" aria-hidden>
                <FileArchive className="size-5" />
              </div>
              <div className="min-w-0 flex-1 basis-40">
                <p className="truncate text-sm font-medium" title={file.name}>
                  {file.name}
                </p>
                <p className="text-xs text-muted-foreground tabular-nums">{formatBytes(file.size)}</p>
              </div>
              <div className="flex shrink-0 gap-2">
                <Button type="button" size="sm" variant="outline" onClick={() => input.current?.click()}>
                  Change…
                </Button>
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  aria-label={`Remove ${file.name}`}
                  onClick={() => patch({ importFile: null })}
                >
                  <X />
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-3 py-4 text-center">
              <div className="rounded-full bg-muted p-3 text-muted-foreground" aria-hidden>
                <FileUp className="size-5" />
              </div>
              <div>
                <p className="text-sm font-medium">Drop the archive here</p>
                <p className="mt-0.5 text-xs text-muted-foreground">.zip, .tar, .tar.gz, .tgz or .tar.xz</p>
              </div>
              <Button type="button" size="sm" variant="outline" onClick={() => input.current?.click()}>
                Choose archive…
              </Button>
            </div>
          )}
          <input
            ref={input}
            id={`${uid}-file`}
            type="file"
            accept={ARCHIVE_ACCEPT}
            className="sr-only"
            tabIndex={-1}
            aria-hidden
            onChange={(e) => {
              pick(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
        {error ? (
          <p id={`${uid}-error`} className="-mt-2 text-xs text-destructive" role="alert">
            {error}
          </p>
        ) : (
          <p id={`${uid}-hint`} className="-mt-2 text-xs text-muted-foreground">
            The upload starts after the server is created. Keep this browser tab open until it finishes.
          </p>
        )}
        {tooBig && (
          <p className="-mt-2 flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300" role="alert">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            Larger than the panel&apos;s default {formatBytes(DEFAULT_UPLOAD_LIMIT_BYTES, 0)} upload limit. It is refused unless
            the administrator raised MCPANEL_MAX_UPLOAD_BYTES.
          </p>
        )}

        <ul className="grid gap-1 rounded-lg border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
          <li>
            <span className="font-medium text-foreground">Kept:</span> worlds, configs, mods, plugins, whitelist, ops and
            other player data.
          </li>
          <li>
            <span className="font-medium text-foreground">Top-level folder:</span> if everything is inside one folder (e.g.{" "}
            <span className="font-mono">My Server/…</span>), that folder is stripped automatically.
          </li>
          <li>
            <span className="font-medium text-foreground">Skipped for safety:</span> symbolic links, special files and paths
            that point outside the server folder.
          </li>
          <li>
            <span className="font-medium text-foreground">Managed by the panel:</span> game port, RCON and memory settings
            follow this wizard, whatever the archive contains.
          </li>
        </ul>

        <div className="flex items-start justify-between gap-4">
          <div>
            <Label htmlFor={`${uid}-overwrite`}>Use the archive&apos;s server.properties</Label>
            <p id={`${uid}-overwrite-hint`} className="mt-0.5 text-xs text-muted-foreground">
              {state.importOverwrite
                ? "Files in the archive replace the defaults the panel writes for a new server (its server.properties with the gameplay settings of this wizard)."
                : "The server.properties written from this wizard's gameplay settings is kept; the archive's copy is skipped. Everything else is imported."}
            </p>
          </div>
          <Switch
            id={`${uid}-overwrite`}
            checked={state.importOverwrite}
            onCheckedChange={(v) => patch({ importOverwrite: v })}
            aria-describedby={`${uid}-overwrite-hint`}
          />
        </div>
      </div>
    </Section>
  );
}
