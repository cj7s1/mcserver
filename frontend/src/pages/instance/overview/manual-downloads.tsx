/**
 * Files a modpack needs but that could not be installed automatically:
 * GET /instances/{id}/manual-downloads (backend/apps/instances/diagnostics.py).
 *
 * Sources are the pack's own Missing Mods Checker list (config/missing_mods_checker.json)
 * and the itzg CurseForge installer's MODS_NEED_DOWNLOAD.txt. The user downloads each
 * file in their browser from the linked page and uploads it here; uploads go through
 * POST /instances/{id}/files/upload?path=<destination>.
 */
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, ExternalLink, FileUp, Info, Loader2, PackageSearch, RefreshCw, TriangleAlert, Upload, XCircle } from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useAuth } from "@/hooks/use-auth";
import { ApiError, api, errorMessage, uploadWithProgress } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, PermissionCode } from "@/lib/types";

export interface ManualDownloadItem {
  name: string;
  filename: string;
  destination: string;
  url: string;
  present: boolean;
  source: "missing_mods_checker" | "curseforge" | (string & {});
  version?: string;
}

export interface ManualDownloads {
  sources: string[];
  items: ManualDownloadItem[];
  missing: number;
  missing_mods_checker_enabled: boolean;
  notes: string[];
}

interface RowUpload {
  status: "uploading" | "done" | "error";
  progress: number;
  error?: string;
}

const rowKey = (i: Pick<ManualDownloadItem, "destination" | "filename">) => `${i.destination}/${i.filename}`;

