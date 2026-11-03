import { useInfiniteQuery } from "@tanstack/react-query";
import { CheckCircle2, ChevronDown, ChevronRight, Loader2, RefreshCw, ScrollText, X, XCircle } from "lucide-react";
import { Fragment, useState } from "react";
import { Link } from "react-router";
import { PageHeader, Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAuth } from "@/hooks/use-auth";
import { api } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { AuditEvent } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AdminNoPermission } from "./admin-shared";
import { useDebouncedValue } from "./admin-utils";

/** Action prefixes recorded by the backend (apps/*: audit.record("<prefix>.<verb>")). */
const ACTION_PREFIXES: { value: string; label: string }[] = [
  { value: "auth.", label: "Authentication" },
  { value: "instance.", label: "Servers" },
  { value: "files.", label: "Files" },
  { value: "backup.", label: "Backups" },
  { value: "snapshot.", label: "Snapshots" },
  { value: "content.", label: "Mods & plugins" },
  { value: "modpack.", label: "Modpacks" },
  { value: "players.", label: "Players" },
  { value: "schedule.", label: "Schedules" },
  { value: "user.", label: "Users" },
  { value: "role.", label: "Roles" },
  { value: "system.", label: "System settings" },
  { value: "node.", label: "Nodes" },
  { value: "notifications.", label: "Notifications" },
  { value: "job.", label: "Jobs" },
];

const ALL = "all";
const PAGE_SIZES = [50, 100, 200];

const CATEGORY_STYLE: Record<string, string> = {
  auth: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  instance: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  files: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  backup: "bg-violet-500/10 text-violet-700 dark:text-violet-300",
  snapshot: "bg-violet-500/10 text-violet-700 dark:text-violet-300",
  content: "bg-teal-500/10 text-teal-700 dark:text-teal-300",
  modpack: "bg-teal-500/10 text-teal-700 dark:text-teal-300",
  players: "bg-lime-500/10 text-lime-700 dark:text-lime-300",
  schedule: "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300",
  user: "bg-rose-500/10 text-rose-700 dark:text-rose-300",
  role: "bg-rose-500/10 text-rose-700 dark:text-rose-300",
  system: "bg-orange-500/10 text-orange-700 dark:text-orange-300",
};

function categoryOf(action: string): string {
  return action.split(".")[0] ?? action;
}

