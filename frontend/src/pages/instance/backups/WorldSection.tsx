/** World section of the Backups page: world folder, size, zip export and "Replace world…". */
import { AlertTriangle, CheckCircle2, Download, FolderX, Globe, Info, Replace } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import type { Instance, Job, WorldFolder, WorldInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import type { TrackedJob } from "./backup-utils";
import { TrackedJobBanner } from "./TrackedJobBanner";
import { ReplaceWorldDialog } from "./WorldDialogs";
import { dimensionsSummary, LEVEL_SOURCE_LABEL, ROLE_LABEL, useWorld } from "./world-utils";

function Notice({ tone = "info", children }: { tone?: "info" | "warn"; children: ReactNode }) {
  const Icon = tone === "warn" ? AlertTriangle : Info;
  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-lg border px-3 py-2 text-xs",
        tone === "warn"
          ? "border-amber-500/30 bg-amber-500/10 text-amber-900 dark:text-amber-200"
          : "bg-muted/40 text-muted-foreground",
      )}
    >
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <div className="min-w-0 [overflow-wrap:anywhere]">{children}</div>
    </div>
  );
}

function newest(folders: WorldFolder[]): number | null {
  const times = folders.map((f) => f.modified).filter((t): t is number => typeof t === "number");
  return times.length ? Math.max(...times) : null;
}

function FolderRow({ folder }: { folder: WorldFolder }) {
  return (
    <li className="flex min-w-0 items-center gap-2 py-1.5 text-xs">
      {folder.exists ? (
        <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden />
      ) : (
        <FolderX className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      )}
      <span className="w-16 shrink-0 text-muted-foreground">{ROLE_LABEL[folder.role]}</span>
      <code className="min-w-0 truncate font-mono text-foreground" title={folder.path}>
        {folder.path}
      </code>
      <span className="ml-auto shrink-0 text-right text-muted-foreground tabular-nums">
        {!folder.exists
          ? "not created yet"
          : folder.role === "overworld" && !folder.has_level_dat
            ? `${formatBytes(folder.bytes)} · no level.dat`
            : `${formatBytes(folder.bytes)} · ${folder.files.toLocaleString()} files`}
      </span>
    </li>
  );
}

function WorldBody({ world, instanceId, busy }: { world: WorldInfo; instanceId: string; busy: boolean }) {
  const modified = newest(world.folders);
  const overworld = world.folders.find((f) => f.role === "overworld");
  return (
    <div className="grid gap-3">
      <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-muted-foreground">World folder</dt>
          <dd className="mt-0.5 truncate font-mono text-sm font-semibold" title={world.level_name}>
            {world.level_name}
          </dd>
          <dd className="truncate text-muted-foreground" title={LEVEL_SOURCE_LABEL[world.level_name_source]}>
            from {LEVEL_SOURCE_LABEL[world.level_name_source]}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-muted-foreground">Size</dt>
          <dd className="mt-0.5 text-sm font-semibold tabular-nums">{world.exists ? formatBytes(world.total_bytes) : "—"}</dd>
          <dd className="truncate text-muted-foreground" title={modified ? formatDateTime(new Date(modified * 1000).toISOString()) : undefined}>
            {modified ? `changed ${relativeTime(new Date(modified * 1000).toISOString())}` : "no world files yet"}
          </dd>
        </div>
        <div className="col-span-2 min-w-0 sm:col-span-1">
          <dt className="text-muted-foreground">Dimensions</dt>
          <dd className="mt-0.5 text-foreground">{dimensionsSummary(world)}</dd>
        </div>
      </dl>

      {world.layout === "bukkit" && (
        <ul className="divide-y rounded-lg border px-3" aria-label="World folders">
          {world.folders.map((f) => (
            <FolderRow key={f.role} folder={f} />
          ))}
        </ul>
      )}

      {world.error && <Notice tone="warn">{world.error.message}</Notice>}
      {!world.error && !world.exists && (
        <Notice>
          {overworld?.exists
            ? `The folder ${world.level_name} has no level.dat yet. `
            : "There is no world yet: the server generates one on its first start. "}
          You can also replace it with your own world.
        </Notice>
      )}
      {world.warnings.map((w) => (
        <Notice key={w} tone="warn">
          {w}
        </Notice>
      ))}
      {/* while a replacement runs, its upload and staging folders are expected */}
      {!busy && world.leftovers.length > 0 && (
        <Notice tone="warn">
          An interrupted world replacement left{" "}
          {world.leftovers.map((l, i) => (
            <span key={l}>
              {i > 0 && ", "}
              <code className="font-mono">{l}</code>
            </span>
          ))}{" "}
          in the server folder. A <code className="font-mono">…-old-…</code> folder holds the previous world; the others are
          partial copies or uploaded archives. Check them on the{" "}
          <Link to={`/servers/${instanceId}/files`} className="font-medium underline">
            Files
          </Link>{" "}
          page and delete them once you no longer need them.
        </Notice>
      )}
      {world.download_url && world.running && (
        <p className="text-xs text-muted-foreground">
          The server is running: the download saves the world to disk first (save-all flush), but changes made while it
          downloads may be partly included. Stop the server for an exact copy.
        </p>
      )}
    </div>
  );
}

export function WorldSection({
  instance,
  canReplacePerm,
  tracked,
  showTracked,
}: {
  instance: Instance;
  /** instance.restore and instance.files.write (the server also reports can_replace) */
  canReplacePerm: boolean;
  tracked: TrackedJob;
  showTracked: boolean;
}) {
  const busy = !!instance.active_job;
  const working = busy || (!!tracked.jobId && !tracked.finished);
  const query = useWorld(instance.id, { busy: working });
  const [dialogKey, setDialogKey] = useState<number | null>(null);
  const world = query.data;
  const canReplace = canReplacePerm && (world?.can_replace ?? true);

  return (
    <Section
      title={
        <span className="flex items-center gap-1.5">
          <Globe className="size-4 text-muted-foreground" aria-hidden /> World
        </span>
      }
      description="Download the world as a zip, or replace it with a singleplayer save or a world from another server."
      actions={
        <>
          {world?.download_url ? (
            <Button size="sm" variant="outline" asChild>
              <a href={world.download_url} download>
                <Download /> <span className="hidden sm:inline">Download world (.zip)</span>
                <span className="sm:hidden">Download</span>
              </a>
            </Button>
          ) : (
            <Button size="sm" variant="outline" disabled>
              <Download /> <span className="hidden sm:inline">Download world (.zip)</span>
              <span className="sm:hidden">Download</span>
            </Button>
          )}
          {canReplace && (
            <Button size="sm" onClick={() => setDialogKey(Date.now())} disabled={busy || !world || !!world.error}>
              <Replace /> Replace world…
            </Button>
          )}
        </>
      }
      className="min-w-0 [&>div:last-child]:p-0"
    >
      {showTracked && <TrackedJobBanner tracked={tracked} />}
      <div className="p-4">
        {query.isLoading ? (
          <LoadingState label="Reading the world folder…" />
        ) : query.error ? (
          <ErrorState error={query.error} onRetry={() => query.refetch()} title="Could not read the world" />
        ) : world ? (
          <WorldBody world={world} instanceId={instance.id} busy={working} />
        ) : null}
      </div>
      {world && dialogKey !== null && (
        <ReplaceWorldDialog
          key={dialogKey}
          instance={instance}
          world={world}
          open
          onOpenChange={(o) => !o && setDialogKey(null)}
          onStarted={(job: Job) => tracked.track(job)}
        />
      )}
    </Section>
  );
}
