/**
 * Known-problem report for the instance Overview: GET /instances/{id}/diagnostics
 * (backend/apps/instances/diagnostics.py). The backend scans the recent container log
 * for verified failure signatures; it never guesses, so "no issues" only means none
 * of the known signatures matched.
 */
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, Info, Loader2, RefreshCw, Stethoscope, TriangleAlert, XCircle } from "lucide-react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { Instance, InstanceStatus, PermissionCode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ManualDownloadsDialog } from "./manual-downloads";
import { CopyButton } from "./overview-utils";

export interface DiagnosticAction {
  kind: "manual_downloads" | "settings" | "console" | "content" | "network" | (string & {});
  label: string;
  field?: string;
  value?: string | number | boolean | null;
}

export interface DiagnosticIssue {
  code: string;
  severity: "error" | "warning" | "info" | (string & {});
  title: string;
  detail: string;
  actions: DiagnosticAction[];
  evidence: string[];
}

export interface Diagnostics {
  issues: DiagnosticIssue[];
  restart_count: number;
  lines_scanned: number;
}

/** Statuses that make a fresh look at the log worthwhile when the server enters them. */
const RECHECK_STATUSES: InstanceStatus[] = ["failed", "unhealthy", "unknown"];

function useDiagnostics(instance: Instance, enabled: boolean) {
  const query = useQuery({
    queryKey: [...qk.instance(instance.id), "diagnostics"] as const,
    queryFn: () => api.get<Diagnostics>(`/instances/${instance.id}/diagnostics`),
    enabled,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const { refetch } = query;
  // Fetched once on mount by the query itself; re-check when the server falls into a problem state.
  const prevStatus = useRef(instance.status);
  useEffect(() => {
    const prev = prevStatus.current;
    prevStatus.current = instance.status;
    if (enabled && prev !== instance.status && RECHECK_STATUSES.includes(instance.status)) void refetch();
  }, [instance.status, enabled, refetch]);
  return query;
}

const SEVERITY: Record<string, { label: string; rank: number; icon: ReactNode; chip: string; card: string; iconClass: string }> = {
  error: {
    label: "Error",
    rank: 3,
    icon: <XCircle aria-hidden />,
    chip: "bg-red-500/10 text-red-700 dark:text-red-300",
    card: "border-destructive/30 bg-destructive/5",
    iconClass: "text-destructive",
  },
  warning: {
    label: "Warning",
    rank: 2,
    icon: <TriangleAlert aria-hidden />,
    chip: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
    card: "border-amber-500/30 bg-amber-500/5",
    iconClass: "text-amber-600 dark:text-amber-400",
  },
  info: {
    label: "Info",
    rank: 1,
    icon: <Info aria-hidden />,
    chip: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
    card: "border-sky-500/30 bg-sky-500/5",
    iconClass: "text-sky-600 dark:text-sky-400",
  },
};

function severityMeta(s: string) {
  return SEVERITY[s] ?? SEVERITY.info;
}

function Evidence({ lines }: { lines: string[] }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  if (!lines.length) return null;
  return (
    <div className="min-w-0">
      <button
        type="button"
        className="inline-flex items-center gap-1 rounded text-xs font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
      >
        <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} aria-hidden />
        {open ? "Hide" : "Show"} log lines ({lines.length})
      </button>
      {open && (
        <div id={id} className="relative mt-1.5">
          <pre className="max-h-48 overflow-auto rounded-md border bg-background/70 p-2 pr-9 font-mono text-[11px] leading-relaxed break-all whitespace-pre-wrap">
            {lines.join("\n")}
          </pre>
          <CopyButton value={lines.join("\n")} label="Copy log lines" className="absolute top-1 right-1" />
        </div>
      )}
    </div>
  );
}

function ActionButton({
  action,
  instance,
  can,
  onManualDownloads,
}: {
  action: DiagnosticAction;
  instance: Instance;
  can: (c: PermissionCode) => boolean;
  onManualDownloads: () => void;
}) {
  const base = `/servers/${instance.id}`;
  const hint = action.field && action.value !== undefined && action.value !== null ? `Suggested ${action.field.replace(/_/g, " ")}: ${action.value}` : undefined;
  const link = (to: string) => (
    <Button asChild size="xs" variant="outline" title={hint}>
      <Link to={to}>{action.label}</Link>
    </Button>
  );
  switch (action.kind) {
    case "manual_downloads":
      return can("instance.files.read") ? (
        <Button size="xs" variant="outline" onClick={onManualDownloads}>
          {action.label}
        </Button>
      ) : null;
    case "settings":
      return can("instance.configure") ? link(`${base}/settings`) : null;
    case "network":
      return can("instance.configure") ? link(`${base}/network`) : null;
    case "console":
      return can("instance.console") ? link(`${base}/console`) : null;
    case "content":
      return instance.content_kind !== "none" ? link(`${base}/content`) : null;
    default:
      return null;
  }
}

function IssueItem({
  issue,
  instance,
  can,
  onManualDownloads,
}: {
  issue: DiagnosticIssue;
  instance: Instance;
  can: (c: PermissionCode) => boolean;
  onManualDownloads: () => void;
}) {
  const sev = severityMeta(issue.severity);
  const actions = issue.actions
    .map((a, i) => <ActionButton key={`${a.kind}-${i}`} action={a} instance={instance} can={can} onManualDownloads={onManualDownloads} />)
    .filter(Boolean);
  return (
    <li className="flex min-w-0 gap-3 rounded-lg border bg-card/60 px-3 py-2.5">
      <span className={cn("mt-0.5 shrink-0 [&_svg]:size-4", sev.iconClass)}>{sev.icon}</span>
      <div className="grid min-w-0 flex-1 gap-1.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-sm font-medium">{issue.title}</span>
          <span className={cn("rounded-full px-1.5 py-px text-[10px] font-medium uppercase tracking-wide", sev.chip)}>{sev.label}</span>
        </div>
        {issue.detail && <p className="text-xs text-muted-foreground">{issue.detail}</p>}
        {actions.length > 0 && <div className="flex flex-wrap gap-1.5">{actions}</div>}
        <Evidence lines={issue.evidence} />
      </div>
    </li>
  );
}

function ScanFacts({ data, checkedAt, now }: { data: Diagnostics; checkedAt: number; now: number }) {
  return (
    <>
      {data.lines_scanned.toLocaleString()} log line{data.lines_scanned === 1 ? "" : "s"} scanned
      {data.restart_count > 0 && (
        <>
          {" "}
          · {data.restart_count} restart{data.restart_count === 1 ? "" : "s"}
        </>
      )}{" "}
      · checked {relativeTime(checkedAt, now)}
    </>
  );
}

/**
 * Shown near the top of the Overview. Renders nothing for users without console access
 * (the endpoint needs instance.console).
 */
export function DiagnosticsCard({
  instance,
  can,
  now,
}: {
  instance: Instance;
  can: (c: PermissionCode) => boolean;
  now: number;
}) {
  const allowed = can("instance.console") && !instance.archived_at;
  const diag = useDiagnostics(instance, allowed);
  const [downloadsOpen, setDownloadsOpen] = useState(false);
  const titleId = useId();
  if (!allowed) return null;

  const recheck = (
    <Button
      size="xs"
      variant="ghost"
      onClick={() => diag.refetch()}
      disabled={diag.isFetching}
      aria-label="Re-check the recent log for known problems"
    >
      {diag.isFetching ? <Loader2 className="animate-spin" /> : <RefreshCw />} Re-check
    </Button>
  );

  const dialog = can("instance.files.read") ? (
    <ManualDownloadsDialog instance={instance} can={can} open={downloadsOpen} onOpenChange={setDownloadsOpen} />
  ) : null;

  if (diag.isLoading) {
    return (
      <p className="flex items-center gap-2 px-1 text-xs text-muted-foreground" role="status">
        <Loader2 className="size-3.5 animate-spin" aria-hidden /> Checking the recent log for known problems…
      </p>
    );
  }

  if (diag.error) {
    return (
      <div className="flex flex-wrap items-center gap-2 px-1 text-xs text-muted-foreground" role="status">
        <TriangleAlert className="size-3.5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
        <span className="min-w-0">Could not check the log for known problems: {errorMessage(diag.error)}</span>
        {recheck}
      </div>
    );
  }

  const data = diag.data;
  if (!data) return null;

  if (data.issues.length === 0) {
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-1 text-xs text-muted-foreground">
        <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden />
        <span className="min-w-0">
          No known problems found in the recent log <span className="opacity-80">(<ScanFacts data={data} checkedAt={diag.dataUpdatedAt} now={now} />)</span>
        </span>
        {recheck}
      </div>
    );
  }

  const worst = data.issues.reduce((w, i) => (severityMeta(i.severity).rank > severityMeta(w.severity).rank ? i : w), data.issues[0]);
  const sev = severityMeta(worst.severity);
  const n = data.issues.length;
  return (
    <section aria-labelledby={titleId} className={cn("rounded-xl border p-3 sm:p-4", sev.card)}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className={cn("mt-0.5 shrink-0 [&_svg]:size-5", sev.iconClass)}>
            <Stethoscope aria-hidden />
          </span>
          <div className="min-w-0">
            <h2 id={titleId} className="text-sm font-semibold">
              {n === 1 ? "1 problem" : `${n} problems`} found in the recent log
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              <ScanFacts data={data} checkedAt={diag.dataUpdatedAt} now={now} />
            </p>
          </div>
        </div>
        {recheck}
      </div>
      <ul className="grid gap-2">
        {data.issues.map((issue, i) => (
          <IssueItem key={`${issue.code}-${i}`} issue={issue} instance={instance} can={can} onManualDownloads={() => setDownloadsOpen(true)} />
        ))}
      </ul>
      {dialog}
    </section>
  );
}
