/**
 * Software tab of a non-Minecraft game with a version catalog (Factorio): pick an exact version from
 * GET /catalog/games/<id>/versions and update through POST /instances/{id}/software (job
 * software_update with a pre-update backup; a downgrade needs an explicit confirmation).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUpCircle, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { JobProgress } from "@/components/JobProgress";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, errorMessage } from "@/lib/api";
import { displayValue, optionValue } from "@/lib/game-config";
import { gameId, gameLabel } from "@/lib/games";
import { OvercommitDeclined, postOrDecline } from "@/lib/power";
import { qk, useInvalidateInstance, useJob } from "@/lib/queries";
import type { Job } from "@/lib/types";
import { useGameInfo, useGameVersionCatalog } from "../game-api";
import { Notice, ReadOnlyNotice, useReportDirty, type TabProps } from "./settings-ui";

const LIVE = ["running", "starting", "unhealthy", "restarting"];

export function GameSoftwareTab({ instance, can, onDirtyChange }: TabProps) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const game = useGameInfo(instance);
  // a game whose variants have their own versions (Terraria) lists the variant's
  const variant = game?.variants?.some((v) => v.id === instance.server_type && v.default_memory_mb !== undefined) ? instance.server_type : null;
  const versions = useGameVersionCatalog(gameId(instance), true, variant);
  const [target, setTarget] = useState<string | null>(null);
  const [backup, setBackup] = useState(true);
  const [startAfter, setStartAfter] = useState(false);
  const [downgrade, setDowngrade] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);
  const jobStatus = job.data?.status;
  const canEdit = can("instance.configure") && !instance.archived_at;
  const live = LIVE.includes(instance.status);
  const changed = !!target && target !== instance.minecraft_version;
  useReportDirty(changed, onDirtyChange);

  useEffect(() => {
    if (jobStatus !== "succeeded" && jobStatus !== "failed") return;
    if (jobStatus === "succeeded") toast.success("Software updated");
    qc.invalidateQueries({ queryKey: qk.instance(instance.id) });
    qc.invalidateQueries({ queryKey: qk.instances });
  }, [jobStatus, instance.id, qc]);

  const update = useMutation({
    mutationFn: (allowDowngrade: boolean) =>
      postOrDecline<Job>(`/instances/${instance.id}/software`, instance, {
        version: target,
        backup,
        start: !live && startAfter,
        ...(allowDowngrade ? { allow_downgrade: true } : {}),
      }),
  });

  const submit = async (allowDowngrade: boolean) => {
    try {
      const j = await update.mutateAsync(allowDowngrade);
      setJobId(j.id);
      setTarget(null);
      setDowngrade(null);
      invalidate(instance.id);
      toast.success(allowDowngrade ? "Downgrade started" : "Update started");
    } catch (err) {
      if (!allowDowngrade && err instanceof ApiError && err.code === "downgrade_unconfirmed") {
        setDowngrade(err.message);
        return;
      }
      if (err instanceof OvercommitDeclined) return;
      toast.error(errorMessage(err));
    }
  };

  const busy = !!instance.active_job || update.isPending || jobStatus === "queued" || jobStatus === "running";
  const list = versions.data?.versions ?? [];
  const options = list.some((v) => v.id === instance.minecraft_version)
    ? list
    : [{ id: instance.minecraft_version, label: instance.minecraft_version, stable: false, latest: false }, ...list];
  const pinned = (game?.options ?? []).filter((o) => o.pinned);

  return (
    <div className="grid grid-cols-1 gap-4">
      {!can("instance.configure") && <ReadOnlyNotice what="change the game version" />}
      {jobId && <JobProgress jobId={jobId} showLog={jobStatus === "failed"} />}
      <Section title={`${gameLabel(instance)} version`} description="Players need exactly the same version (and mods) as the server.">
        {versions.isLoading ? (
          <LoadingState label="Loading versions…" className="py-6" />
        ) : versions.error ? (
          <ErrorState error={versions.error} title="Could not load the versions" onRetry={() => versions.refetch()} />
        ) : (
          <div className="grid grid-cols-1 gap-4">
            <div className="grid gap-1.5 sm:max-w-sm">
              <Label htmlFor="game-version">Version</Label>
              <Select value={target ?? instance.minecraft_version} onValueChange={setTarget} disabled={!canEdit || busy}>
                <SelectTrigger id="game-version" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {options.map((v) => (
                    <SelectItem key={v.id} value={v.id}>
                      {v.label || v.id}
                      {v.id === instance.minecraft_version ? " (current)" : v.latest ? " (latest)" : !v.stable ? " (experimental)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Image <span className="font-mono">{instance.image}</span>
              </p>
            </div>
            {pinned.length > 0 && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                {pinned.map((o) => (
                  <div key={o.key} className="contents">
                    <dt className="text-muted-foreground">{o.label}</dt>
                    <dd>
                      {displayValue(o, optionValue(instance.game_options, o.key))}{" "}
                      <span className="text-xs text-muted-foreground">(fixed when the server was created)</span>
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            {changed && (
              <div className="grid gap-3 rounded-lg border p-3">
                <div className="flex items-start gap-2">
                  <Checkbox id="game-sw-backup" checked={backup} onCheckedChange={(v) => setBackup(v === true)} />
                  <Label htmlFor="game-sw-backup" className="font-normal">
                    Create a pre-update backup first (recommended: saves are converted to the new version)
                  </Label>
                </div>
                {!live && (
                  <div className="flex items-start gap-2">
                    <Checkbox id="game-sw-start" checked={startAfter} onCheckedChange={(v) => setStartAfter(v === true)} />
                    <Label htmlFor="game-sw-start" className="font-normal">
                      Start the server afterwards
                    </Label>
                  </div>
                )}
                {!backup && <p className="text-xs font-medium text-destructive">No backup will be made: you cannot go back.</p>}
                <div className="flex flex-wrap justify-end gap-2">
                  <Button size="sm" variant="outline" onClick={() => setTarget(null)} disabled={busy}>
                    Cancel
                  </Button>
                  <ConfirmDialog
                    trigger={
                      <Button size="sm" disabled={!canEdit || busy}>
                        {update.isPending ? <Loader2 className="animate-spin" /> : <ArrowUpCircle />} Change to {target}
                      </Button>
                    }
                    title={`Change ${gameLabel(instance)} to ${target}?`}
                    description={
                      live
                        ? "The server is stopped, its image changes and it starts again. Players are disconnected."
                        : "The server's image changes; the save is converted when it next starts."
                    }
                    confirmLabel="Change version"
                    onConfirm={() => submit(false)}
                  />
                </div>
              </div>
            )}
            {downgrade && (
              <Notice
                tone="danger"
                title="This is a downgrade"
                action={
                  <Button size="sm" variant="destructive" onClick={() => submit(true)} disabled={busy}>
                    Downgrade anyway
                  </Button>
                }
              >
                {downgrade} Older versions usually cannot load saves written by newer ones; restoring a backup is the safer way back.
              </Notice>
            )}
          </div>
        )}
      </Section>
    </div>
  );
}
