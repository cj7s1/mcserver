import { CheckCircle2, Circle, ExternalLink, FileArchive, Loader2, RefreshCw, XCircle } from "lucide-react";
import { type ReactNode, useRef } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { JobProgress } from "@/components/JobProgress";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ARCHIVE_ACCEPT, type ImportStage, type StageState, stageStates, validateImportArchive } from "./importModel";
import type { ImportRun } from "./useImportDeploy";

function StageIcon({ state }: { state: StageState }) {
  if (state === "done") return <CheckCircle2 className="size-4 text-emerald-500" aria-hidden />;
  if (state === "failed") return <XCircle className="size-4 text-destructive" aria-hidden />;
  if (state === "active") return <Loader2 className="size-4 animate-spin text-primary" aria-hidden />;
  return <Circle className="size-4 text-muted-foreground/60" aria-hidden />;
}

const STATE_LABEL: Record<StageState, string> = {
  done: "done",
  active: "in progress",
  pending: "not started",
  failed: "failed",
};

function Stage({ state, title, children }: { state: StageState; title: ReactNode; children?: ReactNode }) {
  return (
    <li className="grid grid-cols-[1rem_minmax(0,1fr)] gap-x-3 gap-y-1">
      <span className="mt-0.5">
        <StageIcon state={state} />
      </span>
      <span className={cn("text-sm", state === "pending" ? "text-muted-foreground" : "font-medium")}>
        {title}
        <span className="sr-only"> ({STATE_LABEL[state]})</span>
      </span>
      {children && <div className="col-start-2 min-w-0">{children}</div>}
    </li>
  );
}

/** Replaces the wizard once the server exists: install wait, archive upload, hand-off to the server page. */
export function ImportProgress({ run, onRetry }: { run: ImportRun; onRetry: (file?: File) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const states: Record<ImportStage, StageState> = stageStates(run.phase, run.failedAt);
  const { instance, file } = run;
  const failed = run.phase === "failed";
  const pct = Math.round(run.progress * 100);

  return (
    <Section
      title={`Importing into ${instance.name}`}
      description="You can leave this page: the upload continues in the background while this browser tab stays open."
    >
      <div className="grid gap-4">
        <ol className="grid gap-3" aria-label="Import progress" aria-live="polite">
          <Stage state={states.create} title="Create the server">
            <p className="text-xs text-muted-foreground">
              {instance.node_name ? `On ${instance.node_name} · ` : ""}port {instance.game_port}/tcp · stays stopped
            </p>
          </Stage>
          <Stage state={states.prepare} title="Prepare the server folder and container">
            {run.phase === "preparing" && run.job && <JobProgress job={run.job} />}
            {run.phase === "waiting" && states.prepare === "active" && (
              <p className="text-xs text-muted-foreground">Waiting for the server to report that it is ready for files…</p>
            )}
          </Stage>
          <Stage
            state={states.upload}
            title={
              <span className="inline-flex max-w-full items-center gap-1.5">
                Upload <FileArchive className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                <span className="truncate font-mono text-xs" title={file.name}>
                  {file.name}
                </span>
                <span className="shrink-0 text-xs font-normal text-muted-foreground tabular-nums">{formatBytes(file.size)}</span>
              </span>
            }
          >
            {run.phase === "uploading" && (
              <div className="grid gap-1">
                <Progress value={pct} className="h-1.5" aria-label={`Upload progress for ${file.name}`} />
                <p className="text-xs text-muted-foreground tabular-nums">
                  {run.progress >= 1
                    ? "Transferred — handing the archive to the node…"
                    : `${pct}% · ${formatBytes(file.size * run.progress)} of ${formatBytes(file.size)}`}
                </p>
              </div>
            )}
          </Stage>
          <Stage state={states.extract} title="Extract the files">
            <p className="text-xs text-muted-foreground">
              Runs as a job on the server page. A single top-level folder is stripped; links and unsafe paths are skipped.
            </p>
          </Stage>
        </ol>

        {failed && (
          <div className="grid gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3" role="alert">
            <div className="flex gap-2 text-sm">
              <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden />
              <div className="min-w-0">
                <p className="font-medium text-destructive">
                  {run.failedAt === "prepare" ? "Preparing the server failed" : "The archive was not imported"}
                </p>
                <p className="mt-0.5 break-words text-muted-foreground">{run.error}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  The server {instance.name} exists and is stopped. You can retry here, or upload the archive later on its
                  Files tab and extract it there.
                </p>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {run.retryable && (
                <Button type="button" size="sm" onClick={() => onRetry()}>
                  <RefreshCw /> {run.failedAt === "prepare" ? "Upload anyway" : "Retry upload"}
                </Button>
              )}
              {run.retryable && (
                <Button type="button" size="sm" variant="outline" onClick={() => input.current?.click()}>
                  Choose another archive…
                </Button>
              )}
              <Button asChild size="sm" variant="outline">
                <Link to={`/servers/${instance.id}`}>
                  <ExternalLink /> Open server
                </Link>
              </Button>
            </div>
            <input
              ref={input}
              type="file"
              accept={ARCHIVE_ACCEPT}
              className="sr-only"
              tabIndex={-1}
              aria-hidden
              onChange={(e) => {
                const next = e.target.files?.[0];
                e.target.value = "";
                if (!next) return;
                const problem = validateImportArchive(next);
                if (problem) {
                  toast.error(problem);
                  return;
                }
                onRetry(next);
              }}
            />
          </div>
        )}

        {!failed && (
          <div className="flex flex-wrap gap-2">
            <Button asChild size="sm" variant="ghost">
              <Link to={`/servers/${instance.id}`}>
                <ExternalLink /> Open server
              </Link>
            </Button>
          </div>
        )}
      </div>
    </Section>
  );
}
