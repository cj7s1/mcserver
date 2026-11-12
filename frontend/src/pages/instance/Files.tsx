import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUp,
  FileSearch,
  FolderArchive,
  FolderPlus,
  FolderX,
  Globe2,
  HardDrive,
  Lock,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { Fragment, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { CutOffNotice } from "@/components/CutOffNotice";
import { DownloadLink } from "@/components/DownloadLink";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { api, ApiError, downloadUrl, errorMessage } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { FileEntry, Instance, PropertiesResponse } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useInstanceContext } from "./InstanceLayout";
import { ExtractDialog } from "./files/extract-dialog";
import { FileEditorDialog, type EditorTarget } from "./files/file-editor";
import { FileSearchSheet } from "./files/file-search";
import { FileTable, type FileActions } from "./files/file-table";
import {
  baseName,
  hasInvalidName,
  isWorldEntry,
  isWorldPath,
  joinPath,
  normalizePath,
  parentPath,
  sortEntries,
  type SortDir,
  type SortKey,
} from "./files/file-utils";
import { NameDialog } from "./files/name-dialog";
import { DropZone, UploadConflictDialog, UploadList, useFileUploads } from "./files/uploads";

interface Listing {
  path: string;
  entries: FileEntry[];
  /** The folder holds more entries than one listing returns (the node's cap, B-6): only these are shown. */
  truncated?: boolean;
}

const LIVE_STATUSES = ["running", "starting", "unhealthy", "restarting", "stopping"];

/** 202 from rename/delete: the node did not finish within the panel's wait and is still at it. */
interface StillRunning {
  still_running: true;
  message: string;
}

function isStillRunning(res: unknown): res is StillRunning {
  return typeof res === "object" && res !== null && (res as { still_running?: unknown }).still_running === true;
}
const EMPTY = new Set<string>();

export default function Files() {
  const { instance, can } = useInstanceContext();
  if (!can("instance.files.read")) {
    return (
      <EmptyState
        icon={<Lock className="size-5" />}
        title="You cannot browse this server's files"
        description="Browsing files needs the “Files: read” permission on this server. Ask an administrator to grant it."
      />
    );
  }
  return <FileBrowser instance={instance} canWrite={can("instance.files.write")} />;
}

function PathBreadcrumbs({ path }: { path: string }) {
  const parts = path ? path.split("/") : [];
  const href = (p: string) => ({ search: p ? `?path=${encodeURIComponent(p)}` : "" });
  return (
    <Breadcrumb className="min-w-0">
      <BreadcrumbList className="flex-wrap gap-y-0.5 font-mono text-xs sm:text-sm">
        <BreadcrumbItem>
          {parts.length ? (
            <BreadcrumbLink asChild>
              <Link to={href("")} className="inline-flex items-center gap-1.5">
                <HardDrive className="size-3.5" aria-hidden /> <span className="font-sans">Server root</span>
              </Link>
            </BreadcrumbLink>
          ) : (
            <BreadcrumbPage className="inline-flex items-center gap-1.5">
              <HardDrive className="size-3.5" aria-hidden /> <span className="font-sans">Server root</span>
            </BreadcrumbPage>
          )}
        </BreadcrumbItem>
        {parts.map((part, i) => {
          const p = parts.slice(0, i + 1).join("/");
          const last = i === parts.length - 1;
          return (
            <Fragment key={p}>
              <BreadcrumbSeparator />
              <BreadcrumbItem className="min-w-0">
                {last ? (
                  <BreadcrumbPage className="truncate">{part}</BreadcrumbPage>
                ) : (
                  <BreadcrumbLink asChild>
                    <Link to={href(p)} className="truncate">
                      {part}
                    </Link>
                  </BreadcrumbLink>
                )}
              </BreadcrumbItem>
            </Fragment>
          );
        })}
      </BreadcrumbList>
    </Breadcrumb>
  );
}

