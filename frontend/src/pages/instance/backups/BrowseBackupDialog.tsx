/** Browse the files inside a backup snapshot; download single files or restore one file/folder. */
import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronRight, Download, File, Folder, FolderOpen, Home, Link2, RotateCcw } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api, downloadUrl, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { Backup, Instance, Job } from "@/lib/types";
import type { BackupFileEntry, BackupFilesResponse } from "./backup-utils";

const RUNNING = ["running", "starting", "unhealthy", "restarting"];

export function BrowseBackupDialog({
  instance,
  backup,
  canRestore,
  open,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  backup: Backup;
  canRestore: boolean;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const [path, setPath] = useState("");
  const [target, setTarget] = useState<BackupFileEntry | null>(null);
  const base = `/instances/${instance.id}/backups/${backup.id}`;
  const list = useQuery({
    queryKey: ["instances", instance.id, "backups", backup.id, "files", path],
    queryFn: () => api.get<BackupFilesResponse>(`${base}/files`, { path }),
    enabled: open,
    staleTime: 5 * 60_000, // snapshots are immutable
  });
  const restorePath = useMutation({
    mutationFn: (p: string) => api.post<Job>(`${base}/restore-path`, { path: p, confirm: true }),
    onSuccess: (job) => {
      toast.success("Restore started");
      onStarted(job);
      onOpenChange(false);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const parts = path ? path.split("/") : [];
  const running = RUNNING.includes(instance.status);
  const entries = list.data?.entries ?? [];
  const restoreBlocked = !!instance.active_job || restorePath.isPending;
  const folderCount = entries.filter((e) => e.type === "directory").length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="grid max-h-[85svh] grid-rows-[auto_auto_minmax(0,1fr)_auto] sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FolderOpen className="size-4 text-muted-foreground" aria-hidden /> Files in “{backup.name}”
          </DialogTitle>
          <DialogDescription>
            Read-only view of the backup taken {formatDateTime(backup.created_at)}. Download single files, or restore one file or
            folder without touching the rest of the server.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <nav
            aria-label="Folder path"
            className="flex min-w-0 grow basis-64 flex-wrap items-center gap-0.5 rounded-lg border bg-muted/40 px-2 py-1 text-xs"
          >
            <Button variant="ghost" size="xs" onClick={() => setPath("")} aria-label="Server folder root" aria-current={!path ? "page" : undefined}>
              <Home />
            </Button>
            {parts.map((p, i) => {
              const to = parts.slice(0, i + 1).join("/");
              return (
                <span key={to} className="flex min-w-0 items-center gap-0.5">
                  <ChevronRight className="size-3 shrink-0 text-muted-foreground" aria-hidden />
                  <Button
                    variant="ghost"
                    size="xs"
                    className="max-w-48 min-w-0"
                    onClick={() => setPath(to)}
                    aria-current={i === parts.length - 1 ? "page" : undefined}
                    title={p}
                  >
                    <span className="truncate">{p}</span>
                  </Button>
                </span>
              );
            })}
          </nav>
          {canRestore && path && (
            <Button
              variant="outline"
              size="sm"
              disabled={restoreBlocked}
              onClick={() => setTarget({ name: parts[parts.length - 1], path, type: "directory", size: 0, modified: null })}
              title="Restore this whole folder from the backup"
            >
              <RotateCcw /> Restore this folder
            </Button>
          )}
        </div>

        <div className="min-h-0 overflow-auto rounded-lg border">
          {list.isLoading ? (
            <LoadingState label="Reading snapshot…" />
          ) : list.error ? (
            <ErrorState error={list.error} onRetry={() => list.refetch()} title="Could not list files" className="m-3" />
          ) : entries.length === 0 ? (
            <EmptyState title="This folder is empty" className="m-3" />
          ) : (
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-card">
                <tr className="border-b text-left text-muted-foreground">
                  <th className="w-full px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 text-right font-medium">Size</th>
                  <th className="hidden px-3 py-2 font-medium sm:table-cell">Modified</th>
                  <th className="px-3 py-2">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {path && (
                  <tr>
                    <td colSpan={4} className="px-3 py-1.5">
                      <button
                        type="button"
                        className="text-muted-foreground hover:text-foreground hover:underline"
                        onClick={() => setPath(parts.slice(0, -1).join("/"))}
                      >
                        .. (up one level)
                      </button>
                    </td>
                  </tr>
                )}
                {entries.map((e) => {
                  const dir = e.type === "directory";
                  const Icon = dir ? Folder : e.type === "symlink" ? Link2 : File;
                  return (
                    <tr key={e.path} className="hover:bg-muted/40">
                      <td className="max-w-0 px-3 py-1.5">
                        {dir ? (
                          <button
                            type="button"
                            className="flex max-w-full items-center gap-2 rounded text-left font-medium hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
                            onClick={() => setPath(e.path)}
                          >
                            <Icon className="size-3.5 shrink-0 text-sky-600 dark:text-sky-400" aria-hidden />
                            <span className="truncate" title={e.name}>
                              {e.name}
                            </span>
                          </button>
                        ) : (
                          <span className="flex items-center gap-2">
                            <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                            <span className="truncate" title={e.name}>
                              {e.name}
                            </span>
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-1.5 text-right whitespace-nowrap tabular-nums text-muted-foreground">
                        {dir ? "—" : formatBytes(e.size)}
                      </td>
                      <td className="hidden px-3 py-1.5 whitespace-nowrap text-muted-foreground sm:table-cell">
                        {formatDateTime(e.modified)}
                      </td>
                      <td className="px-2 py-1 text-right whitespace-nowrap">
                        {e.type === "file" && (
                          <Button variant="ghost" size="icon-xs" asChild>
                            <a href={downloadUrl(`${base}/download`, { path: e.path })} download={e.name} aria-label={`Download ${e.name}`}>
                              <Download />
                            </a>
                          </Button>
                        )}
                        {canRestore && (
                          <Button
                            variant="ghost"
                            size="icon-xs"
                            aria-label={`Restore ${e.path} from this backup`}
                            title="Restore this item over the current one"
                            disabled={restoreBlocked}
                            onClick={() => setTarget(e)}
                          >
                            <RotateCcw />
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <p className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-muted-foreground" aria-live="polite">
          <span>
            {list.data
              ? `${entries.length} item${entries.length === 1 ? "" : "s"}${folderCount ? ` · ${folderCount} folder${folderCount === 1 ? "" : "s"}` : ""}`
              : "\u00a0"}
          </span>
          {canRestore && instance.active_job && (
            <span>Restoring is available when the current operation ({instance.active_job.title}) finishes.</span>
          )}
        </p>

        {target && (
          <ConfirmDialog
            open
            onOpenChange={(o) => !o && setTarget(null)}
            title={`Restore ${target.type === "directory" ? "folder" : "file"} “${target.path}”?`}
            destructive
            confirmLabel="Restore"
            description={
              <div className="grid gap-2">
                <ol className="grid list-decimal gap-1.5 pl-5 text-sm text-muted-foreground">
                  <li>
                    {running
                      ? "The server is stopped first (players are disconnected)."
                      : "The server is not running and stays stopped while restoring."}
                  </li>
                  <li>
                    The current <code className="font-mono [overflow-wrap:anywhere] text-foreground">{target.path}</code> is
                    replaced with the version from {formatDateTime(backup.created_at)}
                    {target.type === "directory" ? ", including everything inside it" : ""}. Everything else on the server is
                    left unchanged.
                  </li>
                  <li>{running ? "The server is started again afterwards." : "The server is left stopped."}</li>
                </ol>
                <p className="text-xs text-muted-foreground">
                  No safety backup is taken for a single-item restore: create a backup first if you might need the current
                  version.
                </p>
              </div>
            }
            onConfirm={async () => {
              try {
                await restorePath.mutateAsync(target.path);
              } catch {
                /* reported by onError */
              }
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
