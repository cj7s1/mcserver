/** Progress (and final log) of the job tracked by the Backups page, with a dismiss button once finished. */
import { X } from "lucide-react";
import { JobProgress } from "@/components/JobProgress";
import { Button } from "@/components/ui/button";
import type { TrackedJob } from "./backup-utils";

export function TrackedJobBanner({ tracked }: { tracked: TrackedJob }) {
  if (!tracked.jobId) return null;
  return (
    <div className="flex items-start gap-2 border-b p-3">
      <JobProgress jobId={tracked.jobId} showLog={tracked.finished} className="min-w-0 flex-1" />
      {tracked.finished && (
        <Button variant="ghost" size="icon-sm" onClick={tracked.dismiss} aria-label="Dismiss job result">
          <X />
        </Button>
      )}
    </div>
  );
}
