/** Plan query, request and job outcome of "Reset world" (GET/POST /instances/{id}/world/reset -> world_reset job). */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Job, WorldResetPlan } from "@/lib/types";
import { outcomeMessage, type WorldResetBody } from "./reset-world-model";

/**
 * What a reset would delete, read when the dialog opens. Never served from the cache (an earlier
 * visit's folders must not be confirmed) and not refetched on window focus: the agent walks every
 * world file for the sizes, and the request is checked against a fresh plan on the server anyway.
 */
export function useWorldResetPlan(instanceId: string) {
  return useQuery({
    queryKey: qk.worldReset(instanceId),
    queryFn: () => api.get<WorldResetPlan>(`/instances/${instanceId}/world/reset`),
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
}

/** Queue the world_reset job. `confirm_name` is the server name the user typed. */
export function requestWorldReset(instanceId: string, body: WorldResetBody & { confirm_name: string }) {
  return api.post<Job>(`/instances/${instanceId}/world/reset`, body);
}

const FINISHED: readonly string[] = ["succeeded", "failed", "cancelled"];

/** Refresh what a finished world_reset job changed and report its outcome once. */
export function useWorldResetOutcome(job: Job | undefined, instanceId: string) {
  const qc = useQueryClient();
  const handled = useRef<string | null>(null);
  useEffect(() => {
    if (!job || job.kind !== "world_reset" || handled.current === job.id || !FINISHED.includes(job.status)) return;
    handled.current = job.id;
    // A live event marks the cached job as finished without its result and last log lines
    // (lib/events.ts), and a finished job is not polled again: read the final record first.
    void qc
      .fetchQuery({ queryKey: qk.job(job.id), queryFn: () => api.get<Job>(`/jobs/${job.id}`), staleTime: 0 })
      .catch(() => job)
      .then((final) => {
        const outcome = outcomeMessage(final);
        if (!outcome) {
          handled.current = null; // the server says it is still running: report when it really ends
          return;
        }
        qc.invalidateQueries({ queryKey: qk.instance(instanceId), exact: true });
        qc.invalidateQueries({ queryKey: qk.world(instanceId) });
        qc.invalidateQueries({ queryKey: qk.worldReset(instanceId) });
        qc.invalidateQueries({ queryKey: qk.backups(instanceId) });
        qc.invalidateQueries({ queryKey: qk.properties(instanceId) });
        // a failure can ask for a manual check before the server is started: leave it on screen longer
        toast[outcome.tone](outcome.title, {
          description: outcome.description || undefined,
          duration: outcome.tone === "success" ? undefined : 15_000,
        });
      });
  }, [job, instanceId, qc]);
}
