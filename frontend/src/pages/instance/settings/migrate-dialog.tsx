/** Move a server to another node (POST /instances/{id}/migrate -> migrate job). Needs system.nodes + instance.configure. */
import { useId, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { errorMessage } from "@/lib/api";
import { OvercommitDeclined, postOrDecline } from "@/lib/power";
import { relativeTime } from "@/lib/format";
import type { Instance, Job } from "@/lib/types";
import type { PanelNode } from "./migrate-api";
import { Field, Notice } from "./settings-ui";

export function MigrateDialog({
  instance,
  nodes,
  live,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  nodes: PanelNode[];
  live: boolean;
  onOpenChange: (open: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const uid = useId();
  const targets = nodes.filter((n) => n.id !== instance.node);
  const [nodeId, setNodeId] = useState<string>(() => (targets[0] ? String(targets[0].id) : ""));
  const [deleteSource, setDeleteSource] = useState(false);
  const target = targets.find((n) => String(n.id) === nodeId);
  const from = instance.node_name || "the current node";

  return (
    <ConfirmDialog
      open
      onOpenChange={onOpenChange}
      title={`Move ${instance.name} to another node?`}
      confirmLabel={target ? `Move to ${target.name}` : "Move server"}
      destructive={deleteSource}
      description={
        <div className="grid gap-2 text-sm">
          <p>All server files are copied from {from} to the target node, then the container is created there.</p>
          <ol className="list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
            <li>The server is stopped{live ? " (players are disconnected)" : " if it is running"} and started again on the new node if it was running.</li>
            <li>If the game port is already used on the target, the next free port is assigned. The connection address changes to the new host.</li>
            <li>The container on {from} is removed after the copy succeeds.</li>
          </ol>
        </div>
      }
      onConfirm={async () => {
        if (!target) {
          toast.error("Choose a target node");
          throw new Error("no target");
        }
        try {
          // a running server is started on the target node: it must fit there ("Start anyway?")
          const job = await postOrDecline<Job>(`/instances/${instance.id}/migrate`, instance, {
            node: target.id,
            confirm: true,
            delete_source: deleteSource,
          });
          onStarted(job);
          toast.success(`Moving ${instance.name} to ${target.name}`);
        } catch (err) {
          if (!(err instanceof OvercommitDeclined)) toast.error(errorMessage(err));
          throw err;
        }
      }}
    >
      <div className="grid gap-3">
        <Field
          id={`${uid}-node`}
          label="Target node"
          hint={
            target?.last_error
              ? `Last check failed: ${target.last_error}`
              : target?.last_seen_at
                ? `Agent last reached ${relativeTime(target.last_seen_at)} · ${target.instance_count} server${target.instance_count === 1 ? "" : "s"}`
                : undefined
          }
        >
          <Select value={nodeId} onValueChange={setNodeId}>
            <SelectTrigger id={`${uid}-node`} className="w-full">
              <SelectValue placeholder="Choose a node" />
            </SelectTrigger>
            <SelectContent>
              {targets.map((n) => (
                <SelectItem key={n.id} value={String(n.id)}>
                  {n.name}
                  {n.is_default ? " (default)" : ""}
                  {n.last_error ? " · unreachable at last check" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <div className="flex items-start gap-2 rounded-lg border bg-muted/30 p-3">
          <Checkbox id={`${uid}-delete`} checked={deleteSource} onCheckedChange={(v) => setDeleteSource(v === true)} />
          <Label htmlFor={`${uid}-delete`} className="grid gap-0.5 font-normal">
            <span className="font-medium">Delete files on the old node after moving</span>
            <span className="text-xs text-muted-foreground">
              Frees disk space on {from}. Off by default, so the old copy remains as a fallback.
            </span>
          </Label>
        </div>
        {deleteSource ? (
          <Notice tone="danger" title={`The server folder and its backups on ${from} are deleted`}>
            Backups are not copied to the new node, so deleting the old files also removes every existing backup of this server. Create
            a new backup on {target?.name ?? "the new node"} after the move.
          </Notice>
        ) : (
          <Notice tone="warning" title={`Backups stay on ${from}`}>
            Backups live in the old node's repository and are not copied. They cannot be restored on {target?.name ?? "the new node"};
            create a new backup there after the move.
          </Notice>
        )}
      </div>
    </ConfirmDialog>
  );
}
