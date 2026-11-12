/** Directory listing table: selection, sortable columns and per-row actions. */
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Copy,
  Download,
  File,
  FileArchive,
  FileCode2,
  FileText,
  Folder,
  FolderOpen,
  Globe2,
  Link2,
  MoreHorizontal,
  PackageOpen,
  Pencil,
  SquarePen,
  Trash2,
} from "lucide-react";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { errorMessage } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { startDownload } from "@/lib/downloads";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import type { FileEntry } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  canTryAsText,
  hasInvalidName,
  INVALID_NAME_HINT,
  isArchive,
  isTextEditable,
  isWorldEntry,
  typeLabel,
  type SortDir,
  type SortKey,
} from "./file-utils";

/** A menu item's download: a ticket first, then the browser downloads it (lib/downloads.ts, D-1). */
function download(url: string, name: string): void {
  startDownload(url, name).catch((err: unknown) => toast.error(`Download failed: ${errorMessage(err)}`));
}

export interface FileActions {
  open: (entry: FileEntry) => void;
  edit: (entry: FileEntry) => void;
  rename: (entry: FileEntry) => void;
  remove: (entries: FileEntry[]) => void;
  extract: (entry: FileEntry) => void;
  downloadHref: (entry: FileEntry) => string;
  /** Zip of a folder, built on the fly by the agent (GET /files/archive). */
  archiveHref: (entry: FileEntry) => string;
}

function EntryIcon({ entry, world }: { entry: FileEntry; world: boolean }) {
  const cls = "size-4 shrink-0";
  if (entry.type === "directory")
    return world ? (
      <Globe2 className={cn(cls, "text-emerald-600 dark:text-emerald-400")} aria-hidden />
    ) : (
      <Folder className={cn(cls, "text-sky-600 dark:text-sky-400")} aria-hidden />
    );
  if (entry.type === "symlink") return <Link2 className={cn(cls, "text-violet-600 dark:text-violet-400")} aria-hidden />;
  if (isTextEditable(entry)) {
    const code = /\.(json5?|toml|ya?ml|js|zs|xml|sh|snbt|mcmeta)$/i.test(entry.name);
    return code ? (
      <FileCode2 className={cn(cls, "text-muted-foreground")} aria-hidden />
    ) : (
      <FileText className={cn(cls, "text-muted-foreground")} aria-hidden />
    );
  }
  return <File className={cn(cls, "text-muted-foreground")} aria-hidden />;
}

function SortHeader({
  label,
  column,
  sort,
  onSort,
  className,
}: {
  label: string;
  column: SortKey;
  sort: { key: SortKey; dir: SortDir };
  onSort: (key: SortKey) => void;
  className?: string;
}) {
  const active = sort.key === column;
  return (
    <th
      scope="col"
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      className={cn("h-9 px-2 text-left align-middle text-xs font-medium whitespace-nowrap text-muted-foreground", className)}
    >
      <button
        type="button"
        onClick={() => onSort(column)}
        className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none pointer-coarse:py-2"
      >
        {label}
        {active && (sort.dir === "asc" ? <ArrowUp className="size-3" aria-hidden /> : <ArrowDown className="size-3" aria-hidden />)}
      </button>
    </th>
  );
}

