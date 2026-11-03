import { Activity, ChevronRight, FilterX, RefreshCw } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatDateTime, relativeTime } from "@/lib/format";
import { useInstances, useJobs } from "@/lib/queries";
import type { Job, JobStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { JobStatusIcon, JobStatusText } from "./activity/job-status";
import { JOB_STATUSES, jobDurationLabel, jobStatusLabel } from "./activity/job-utils";

const LIMIT = 100;

const STATUS_OPTIONS: { value: string; label: string; query?: string }[] = [
  { value: "all", label: "All statuses" },
  { value: "active", label: "Active (queued or running)", query: "queued,running" },
  ...JOB_STATUSES.map((s) => ({ value: s, label: jobStatusLabel(s), query: s })),
];

export default function ActivityPage() {
  const [params, setParams] = useSearchParams();
  const statusParam = params.get("status") ?? "all";
  const statusOpt = STATUS_OPTIONS.find((o) => o.value === statusParam) ?? STATUS_OPTIONS[0];
  const instanceFilter = params.get("instance") ?? "all";
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  const jobs = useJobs({
    status: statusOpt.query,
    instance: instanceFilter === "all" ? undefined : instanceFilter,
    limit: LIMIT,
  });
  const instances = useInstances("all");

  const setParam = (key: string, value: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value === "all") next.delete(key);
        else next.set(key, value);
        return next;
      },
      { replace: true },
    );

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const serverOptions = useMemo(
    () => [...(instances.data ?? [])].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" })),
    [instances.data],
  );
  const unknownInstance = instanceFilter !== "all" && instances.data && !instances.data.some((i) => i.id === instanceFilter);

  const counts = useMemo(() => {
    const c: Partial<Record<JobStatus, number>> = {};
    for (const j of jobs.data ?? []) c[j.status] = (c[j.status] ?? 0) + 1;
    return c;
  }, [jobs.data]);

  const filtersActive = statusOpt.value !== "all" || instanceFilter !== "all";
  const list = jobs.data ?? [];

  return (
    <div className="grid gap-4">
      <PageHeader
        className="pb-0"
        title="Activity"
        description="Background jobs: installs, power actions, backups, restores and updates. Refreshes automatically."
        actions={
          <Button variant="outline" size="sm" onClick={() => jobs.refetch()} disabled={jobs.isFetching}>
            <RefreshCw className={cn(jobs.isFetching && "animate-spin")} /> Refresh
          </Button>
        }
      />

      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end">
        <div className="grid gap-1.5">
          <Label htmlFor="activity-status" className="text-xs text-muted-foreground">
            Status
          </Label>
          <Select value={statusOpt.value} onValueChange={(v) => setParam("status", v)}>
            <SelectTrigger id="activity-status" size="sm" className="w-full sm:w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="activity-instance" className="text-xs text-muted-foreground">
            Server
          </Label>
          <Select value={instanceFilter} onValueChange={(v) => setParam("instance", v)}>
            <SelectTrigger id="activity-instance" size="sm" className="w-full sm:w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All servers</SelectItem>
              {serverOptions.length > 0 && <SelectSeparator />}
              {serverOptions.map((i) => (
                <SelectItem key={i.id} value={i.id}>
                  {i.name}
                  {i.archived_at && <span className="text-xs text-muted-foreground">archived</span>}
                </SelectItem>
              ))}
              {unknownInstance && <SelectItem value={instanceFilter}>Unknown or deleted server</SelectItem>}
            </SelectContent>
          </Select>
        </div>
        {filtersActive && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setParams(new URLSearchParams(), { replace: true })}
            className="self-start sm:self-auto"
          >
            <FilterX /> Clear filters
          </Button>
        )}
        {jobs.data && (
          <p className="text-xs text-muted-foreground sm:ml-auto sm:pb-1.5" aria-live="polite">
            {list.length === LIMIT ? `Latest ${LIMIT} jobs` : `${list.length} job${list.length === 1 ? "" : "s"}`}
            {JOB_STATUSES.filter((s) => counts[s])
              .map((s) => ` · ${counts[s]} ${jobStatusLabel(s).toLowerCase()}`)
              .join("")}
          </p>
        )}
      </div>

      {jobs.isLoading ? (
        <LoadingState label="Loading activity…" />
      ) : jobs.error ? (
        <ErrorState error={jobs.error} onRetry={() => jobs.refetch()} title="Could not load activity" />
      ) : list.length === 0 ? (
        <EmptyState
          icon={<Activity className="size-5" />}
          title={filtersActive ? "No jobs match these filters" : "No activity yet"}
          description={
            filtersActive
              ? "Try another status or server."
              : "Jobs appear here when servers are installed, started, stopped, backed up or updated."
          }
          action={
            filtersActive && (
              <Button size="sm" variant="outline" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
                <FilterX /> Clear filters
              </Button>
            )
          }
        />
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card">
          <div
            className="hidden items-center gap-3 border-b bg-muted/40 px-4 py-2 text-xs font-medium text-muted-foreground md:flex"
            aria-hidden
          >
            <span className="flex-1 pl-[3.25rem]">Job</span>
            <span className="w-40">Server</span>
            <span className="w-24">By</span>
            <span className="w-28">Created</span>
            <span className="w-16 text-right">Duration</span>
          </div>
          <ul className="divide-y" aria-label="Jobs">
            {list.map((job) => (
              <JobRow key={job.id} job={job} open={expanded.has(job.id)} onToggle={() => toggle(job.id)} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function JobRow({ job, open, onToggle }: { job: Job; open: boolean; onToggle: () => void }) {
  const detailsId = `job-details-${job.id}`;
  const summary = job.status === "failed" && job.error ? job.error : job.message;
  return (
    <li className={cn(open && "bg-muted/20")}>
      <div className="flex flex-col gap-1 px-4 py-2.5 md:flex-row md:items-center md:gap-3">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={detailsId}
          className="-mx-1 flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-1 py-0.5 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <ChevronRight
            className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
            aria-hidden
          />
          <span title={jobStatusLabel(job.status)}>
            <JobStatusIcon status={job.status} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate text-sm font-medium">{job.title}</span>
              <Badge variant="outline" className="hidden font-mono text-[10px] text-muted-foreground sm:inline-flex">
                {job.kind}
              </Badge>
            </span>
            <span className="flex min-w-0 items-center gap-1.5 text-xs">
              <JobStatusText status={job.status} className="shrink-0" />
              {job.status === "running" && (
                <span className="tabular-nums text-muted-foreground">{Math.round(job.progress * 100)}%</span>
              )}
              {summary && (
                <span className={cn("truncate", job.status === "failed" ? "text-destructive" : "text-muted-foreground")}>
                  · {summary}
                </span>
              )}
            </span>
          </span>
        </button>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-[3.25rem] text-xs text-muted-foreground md:flex-nowrap md:pl-0">
          <span className="truncate md:w-40">
            {job.instance && job.instance_name ? (
              <Link to={`/servers/${job.instance}`} className="text-foreground hover:underline">
                {job.instance_name}
              </Link>
            ) : (
              "Panel"
            )}
          </span>
          <span className="truncate md:w-24" title="Started by">
            {job.user_name ?? "System"}
          </span>
          <time dateTime={job.created_at} title={formatDateTime(job.created_at)} className="md:w-28">
            {relativeTime(job.created_at)}
          </time>
          <span className="tabular-nums md:w-16 md:text-right" title="Run time">
            {jobDurationLabel(job)}
          </span>
        </div>
      </div>
      {job.status === "running" && (
        <div className="px-4 pb-2 md:pl-[4.25rem]">
          <Progress value={job.progress * 100} className="h-1" aria-label={`${job.title} progress`} />
        </div>
      )}
      {open && <JobDetails job={job} id={detailsId} />}
    </li>
  );
}

function JobDetails({ job, id }: { job: Job; id: string }) {
  const hasResult = job.result && Object.keys(job.result).length > 0;
  return (
    <div id={id} className="grid gap-3 border-t bg-muted/30 px-4 py-3 md:pl-[4.25rem]">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3 lg:grid-cols-6">
        <Meta label="Created" value={formatDateTime(job.created_at)} />
        <Meta label="Started" value={formatDateTime(job.started_at)} />
        <Meta label="Finished" value={formatDateTime(job.finished_at)} />
        <Meta label="Duration" value={jobDurationLabel(job)} />
        <Meta label="Kind" value={<span className="font-mono">{job.kind}</span>} />
        <Meta label="Job ID" value={<span className="break-all font-mono text-[11px]">{job.id}</span>} />
      </dl>

      {job.message && (
        <div className="text-sm">
          <span className="text-xs text-muted-foreground">Last message: </span>
          {job.message}
        </div>
      )}

      {job.error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm" role="note">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-destructive">Error</span>
            {job.error_code && (
              <Badge variant="outline" className="font-mono text-[10px]">
                {job.error_code}
              </Badge>
            )}
          </div>
          <p className="mt-1 whitespace-pre-wrap break-words">{job.error}</p>
        </div>
      )}

      {hasResult && (
        <div className="grid gap-1">
          <span className="text-xs font-medium text-muted-foreground">Result</span>
          <pre className="max-h-48 overflow-auto rounded-lg border bg-background p-2 font-mono text-[11px] leading-relaxed">
            {JSON.stringify(job.result, null, 2)}
          </pre>
        </div>
      )}

      <div className="grid gap-1">
        <span className="text-xs font-medium text-muted-foreground">Log{job.log.length ? ` (${job.log.length} lines)` : ""}</span>
        {job.log.length ? (
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg border bg-background p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {job.log.join("\n")}
          </pre>
        ) : (
          <p className="text-xs text-muted-foreground">No log output.</p>
        )}
      </div>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-0.5">{value}</dd>
    </div>
  );
}
