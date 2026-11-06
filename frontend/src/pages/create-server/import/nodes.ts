/**
 * Nodes and per-node free ports for the create-server wizard.
 *
 *   GET /nodes                      -> NodeSerializer[] (any signed-in user; tokens are write-only)
 *   GET /system/ports?node=<id>     -> { range, in_use[], suggested } for that node (default node without ?node)
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { mpk, type PortsInfo } from "../../modpacks/modpackQueries";

/** The subset of GET /nodes the wizard uses (backend/apps/nodes/views.py NodeSerializer). */
export interface WizardNode {
  id: number;
  name: string;
  is_default: boolean;
  transport?: "plain" | "tls_pinned" | "tls_ca";
  last_seen_at: string | null;
  last_error: string;
  instance_count: number;
}

/** Same key as the admin Nodes section and the migrate dialog (same endpoint and shape). */
export const nodesKey = ["nodes"] as const;

export function useWizardNodes() {
  return useQuery({
    queryKey: nodesKey,
    queryFn: () => api.get<WizardNode[]>("/nodes"),
    staleTime: 30_000,
  });
}

/** Query key for a node's ports; the default node shares the key the rest of the wizard uses. */
export function portsKey(node: number | null) {
  return node === null ? mpk.ports : ([...mpk.ports, { node }] as const);
}

/** Free-port suggestion and ports in use on a node (null = the default node). */
export function useNodePorts(node: number | null) {
  return useQuery({
    queryKey: portsKey(node),
    queryFn: () => api.get<PortsInfo>("/system/ports", node === null ? undefined : { node }),
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
}

/** The node a selection refers to (null = the default node). */
export function selectedNode(nodes: WizardNode[] | undefined, node: number | null): WizardNode | undefined {
  if (!nodes?.length) return undefined;
  return node === null ? (nodes.find((n) => n.is_default) ?? nodes[0]) : nodes.find((n) => n.id === node);
}
