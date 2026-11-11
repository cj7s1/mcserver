/**
 * Saves area of the Backups page for games other than Minecraft (GET /instances/{id}/saves): the
 * save files, the ones a reset or import moved aside, download of the current save, import of a save
 * and a new map (backend contract B8).
 */
import { Download, Eraser, FileArchive, Upload } from "lucide-react";
import { useState } from "react";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { downloadUrl } from "@/lib/api";
import { formatBytes, relativeTime } from "@/lib/format";
import { hasCapability } from "@/lib/games";
import type { Instance, SaveFile } from "@/lib/types";
import { useGameInfo, useSaves } from "../game-api";
import type { TrackedJob } from "./backup-utils";
import { SavesImportDialog, SavesResetDialog } from "./SavesDialogs";
import { TrackedJobBanner } from "./TrackedJobBanner";

function modifiedText(v: SaveFile["modified"]): string {
  if (v === null || v === undefined || v === "") return "—";
  const iso = typeof v === "number" ? new Date(v * 1000).toISOString() : v;
  return relativeTime(iso);
}

export function SavesSection({
  instance,
  canChange,
  canDownload,
  tracked,
  showTracked,
}: {
  instance: Instance;
  /** instance.restore + instance.files.write (as Minecraft's Replace world) */
  canChange: boolean;
  canDownload: boolean;
  tracked: TrackedJob;
  showTracked: boolean;
}) {
  const query = useSaves(instance.id);
  const game = useGameInfo(instance);
  const [dialog, setDialog] = useState<"reset" | "import" | null>(null);
  const busy = !!instance.active_job;
  const archived = !!instance.archived_at;
  const canImportExport = hasCapability(instance, "world_import_export");
  const canReset = hasCapability(instance, "world_reset");
  const data = query.data;
  const resetFields = data?.reset_fields?.length ? data.reset_fields : game?.world?.reset_fields;
  const extension = game?.world?.extension ?? ".zip";
  // Satisfactory: a new game, an import and the download go through the running server's API
  const viaApi = !!game?.world?.via_api;

  return (
    <Section
      title="Saves"
      description={
        viaApi
          ? "The server's saves by session. A new game, an import and the download go through the running server (a stopped server is started for it and stopped again); older sessions stay."
          : "The map the server loads, and the saves a new map or an import moved aside."
      }
      actions={
        <>
          {canImportExport && canDownload && data?.current && (
            <Button asChild size="sm" variant="outline">
              <a href={downloadUrl(`/instances/${instance.id}/saves/export`)} download>
                <Download /> Download
              </a>
            </Button>
          )}
          {canImportExport && canChange && (
            <Button size="sm" variant="outline" onClick={() => setDialog("import")} disabled={busy || archived}>
              <Upload /> Import…
            </Button>
          )}
          {canReset && canChange && (
            <Button size="sm" variant="destructive" onClick={() => setDialog("reset")} disabled={busy || archived}>
              <Eraser /> {viaApi ? "New game…" : "New map…"}
            </Button>
          )}
        </>
      }
      className="min-w-0 [&>div:last-child]:p-0"
    >
      {showTracked && <TrackedJobBanner tracked={tracked} />}
      {query.isLoading ? (
        <LoadingState label="Loading saves…" />
      ) : query.error ? (
        <div className="p-4">
          <ErrorState error={query.error} title="Could not load the saves" onRetry={() => query.refetch()} />
        </div>
      ) : (
        <div className="grid gap-3 p-4">
          {!data?.saves.length ? (
            <p className="text-sm text-muted-foreground">No save yet: the server creates its map on the first start.</p>
          ) : (
            <ul className="divide-y rounded-lg border" aria-label="Save files">
              {data.saves.map((s) => (
                <li key={s.path} className="flex min-w-0 items-center gap-2 px-3 py-2 text-sm">
                  <FileArchive className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                  <span className="min-w-0 truncate font-mono" title={s.path}>
                    {s.name}
                  </span>
                  {s.session && <span className="truncate text-xs text-muted-foreground">{s.session}</span>}
                  {s.current && <Badge variant="secondary">loaded</Badge>}
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">
                    {formatBytes(s.size)} · {modifiedText(s.modified)}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {(data?.set_aside ?? []).length > 0 && (
            <div className="grid gap-1 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">Moved aside</p>
              <ul className="grid gap-0.5">
                {data!.set_aside.map((g) => (
                  <li key={g.folder}>
                    <span className="font-mono">{g.folder}</span>: {g.files.join(", ") || "empty"}
                  </li>
                ))}
              </ul>
              <p>These stay in the server folder (and its backups) until you delete them on the Files page.</p>
            </div>
          )}
        </div>
      )}
      {dialog === "reset" && (
        <SavesResetDialog instance={instance} fields={resetFields} onClose={() => setDialog(null)} onStarted={tracked.track} />
      )}
      {dialog === "import" && (
        <SavesImportDialog instance={instance} extension={extension} onClose={() => setDialog(null)} onStarted={tracked.track} />
      )}
    </Section>
  );
}
