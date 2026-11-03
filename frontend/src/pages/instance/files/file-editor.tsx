/** Modal text editor for a single server file (GET/PUT /instances/{id}/files/text). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Download, FileText, Loader2, Lock, RefreshCw, Save } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api, ApiError, downloadUrl, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CodeEditor, type CursorPos } from "./code-editor";
import { baseName, countLines, parentPath, TEXT_EDIT_LIMIT, utf8Length } from "./file-utils";
import { UnsavedChangesDialog, useUnsavedChangesGuard } from "./use-unsaved-guard";

interface TextFile {
  path: string;
  content: string;
  size: number;
  modified: number;
}

export interface EditorTarget {
  path: string;
  line?: number | null;
  /** Changes on every open request so re-opening the same file at another line re-jumps. */
  key: number;
}

const LIVE_STATUSES = ["running", "starting", "unhealthy", "restarting", "stopping"];
const PLAYER_FILES = ["ops.json", "whitelist.json", "banned-players.json", "banned-ips.json"];

function textFileKey(instanceId: string, path: string) {
  return [...qk.files(instanceId, path), "text"] as const;
}

export function FileEditorDialog({
  instance,
  target,
  canWrite,
  onClose,
}: {
  instance: Instance;
  target: EditorTarget;
  canWrite: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const key = textFileKey(instance.id, target.path);
  const file = useQuery({
    queryKey: key,
    queryFn: () => api.get<TextFile>(`/instances/${instance.id}/files/text`, { path: target.path }),
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const [draft, setDraft] = useState<string | null>(null);
  const [cursor, setCursor] = useState<CursorPos>({ line: 1, col: 1 });
  const [confirm, setConfirm] = useState<"close" | "reload" | null>(null);

  const text = draft ?? file.data?.content ?? "";
  const dirty = draft !== null && file.data !== undefined && draft !== file.data.content;
  const bytes = useMemo(() => utf8Length(text), [text]);
  const lineCount = useMemo(() => countLines(text), [text]);
  const tooLarge = bytes > TEXT_EDIT_LIMIT;
  const blocker = useUnsavedChangesGuard(dirty);
  const live = LIVE_STATUSES.includes(instance.status);
  const name = baseName(target.path);

  const save = useMutation({
    mutationFn: (content: string) =>
      api.put<{ path: string; size: number }>(`/instances/${instance.id}/files/text`, { path: target.path, content }),
    onSuccess: (res, content) => {
      qc.setQueryData<TextFile>(key, (old) => (old ? { ...old, content, size: res.size, modified: Date.now() / 1000 } : old));
      setDraft(null);
      toast.success(`Saved ${name}`);
      qc.invalidateQueries({ queryKey: qk.files(instance.id, parentPath(target.path)) });
      if (target.path === "server.properties") qc.invalidateQueries({ queryKey: qk.properties(instance.id) });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  const doSave = () => {
    if (!canWrite || !dirty || tooLarge || save.isPending) return;
    save.mutate(text);
  };

  const reload = async () => {
    setDraft(null);
    await file.refetch();
  };

  const requestClose = () => {
    if (dirty) setConfirm("close");
    else onClose();
  };

  const notEditable =
    file.error instanceof ApiError && ["binary_file", "too_large", "not_a_file", "symlink"].includes(file.error.code);

  return (
    <>
      <Dialog open onOpenChange={(o) => !o && requestClose()}>
        <DialogContent
          className="flex h-[calc(100svh-2rem)] max-h-[960px] flex-col gap-3 sm:max-w-6xl"
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
              e.preventDefault();
              doSave();
            }
          }}
        >
          <DialogHeader className="min-w-0 pr-8">
            <DialogTitle className="flex min-w-0 items-center gap-2">
              <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              <span className="truncate">{name}</span>
              {!canWrite && (
                <Badge variant="outline" className="shrink-0">
                  <Lock /> Read-only
                </Badge>
              )}
              {dirty && (
                <Badge variant="secondary" className="shrink-0">
                  Unsaved
                </Badge>
              )}
            </DialogTitle>
            <DialogDescription className="truncate font-mono text-xs">/{target.path}</DialogDescription>
          </DialogHeader>

          {live && canWrite && (
            <div className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              <p>
                The server is running. It rewrites some files itself (for example server.properties, ops.json, whitelist.json
                and world data) when settings change or on shutdown, which can overwrite your edits. Most config files are only
                read at startup, so restart after saving.
              </p>
            </div>
          )}
          {target.path === "server.properties" && (
            <p className="text-xs text-muted-foreground">
              Tip: the{" "}
              <Link to={`/servers/${instance.id}/settings?tab=properties`} className="text-primary hover:underline">
                Settings → Properties
              </Link>{" "}
              editor validates values. Ports and RCON keys are managed by MCPanel and reverted on start.
            </p>
          )}
          {PLAYER_FILES.includes(target.path) && (
            <p className="text-xs text-muted-foreground">
              Tip: operators, the whitelist and bans can be managed on the{" "}
              <Link to={`/servers/${instance.id}/players`} className="text-primary hover:underline">
                Players
              </Link>{" "}
              tab.
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => (dirty ? setConfirm("reload") : reload())}
              disabled={file.isFetching}
            >
              <RefreshCw className={cn(file.isFetching && "animate-spin")} /> Reload
            </Button>
            <Button size="sm" variant="outline" asChild>
              <a href={downloadUrl(`/instances/${instance.id}/files/download`, { path: target.path })} download={name}>
                <Download /> Download
              </a>
            </Button>
            {canWrite && (
              <Button size="sm" className="ml-auto" onClick={doSave} disabled={!dirty || tooLarge || save.isPending}>
                {save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save
                <kbd className="ml-1 hidden rounded border border-primary-foreground/30 px-1 font-sans text-[10px] sm:inline">
                  Ctrl+S
                </kbd>
              </Button>
            )}
          </div>

          <div className="flex min-h-0 flex-1 flex-col">
            {file.isLoading ? (
              <LoadingState label="Opening file…" />
            ) : file.error ? (
              <ErrorState
                error={file.error}
                title={notEditable ? "This file cannot be edited as text" : "Could not open the file"}
                onRetry={notEditable ? undefined : () => file.refetch()}
              />
            ) : (
              <CodeEditor
                className="flex-1"
                value={text}
                onChange={canWrite ? setDraft : undefined}
                readOnly={!canWrite}
                ariaLabel={`Contents of ${name}`}
                jumpTo={target.line ? { line: target.line, key: target.key } : null}
                onCursor={setCursor}
                invalid={tooLarge}
              />
            )}
          </div>

          {file.data && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground tabular-nums" aria-live="polite">
              <span>
                Ln {cursor.line}, Col {cursor.col}
              </span>
              <span>{lineCount.toLocaleString()} lines</span>
              <span className={cn(tooLarge && "font-medium text-destructive")}>
                {formatBytes(bytes)} / {formatBytes(TEXT_EDIT_LIMIT, 0)} limit
              </span>
              <span className="hidden sm:inline">Modified {formatDateTime(file.data.modified)}</span>
              {canWrite && <span className="ml-auto hidden md:inline">Ctrl/⌘+S saves</span>}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={confirm === "reload" ? "Reload and discard your changes?" : "Discard unsaved changes?"}
        description={
          confirm === "reload"
            ? "The file is read again from the server. Your unsaved edits are lost."
            : `Your edits to ${name} have not been saved.`
        }
        confirmLabel={confirm === "reload" ? "Discard and reload" : "Discard changes"}
        destructive
        onConfirm={async () => {
          if (confirm === "reload") await reload();
          else onClose();
        }}
      />
      <UnsavedChangesDialog blocker={blocker} description={`Your edits to ${name} have not been saved.`} />
    </>
  );
}
