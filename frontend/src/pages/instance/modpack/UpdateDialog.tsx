/** Confirmation for switching a modpack server to another release, built from the backend's update plan. */
import { ArrowRight } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/api";
import { OvercommitDeclined, postOrDecline } from "@/lib/power";
import type { Instance, Job } from "@/lib/types";
import { cn } from "@/lib/utils";
import { loaderLabel } from "@/pages/modpacks/modpackMeta";
import { Notice } from "../settings/settings-ui";
import { pinLabel, type ModpackRelease, type Relation, type UpdatePlan } from "./modpack-api";

function ChangeRow({ label, from, to }: { label: string; from: string; to: string }) {
  const changed = from !== to;
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-2 py-1 text-xs">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className={cn("break-all", changed ? "text-muted-foreground line-through decoration-muted-foreground/50" : "text-foreground")}>
          {from || "—"}
        </span>
        {changed && (
          <>
            <ArrowRight className="size-3 shrink-0 text-muted-foreground" aria-label="changes to" />
            <span className="font-medium break-all text-foreground">{to || "—"}</span>
          </>
        )}
        {!changed && <span className="text-muted-foreground">(unchanged)</span>}
      </dd>
    </div>
  );
}

function loaderText(loader: string, version: string): string {
  return [loader ? loaderLabel(loader) : "", version].filter(Boolean).join(" ");
}

export function UpdateDialog({
  instance,
  release,
  relation,
  plan,
  onOpenChange,
  onStarted,
}: {
  instance: Instance;
  release: ModpackRelease;
  relation: Relation | null;
  plan: UpdatePlan;
  onOpenChange: (open: boolean) => void;
  onStarted: (job: Job) => void;
}) {
  const uid = useId();
  const [backup, setBackup] = useState(true);
  const [start, setStart] = useState(false);
  const [ack, setAck] = useState(false);
  const [ackError, setAckError] = useState(false);
  const { current, target } = plan;
  const older = relation === "older";
  const targetName = target.modpack_version_name || release.version_number || release.name || target.modpack_version_id || release.id;

  return (
    <ConfirmDialog
      open
      onOpenChange={onOpenChange}
      title={older ? `Switch to the older release ${targetName}?` : `Update to ${targetName}?`}
      confirmLabel={plan.same_release ? "Already installed" : older ? "Switch release" : "Update modpack"}
      destructive={older || (!backup && plan.minecraft_changed)}
      description={
        <div className="grid gap-3 text-sm">
          <dl className="divide-y rounded-lg border px-3 py-1">
            <ChangeRow label="Release" from={pinLabel(current)} to={pinLabel(target)} />
            <ChangeRow label="Minecraft" from={current.minecraft_version} to={target.minecraft_version} />
            <ChangeRow
              label="Loader"
              from={loaderText(current.modpack_loader, current.loader_version)}
              to={loaderText(target.modpack_loader, target.loader_version)}
            />
            <ChangeRow label="Java" from={`Java ${current.java_version}`} to={`Java ${target.java_version}`} />
          </dl>
          <ol className="list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
            <li>The server is stopped if it is running.</li>
            {backup && <li>A “Before update” backup is created; it becomes a rollback point on this page.</li>}
            <li>The server is pinned to {targetName} and its container is recreated.</li>
            <li>
              On the next start the installer downloads the new release and removes files that belonged to the old one. Worlds are
              kept.
            </li>
          </ol>
          {plan.same_release && (
            <Notice tone="info" title="This release is already installed">
              Nothing would change. Pick a different release.
            </Notice>
          )}
          {older && (
            <Notice tone="danger" title="This is an older release than the installed one">
              Worlds saved with newer mod versions may not load, and items or blocks from removed mods are lost. To undo an update,
              prefer a rollback point: it restores the files from before the update together with the old release.
            </Notice>
          )}
          {plan.warnings.length > 0 && (
            <Notice tone="warning" title={plan.warnings.length === 1 ? "Warning" : `${plan.warnings.length} warnings`}>
              <ul className="grid gap-1">
                {plan.warnings.map((w) => (
                  <li key={w} className="break-words">
                    {w}
                  </li>
                ))}
              </ul>
            </Notice>
          )}
        </div>
      }
      onConfirm={async () => {
        if (plan.same_release) throw new Error("same release");
        if (plan.minecraft_changed && !ack) {
          setAckError(true);
          throw new Error("world upgrade not acknowledged");
        }
        try {
          const job = await postOrDecline<Job>(`/instances/${instance.id}/modpack/update`, instance, {
            version_id: target.modpack_version_id || release.id,
            backup,
            allow_minecraft_change: plan.minecraft_changed && ack,
            start,
          });
          onStarted(job);
          toast.success(`Updating to ${targetName}`);
        } catch (err) {
          if (!(err instanceof OvercommitDeclined)) toast.error(errorMessage(err));
          throw err;
        }
      }}
    >
      <div className="grid gap-3 rounded-lg border bg-muted/30 p-3">
        <div className="flex items-start gap-2">
          <Checkbox id={`${uid}-backup`} checked={backup} onCheckedChange={(v) => setBackup(v === true)} />
          <Label htmlFor={`${uid}-backup`} className="grid gap-0.5 font-normal">
            <span className="font-medium">Create a backup first</span>
            <span className="text-xs text-muted-foreground">Recommended. Without it there is no rollback point for this update.</span>
          </Label>
        </div>
        <div className="flex items-start gap-2">
          <Checkbox id={`${uid}-start`} checked={start} onCheckedChange={(v) => setStart(v === true)} />
          <Label htmlFor={`${uid}-start`} className="grid gap-0.5 font-normal">
            <span className="font-medium">Start the server afterwards</span>
            <span className="text-xs text-muted-foreground">A server that was running is started again either way.</span>
          </Label>
        </div>
        {plan.minecraft_changed && (
          <div className="grid gap-1">
            <div className="flex items-start gap-2">
              <Checkbox
                id={`${uid}-ack`}
                checked={ack}
                aria-invalid={ackError && !ack ? true : undefined}
                aria-describedby={ackError && !ack ? `${uid}-ack-error` : undefined}
                aria-required
                onCheckedChange={(v) => {
                  setAck(v === true);
                  if (v === true) setAckError(false);
                }}
              />
              <Label htmlFor={`${uid}-ack`} className="grid gap-0.5 font-normal">
                <span className="font-medium">I understand the world will be upgraded</span>
                <span className="text-xs text-muted-foreground">
                  Minecraft {current.minecraft_version} → {target.minecraft_version}. Upgraded worlds cannot be opened by the older
                  version again.
                </span>
              </Label>
            </div>
            {ackError && !ack && (
              <p id={`${uid}-ack-error`} className="pl-6 text-xs text-destructive" role="alert">
                Tick this box to confirm the world upgrade.
              </p>
            )}
          </div>
        )}
        {!backup && <p className="text-xs font-medium text-destructive">No backup will be made — this update cannot be rolled back.</p>}
      </div>
    </ConfirmDialog>
  );
}
