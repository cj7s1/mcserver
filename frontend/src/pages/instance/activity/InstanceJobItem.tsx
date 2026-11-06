/**
 * Expandable job row for the instance Activity tab. Same layout as the Overview's JobItem
 * (overview/instance-jobs.tsx), plus an action strip for stuck queued/running jobs.
 */
import { ChevronRight } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { Progress } from "@/components/ui/progress";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Job } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AbandonJobButton } from "../../activity/AbandonJobButton";
import { isJobActive, JOB_STATUS_META, JobStatusIcon, jobDuration } from "../overview/instance-jobs";
import { CopyButton } from "../overview/overview-utils";

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="truncate text-xs tabular-nums">{children}</dd>
    </div>
  );
}

export function InstanceJobItem({
  job,
  now,
  defaultOpen = false,
  canAbandon = false,
}: {
  job: Job;
  now: number;
  defaultOpen?: boolean;
  /** Show "Mark as failed" (caller checks instance.control; only queued/running jobs). */
  canAbandon?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const panelId = useId();
  const meta = JOB_STATUS_META[job.status];
  const active = isJobActive(job);
  const hasResult = job.result && Object.keys(job.result).length > 0;
  const showAbandon = canAbandon && active;
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
          {showAbandon && (
            <div className="flex flex-col gap-2 rounded-md border bg-muted/30 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-xs text-muted-foreground">
                Is this job stuck? Marking it as failed releases this server's lock. It does not stop work the agent may
                still be doing.
              </p>
              <AbandonJobButton job={job} className="shrink-0 self-start sm:self-auto" />
            </div>
          )}
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