function NameCell({ entry, world, actions }: { entry: FileEntry; world: boolean; actions: FileActions }) {
  const icon = <EntryIcon entry={entry} world={world} />;
  if (hasInvalidName(entry)) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex min-w-0 items-center gap-2 rounded" tabIndex={0}>
            {icon}
            <span className="truncate max-sm:line-clamp-2 max-sm:break-all max-sm:whitespace-normal" title={entry.name}>
              {entry.name}
            </span>
            <AlertTriangle className="size-3.5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
            <span className="sr-only">(name is not valid UTF-8; no actions available)</span>
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-72">{INVALID_NAME_HINT}</TooltipContent>
      </Tooltip>
    );
  }
  if (entry.type === "symlink") {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex min-w-0 items-center gap-2 rounded" tabIndex={0}>
            {icon}
            <span className="truncate max-sm:line-clamp-2 max-sm:break-all max-sm:whitespace-normal" title={entry.name}>
              {entry.name}
            </span>
            <span className="sr-only">(symbolic link, not followed)</span>
          </span>
        </TooltipTrigger>
        <TooltipContent>Symbolic link — not followed for safety. It can be renamed or deleted, not opened.</TooltipContent>
      </Tooltip>
    );
  }
  const action =
    entry.type === "directory" ? () => actions.open(entry) : isTextEditable(entry) ? () => actions.edit(entry) : null;
  if (!action) {
    return (
      <span className="inline-flex min-w-0 items-center gap-2">
        {icon}
        <span className="truncate max-sm:line-clamp-2 max-sm:break-all max-sm:whitespace-normal" title={entry.name}>
          {entry.name}
        </span>
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={action}
      className="inline-flex max-w-full min-w-0 items-center gap-2 rounded text-left hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none pointer-coarse:-my-1.5 pointer-coarse:min-h-8 pointer-coarse:py-1.5"
    >
      {icon}
      <span className="truncate max-sm:line-clamp-2 max-sm:break-all max-sm:whitespace-normal" title={entry.name}>
        {entry.name}
      </span>
    </button>
  );
}

