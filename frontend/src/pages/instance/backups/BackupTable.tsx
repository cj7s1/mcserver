/** Compact table of an instance's backups with per-row actions. */
import {
  AlertTriangle,
  CheckCircle2,
  CopyPlus,
  FolderOpen,
  Loader2,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  RotateCcw,
  ShieldCheck,
  ShieldQuestion,
  ShieldX,
  StickyNote,
  Trash2,
  XCircle,
} from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatBytes, formatDateTime, formatDuration, relativeTime } from "@/lib/format";
import type { Backup } from "@/lib/types";
import { cn } from "@/lib/utils";
import { KindBadge } from "./KindBadge";

export type BackupAction = "restore" | "restore-new" | "browse" | "edit" | "delete";

export interface BackupPermissions {
  backup: boolean;
  restore: boolean;
  files: boolean;
  createInstance: boolean;
}

function Tip({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-xs">{label}</TooltipContent>
    </Tooltip>
  );
}

export function ConsistencyBadge({ consistent }: { consistent: boolean }) {
  return (
    <Tip
      label={
        consistent
          ? "World saving was paused and flushed (save-off / save-all flush), or the server was stopped, so the world files were captured in a consistent state."
          : "Files were copied while the server may have been writing them (world saving could not be paused). Restoring is like recovering from a power cut: usually fine, but the most recent chunks may be lost or damaged."
      }
    >
      <span
        tabIndex={0}
        className={cn(
          "inline-flex h-5 cursor-help items-center rounded-full px-2 text-[11px] font-medium whitespace-nowrap outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
          consistent
            ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
            : "bg-amber-500/10 text-amber-700 dark:text-amber-300",
        )}
      >
        {consistent ? "consistent" : "crash-consistent"}
      </span>
    </Tip>
  );
}

function VerifiedCell({ b }: { b: Backup }) {
  if (b.status !== "succeeded") return <span className="text-muted-foreground">—</span>;
  if (b.verify_ok === null || !b.verified_at)
    return (
      <span className="inline-flex items-center gap-1 text-muted-foreground">
        <ShieldQuestion className="size-3.5" aria-hidden /> Not verified
      </span>
    );
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1",
        b.verify_ok ? "text-emerald-700 dark:text-emerald-300" : "text-destructive",
      )}
      title={formatDateTime(b.verified_at)}
    >
      {b.verify_ok ? <ShieldCheck className="size-3.5" aria-hidden /> : <ShieldX className="size-3.5" aria-hidden />}
      {b.verify_ok ? "Verified" : "Check failed"}
      <span className="text-muted-foreground">· {relativeTime(b.verified_at)}</span>
    </span>
  );
}

function StatusCell({ b }: { b: Backup }) {
  const warnings = b.warnings ?? [];
  return (
    <div className="flex items-center gap-1.5">
      {b.status === "running" ? (
        <span className="inline-flex items-center gap-1 text-primary">
          <Loader2 className="size-3.5 animate-spin" aria-hidden /> Running
        </span>
      ) : b.status === "failed" ? (
        <Tip label={b.error || "The backup failed."}>
          <span tabIndex={0} className="inline-flex cursor-help items-center gap-1 text-destructive outline-none focus-visible:underline">
            <XCircle className="size-3.5" aria-hidden /> Failed
          </span>
        </Tip>
      ) : (
        <span className="inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-300">
          <CheckCircle2 className="size-3.5" aria-hidden /> OK
        </span>
      )}
      {warnings.length > 0 && (
        <Tip
          label={
            <ul className="grid gap-1">
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          }
        >
          <button
            type="button"
            className="inline-flex items-center gap-0.5 rounded text-amber-700 outline-none focus-visible:ring-3 focus-visible:ring-ring/50 dark:text-amber-300"
            aria-label={`${warnings.length} warning${warnings.length === 1 ? "" : "s"}: ${warnings.join("; ")}`}
          >
            <AlertTriangle className="size-3.5" aria-hidden />
            <span className="text-[11px] tabular-nums">{warnings.length}</span>
          </button>
        </Tip>
      )}
    </div>
  );
}

