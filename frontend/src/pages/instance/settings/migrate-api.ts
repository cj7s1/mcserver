/** Nodes list and migrate-job outcome for the "Move to another node" action. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Job } from "@/lib/types";

/** GET /nodes (backend/apps/nodes/views.py NodeSerializer; tokens are write-only). */
export interface PanelNode {
  id: number;
  name: string;
  agent_url: string;
  use_settings_token: boolean;
  is_default: boolean;
  public_address: string;
  lan_address: string;
  last_seen_at: string | null;
  last_error: string;
  instance_count: number;
  created_at: string;
}

export function useNodes(enabled: boolean) {
  return useQuery({
    queryKey: ["nodes"],
    queryFn: () => api.get<PanelNode[]>("/nodes"),
    enabled,
    staleTime: 30_000,
  });
}

interface MigrateResult {
  from?: string;
  to?: string;
  port_changed?: [number, number] | null;
  restarted?: boolean;
  source_data_deleted?: boolean;
  backups_left_on_source?: number;
}

/** Report the outcome of a finished migrate job once (port changes, backups left behind). */
export function useMigrationOutcome(job: Job | undefined, instanceId: string) {
  const qc = useQueryClient();
  const handled = useRef<string | null>(null);
  useEffect(() => {
    if (!job || job.kind !== "migrate" || handled.current === job.id) return;
    if (job.status !== "succeeded" && job.status !== "failed") return;
    handled.current = job.id;
    qc.invalidateQueries({ queryKey: qk.instance(instanceId) });
    qc.invalidateQueries({ queryKey: qk.instances });
    qc.invalidateQueries({ queryKey: qk.backups(instanceId) });
    qc.invalidateQueries({ queryKey: ["nodes"] });
    if (job.status === "failed") {
      toast.error(`Move failed${job.error ? `: ${job.error}` : ""}. The server stays on its current node.`);
      return;
    }
    const r = (job.result ?? {}) as MigrateResult;
    const notes = [
      r.port_changed ? `The game port changed from ${r.port_changed[0]} to ${r.port_changed[1]} (the old one is taken there).` : "",
      r.backups_left_on_source ? `${r.backups_left_on_source} backup${r.backups_left_on_source === 1 ? "" : "s"} stayed on ${r.from}.` : "",
    ].filter(Boolean);
    toast.success(`Moved to ${r.to ?? "the new node"}`, { description: notes.join(" ") || undefined });
  }, [job, instanceId, qc]);
}