/** Only plain web links are rendered: the list comes from files inside the modpack. */
function safeUrl(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

function extensionOf(name: string): string | undefined {
  const m = /\.[A-Za-z0-9]+$/.exec(name);
  return m ? m[0].toLowerCase() : undefined;
}

function useManualDownloads(instanceId: string, enabled: boolean) {
  return useQuery({
    queryKey: [...qk.instance(instanceId), "manual-downloads"] as const,
    queryFn: () => api.get<ManualDownloads>(`/instances/${instanceId}/manual-downloads`),
    enabled,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

/** Upload one file to `dir`, creating the folder first if the server does not have it yet. */
async function uploadInto(instanceId: string, dir: string, file: File, saveAs: string, onProgress: (f: number) => void) {
  const send = () => {
    const form = new FormData();
    form.append("file", file, saveAs);
    return uploadWithProgress<{ uploaded: { path: string; size: number }[] }>(
      `/instances/${instanceId}/files/upload`,
      form,
      { path: dir },
      onProgress,
    );
  };
  try {
    return await send();
  } catch (err) {
    if (err instanceof ApiError && err.status === 404 && err.code === "not_found") {
      await api.post(`/instances/${instanceId}/files/mkdir`, { path: dir, parents: true });
      return await send();
    }
    throw err;
  }
}

function StatusCell({ item, upload }: { item: ManualDownloadItem; upload?: RowUpload }) {
  if (upload?.status === "uploading") {
    return (
      <div className="grid min-w-20 gap-1">
        <span className="inline-flex items-center gap-1 text-primary">
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
          {upload.progress >= 1 ? "Saving…" : `${Math.round(upload.progress * 100)}%`}
        </span>
        <Progress value={upload.progress * 100} className="h-1" aria-label={`Upload progress for ${item.filename}`} />
      </div>
    );
  }
  if (item.present || upload?.status === "done") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-700 dark:text-emerald-300">
        <CheckCircle2 className="size-3" aria-hidden /> {item.present ? "Present" : "Uploaded"}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 font-medium text-amber-700 dark:text-amber-300">
      <XCircle className="size-3" aria-hidden /> Missing
    </span>
  );
}

export function ManualDownloadsDialog({
  instance,
  can,
  open,
  onOpenChange,
}: {
  instance: Instance;
  can: (c: PermissionCode) => boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const auth = useAuth();
  const q = useManualDownloads(instance.id, open);
  const canWrite = can("instance.files.write") && !instance.archived_at;
  const [showAll, setShowAll] = useState(false);
  const [uploads, setUploads] = useState<Record<string, RowUpload>>({});
  const [rename, setRename] = useState<{ item: ManualDownloadItem; file: File } | null>(null);
  const [bulk, setBulk] = useState<{ done: number; total: number; current: string } | null>(null);
  const [unmatched, setUnmatched] = useState<string[]>([]);
  const rowInput = useRef<HTMLInputElement>(null);
  const bulkInput = useRef<HTMLInputElement>(null);
  const target = useRef<ManualDownloadItem | null>(null);

  const data = q.data;
  const items = data?.items ?? [];
  const missingItems = items.filter((i) => !i.present);
  const filtered = showAll || missingItems.length === 0 ? items : missingItems;
  const busy = !!bulk || Object.values(uploads).some((u) => u.status === "uploading");

  const patch = (key: string, p: RowUpload) => setUploads((m) => ({ ...m, [key]: p }));

  const uploadRow = async (item: ManualDownloadItem, file: File): Promise<boolean> => {
    const key = rowKey(item);
    patch(key, { status: "uploading", progress: 0 });
    try {
      await uploadInto(instance.id, item.destination, file, item.filename, (f) => patch(key, { status: "uploading", progress: f }));
      patch(key, { status: "done", progress: 1 });
      return true;
    } catch (err) {
      patch(key, { status: "error", progress: 0, error: errorMessage(err) });
      return false;
    }
  };

  const startRow = (item: ManualDownloadItem, file: File) => {
    if (file.size === 0) {
      patch(rowKey(item), { status: "error", progress: 0, error: "The selected file is empty." });
      return;
    }
    if (file.name !== item.filename) {
      setRename({ item, file });
      return;
    }
    void uploadRow(item, file).then((ok) => {
      if (ok) toast.success(`Uploaded ${item.filename} to /${item.destination}`);
      void q.refetch();
    });
  };

  const startBulk = async (files: File[]) => {
    if (!files.length) return;
    const byName = new Map<string, ManualDownloadItem>();
    for (const i of missingItems) if (!byName.has(i.filename)) byName.set(i.filename, i);
    const matched: { item: ManualDownloadItem; file: File }[] = [];
    const skipped: string[] = [];
    for (const f of files) {
      const item = byName.get(f.name);
      if (item && f.size > 0) {
        matched.push({ item, file: f });
        byName.delete(f.name);
      } else {
        skipped.push(f.name);
      }
    }
    setUnmatched(skipped);
    if (!matched.length) {
      toast.error("None of the selected files match a missing file name.");
      return;
    }
    let ok = 0;
    // sequential: one request at a time keeps per-file errors and progress clear
    for (let n = 0; n < matched.length; n++) {
      const { item, file } = matched[n];
      setBulk({ done: n, total: matched.length, current: item.filename });
      if (await uploadRow(item, file)) ok++;
    }
    setBulk(null);
    void q.refetch();
    const failed = matched.length - ok;
    if (failed) toast.error(`Uploaded ${ok} of ${matched.length} files; ${failed} failed (see the list).`);
    else toast.success(`Uploaded ${ok} file${ok === 1 ? "" : "s"}`);
  };

  const showCurseForgeHint = instance.modpack_provider !== "curseforge" && items.some((i) => i.source === "missing_mods_checker");
  const cfSearch = `/modpacks?provider=curseforge${instance.modpack_name ? `&q=${encodeURIComponent(instance.modpack_name)}` : ""}`;

  return (
    <>
      <Dialog open={open} onOpenChange={(o) => (busy && !o ? undefined : onOpenChange(o))}>
        <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Files to download manually</DialogTitle>
            <DialogDescription>
              {data
                ? data.items.length
                  ? `${data.missing} of ${data.items.length} file${data.items.length === 1 ? "" : "s"} missing. The mod authors do not allow automated downloads, so each file has to be downloaded in your browser and uploaded here.`
                  : "Files the modpack expects but that could not be installed automatically."
                : "Files the modpack expects but that could not be installed automatically."}
            </DialogDescription>
          </DialogHeader>

          {q.isLoading ? (
            <LoadingState label="Reading the modpack's download list…" className="py-8" />
          ) : q.error ? (
            <ErrorState error={q.error} onRetry={() => q.refetch()} title="Could not read the download list" className="py-8" />
          ) : !data ? null : items.length === 0 ? (
            <EmptyState
              icon={<PackageSearch className="size-5" />}
              title="No manual downloads listed"
              description={
                data.sources.length
                  ? `The list in ${data.sources.join(" and ")} is empty.`
                  : "This server has neither config/missing_mods_checker.json nor MODS_NEED_DOWNLOAD.txt, so the panel has no list of files to download by hand. If the server still fails, check the Console for the first error."
              }
              className="py-8"
            />
          ) : (
            <div className="grid min-w-0 gap-3">
              {showCurseForgeHint && (
                <div className="flex gap-2.5 rounded-lg border border-sky-500/30 bg-sky-500/10 p-3 text-xs">
                  <Info className="mt-0.5 size-4 shrink-0 text-sky-600 dark:text-sky-400" aria-hidden />
                  <p>
                    <span className="font-medium">Easier option:</span>{" "}
                    <span className="text-muted-foreground">
                      if this pack is also published on CurseForge, a server created from its CurseForge edition downloads these
                      files automatically.
                    </span>{" "}
                    {auth.can("instance.create") && (
                      <Link to={cfSearch} className="font-medium text-primary hover:underline" onClick={() => onOpenChange(false)}>
                        Search CurseForge modpacks
                      </Link>
                    )}
                  </p>
                </div>
              )}
              {data.missing > 0 && data.missing_mods_checker_enabled && (
                <div className="flex gap-2.5 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200">
                  <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
                  <p>The pack&apos;s Missing Mods Checker is installed: the server keeps failing at startup until every listed file is present.</p>
                </div>
              )}
              {data.missing === 0 && (
                <div className="flex gap-2.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-xs text-emerald-800 dark:text-emerald-200">
                  <CheckCircle2 className="mt-0.5 size-4 shrink-0" aria-hidden />
                  <p>All listed files are present. Start the server again to continue.</p>
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                {missingItems.length > 0 && missingItems.length < items.length && (
                  <ToggleGroup
                    type="single"
                    size="sm"
                    variant="outline"
                    spacing={0}
                    value={showAll ? "all" : "missing"}
                    onValueChange={(v) => v && setShowAll(v === "all")}
                    aria-label="Which files to list"
                  >
                    <ToggleGroupItem value="missing" className="text-xs">
                      Missing <span className="ml-1 text-muted-foreground tabular-nums">{missingItems.length}</span>
                    </ToggleGroupItem>
                    <ToggleGroupItem value="all" className="text-xs">
                      All <span className="ml-1 text-muted-foreground tabular-nums">{items.length}</span>
                    </ToggleGroupItem>
                  </ToggleGroup>
                )}
                <div className="ml-auto flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="ghost" onClick={() => q.refetch()} disabled={q.isFetching || busy}>
                    {q.isFetching ? <Loader2 className="animate-spin" /> : <RefreshCw />} Refresh
                  </Button>
                  {canWrite && missingItems.length > 0 && (
                    <Button size="sm" variant="outline" onClick={() => bulkInput.current?.click()} disabled={busy}>
                      <FileUp /> Upload downloaded files…
                    </Button>
                  )}
                </div>
              </div>
              {canWrite && missingItems.length > 0 && !bulk && (
                <p className="text-xs text-muted-foreground">
                  Select several downloaded files at once: each is matched to the list by its exact file name and saved to its folder.
                </p>
              )}
              {!canWrite && missingItems.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  You need file write access to upload here; ask someone with that permission, or download the files and upload them
                  together later.
                </p>
              )}

              {bulk && (
                <div className="rounded-lg border bg-muted/30 p-3" aria-live="polite">
                  <div className="flex items-center gap-2 text-xs">
                    <Loader2 className="size-3.5 animate-spin text-primary" aria-hidden />
                    <span className="min-w-0 truncate">
                      Uploading {bulk.done + 1} of {bulk.total}: <span className="font-mono">{bulk.current}</span>
                    </span>
                  </div>
                  <Progress value={(bulk.done / bulk.total) * 100} className="mt-2 h-1.5" aria-label="Overall upload progress" />
                </div>
              )}
              {unmatched.length > 0 && !bulk && (
                <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200" role="status">
                  <p className="font-medium">
                    {unmatched.length} selected file{unmatched.length === 1 ? " was" : "s were"} not uploaded (no missing file has that
                    exact name, or the file is empty):
                  </p>
                  <ul className="mt-1 grid gap-0.5 font-mono text-[11px] break-all">
                    {unmatched.slice(0, 10).map((n) => (
                      <li key={n}>{n}</li>
                    ))}
                    {unmatched.length > 10 && <li className="font-sans">+{unmatched.length - 10} more</li>}
                  </ul>
                  <p className="mt-1">Use the row&apos;s Upload button to upload a file under the expected name.</p>
                </div>
              )}

              <div className="min-w-0 rounded-lg border">
                <Table className="text-xs">
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead>File</TableHead>
                      <TableHead className="hidden sm:table-cell">Folder</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="text-right">
                        <span className="sr-only">Actions</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filtered.map((item) => {
                      const key = rowKey(item);
                      const up = uploads[key];
                      const href = safeUrl(item.url);
                      const missing = !item.present && up?.status !== "done";
                      return (
                        <TableRow key={key}>
                          <TableCell className="w-full min-w-32 whitespace-normal">
                            <div className="font-medium break-words">{item.name}</div>
                            <div className="font-mono text-[11px] break-all text-muted-foreground">
                              {item.filename}
                              {item.version && item.version !== item.filename && <span className="font-sans"> · {item.version}</span>}
                            </div>
                            <div className="font-mono text-[11px] text-muted-foreground sm:hidden">→ /{item.destination}</div>
                            {up?.status === "error" && <p className="mt-1 text-[11px] text-destructive">{up.error}</p>}
                          </TableCell>
                          <TableCell className="hidden font-mono sm:table-cell">/{item.destination}</TableCell>
                          <TableCell>
                            <StatusCell item={item} upload={up} />
                          </TableCell>
                          <TableCell className="text-right">
                            <div className="flex flex-col items-end gap-1 sm:flex-row sm:justify-end">
                              {href ? (
                                <Button asChild size="xs" variant="ghost">
                                  <a href={href} target="_blank" rel="noopener noreferrer" aria-label={`Download page for ${item.name} (opens in a new tab)`}>
                                    <ExternalLink aria-hidden />
                                    <span className="hidden sm:inline">Download page</span>
                                  </a>
                                </Button>
                              ) : (
                                <span className="text-muted-foreground">No link</span>
                              )}
                              {canWrite && missing && (
                                <Button
                                  size="xs"
                                  variant="outline"
                                  disabled={busy}
                                  aria-label={`Upload ${item.filename} to /${item.destination}`}
                                  onClick={() => {
                                    target.current = item;
                                    const input = rowInput.current;
                                    if (!input) return;
                                    const ext = extensionOf(item.filename);
                                    if (ext) input.accept = ext;
                                    else input.removeAttribute("accept");
                                    input.click();
                                  }}
                                >
                                  <Upload aria-hidden />
                                  <span className="hidden sm:inline">Upload file</span>
                                </Button>
                              )}
                            </div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>

              {data.sources.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Read from <span className="font-mono">{data.sources.join(", ")}</span> inside the server folder.
                </p>
              )}
              {data.notes.length > 0 && (
                <ul className="grid gap-1.5">
                  {data.notes.map((n) => (
                    <li key={n} className="flex gap-2 text-xs text-muted-foreground">
                      <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                      <span>{n}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <input
            ref={rowInput}
            type="file"
            className="sr-only"
            tabIndex={-1}
            aria-hidden
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              const item = target.current;
              if (file && item) startRow(item, file);
            }}
          />
          <input
            ref={bulkInput}
            type="file"
            multiple
            className="sr-only"
            tabIndex={-1}
            aria-hidden
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              e.target.value = "";
              void startBulk(files);
            }}
          />
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={!!rename}
        onOpenChange={(o) => !o && setRename(null)}
        title="Upload under the expected name?"
        description={
          rename && (
            <div className="grid gap-2">
              <p>
                You selected <span className="font-mono break-all">{rename.file.name}</span>, but the pack looks for{" "}
                <span className="font-mono break-all">{rename.item.filename}</span> in{" "}
                <span className="font-mono">/{rename.item.destination}</span>.
              </p>
              <p>The file is saved as {rename.item.filename}. Only continue if it really is that file from its download page.</p>
            </div>
          )
        }
        confirmLabel="Upload with expected name"
        onConfirm={async () => {
          const r = rename;
          if (!r) return;
          setRename(null);
          const ok = await uploadRow(r.item, r.file);
          if (ok) toast.success(`Uploaded ${r.item.filename} to /${r.item.destination}`);
          void q.refetch();
        }}
      />
    </>
  );
}