export function BackupTable({
  backups,
  perms,
  busy,
  pinPending,
  onTogglePin,
  onAction,
}: {
  backups: Backup[];
  perms: BackupPermissions;
  /** another operation is running on the instance (jobs are exclusive) */
  busy: boolean;
  pinPending: string | null;
  onTogglePin: (b: Backup) => void;
  onAction: (action: BackupAction, b: Backup) => void;
}) {
  return (
    <Table className="text-xs">
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="w-8 px-2">
            <span className="sr-only">Pinned</span>
          </TableHead>
          <TableHead className="min-w-36 sm:min-w-44">Backup</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="hidden md:table-cell">Created</TableHead>
          <TableHead className="hidden text-right sm:table-cell">
            <Tip label="Added: new data this backup stored after deduplication. Source: total size of the server files it covered.">
              <span
                tabIndex={0}
                className="cursor-help rounded-sm underline decoration-dotted underline-offset-2 outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
              >
                Size
              </span>
            </Tip>
          </TableHead>
          <TableHead className="hidden text-right lg:table-cell">Files · time</TableHead>
          <TableHead className="hidden md:table-cell">Integrity</TableHead>
          <TableHead className="text-right">
            <span className="sr-only">Actions</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {backups.map((b) => {
          const ok = b.status === "succeeded";
          return (
            <TableRow key={b.id} className={cn(b.pinned && "bg-primary/[0.03]")}>
              <TableCell className="px-2">
                {perms.restore ? (
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    aria-pressed={b.pinned}
                    aria-label={b.pinned ? `Unpin ${b.name}` : `Pin ${b.name} (never removed by retention)`}
                    title={b.pinned ? "Pinned: never removed by retention. Click to unpin." : "Pin: keep forever, ignore retention"}
                    disabled={pinPending === b.id || b.status === "running"}
                    onClick={() => onTogglePin(b)}
                    className={b.pinned ? "text-primary" : "text-muted-foreground hover:text-foreground"}
                  >
                    {pinPending === b.id ? <Loader2 className="animate-spin" /> : <Pin className={b.pinned ? "fill-current" : ""} />}
                  </Button>
                ) : b.pinned ? (
                  <Pin className="mx-auto size-3 fill-current text-primary" aria-label="Pinned" />
                ) : null}
              </TableCell>
              <TableCell className="max-w-72 min-w-36 whitespace-normal sm:min-w-44">
                <div className="flex min-w-0 items-center gap-1.5">
                  <span className="min-w-0 font-medium text-sm [overflow-wrap:anywhere] sm:truncate" title={b.name}>
                    {b.name}
                  </span>
                  {b.notes && (
                    <Tip label={<span className="whitespace-pre-wrap">{b.notes}</span>}>
                      <button
                        type="button"
                        className="shrink-0 rounded text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
                        aria-label={`Notes: ${b.notes}`}
                      >
                        <StickyNote className="size-3.5" aria-hidden />
                      </button>
                    </Tip>
                  )}
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-muted-foreground">
                  <KindBadge kind={b.kind} />
                  {b.created_by_name && <span>by {b.created_by_name}</span>}
                  {b.snapshot_id && (
                    <code className="font-mono text-[10px]" title={`restic snapshot ${b.snapshot_id}`}>
                      {b.snapshot_id.slice(0, 8)}
                    </code>
                  )}
                </div>
                <div className="mt-0.5 text-muted-foreground md:hidden" title={formatDateTime(b.created_at)}>
                  {relativeTime(b.created_at)}
                  {ok && <span className="sm:hidden"> · {formatBytes(b.total_bytes)}</span>}
                </div>
              </TableCell>
              <TableCell>
                <StatusCell b={b} />
              </TableCell>
              <TableCell className="hidden whitespace-nowrap md:table-cell" title={formatDateTime(b.created_at)}>
                <div>{relativeTime(b.created_at)}</div>
                <div className="text-muted-foreground">{formatDateTime(b.created_at)}</div>
              </TableCell>
              <TableCell className="hidden text-right whitespace-nowrap tabular-nums sm:table-cell">
                {ok ? (
                  <>
                    <div>
                      +{formatBytes(b.data_added_bytes)} <span className="text-muted-foreground">added</span>
                    </div>
                    <div className="text-muted-foreground">{formatBytes(b.total_bytes)} source size</div>
                  </>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="hidden text-right whitespace-nowrap tabular-nums lg:table-cell">
                {ok ? (
                  <>
                    <div>{b.files_count?.toLocaleString() ?? "—"} files</div>
                    <div className="text-muted-foreground" title="Backup duration">
                      {formatDuration(b.duration_seconds)}
                    </div>
                  </>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="hidden whitespace-nowrap md:table-cell">
                {ok ? (
                  <div className="grid justify-items-start gap-1">
                    <ConsistencyBadge consistent={b.consistent} />
                    <VerifiedCell b={b} />
                  </div>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="text-right">
                <div className="flex items-center justify-end gap-1">
                  {perms.restore && ok && (
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={busy}
                      onClick={() => onAction("restore", b)}
                      aria-label={`Restore ${b.name}`}
                    >
                      <RotateCcw /> <span className="hidden 2xl:inline">Restore</span>
                    </Button>
                  )}
                  {perms.files && ok && (
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      className="hidden sm:inline-flex"
                      onClick={() => onAction("browse", b)}
                      aria-label={`Browse files in ${b.name}`}
                      title="Browse files"
                    >
                      <FolderOpen />
                    </Button>
                  )}
                  <DropdownMenu modal={false}>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon-xs" aria-label={`More actions for ${b.name}`}>
                        <MoreHorizontal />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="w-60">
                      <DropdownMenuLabel className="truncate text-xs text-muted-foreground">{b.name}</DropdownMenuLabel>
                      {perms.restore && ok && (
                        <DropdownMenuItem disabled={busy} onSelect={() => onAction("restore", b)}>
                          <RotateCcw /> Restore over this server…
                        </DropdownMenuItem>
                      )}
                      {perms.restore && perms.createInstance && ok && (
                        <DropdownMenuItem disabled={busy} onSelect={() => onAction("restore-new", b)}>
                          <CopyPlus /> Restore as a new server…
                        </DropdownMenuItem>
                      )}
                      {perms.files && ok && (
                        <DropdownMenuItem onSelect={() => onAction("browse", b)}>
                          <FolderOpen /> Browse files…
                        </DropdownMenuItem>
                      )}
                      {perms.backup && (
                        <DropdownMenuItem onSelect={() => onAction("edit", b)}>
                          <Pencil /> Edit name & notes…
                        </DropdownMenuItem>
                      )}
                      {perms.restore && b.status !== "running" && (
                        <>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem
                            variant="destructive"
                            disabled={b.pinned || busy}
                            onSelect={() => onAction("delete", b)}
                          >
                            {b.pinned ? <PinOff /> : <Trash2 />}
                            {b.pinned ? "Unpin to delete" : "Delete backup…"}
                          </DropdownMenuItem>
                        </>
                      )}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