function FileBrowser({ instance, canWrite }: { instance: Instance; canWrite: boolean }) {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const path = normalizePath(params.get("path") ?? "");
  const listingKey = qk.files(instance.id, path);
  const listing = useQuery({
    queryKey: listingKey,
    queryFn: () => api.get<Listing>(`/instances/${instance.id}/files`, { path }),
    retry: false,
  });
  // level-name tells us which folder holds the world (used for warnings only).
  const props = useQuery({
    queryKey: qk.properties(instance.id),
    queryFn: () => api.get<PropertiesResponse>(`/instances/${instance.id}/properties`),
    staleTime: 60_000,
    retry: false,
  });
  const levelName = props.data?.values["level-name"] || "world";

  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: "name", dir: "asc" });
  const [sel, setSel] = useState<{ path: string; set: Set<string> }>({ path, set: EMPTY });
  const [filterState, setFilterState] = useState<{ path: string; value: string }>({ path, value: "" });
  const selected = sel.path === path ? sel.set : EMPTY;
  const filter = filterState.path === path ? filterState.value : "";

  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const [renameTarget, setRenameTarget] = useState<FileEntry | null>(null);
  const [overwritePrompt, setOverwritePrompt] = useState<{ source: string; destination: string } | null>(null);
  const [deleteTargets, setDeleteTargets] = useState<FileEntry[] | null>(null);
  const [extractTarget, setExtractTarget] = useState<FileEntry | null>(null);
  const [newFolder, setNewFolder] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const uploadInput = useRef<HTMLInputElement>(null);
  const uploads = useFileUploads(instance.id);

  const entries = useMemo(() => listing.data?.entries ?? [], [listing.data]);
  const sorted = useMemo(() => sortEntries(entries, sort.key, sort.dir), [entries, sort]);
  const visible = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return f ? sorted.filter((e) => e.name.toLowerCase().includes(f)) : sorted;
  }, [sorted, filter]);
  const selectedEntries = entries.filter((e) => selected.has(e.path) && !hasInvalidName(e));
  const existingNames = useMemo(() => new Set(entries.map((e) => e.name)), [entries]);
  const live = LIVE_STATUSES.includes(instance.status);
  const inWorld = isWorldPath(path, levelName, entries);
  const fileCount = entries.filter((e) => e.type === "file").length;
  const dirCount = entries.filter((e) => e.type === "directory").length;
  const totalBytes = entries.reduce((n, e) => n + (e.type === "file" ? e.size : 0), 0);

  const go = (p: string) => {
    const next = normalizePath(p);
    setParams((prev) => {
      const n = new URLSearchParams(prev);
      if (next) n.set("path", next);
      else n.delete("path");
      return n;
    });
  };
  const refreshDir = (dir: string) => qc.invalidateQueries({ queryKey: qk.files(instance.id, dir) });
  const openEditor = (p: string, line?: number | null) => setEditor({ path: p, line, key: Date.now() });

  const actions: FileActions = {
    open: (e) => go(e.path),
    edit: (e) => openEditor(e.path),
    rename: (e) => setRenameTarget(e),
    remove: (list) => setDeleteTargets(list),
    extract: (e) => setExtractTarget(e),
    downloadHref: (e) => downloadUrl(`/instances/${instance.id}/files/download`, { path: e.path }),
    archiveHref: (e) => downloadUrl(`/instances/${instance.id}/files/archive`, { path: e.path }),
  };
  const folderZipHref = downloadUrl(`/instances/${instance.id}/files/archive`, { path });
  const folderZipName = `${baseName(path) || "server"}.zip`;

  const doRename = async (source: string, destination: string, overwrite = false) => {
    try {
      const res = await api.post<{ destination: string } | StillRunning>(`/instances/${instance.id}/files/rename`, {
        source,
        destination,
        overwrite,
      });
      // 202: the node is still moving a big folder; neither done nor failed yet
      if (isStillRunning(res)) toast.info(res.message);
      else toast.success(`Renamed to ${destination}`);
    } finally {
      refreshDir(parentPath(source));
      if (parentPath(destination) !== parentPath(source)) refreshDir(parentPath(destination));
    }
  };

  const doDelete = async (targets: FileEntry[]) => {
    const recursive = targets.some((e) => e.type === "directory");
    try {
      const res = await api.post<{ deleted: string[] } | StillRunning>(`/instances/${instance.id}/files/delete`, {
        paths: targets.map((e) => e.path),
        recursive,
      });
      if (isStillRunning(res)) toast.info(res.message);
      else toast.success(res.deleted.length === 1 ? `Deleted ${baseName(res.deleted[0])}` : `Deleted ${res.deleted.length} items`);
      setSel({ path, set: EMPTY });
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      refreshDir(path);
    }
  };

  const onSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "name" ? "asc" : "desc" }));

  const listingError = listing.error instanceof ApiError ? listing.error : null;

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      {inWorld && (
        <div className="flex gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-100" role="note">
          <Globe2 className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
          <div>
            <p className="font-medium">You are inside world data</p>
            <p className="mt-0.5 text-xs text-amber-800/90 dark:text-amber-200/90">
              {live
                ? "The server is running: changing, deleting or uploading files here can corrupt the world. Stop the server and take a backup first."
                : "Changing, deleting or uploading files here can corrupt the world. Take a backup before making changes."}
            </p>
          </div>
        </div>
      )}

      <DropZone
        enabled={canWrite && !listing.error}
        target={path}
        onFiles={(files) => uploads.start(path, files, existingNames)}
        className="rounded-xl border bg-card text-card-foreground"
      >
        <div className="flex flex-col gap-3 border-b px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="Parent folder"
              disabled={!path}
              onClick={() => go(parentPath(path))}
            >
              <ArrowUp />
            </Button>
            <PathBreadcrumbs path={path} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <InputGroup className="h-8 w-full sm:w-56">
              <InputGroupAddon>
                <Search aria-hidden />
              </InputGroupAddon>
              <InputGroupInput
                aria-label="Filter this folder by name"
                placeholder="Filter this folder"
                value={filter}
                onChange={(e) => setFilterState({ path, value: e.target.value })}
              />
            </InputGroup>
            <Button variant="outline" size="sm" onClick={() => setSearchOpen(true)}>
              <FileSearch /> <span className="sm:hidden">Search</span>
              <span className="hidden sm:inline">Search contents</span>
            </Button>
            <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
              {listing.error || entries.length === 0 ? (
                <Button variant="outline" size="sm" disabled aria-label="Download this folder as a .zip">
                  <FolderArchive /> <span className="hidden sm:inline">Download .zip</span>
                </Button>
              ) : (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button asChild variant="outline" size="sm">
                      <DownloadLink
                        href={folderZipHref}
                        download={folderZipName}
                        aria-label={path ? `Download the folder ${baseName(path)} as a .zip` : "Download the whole server folder as a .zip"}
                      >
                        <FolderArchive /> <span className="hidden sm:inline">Download .zip</span>
                      </DownloadLink>
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-64">
                    {path ? "This folder and everything inside it" : "The whole server folder"}, zipped on the fly.
                    {live ? " The server is running, so files that change during the download may be inconsistent." : ""}
                  </TooltipContent>
                </Tooltip>
              )}
              {canWrite && (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setNewFolder(true)}
                    disabled={!!listing.error}
                    aria-label="New folder"
                  >
                    <FolderPlus /> <span className="hidden sm:inline">New folder</span>
                  </Button>
                  <Button size="sm" onClick={() => uploadInput.current?.click()} disabled={!!listing.error}>
                    <Upload /> Upload
                  </Button>
                  <input
                    ref={uploadInput}
                    type="file"
                    multiple
                    hidden
                    aria-hidden
                    tabIndex={-1}
                    onChange={(e) => {
                      const files = Array.from(e.target.files ?? []);
                      e.target.value = "";
                      uploads.start(path, files, existingNames);
                    }}
                  />
                </>
              )}
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Refresh listing"
                onClick={() => listing.refetch()}
                disabled={listing.isFetching}
              >
                <RefreshCw className={cn(listing.isFetching && "animate-spin")} />
              </Button>
            </div>
          </div>
        </div>

        {uploads.items.length > 0 && (
          <div className="border-b px-4 py-3">
            <UploadList items={uploads.items} onDismiss={uploads.dismiss} />
          </div>
        )}

        {canWrite && selectedEntries.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b bg-primary/5 px-4 py-2 text-sm" role="region" aria-label="Selection">
            <span className="font-medium">{selectedEntries.length} selected</span>
            <Button variant="ghost" size="xs" onClick={() => setSel({ path, set: EMPTY })}>
              <X /> Clear
            </Button>
            <Button variant="destructive" size="sm" className="ml-auto" onClick={() => setDeleteTargets(selectedEntries)}>
              <Trash2 /> Delete selected
            </Button>
          </div>
        )}

        {listing.data?.truncated && (
          <CutOffNotice className="mx-4 mt-3">
            List cut off at {entries.length.toLocaleString()} entries: this folder holds more than the node lists at once. Open a
            subfolder, or use Search contents to find a file.
          </CutOffNotice>
        )}
        {listing.isLoading ? (
          <LoadingState label="Loading folder…" />
        ) : listing.error ? (
          <div className="p-4">
            {listingError?.code === "not_a_directory" ? (
              <EmptyState
                icon={<FolderX className="size-5" />}
                title="This path is a file, not a folder"
                description={<span className="font-mono">/{path}</span>}
                action={
                  <div className="flex flex-wrap justify-center gap-2">
                    <Button size="sm" onClick={() => openEditor(path)}>
                      Open in editor
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => go(parentPath(path))}>
                      Go to containing folder
                    </Button>
                  </div>
                }
              />
            ) : listingError?.code === "not_found" ? (
              <EmptyState
                icon={<FolderX className="size-5" />}
                title="Folder not found"
                description={
                  <>
                    <span className="font-mono">/{path}</span> does not exist
                    {path ? " (it may have been moved or deleted)" : ". The server has probably not been installed yet"}.
                  </>
                }
                action={
                  path ? (
                    <Button size="sm" variant="outline" onClick={() => go("")}>
                      Go to server root
                    </Button>
                  ) : undefined
                }
              />
            ) : (
              <ErrorState error={listing.error} title="Could not list this folder" onRetry={() => listing.refetch()} />
            )}
          </div>
        ) : entries.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="This folder is empty"
              description={canWrite ? "Drop files here or use Upload to add files." : undefined}
            />
          </div>
        ) : visible.length === 0 ? (
          <div className="p-4">
            <EmptyState
              icon={<Search className="size-5" />}
              title="No matching items"
              description={
                <>
                  Nothing in this folder matches “{filter}”. To search inside files, use{" "}
                  <button type="button" className="text-primary hover:underline" onClick={() => setSearchOpen(true)}>
                    Search contents
                  </button>
                  .
                </>
              }
            />
          </div>
        ) : (
          <FileTable
            entries={visible}
            selected={selected}
            onSelectedChange={(update) => setSel((s) => ({ path, set: update(s.path === path ? s.set : EMPTY) }))}
            canWrite={canWrite}
            levelName={levelName}
            sort={sort}
            onSort={onSort}
            actions={actions}
            now={listing.dataUpdatedAt}
          />
        )}

        {listing.data && entries.length > 0 && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t px-4 py-2 text-xs text-muted-foreground">
            <span>
              {dirCount} folder{dirCount === 1 ? "" : "s"}, {fileCount} file{fileCount === 1 ? "" : "s"}
              {filter && ` · ${visible.length} shown`}
            </span>
            <span>{formatBytes(totalBytes)} in files directly in this folder</span>
            {canWrite && <span className="ml-auto hidden sm:inline">Drag files onto the list to upload here</span>}
          </div>
        )}
      </DropZone>

      {editor && (
        <FileEditorDialog
          key={editor.path}
          instance={instance}
          target={editor}
          canWrite={canWrite}
          onClose={() => setEditor(null)}
        />
      )}

      <FileSearchSheet
        instanceId={instance.id}
        scope={path}
        open={searchOpen}
        onOpenChange={setSearchOpen}
        onOpenResult={(p, line) => {
          setSearchOpen(false);
          openEditor(p, line);
        }}
      />

      {newFolder && (
        <NameDialog
          title="New folder"
          description={
            <>
              Create a folder in <span className="font-mono">/{path}</span>.
            </>
          }
          label="Folder name"
          initial=""
          confirmLabel="Create folder"
          help="Use slashes (a/b) to create nested folders."
          onClose={() => setNewFolder(false)}
          onSubmit={async (name) => {
            const target = joinPath(path, name);
            await api.post(`/instances/${instance.id}/files/mkdir`, { path: target, parents: name.includes("/") });
            toast.success(`Created ${target}`);
            refreshDir(path);
          }}
        />
      )}

      {renameTarget && (
        <NameDialog
          title={`Rename ${renameTarget.type === "directory" ? "folder" : "file"}`}
          description={
            <>
              Currently <span className="font-mono">/{renameTarget.path}</span>
            </>
          }
          label="New name"
          initial={renameTarget.name}
          confirmLabel="Rename"
          help={
            <>
              To move it, enter a path starting with <span className="font-mono">/</span> (relative to the server folder), e.g.{" "}
              <span className="font-mono">/config/{renameTarget.name}</span>.
            </>
          }
          onClose={() => setRenameTarget(null)}
          onSubmit={async (value) => {
            const source = renameTarget.path;
            const destination = value.startsWith("/") ? normalizePath(value) : joinPath(parentPath(source), value);
            if (!destination) throw new Error("Choose a name inside the server folder");
            if (destination === source) return;
            try {
              await doRename(source, destination);
            } catch (err) {
              if (err instanceof ApiError && err.status === 409 && err.code === "exists") {
                setOverwritePrompt({ source, destination });
                return;
              }
              throw err;
            }
          }}
        />
      )}

      <ConfirmDialog
        open={!!overwritePrompt}
        onOpenChange={(o) => !o && setOverwritePrompt(null)}
        title="Replace the existing item?"
        description={
          <>
            <span className="font-mono">/{overwritePrompt?.destination}</span> already exists. Replacing it deletes the current
            one permanently.
          </>
        }
        confirmLabel="Replace"
        destructive
        onConfirm={async () => {
          if (!overwritePrompt) return;
          try {
            await doRename(overwritePrompt.source, overwritePrompt.destination, true);
          } catch (err) {
            toast.error(errorMessage(err));
          }
        }}
      />

      <DeleteConfirm
        targets={deleteTargets}
        levelName={levelName}
        inWorld={inWorld}
        live={live}
        onClose={() => setDeleteTargets(null)}
        onConfirm={doDelete}
      />

      {extractTarget && (
        <ExtractDialog instance={instance} entry={extractTarget} dir={path} onClose={() => setExtractTarget(null)} />
      )}

      <UploadConflictDialog conflict={uploads.conflict} onResolve={uploads.resolve} />
    </div>
  );
}

