/** "Mark as failed" for a stuck queued/running job (POST /jobs/{id}/abandon). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { OctagonX } from "lucide-react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { ApiError, api, errorMessage } from "@/lib/api";
import { qk, useInvalidateInstance } from "@/lib/queries";
import type { Job } from "@/lib/types";

export function AbandonJobButton({
  job,
  size = "sm",
  className,
}: {
  job: Pick<Job, "id" | "title" | "status" | "instance" | "instance_name">;
  size?: "sm" | "xs";
  className?: string;
}) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const refresh = () => {
    qc.invalidateQueries({ queryKey: qk.job(job.id) });
    invalidate(job.instance ?? undefined);
  };
  const abandon = useMutation({
    mutationFn: () => api.post<Job>(`/jobs/${job.id}/abandon`),
    onSuccess: (updated) => {
      qc.setQueryData(qk.job(updated.id), updated);
      refresh();
      toast.success(
        updated.instance_name
          ? `“${updated.title}” marked as failed. ${updated.instance_name} accepts new actions again.`
          : `“${updated.title}” marked as failed.`,
      );
    },
  });

  return (
    <ConfirmDialog
      trigger={
        <Button type="button" variant="outline" size={size} className={className}>
          <OctagonX /> Mark as failed
        </Button>
      }
      title="Mark this job as failed?"
      destructive
      confirmLabel="Mark as failed"
      description={
        <div className="grid gap-2">
          <p>
            <span className="font-medium text-foreground">{job.title}</span>
            {job.instance_name ? <> on {job.instance_name}</> : null} will be recorded as failed and the server's operation
            lock is released, so other actions can run again.
            {job.status === "queued" && " A queued job marked as failed will not start."}
          </p>
          <p>
            This does <span className="font-medium text-foreground">not</span> stop work the agent may still be doing: a
            download, backup, restore or container change can keep running in the background. Use it only for a job that
            is stuck, for example after the worker restarted, and check the server's status, console and files afterwards.
          </p>
        </div>
      }
      onConfirm={async () => {
        try {
          await abandon.mutateAsync();
        } catch (err) {
          if (err instanceof ApiError && err.code === "not_active") {
            // finished in the meantime: nothing to release, just show the current state
            toast.info("This job already finished.");
            refresh();
            return;
          }
          toast.error(errorMessage(err));
          throw err; // keep the dialog open
        }
      }}
    />
  );
}
