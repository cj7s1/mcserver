/** System → Nodes: the Docker hosts this panel manages (GET/POST /nodes, PATCH/DELETE /nodes/{id}, POST /nodes/{id}/check). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Boxes, Loader2, Lock, LockOpen, Pencil, Plus, ShieldCheck, Trash2, TriangleAlert } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AdminCallout, AdminCopyButton } from "../admin-shared";
import { NodeDialog } from "./NodeDialog";
import {
  checkSummary,
  nodesKey,
  TRANSPORT_META,
  type NodeCheckResult,
  type NodeTransport,
  type PanelNodeRecord,
} from "./nodes-model";

type CheckState = { at: number; result: NodeCheckResult };

export function NodesSection() {
  const qc = useQueryClient();
  const nodes = useQuery({
    queryKey: nodesKey,
    queryFn: () => api.get<PanelNodeRecord[]>("/nodes"),
    refetchInterval: 30_000,
  });
  const [dialog, setDialog] = useState<{ node?: PanelNodeRecord } | null>(null);
  const [checks, setChecks] = useState<Record<number, CheckState>>({});

  const check = useMutation({
    mutationFn: (node: PanelNodeRecord) => api.post<NodeCheckResult>(`/nodes/${node.id}/check`),
    onSuccess: (result, node) => {
      setChecks((c) => ({ ...c, [node.id]: { at: Date.now(), result } }));
      if (result.ok) toast.success(`${node.name}: the agent answered`, { description: checkSummary(result.info) });
      else toast.error(`${node.name}: the agent did not answer`, { description: result.error });
    },
    onError: (err, node) => toast.error(`${node.name}: check failed`, { description: errorMessage(err) }),
    onSettled: () => qc.invalidateQueries({ queryKey: nodesKey }),
  });

  const list = nodes.data ?? [];

  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <Boxes className="size-4 text-muted-foreground" aria-hidden /> Nodes
        </span>
      }
      description={
        <>
          Docker hosts this panel manages. Every extra node runs its own MCPanel agent from{" "}
          <span className="font-mono">deploy/docker-compose.node.yml</span>; see “Additional nodes” in{" "}
          <span className="font-medium">DEPLOYMENT.md</span> for the setup.
        </>
      }
      actions={
        <Button size="sm" onClick={() => setDialog({})}>
          <Plus /> Add node
        </Button>
      }
    >
      {nodes.isLoading ? (
        <LoadingState label="Loading nodes…" className="py-8" />
      ) : nodes.error && !nodes.data ? (
        <ErrorState error={nodes.error} title="Couldn't load nodes" onRetry={() => nodes.refetch()} />
      ) : list.length === 0 ? (
        <EmptyState
          icon={<Boxes className="size-5" />}
          title="No nodes"
          description="The panel's own agent appears here once the API has contacted it."
          className="py-8"
        />
      ) : (
        <ul className="grid gap-2" aria-label="Nodes">
          {list.map((n) => (
            <NodeRow
              key={n.id}
              node={n}
              checkState={checks[n.id]}
              checking={check.isPending && check.variables?.id === n.id}
              onCheck={() => check.mutate(n)}
              onEdit={() => setDialog({ node: n })}
            />
          ))}
        </ul>
      )}

      <NodeDialog open={dialog !== null} onOpenChange={(o) => !o && setDialog(null)} node={dialog?.node} />
    </Section>
  );
}

const TRANSPORT_STYLE: Record<NodeTransport, { className: string; icon: typeof Lock }> = {
  plain: { className: "border-amber-500/40 text-amber-700 dark:text-amber-300", icon: LockOpen },
  tls_pinned: { className: "border-emerald-500/40 text-emerald-700 dark:text-emerald-300", icon: ShieldCheck },
  tls_ca: { className: "border-sky-500/40 text-sky-700 dark:text-sky-300", icon: Lock },
};

function TransportBadge({ transport }: { transport: NodeTransport }) {
  const meta = TRANSPORT_META[transport] ?? { label: transport, description: "" };
  const style = TRANSPORT_STYLE[transport] ?? TRANSPORT_STYLE.plain;
  const Icon = style.icon;
  return (
    <Badge variant="outline" className={style.className} title={meta.description}>
      <Icon aria-hidden /> {meta.label}
      <span className="sr-only">: {meta.description}</span>
    </Badge>
  );
}

function NodeHealth({ node }: { node: PanelNodeRecord }) {
  const state = node.last_error ? "error" : node.last_seen_at ? "ok" : "unknown";
  const label = state === "error" ? "Error" : state === "ok" ? "Reachable" : "Not contacted yet";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" role="status">
      <span
        className={cn(
          "size-2 shrink-0 rounded-full",
          state === "error" ? "bg-red-500" : state === "ok" ? "bg-emerald-500" : "bg-zinc-400",
        )}
        aria-hidden
      />
      <span className={cn(state === "error" ? "text-destructive" : "text-muted-foreground")}>{label}</span>
    </span>
  );
}

function NodeRow({
  node,
  checkState,
  checking,
  onCheck,
  onEdit,
}: {
  node: PanelNodeRecord;
  checkState?: CheckState;
  checking: boolean;
  onCheck: () => void;
  onEdit: () => void;
}) {
  return (
    <li className="grid min-w-0 gap-2.5 rounded-lg border bg-background/40 p-3">
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate text-sm font-semibold">{node.name}</span>
            {node.is_default && <Badge variant="secondary">Default</Badge>}
            <TransportBadge transport={node.transport} />
            <NodeHealth node={node} />
          </div>
          <p className="mt-0.5 font-mono text-xs break-all text-muted-foreground">{node.agent_url}</p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <Button size="sm" variant="outline" onClick={onCheck} disabled={checking} aria-label={`Check node ${node.name}`}>
            {checking ? <Loader2 className="animate-spin" /> : <Activity />} Check
          </Button>
          <Button size="sm" variant="outline" onClick={onEdit} aria-label={`Edit node ${node.name}`}>
            <Pencil /> Edit
          </Button>
          {!node.is_default && <DeleteNodeButton node={node} />}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-4">
        <Meta label="Servers">{node.instance_count}</Meta>
        <Meta label="Last contact">
          {node.last_seen_at ? (
            <time dateTime={node.last_seen_at} title={formatDateTime(node.last_seen_at)}>
              {relativeTime(node.last_seen_at)}
            </time>
          ) : (
            "Never"
          )}
        </Meta>
        <Meta label="LAN address">{node.lan_address ? <span className="font-mono">{node.lan_address}</span> : "—"}</Meta>
        <Meta label="Public address">
          {node.public_address ? <span className="font-mono">{node.public_address}</span> : "—"}
        </Meta>
      </dl>

      {node.tls_fingerprint && (
        <div className="flex min-w-0 flex-col gap-1 text-xs sm:flex-row sm:items-center sm:gap-2">
          <span className="shrink-0 text-muted-foreground">
            {node.tls_pinned ? "Pinned certificate" : "Pinned fingerprint (certificate not read yet)"}
          </span>
          <span className="flex min-w-0 items-center gap-1.5">
            <code className="min-w-0 font-mono text-[11px] break-all">{node.tls_fingerprint}</code>
            <AdminCopyButton value={node.tls_fingerprint} label="Copy" size="sm" className="h-6 shrink-0 px-2 text-xs" />
          </span>
        </div>
      )}

      {node.security_warning && (
        <AdminCallout tone="warning" icon={<TriangleAlert />} title="Insecure connection">
          {node.security_warning}
        </AdminCallout>
      )}

      {node.last_error && (
        <p className="rounded-md border border-destructive/30 bg-destructive/5 px-2.5 py-1.5 text-xs break-words text-destructive">
          <span className="font-medium">Last error:</span> {node.last_error}
        </p>
      )}

      {checkState && (
        <p
          className={cn(
            "text-xs",
            checkState.result.ok ? "text-emerald-700 dark:text-emerald-300" : "text-destructive",
          )}
          aria-live="polite"
        >
          Checked {relativeTime(checkState.at)}:{" "}
          {checkState.result.ok
            ? checkSummary(checkState.result.info) || "the agent answered"
            : checkState.result.error}
        </p>
      )}
    </li>
  );
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="truncate tabular-nums">{children}</dd>
    </div>
  );
}

function DeleteNodeButton({ node }: { node: PanelNodeRecord }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: () => api.delete(`/nodes/${node.id}`),
    onSuccess: () => {
      toast.success(`Node “${node.name}” removed`);
      qc.invalidateQueries({ queryKey: nodesKey });
    },
    onError: (err) => {
      setError(errorMessage(err));
      toast.error(errorMessage(err));
      qc.invalidateQueries({ queryKey: nodesKey });
    },
  });
  return (
    <ConfirmDialog
      trigger={
        <Button size="sm" variant="outline" aria-label={`Remove node ${node.name}`}>
          <Trash2 /> <span className="sr-only sm:not-sr-only">Remove</span>
        </Button>
      }
      onOpenChange={(o) => o && setError(null)}
      title={`Remove node “${node.name}”?`}
      destructive
      confirmLabel="Remove node"
      description={
        <div className="grid gap-2">
          <p>
            The panel forgets this node and its token. The agent, its containers and its files on that host are left
            untouched.
          </p>
          {node.instance_count > 0 && (
            <p className="text-amber-700 dark:text-amber-300">
              It still has {node.instance_count} server{node.instance_count === 1 ? "" : "s"}. The panel refuses to remove
              a node with servers: move them (server Settings → Danger zone → Move to another node) or delete them first.
            </p>
          )}
        </div>
      }
      onConfirm={async () => {
        setError(null);
        await remove.mutateAsync();
      }}
    >
      {error && (
        <AdminCallout tone="danger" icon={<TriangleAlert />} title="The node was not removed">
          {error}
        </AdminCallout>
      )}
    </ConfirmDialog>
  );
}
