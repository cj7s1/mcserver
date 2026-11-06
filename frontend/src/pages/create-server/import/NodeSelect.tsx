import { AlertTriangle, Loader2, Server } from "lucide-react";
import { useId } from "react";
import { Section } from "@/components/PageHeader";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { relativeTime } from "@/lib/format";
import { describedBy, Field } from "../WizardBits";
import type { FieldErrors, WizardState } from "../wizardModel";
import { selectedNode, useNodePorts, type WizardNode } from "./nodes";

const TRANSPORT: Record<NonNullable<WizardNode["transport"]>, string> = {
  plain: "plain HTTP",
  tls_pinned: "TLS pinned",
  tls_ca: "TLS",
};

/** Resources step: where the server runs (shown only when the panel manages several nodes). */
export function NodeSelect({
  nodes,
  state,
  patch,
  errors,
}: {
  nodes: WizardNode[];
  state: WizardState;
  patch: (p: Partial<WizardState>) => void;
  errors: FieldErrors;
}) {
  const id = useId();
  const current = selectedNode(nodes, state.node);
  const ports = useNodePorts(state.node);
  const hint = ports.isLoading ? (
    <span className="inline-flex items-center gap-1">
      <Loader2 className="size-3 animate-spin" aria-hidden /> Checking free ports on {current?.name ?? "the node"}…
    </span>
  ) : (
    "The Docker host the server runs on. Ports are checked per node; changing it picks that node's first free port."
  );
  const error = errors.node;

  return (
    <Section title="Node">
      <div className="grid gap-2">
        <Field id={id} label="Run on node" hint={hint} error={error} className="max-w-md">
          <Select
            value={current ? String(current.id) : undefined}
            onValueChange={(v) => {
              const picked = nodes.find((n) => String(n.id) === v);
              if (!picked) return;
              patch({ node: picked.is_default ? null : picked.id, gamePort: null });
            }}
          >
            <SelectTrigger
              id={id}
              className="w-full"
              aria-invalid={!!error || undefined}
              aria-describedby={describedBy(id, error, hint)}
            >
              <SelectValue placeholder="Choose a node" />
            </SelectTrigger>
            <SelectContent position="popper">
              {nodes.map((n) => (
                <SelectItem key={n.id} value={String(n.id)}>
                  <Server className="text-muted-foreground" aria-hidden />
                  <span className="truncate">{n.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {n.is_default ? "default · " : ""}
                    {n.instance_count} server{n.instance_count === 1 ? "" : "s"}
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        {current && (
          <p className="text-xs text-muted-foreground">
            {current.last_error ? (
              <span className="flex items-start gap-1.5 text-amber-700 dark:text-amber-300" role="alert">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                <span className="min-w-0 break-words">
                  The panel could not reach this node last time: {current.last_error}
                </span>
              </span>
            ) : (
              <>
                {current.last_seen_at ? `Agent answered ${relativeTime(current.last_seen_at)}` : "Not contacted yet"}
                {current.transport ? ` · ${TRANSPORT[current.transport]}` : ""}
                {!current.is_default && " · host memory is not checked for this node"}
              </>
            )}
          </p>
        )}
      </div>
    </Section>
  );
}