function RowMenu({ entry, canWrite, actions }: { entry: FileEntry; canWrite: boolean; actions: FileActions }) {
  if (hasInvalidName(entry)) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          {/* a disabled button gets no pointer events: the wrapper carries the tooltip */}
          <span tabIndex={0} className="inline-flex rounded" aria-label={`No actions for ${entry.name}: ${INVALID_NAME_HINT}`}>
            <Button variant="ghost" size="icon-sm" disabled aria-hidden tabIndex={-1}>
              <MoreHorizontal />
            </Button>
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-72">{INVALID_NAME_HINT}</TooltipContent>
      </Tooltip>
    );
  }
  const items: ReactNode[] = [];
  if (entry.type === "directory") {
    items.push(
      <DropdownMenuItem key="open" onSelect={() => actions.open(entry)}>
        <FolderOpen /> Open
      </DropdownMenuItem>,
      <DropdownMenuItem key="zip" onSelect={() => download(actions.archiveHref(entry), `${entry.name}.zip`)}>
        <FileArchive /> Download as .zip
      </DropdownMenuItem>,
    );
  }
  if (entry.type === "file") {
    if (isTextEditable(entry)) {
      items.push(
        <DropdownMenuItem key="edit" onSelect={() => actions.edit(entry)}>
          <SquarePen /> {canWrite ? "Edit" : "View"}
        </DropdownMenuItem>,
      );
    } else if (canTryAsText(entry)) {
      items.push(
        <DropdownMenuItem key="astext" onSelect={() => actions.edit(entry)}>
          <FileText /> Open as text
        </DropdownMenuItem>,
      );
    }
    items.push(
      <DropdownMenuItem key="download" onSelect={() => download(actions.downloadHref(entry), entry.name)}>
        <Download /> Download
      </DropdownMenuItem>,
    );
  }
  items.push(
    <DropdownMenuItem
      key="copy"
      onSelect={() => {
        // works over plain HTTP too (no async Clipboard API there)
        void copyText(entry.path).then((ok) => (ok ? toast.success("Path copied") : toast.error("Could not copy to the clipboard")));
      }}
    >
      <Copy /> Copy path
    </DropdownMenuItem>,
  );
  if (canWrite) {
    if (isArchive(entry)) {
      items.push(
        <DropdownMenuItem key="extract" onSelect={() => actions.extract(entry)}>
          <PackageOpen /> Extract here…
        </DropdownMenuItem>,
      );
    }
    items.push(
      <DropdownMenuSeparator key="sep" />,
      <DropdownMenuItem key="rename" onSelect={() => actions.rename(entry)}>
        <Pencil /> Rename / move…
      </DropdownMenuItem>,
      <DropdownMenuItem key="delete" variant="destructive" onSelect={() => actions.remove([entry])}>
        <Trash2 /> Delete…
      </DropdownMenuItem>,
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${entry.name}`}>
          <MoreHorizontal />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-w-72 min-w-44">
        <DropdownMenuLabel className="font-mono text-xs font-normal break-all text-muted-foreground">{entry.name}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {items}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function FileTable({
  entries,
  selected,
  onSelectedChange,
  canWrite,
  levelName,
  sort,
  onSort,
  actions,
  now,
}: {
  entries: FileEntry[];
  selected: Set<string>;
  onSelectedChange: (update: (prev: Set<string>) => Set<string>) => void;
  canWrite: boolean;
  levelName?: string;
  sort: { key: SortKey; dir: SortDir };
  onSort: (key: SortKey) => void;
  actions: FileActions;
  now: number;
}) {
  // entries whose real name the agent could not give cannot be deleted, so they are never selected
  const selectable = entries.filter((e) => !hasInvalidName(e));
  const allSelected = selectable.length > 0 && selectable.every((e) => selected.has(e.path));
  const someSelected = !allSelected && selectable.some((e) => selected.has(e.path));
  const toggle = (path: string, on: boolean) =>
    onSelectedChange((prev) => {
      const next = new Set(prev);
      if (on) next.add(path);
      else next.delete(path);
      return next;
    });
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b bg-muted/30">
          <tr>
            {canWrite && (
              <th scope="col" className="h-9 w-9 pr-0 pl-4 align-middle">
                <Checkbox
                  aria-label="Select all items in this folder"
                  checked={allSelected ? true : someSelected ? "indeterminate" : false}
                  onCheckedChange={(v) => onSelectedChange(() => (v === true ? new Set(selectable.map((e) => e.path)) : new Set()))}
                  disabled={selectable.length === 0}
                />
              </th>
            )}
            <SortHeader label="Name" column="name" sort={sort} onSort={onSort} className={cn(!canWrite && "pl-3")} />
            <SortHeader label="Size" column="size" sort={sort} onSort={onSort} className="w-24 text-right [&>button]:ml-auto [&>button]:flex" />
            <SortHeader label="Modified" column="modified" sort={sort} onSort={onSort} className="hidden w-40 md:table-cell" />
            <th scope="col" className="hidden h-9 w-36 px-2 text-left align-middle text-xs font-medium text-muted-foreground lg:table-cell">
              Type
            </th>
            <th scope="col" className="h-9 w-12 px-2">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => {
            const isSel = selected.has(e.path);
            const world = isWorldEntry(e, levelName);
            return (
              <tr
                key={e.path}
                data-state={isSel ? "selected" : undefined}
                className="border-b last:border-b-0 hover:bg-muted/40 data-[state=selected]:bg-primary/5"
              >
                {canWrite && (
                  <td className="w-9 py-1.5 pr-0 pl-4 align-middle">
                    <Checkbox
                      aria-label={`Select ${e.name}`}
                      checked={isSel}
                      onCheckedChange={(v) => toggle(e.path, v === true)}
                      disabled={hasInvalidName(e)}
                      title={hasInvalidName(e) ? INVALID_NAME_HINT : undefined}
                    />
                  </td>
                )}
                <td className={cn("max-w-0 px-2 py-1.5 align-middle", !canWrite && "pl-4")}>
                  <div className="flex min-w-0 items-center gap-2">
                    <NameCell entry={e} world={world} actions={actions} />
                    {world && (
                      <span className="hidden shrink-0 rounded bg-emerald-500/10 px-1.5 py-px text-[10px] font-medium text-emerald-700 sm:inline dark:text-emerald-300">
                        world
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-2 py-1.5 text-right align-middle text-xs whitespace-nowrap text-muted-foreground tabular-nums">
                  {e.type === "file" ? formatBytes(e.size) : "—"}
                </td>
                <td
                  className="hidden px-2 py-1.5 align-middle text-xs whitespace-nowrap text-muted-foreground md:table-cell"
                  title={formatDateTime(e.modified)}
                >
                  {relativeTime(e.modified, now)}
                </td>
                <td className="hidden px-2 py-1.5 align-middle text-xs whitespace-nowrap text-muted-foreground lg:table-cell">
                  {typeLabel(e)}
                </td>
                <td className="px-2 py-1 text-right align-middle">
                  <RowMenu entry={e} canWrite={canWrite} actions={actions} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
