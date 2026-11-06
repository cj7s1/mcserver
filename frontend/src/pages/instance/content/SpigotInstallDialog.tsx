/**
 * Details and install/update for one SpigotMC resource. Everything from Spiget is shown as plain text
 * (the backend converts SpigotMC's HTML descriptions to text; nothing here renders HTML).
 */
import { useMutation } from "@tanstack/react-query";
import { ArrowUpCircle, CheckCircle2, Download, ExternalLink, Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import {
  contentPath,
  isRunningLike,
  safeHttpUrl,
  testedSummary,
  useContentInventory,
  useInvalidateContent,
  useSpigotResource,
  type SpigotDetails,
  type SpigotInstallResponse,
  type SpigotUpdateResponse,
  type SpigotWarning,
} from "./content-api";
import type { ContentReport, ReportLine } from "./content-report";
import { Notice, ProjectIcon, type ContentEnv } from "./content-ui";
import { findInstalled } from "./HangarInstallDialog";

export interface SpigotTarget {
  id: number;
  title: string;
  icon_url: string | null;
  description: string | null;
  url: string | null;
}

const VERSIONS_SHOWN = 6;

function reportFromInstall(res: SpigotInstallResponse): ContentReport {
  const i = res.installed;
  const lines: ReportLine[] = [
    {
      tone: "ok",
      label: i.filename,
      detail: [i.version ? `version ${i.version} (plugin.yml)` : null, i.size != null ? formatBytes(i.size) : null, "SHA-256 checked by the node"]
        .filter(Boolean)
        .join(" · "),
    },
  ];
  if (i.mirror !== "current")
    lines.push({
      tone: "warn",
      label: "Spiget's copy",
      detail: i.latest_version ? `may be older than SpigotMC's latest version ${i.latest_version}` : "version could not be confirmed",
    });
  return {
    title: `Installed ${i.title}${i.version ? ` ${i.version}` : ""} from SpigotMC`,
    tone: res.warnings.length || i.mirror !== "current" ? "warning" : "success",
    lines,
    missing: [],
    warnings: [...res.warnings, ...res.notes],
  };
}

function reportFromUpdate(res: SpigotUpdateResponse, title: string): ContentReport {
  return {
    title: `Updated ${title}${res.to_version ? ` to ${res.to_version}` : ""}`,
    tone: res.warnings.length ? "warning" : "success",
    lines: [
      {
        tone: "ok",
        label: res.new_filename ?? res.filename,
        detail: [res.from_version ? `was ${res.from_version}` : null, res.size != null ? formatBytes(res.size) : null, "SHA-256 checked by the node"]
          .filter(Boolean)
          .join(" · "),
      },
    ],
    missing: [],
    warnings: res.warnings,
  };
}

function MirrorSummary({ data }: { data: SpigotDetails }) {
  const m = data.mirror;
  const latest = data.latest_version;
  if (!m) return null;
  if (!m.available)
    return (
      <Notice tone="warning">
        {m.error ?? "Spiget does not hold a copy of this file."} Download it from SpigotMC and add the jar in the Upload tab.
      </Notice>
    );
  return (
    <div className="grid gap-1 rounded-md border bg-muted/30 px-2.5 py-2 text-xs">
      <p>
        <span className="font-medium">Installs Spiget's copy</span>
        <span className="text-muted-foreground">
          {m.size != null ? ` · ${formatBytes(m.size)}` : ""}
          {m.uploaded_at ? ` · stored ${formatDateTime(m.uploaded_at)}` : ""}
        </span>
      </p>
      {m.freshness === "current" && latest && (
        <p className="inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-300">
          <CheckCircle2 className="size-3.5 shrink-0" aria-hidden /> Stored after SpigotMC's latest version {latest.name} was released
        </p>
      )}
      <p className="text-muted-foreground">
        SpigotMC publishes no checksum. The panel checks the download against the SHA-1 Spiget's storage reports, makes sure it is a
        plugin for this server, and the node verifies the SHA-256 of what it receives. The version installed is read from the jar.
      </p>
    </div>
  );
}

function WarningList({ warnings, ack, onAck }: { warnings: SpigotWarning[]; ack: boolean; onAck: (v: boolean) => void }) {
  if (!warnings.length) return null;
  return (
    <div className="grid gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-xs">
      <ul className="grid gap-1 text-amber-800 dark:text-amber-200">
        {warnings.map((w) => (
          <li key={w.code}>{w.message}</li>
        ))}
      </ul>
      <div className="flex items-start gap-2">
        <Checkbox id="spigot-ack" checked={ack} onCheckedChange={(c) => onAck(c === true)} className="mt-0.5" />
        <Label htmlFor="spigot-ack" className="text-xs leading-snug font-normal">
          I understand. Install it anyway (back up the server first).
        </Label>
      </div>
    </div>
  );
}

export function SpigotInstallDialog({
  env,
  target,
  onOpenChange,
}: {
  env: ContentEnv;
  target: SpigotTarget | null;
  onOpenChange: (open: boolean) => void;
}) {
  const details = useSpigotResource(env.instance.id, target?.id ?? null);
  const invalidate = useInvalidateContent(env.instance.id);
  const [ackFor, setAckFor] = useState<string | null>(null);
  const data = details.data;
  const res = data?.resource;
  const warnings = data?.warnings ?? [];
  const warningKey = `${target?.id}:${warnings.map((w) => w.code).join(",")}`;
  const ack = ackFor === warningKey;
  const installedFile = data?.installed_files[0] ?? null;
  const inventory = useContentInventory(env.instance.id);
  // a jar of the same plugin added another way would be loaded twice (a name match, not a guarantee)
  const lookalike = !installedFile && res?.installable ? findInstalled(inventory.data?.files ?? [], res.title) : null;
  const link = safeHttpUrl(res?.url ?? target?.url);
  const external = safeHttpUrl(res?.external_url);
  const source = safeHttpUrl(res?.source_url);
  const title = res?.title ?? target?.title ?? "";
  const confirm = ack ? warnings.map((w) => w.code) : [];

  const onError = (err: unknown) => {
    toast.error(errorMessage(err));
    // Spiget's copy may have changed since the details were loaded: show the current warnings
    if (err instanceof ApiError && err.code === "confirmation_required") details.refetch();
  };
  const install = useMutation({
    mutationFn: () => api.post<SpigotInstallResponse>(contentPath(env.instance.id, "spigot/install"), { resource_id: target?.id, confirm }),
    onSuccess: (r) => {
      invalidate();
      env.notify(`Installed ${r.installed.title} from SpigotMC`, isRunningLike(env.instance));
      env.showReport(reportFromInstall(r));
      onOpenChange(false);
    },
    onError,
  });
  const update = useMutation({
    mutationFn: (filename: string) =>
      api.post<SpigotUpdateResponse>(contentPath(env.instance.id, "spigot/update"), { filename, confirm }),
    onSuccess: (r) => {
      invalidate();
      if (r.status === "up_to_date") {
        toast.success(`${title} already matches Spiget's copy`);
        return;
      }
      env.notify(`Updated ${title}${r.to_version ? ` to ${r.to_version}` : ""}`, r.restart_required);
      env.showReport(reportFromUpdate(r, title));
      onOpenChange(false);
    },
    onError,
  });
  const busy = install.isPending || update.isPending;
  const mirrorOk = !!data?.mirror?.available;
  const blocked = !env.canEdit || !!env.locked || !res?.installable || !mirrorOk || (warnings.length > 0 && !ack) || busy;

  return (
    <Dialog open={!!target} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-3 pr-6">
            <ProjectIcon url={res?.icon_url ?? target?.icon_url} className="size-11" />
            <div className="min-w-0">
              <DialogTitle className="truncate">{res?.installable === false ? title : `Install ${title}`}</DialogTitle>
              <DialogDescription className="line-clamp-2 break-words">
                {res?.description ?? target?.description ?? "A plugin from SpigotMC."}
              </DialogDescription>
              <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                {res?.author && <span>by {res.author}</span>}
                {link && (
                  <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                    View on SpigotMC <ExternalLink className="size-3" aria-hidden />
                  </a>
                )}
                {source && (
                  <a href={source} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                    Source code <ExternalLink className="size-3" aria-hidden />
                  </a>
                )}
              </p>
            </div>
          </div>
        </DialogHeader>

        {details.isLoading ? (
          <LoadingState label="Loading from SpigotMC…" className="py-8" />
        ) : details.error ? (
          <ErrorState error={details.error} title="Cannot load this resource" onRetry={() => details.refetch()} className="py-6" />
        ) : !data || !res ? (
          <EmptyState title="Nothing to show" className="py-8" />
        ) : (
          <div className="grid max-h-[60vh] min-w-0 gap-3 overflow-y-auto pr-1">
            {!res.installable && (
              <Notice
                tone="info"
                title="Cannot be installed automatically"
                action={
                  (external ?? link) && (
                    <Button asChild size="sm" variant="outline">
                      <a href={(external ?? link)!} target="_blank" rel="noopener noreferrer">
                        <ExternalLink /> {external ? "Author's site" : "Open on SpigotMC"}
                      </a>
                    </Button>
                  )
                }
              >
                {res.reason_message}
              </Notice>
            )}

            {installedFile && (
              <Notice tone="info">
                Installed as <span className="font-mono break-all text-foreground">{installedFile.filename}</span>
                {installedFile.version ? ` (version ${installedFile.version})` : ""}.{" "}
                {installedFile.update_available
                  ? "Spiget's copy is a different file, so an update may be available."
                  : "It matches Spiget's current copy."}
              </Notice>
            )}

            {lookalike && (
              <Notice tone="warning">
                Looks installed already as <span className="font-mono break-all text-foreground">{lookalike.filename}</span>. Remove or
                disable it in the Installed tab first, otherwise both jars are loaded.
              </Notice>
            )}

            <p className="text-xs text-muted-foreground">
              Tested by the author on: <span className="text-foreground">{testedSummary(res)}</span>
              {res.tested === true && ` (includes ${env.minecraftVersion})`}
              {res.file_size && ` · listed size ${res.file_size}`}
              {res.updated && ` · updated ${relativeTime(res.updated)}`}
            </p>

            {res.installable && <MirrorSummary data={data} />}
            {res.installable && mirrorOk && (!installedFile || installedFile.update_available) && (
              <WarningList warnings={warnings} ack={ack} onAck={(v) => setAckFor(v ? warningKey : null)} />
            )}

            {data.versions.length > 0 && (
              <div className="grid gap-1">
                <p className="text-xs font-medium">
                  Versions <span className="font-normal text-muted-foreground">· older ones are only downloadable on SpigotMC</span>
                </p>
                <ul className="grid gap-1">
                  {data.versions.slice(0, VERSIONS_SHOWN).map((v, i) => (
                    <li key={v.id} className="flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
                      <span className="min-w-0 flex-1 truncate font-mono" title={v.name}>
                        {v.name}
                      </span>
                      {i === 0 && <span className="rounded bg-emerald-500/10 px-1 text-[10px] font-medium text-emerald-700 uppercase dark:text-emerald-300">latest</span>}
                      <span className="shrink-0 text-muted-foreground" title={formatDateTime(v.released)}>
                        {relativeTime(v.released)}
                      </span>
                      <a
                        href={v.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex shrink-0 items-center gap-1 text-primary hover:underline"
                        aria-label={`Download page for ${v.name} on SpigotMC (opens in a new tab)`}
                      >
                        SpigotMC <ExternalLink className="size-3" aria-hidden />
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {res.description_text && (
              <div className="grid gap-1">
                <p className="text-xs font-medium">Description</p>
                <div className="max-h-48 overflow-y-auto rounded-md border bg-muted/20 px-2.5 py-2 text-xs break-words whitespace-pre-line text-muted-foreground">
                  {res.description_text}
                </div>
              </div>
            )}
            {res.installable && (
              <p className="text-xs text-muted-foreground">
                Plugins run with the server's full permissions. Only install ones you trust.
              </p>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            {res?.installable === false ? "Close" : "Cancel"}
          </Button>
          {res?.installable &&
            env.canEdit &&
            (installedFile ? (
              <Button
                disabled={blocked || !installedFile.update_available}
                onClick={() => update.mutate(installedFile.filename)}
                title={installedFile.update_available ? undefined : "Already matches Spiget's copy"}
              >
                {update.isPending ? <Loader2 className="animate-spin" /> : <ArrowUpCircle />}
                {update.isPending ? "Updating…" : "Update"}
              </Button>
            ) : (
              <Button disabled={blocked} onClick={() => install.mutate()}>
                {install.isPending ? <Loader2 className="animate-spin" /> : <Download />}
                {install.isPending ? "Installing…" : "Install"}
              </Button>
            ))}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
