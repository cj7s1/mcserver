/** Job list pieces for an instance (used by the Overview "Recent jobs" card and the Activity tab). */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CheckCircle2, ChevronRight, Clock, Loader2, XCircle } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { SecretRequiredJobHint } from "@/components/SecretRequiredDialog";
import { Progress } from "@/components/ui/progress";
import { api } from "@/lib/api";
import { formatDateTime, formatDuration, relativeTime, toDate } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { ActiveJob, Job, JobStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CopyButton } from "./overview-utils";

export const JOB_STATUS_META: Record<JobStatus, { label: string; className: string }> = {
  queued: { label: "Queued", className: "text-muted-foreground" },
  running: { label: "Running", className: "text-sky-700 dark:text-sky-400" },
  succeeded: { label: "Succeeded", className: "text-emerald-700 dark:text-emerald-400" },
  failed: { label: "Failed", className: "text-destructive" },
  cancelled: { label: "Cancelled", className: "text-muted-foreground" },
};

export function isJobActive(j: Pick<Job, "status">): boolean {
  return j.status === "queued" || j.status === "running";
}

/**
 * GET /instances/{id}/jobs (newest first, max 50). Polls quickly while a job is
 * active and refreshes whenever the instance's active job changes.
 */
export function useInstanceJobs(instanceId: string, activeJob: ActiveJob | null) {
  const qc = useQueryClient();
  const activeId = activeJob?.id ?? null;
  useEffect(() => {
    qc.invalidateQueries({ queryKey: qk.instanceJobs(instanceId) });
  }, [activeId, instanceId, qc]);
  return useQuery({
    queryKey: qk.instanceJobs(instanceId),
    queryFn: () => api.get<Job[]>(`/instances/${instanceId}/jobs`),
    refetchInterval: (q) => {
      const d = q.state.data as Job[] | undefined;
      return d?.some(isJobActive) ? 2500 : 20_000;
    },
  });
}

export function JobStatusIcon({ status, className }: { status: JobStatus; className?: string }) {
  const cls = cn("size-4 shrink-0", JOB_STATUS_META[status]?.className, className);
  switch (status) {
    case "running":
      return <Loader2 className={cn(cls, "animate-spin")} aria-hidden />;
    case "succeeded":
      return <CheckCircle2 className={cls} aria-hidden />;
    case "failed":
      return <XCircle className={cls} aria-hidden />;
    case "cancelled":
      return <Ban className={cls} aria-hidden />;
    default:
      return <Clock className={cls} aria-hidden />;
  }
}

export function jobDuration(job: Pick<Job, "started_at" | "finished_at">, now = Date.now()): string {
  const start = toDate(job.started_at);
  if (!start) return "—";
  const end = toDate(job.finished_at)?.getTime() ?? now;
  return formatDuration(Math.max(0, (end - start.getTime()) / 1000));
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="truncate text-xs tabular-nums">{children}</dd>
    </div>
  );
}

/** One expandable job row. Expanded view shows timings, error, result and the job log. */
export function JobItem({ job, now, defaultOpen = false }: { job: Job; now: number; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const panelId = useId();
  const meta = JOB_STATUS_META[job.status];
  const active = isJobActive(job);
  const hasResult = job.result && Object.keys(job.result).length > 0;
  return (
    <li className="min-w-0 rounded-lg border bg-background/40">
      <button
        type="button"
        className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left outline-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
      >
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden />
        <JobStatusIcon status={job.status} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="truncate text-sm font-medium">{job.title}</span>
            <span className={cn("text-xs", meta.className)}>{meta.label}</span>
          </div>
          <div className="truncate text-xs text-muted-foreground">
            {job.status === "failed" && job.error ? job.error : job.message || job.kind}
          </div>
        </div>
        <div className="hidden shrink-0 text-right text-xs text-muted-foreground sm:block">
          <div title={formatDateTime(job.created_at)}>{relativeTime(job.created_at, now)}</div>
          <div className="tabular-nums">{active && !job.started_at ? "waiting" : jobDuration(job, now)}</div>
        </div>
      </button>
      {active && (
        <div className="px-3 pb-2">
          <Progress value={(job.progress ?? 0) * 100} className="h-1" aria-label={`${job.title} progress`} />
        </div>
      )}
      {open && (
        <div id={panelId} className="grid gap-3 border-t px-3 py-3">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
            <Meta label="Started by">{job.user_name ?? "System"}</Meta>
            <Meta label="Queued">{formatDateTime(job.created_at)}</Meta>
            <Meta label="Finished">{job.finished_at ? formatDateTime(job.finished_at) : active ? "In progress" : "—"}</Meta>
            <Meta label="Duration">{jobDuration(job, now)}</Meta>
            <Meta label="Kind">
              <span className="font-mono">{job.kind}</span>
            </Meta>
            <Meta label="Progress">{Math.round((job.progress ?? 0) * 100)}%</Meta>
          </dl>
          {job.status === "failed" && job.error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {job.error}
              {job.error_code && <span className="ml-2 font-mono opacity-70">({job.error_code})</span>}
              <SecretRequiredJobHint job={job} className="mt-1" />
            </div>
          )}
          {hasResult && (
            <div>
              <div className="mb-1 text-[11px] uppercase tracking-wide text-muted-foreground">Result</div>
              <pre className="max-h-40 overflow-auto rounded-md bg-muted/50 p-2 font-mono text-[11px] leading-relaxed">
                {JSON.stringify(job.result, null, 2)}
              </pre>
            </div>
          )}
          <div>
            <div className="mb-1 flex items-center justify-between gap-2">
              <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Log ({job.log?.length ?? 0} lines)</span>
              {job.log?.length > 0 && <CopyButton value={job.log.join("\n")} label="Copy job log" />}
            </div>
            {job.log?.length ? (
              <pre className="max-h-72 overflow-auto rounded-md bg-muted/50 p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all">
                {job.log.join("\n")}
              </pre>
            ) : (
              <p className="text-xs text-muted-foreground">This job did not write any log lines.</p>
            )}
          </div>
        </div>
      )}
    </li>
  );
}
