/** Recreate, clone, archive/unarchive and permanent delete. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, Copy, Loader2, RefreshCcw, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { JobProgress } from "@/components/JobProgress";
import { EmptyState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/hooks/use-auth";
import { api, errorMessage } from "@/lib/api";
import { qk, useInvalidateInstance, useJob } from "@/lib/queries";
import type { Instance, Job } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Field, fieldErrorsOf, InlineField, Notice, parseNum, type TabProps } from "./settings-ui";

const LIVE = ["running", "starting", "unhealthy", "restarting", "stopping"];

function ActionRow({
  title,
  description,
  action,
  danger,
}: {
  title: string;
  description: ReactNode;
  action: ReactNode;
  danger?: boolean;
}) {
  return (
    <div className={cn("flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between", danger && "bg-destructive/5")}>
      <div className="min-w-0">
        <p className={cn("text-sm font-medium", danger && "text-destructive")}>{title}</p>
        <div className="mt-0.5 text-xs text-muted-foreground">{description}</div>
      </div>
      <div className="shrink-0">{action}</div>
    </div>
  );
}

function CloneDialog({ instance, onClose }: { instance: Instance; onClose: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [name, setName] = useState(`${instance.name} (copy)`.slice(0, 80));
  const [port, setPort] = useState("");
  const [excludeWorlds, setExcludeWorlds] = useState(false);
  const [result, setResult] = useState<{ instance: Instance; job: Job } | null>(null);
  const job = useJob(result?.job.id);
  const status = job.data?.status;
  const clone = useMutation({
    mutationFn: () =>
      api.post<{ instance: Instance; job: Job }>(`/instances/${instance.id}/clone`, {
        name: name.trim(),
        game_port: parseNum(port) ?? undefined,
        exclude_worlds: excludeWorlds,
      }),
    onSuccess: (res) => {
      setResult(res);
      qc.invalidateQueries({ queryKey: qk.instances });
      qc.invalidateQueries({ queryKey: qk.instance(instance.id) });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const target = result?.instance;
  useEffect(() => {
    if (!target || (status !== "succeeded" && status !== "failed")) return;
    qc.invalidateQueries({ queryKey: qk.instances });
    if (status === "succeeded") {
      toast.success(`Created ${target.name}`);
      navigate(`/servers/${target.id}`);
    }
  }, [status, target, navigate, qc]);

  const errors = fieldErrorsOf(clone.error);
  const portNum = parseNum(port);
  const portError = port && (portNum === null || !Number.isInteger(portNum) || portNum < 1024 || portNum > 65535) ? "1024–65535" : null;
  const nameError = !name.trim() ? "A name is required" : name.length > 80 ? "At most 80 characters" : null;

  return (
    <Dialog open onOpenChange={(o) => !o && !clone.isPending && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Clone {instance.name}</DialogTitle>
          <DialogDescription>
            Creates a new server with the same settings and a copy of this server's files. The copy gets its own port and starts
            stopped.
          </DialogDescription>
        </DialogHeader>
        {result ? (
          <div className="grid grid-cols-1 gap-3">
            <JobProgress jobId={result.job.id} showLog={status === "failed"} />
            {status === "failed" ? (
              <p className="text-xs text-muted-foreground">
                The new server entry “{result.instance.name}” was created but copying failed. You can open it, or delete it from
                its Settings.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">You are taken to the new server when copying finishes.</p>
            )}
          </div>
        ) : (
          <form
            id="clone-form"
            className="grid grid-cols-1 gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (!nameError && !portError) clone.mutate();
            }}
          >
            <Field id="clone-name" label="Name" error={nameError ?? errors.name}>
              <Input id="clone-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} autoFocus />
            </Field>
            <Field
              id="clone-port"
              label="Game port"
              error={portError ?? errors.game_port}
              hint="Leave empty to pick the next free port automatically."
            >
              <Input
                id="clone-port"
                type="number"
                inputMode="numeric"
                min={1024}
                max={65535}
                placeholder="Automatic"
                value={port}
                onChange={(e) => setPort(e.target.value)}
                className="w-36 tabular-nums"
              />
            </Field>
            <InlineField
              id="clone-exclude-worlds"
              label="Leave out worlds"
              hint="Copy configuration, mods and plugins only; the clone generates a fresh world on first start."
            >
              <Switch id="clone-exclude-worlds" checked={excludeWorlds} onCheckedChange={setExcludeWorlds} />
            </InlineField>
          </form>
        )}
        <DialogFooter>
          {result ? (
            <>
              <Button variant="outline" onClick={onClose}>
                Close
              </Button>
              <Button onClick={() => navigate(`/servers/${result.instance.id}`)}>Open the clone</Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={onClose} disabled={clone.isPending}>
                Cancel
              </Button>
              <Button type="submit" form="clone-form" disabled={clone.isPending || !!nameError || !!portError}>
                {clone.isPending ? <Loader2 className="animate-spin" /> : <Copy />} Clone server
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DangerTab({ instance, can }: TabProps) {
  const auth = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const [jobId, setJobId] = useState<string | null>(null);
  const [cloneOpen, setCloneOpen] = useState(false);
  const live = LIVE.includes(instance.status);
  const busy = !!instance.active_job;
  const archived = !!instance.archived_at;

  const canConfigure = can("instance.configure");
  const canDelete = can("instance.delete");
  const canClone = auth.can("instance.create") && can("instance.files.read");

  const action = useMutation({
    mutationFn: (kind: "recreate" | "archive") => api.post<Job>(`/instances/${instance.id}/${kind}`),
    onSuccess: (job, kind) => {
      setJobId(job.id);
      toast.success(kind === "recreate" ? "Recreating the container" : "Archiving the server");
      invalidate(instance.id);
    },
  });
  const unarchive = useMutation({
    mutationFn: () => api.post<Instance>(`/instances/${instance.id}/unarchive`),
    onSuccess: (data) => {
      qc.setQueryData(qk.instance(instance.id), data);
      invalidate(instance.id);
      toast.success("Server restored from the archive. Start it to recreate its container.");
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  if (!canConfigure && !canDelete && !canClone) {
    return (
      <EmptyState
        title="No actions available"
        description="Recreating, cloning, archiving and deleting need the Configure, Create servers or Delete permissions."
      />
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4">
      {jobId && <JobProgress jobId={jobId} />}
      <section className="overflow-hidden rounded-xl border bg-card text-card-foreground">
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold">Maintenance</h2>
        </div>
        <div className="divide-y">
          {canConfigure && (
            <ActionRow
              title="Recreate container"
              description="Removes the Docker container and creates it again from the current settings. Files and worlds are kept; a running server is restarted. Use this if the container is broken or out of sync."
              action={
                <ConfirmDialog
                  trigger={
                    <Button variant="outline" size="sm" disabled={busy || archived}>
                      <RefreshCcw /> Recreate…
                    </Button>
                  }
                  title="Recreate the container?"
                  description={
                    live
                      ? "The server is running: it is stopped, the container is recreated and the server starts again. Players are disconnected."
                      : "The container is removed and created again from the current settings. Server files are not touched."
                  }
                  confirmLabel="Recreate"
                  onConfirm={async () => {
                    try {
                      await action.mutateAsync("recreate");
                    } catch (err) {
                      toast.error(errorMessage(err));
                    }
                  }}
                />
              }
            />
          )}
          {canClone && (
            <ActionRow
              title="Clone server"
              description={
                live
                  ? "Stop the server first — files can only be copied while it is stopped."
                  : "Make a copy with the same settings and files, e.g. to test an update safely."
              }
              action={
                <Button variant="outline" size="sm" onClick={() => setCloneOpen(true)} disabled={live || busy || archived}>
                  <Copy /> Clone…
                </Button>
              }
            />
          )}
        </div>
      </section>

      {canDelete && (
        <section className="overflow-hidden rounded-xl border border-destructive/40 bg-card text-card-foreground">
          <div className="border-b border-destructive/30 px-4 py-3">
            <h2 className="text-sm font-semibold text-destructive">Danger zone</h2>
          </div>
          <div className="divide-y">
            {archived ? (
              <ActionRow
                title="Unarchive server"
                description="Bring the server back to the active list. Its container is recreated on the next start."
                action={
                  <Button variant="outline" size="sm" onClick={() => unarchive.mutate()} disabled={unarchive.isPending || busy}>
                    {unarchive.isPending ? <Loader2 className="animate-spin" /> : <ArchiveRestore />} Unarchive
                  </Button>
                }
              />
            ) : (
              <ActionRow
                title="Archive server"
                description="Stops the server, removes its container and hides it from the default server list. Files and backups are kept, and it can be unarchived at any time."
                action={
                  <ConfirmDialog
                    trigger={
                      <Button variant="outline" size="sm" disabled={busy}>
                        <Archive /> Archive…
                      </Button>
                    }
                    title={`Archive ${instance.name}?`}
                    description={
                      live
                        ? "The server is running and will be stopped. Its container is removed; files and backups stay on disk."
                        : "Its container is removed; files and backups stay on disk. You can unarchive it later."
                    }
                    confirmLabel="Archive"
                    onConfirm={async () => {
                      try {
                        await action.mutateAsync("archive");
                      } catch (err) {
                        toast.error(errorMessage(err));
                      }
                    }}
                  />
                }
              />
            )}
            <ActionRow
              danger
              title="Delete permanently"
              description="Deletes the container, all server files including worlds, and every backup of this server. This cannot be undone."
              action={
                <ConfirmDialog
                  trigger={
                    <Button variant="destructive" size="sm" disabled={busy}>
                      <Trash2 /> Delete…
                    </Button>
                  }
                  title={`Delete ${instance.name} permanently?`}
                  destructive
                  confirmLabel="Delete forever"
                  confirmText={instance.name}
                  description={
                    <div className="grid grid-cols-1 gap-2">
                      <p>This removes, with no way to recover them:</p>
                      <ul className="list-disc space-y-0.5 pl-5">
                        <li>the container{live ? " (the running server is stopped)" : ""}</li>
                        <li>all files: worlds, configs, mods and plugins</li>
                        <li>all backups of this server</li>
                      </ul>
                      <p>If you might need it again, archive it instead or download what you need first.</p>
                    </div>
                  }
                  onConfirm={async () => {
                    try {
                      await api.delete<Job>(`/instances/${instance.id}`, { confirm_name: instance.name });
                      toast.success(`Deleting ${instance.name}. This continues in the background.`);
                      qc.invalidateQueries({ queryKey: qk.instances });
                      qc.invalidateQueries({ queryKey: qk.jobs });
                      navigate("/servers");
                    } catch (err) {
                      toast.error(errorMessage(err));
                    }
                  }}
                />
              }
            />
          </div>
        </section>
      )}

      {busy && <Notice>Another operation is running on this server; these actions are available when it finishes.</Notice>}
      {cloneOpen && <CloneDialog instance={instance} onClose={() => setCloneOpen(false)} />}
    </div>
  );
}
