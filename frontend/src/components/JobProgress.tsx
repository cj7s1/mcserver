import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { SecretRequiredJobHint } from "@/components/SecretRequiredDialog";
import { Progress } from "@/components/ui/progress";
import { useJob } from "@/lib/queries";
import type { ActiveJob, Job } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Inline progress for an active job (instance.active_job or a job id). */
export function JobProgress({ job, jobId, className, showLog = false }: {
  job?: ActiveJob | Job | null;
  jobId?: string | null;
  className?: string;
  showLog?: boolean;
}) {
  const { data } = useJob(job ? null : jobId);
  const j = (job ?? data) as Job | ActiveJob | undefined | null;
  if (!j) return null;
  const done = j.status === "succeeded";
  const failed = j.status === "failed";
  return (
    <div className={cn("rounded-lg border bg-muted/30 p-3", className)} aria-live="polite">
      <div className="flex items-center gap-2 text-sm">
        {done ? (
          <CheckCircle2 className="size-4 text-emerald-500" />
        ) : failed ? (
          <XCircle className="size-4 text-destructive" />
        ) : (
          <Loader2 className="size-4 animate-spin text-primary" />
        )}
        <span className="font-medium">{j.title}</span>
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">{Math.round((j.progress ?? 0) * 100)}%</span>
      </div>
      {!done && !failed && <Progress value={(j.progress ?? 0) * 100} className="mt-2 h-1.5" />}
      {j.message && !failed && <p className="mt-1.5 truncate text-xs text-muted-foreground">{j.message}</p>}
      {failed && "error" in j && j.error && <p className="mt-1.5 text-xs text-destructive">{j.error}</p>}
      {failed && "error_code" in j && <SecretRequiredJobHint job={j} className="mt-1 text-xs" />}
      {showLog && "log" in j && j.log?.length > 0 && (
        <pre className="mt-2 max-h-48 overflow-auto rounded bg-background/60 p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
          {j.log.join("\n")}
        </pre>
      )}
    </div>
  );
}