function DeleteConfirm({
  targets,
  levelName,
  inWorld,
  live,
  onClose,
  onConfirm,
}: {
  targets: FileEntry[] | null;
  levelName: string;
  inWorld: boolean;
  live: boolean;
  onClose: () => void;
  onConfirm: (targets: FileEntry[]) => Promise<void>;
}) {
  const list = targets ?? [];
  const dirs = list.filter((e) => e.type === "directory");
  const worlds = list.filter((e) => isWorldEntry(e, levelName));
  const single = list.length === 1 ? list[0] : null;
  return (
    <ConfirmDialog
      open={!!targets}
      onOpenChange={(o) => !o && onClose()}
      title={single ? `Delete ${single.name}?` : `Delete ${list.length} items?`}
      confirmLabel="Delete permanently"
      destructive
      confirmText={dirs.length ? "delete" : undefined}
      description={
        <div className="grid gap-2 text-sm">
          {!single && (
            <ul className="max-h-32 overflow-auto rounded border bg-muted/40 px-2 py-1 font-mono text-xs">
              {list.map((e) => (
                <li key={e.path} className="truncate">
                  {e.name}
                  {e.type === "directory" ? "/" : ""}
                </li>
              ))}
            </ul>
          )}
          {dirs.length > 0 && (
            <p className="flex gap-2 text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                {dirs.length === 1 ? "The folder" : `${dirs.length} folders`} and everything inside{" "}
                {dirs.length === 1 ? "it" : "them"} will be deleted recursively.
              </span>
            </p>
          )}
          {inWorld && worlds.length === 0 && <p>These are part of the world save; removing them can corrupt or reset the world.</p>}
          {worlds.length > 0 && (
            <p>
              This includes world data (<span className="font-mono">{worlds.map((w) => w.name).join(", ")}</span>). The world is
              lost unless you restore it from a backup.
            </p>
          )}
          {live && <p>The server is running — removing files it uses can crash it or corrupt data.</p>}
          <p className="text-muted-foreground">Deleted files cannot be recovered except from a backup.</p>
        </div>
      }
      onConfirm={async () => {
        if (targets) await onConfirm(targets);
      }}
    />
  );
}
