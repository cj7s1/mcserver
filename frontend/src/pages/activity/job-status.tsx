import { Ban, CheckCircle2, Clock, Loader2, XCircle } from "lucide-react";
import type { JobStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { JOB_STATUS, jobStatusLabel } from "./job-utils";

export function JobStatusIcon({ status, className }: { status: JobStatus; className?: string }) {
  const cls = cn("size-4 shrink-0", JOB_STATUS[status]?.className, className);
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

export function JobStatusText({ status, className }: { status: JobStatus; className?: string }) {
  return <span className={cn("text-xs font-medium", JOB_STATUS[status]?.className, className)}>{jobStatusLabel(status)}</span>;
}