export default function AuditPage() {
  const { can } = useAuth();
  const allowed = can("system.audit");
  const [action, setAction] = useState(ALL);
  const [actorInput, setActorInput] = useState("");
  const [pageSize, setPageSize] = useState(100);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const actor = useDebouncedValue(actorInput.trim(), 400);

  const q = useInfiniteQuery({
    queryKey: [...qk.audit, "global", { action, actor, pageSize }],
    queryFn: ({ pageParam, signal }) =>
      api.get<AuditEvent[]>(
        "/audit",
        { action: action === ALL ? undefined : action, actor: actor || undefined, limit: pageSize, before: pageParam },
        signal,
      ),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => (last.length < pageSize ? undefined : last[last.length - 1]?.id),
    enabled: allowed,
  });

  if (!allowed) return <AdminNoPermission permission="system.audit" what="The audit log" />;

  const events = q.data?.pages.flat() ?? [];
  const filtered = action !== ALL || !!actor;

  function toggle(id: number) {
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="grid grid-cols-1 gap-4">
      <PageHeader
        className="pb-0"
        icon={<ScrollText className="size-5 text-muted-foreground" />}
        title="Audit log"
        description="Every sign-in, configuration change and destructive action, newest first."
        actions={
          <Button variant="outline" size="sm" onClick={() => q.refetch()} disabled={q.isFetching}>
            <RefreshCw className={cn(q.isFetching && !q.isFetchingNextPage && "animate-spin")} /> Refresh
          </Button>
        }
      />

      <Section>
        <div className="grid gap-3 sm:grid-cols-[minmax(0,14rem)_minmax(0,14rem)_auto_1fr] sm:items-end">
          <div className="grid content-start gap-1.5">
            <Label htmlFor="audit-action">Action</Label>
            <Select value={action} onValueChange={setAction}>
              <SelectTrigger id="audit-action" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                <SelectItem value={ALL}>All actions</SelectItem>
                <SelectSeparator />
                {ACTION_PREFIXES.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                    <span className="font-mono text-xs text-muted-foreground">{p.value}*</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid content-start gap-1.5">
            <Label htmlFor="audit-actor">Actor username</Label>
            <Input
              id="audit-actor"
              value={actorInput}
              onChange={(e) => setActorInput(e.target.value)}
              placeholder="Exact username"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <div className="grid content-start gap-1.5">
            <Label htmlFor="audit-limit">Page size</Label>
            <Select value={String(pageSize)} onValueChange={(v) => setPageSize(Number(v))}>
              <SelectTrigger id="audit-limit" className="w-full sm:w-24">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {PAGE_SIZES.map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {n}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center justify-between gap-2 sm:justify-end">
            <span className="text-xs text-muted-foreground tabular-nums" aria-live="polite">
              {q.data ? `${events.length} event${events.length === 1 ? "" : "s"} loaded` : ""}
            </span>
            {filtered && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setAction(ALL);
                  setActorInput("");
                }}
              >
                <X /> Clear filters
              </Button>
            )}
          </div>
        </div>
      </Section>

      {q.isLoading ? (
        <LoadingState label="Loading audit log…" />
      ) : q.error && !q.data ? (
        <ErrorState error={q.error} title="Couldn't load the audit log" onRetry={() => q.refetch()} />
      ) : events.length === 0 ? (
        <EmptyState
          icon={<ScrollText className="size-5" />}
          title={filtered ? "No events match these filters" : "No audit events yet"}
          description={filtered ? "The actor filter matches the exact username." : undefined}
        />
      ) : (
        <section className="rounded-xl border bg-card">
          <Table className="text-[13px]">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-8">
                  <span className="sr-only">Details</span>
                </TableHead>
                <TableHead className="text-xs text-muted-foreground">Time</TableHead>
                <TableHead className="text-xs text-muted-foreground">Actor</TableHead>
                <TableHead className="text-xs text-muted-foreground">Action</TableHead>
                <TableHead className="text-xs text-muted-foreground">Target</TableHead>
                <TableHead className="text-xs text-muted-foreground">Server</TableHead>
                <TableHead className="text-xs text-muted-foreground">IP address</TableHead>
                <TableHead className="text-xs text-muted-foreground">Result</TableHead>
                <TableHead className="text-xs text-muted-foreground">Details</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((e) => (
                <AuditRow
                  key={e.id}
                  event={e}
                  open={expanded.has(e.id)}
                  onToggle={() => toggle(e.id)}
                  onFilterActor={(name) => setActorInput(name)}
                  onFilterAction={(prefix) => setAction(prefix)}
                />
              ))}
            </TableBody>
          </Table>
          <div className="flex items-center justify-center border-t p-3">
            {q.hasNextPage ? (
              <Button variant="outline" size="sm" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>
                {q.isFetchingNextPage && <Loader2 className="animate-spin" />}
                Load older events
              </Button>
            ) : (
              <span className="text-xs text-muted-foreground">Beginning of the log reached</span>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

function AuditRow({
  event: e,
  open,
  onToggle,
  onFilterActor,
  onFilterAction,
}: {
  event: AuditEvent;
  open: boolean;
  onToggle: () => void;
  onFilterActor: (name: string) => void;
  onFilterAction: (prefix: string) => void;
}) {
  const keys = Object.keys(e.details ?? {});
  const hasDetails = keys.length > 0;
  const category = categoryOf(e.action);
  const knownPrefix = ACTION_PREFIXES.find((p) => p.value === `${category}.`);
  const detailsId = `audit-details-${e.id}`;
  const preview = keys
    .slice(0, 2)
    .map((k) => {
      const v = e.details[k];
      const s = typeof v === "string" ? v : JSON.stringify(v);
      return `${k}=${s && s.length > 40 ? `${s.slice(0, 40)}…` : s}`;
    })
    .join(" · ");

  return (
    <Fragment>
      <TableRow className={cn(!e.success && "bg-destructive/5", open && "bg-muted/40")}>
        <TableCell className="py-1.5">
          {hasDetails && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-expanded={open}
              aria-controls={detailsId}
              aria-label={open ? "Hide details" : "Show details"}
              onClick={onToggle}
            >
              {open ? <ChevronDown /> : <ChevronRight />}
            </Button>
          )}
        </TableCell>
        <TableCell className="py-1.5 tabular-nums" title={relativeTime(e.created_at)}>
          {formatDateTime(e.created_at)}
        </TableCell>
        <TableCell className="py-1.5">
          {e.actor_username ? (
            <button
              type="button"
              className="rounded font-medium hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              onClick={() => onFilterActor(e.actor_username)}
              title={`Show only events by ${e.actor_username}`}
            >
              {e.actor_username}
            </button>
          ) : (
            <span className="text-muted-foreground italic" title="No signed-in user (system task or anonymous request)">
              system
            </span>
          )}
        </TableCell>
        <TableCell className="py-1.5">
          {knownPrefix ? (
            <button
              type="button"
              onClick={() => onFilterAction(knownPrefix.value)}
              title={`Show only ${knownPrefix.label.toLowerCase()} events`}
              className={cn(
                "inline-flex items-center rounded-md px-1.5 py-0.5 font-mono text-[11px] font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                CATEGORY_STYLE[category] ?? "bg-muted text-foreground",
              )}
            >
              {e.action}
            </button>
          ) : (
            <span className="inline-flex rounded-md bg-muted px-1.5 py-0.5 font-mono text-[11px] font-medium">
              {e.action}
            </span>
          )}
        </TableCell>
        <TableCell className="max-w-56 truncate py-1.5" title={e.target_id ? `${e.target_type} ${e.target_id}` : undefined}>
          {e.target_type ? (
            <>
              <span className="text-muted-foreground">{e.target_type}</span>{" "}
              <span className="font-mono text-xs">{shortId(e.target_id)}</span>
            </>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </TableCell>
        <TableCell className="max-w-44 truncate py-1.5">
          {e.instance ? (
            <Link
              to={`/servers/${e.instance}`}
              className="text-primary underline-offset-3 hover:underline"
              title={e.instance_name ?? e.instance}
            >
              {e.instance_name ?? shortId(e.instance)}
            </Link>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </TableCell>
        <TableCell className="py-1.5 font-mono text-xs text-muted-foreground">{e.ip_address ?? "—"}</TableCell>
        <TableCell className="py-1.5">
          {e.success ? (
            <span className="inline-flex items-center gap-1 text-xs text-emerald-700 dark:text-emerald-300">
              <CheckCircle2 className="size-3.5" aria-hidden /> OK
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 text-xs font-medium text-destructive">
              <XCircle className="size-3.5" aria-hidden /> Failed
            </span>
          )}
        </TableCell>
        <TableCell className="max-w-56 truncate py-1.5 font-mono text-[11px] text-muted-foreground">
          {hasDetails ? (
            <button type="button" className="block max-w-56 truncate text-left hover:text-foreground" onClick={onToggle} tabIndex={-1}>
              {preview}
              {keys.length > 2 && ` · +${keys.length - 2}`}
            </button>
          ) : (
            "—"
          )}
        </TableCell>
      </TableRow>
      {open && hasDetails && (
        <TableRow className="bg-muted/30 hover:bg-muted/30">
          <TableCell colSpan={9} className="p-0">
            <pre
              id={detailsId}
              className="sticky left-0 max-h-80 max-w-[calc(100vw-3rem)] overflow-auto px-4 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all sm:max-w-3xl"
            >
              {JSON.stringify(e.details, null, 2)}
            </pre>
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  );
}

function shortId(id: string): string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(id) ? id.slice(0, 8) : id;
}
