/** Releases of the pinned modpack project, with their position relative to the installed one. */
import { ArrowDownCircle, ArrowUpCircle, CalendarDays, CheckCircle2, ChevronDown, Download, ExternalLink, FileArchive, Loader2 } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { formatBytes, formatDateTime, formatNumber, relativeTime } from "@/lib/format";
import type { Instance, Provider } from "@/lib/types";
import { cn } from "@/lib/utils";
import { LoaderBadges, plainText, VersionTypeBadge } from "@/pages/modpacks/modpackMeta";
import { useReleaseDetail } from "@/pages/modpacks/modpackQueries";
import { relationOf, releaseUrl, type InstanceModpack, type ModpackRelease, type Relation } from "./modpack-api";

const INITIAL_VISIBLE = 8;

const RELATION_META: Record<Relation, { label: string; className: string; icon: typeof CheckCircle2 }> = {
  current: { label: "Installed", className: "bg-primary/10 text-primary", icon: CheckCircle2 },
  newer: { label: "Newer", className: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300", icon: ArrowUpCircle },
  older: { label: "Older", className: "bg-muted text-muted-foreground", icon: ArrowDownCircle },
};

export function RelationBadge({ relation }: { relation: Relation }) {
  const m = RELATION_META[relation];
  return (
    <span className={cn("inline-flex h-5 items-center gap-1 rounded-md px-1.5 text-[11px] font-medium", m.className)}>
      <m.icon className="size-3" aria-hidden />
      {m.label}
    </span>
  );
}

function Changelog({ provider, projectId, release }: { provider: Provider; projectId: string; release: ModpackRelease }) {
  const inline = release.changelog;
  const detail = useReleaseDetail(provider, projectId, release.id, !inline);
  if (!inline && detail.isLoading) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Loader2 className="size-3 animate-spin" aria-hidden /> Loading changelog…
      </p>
    );
  }
  if (!inline && detail.isError) return <p className="text-xs text-destructive">Could not load the changelog.</p>;
  const text = plainText(inline || detail.data?.changelog);
  return text ? (
    <div className="max-h-56 overflow-y-auto rounded-md border bg-muted/30 p-2.5 text-xs leading-relaxed break-words whitespace-pre-wrap text-muted-foreground">
      {text}
    </div>
  ) : (
    <p className="text-xs text-muted-foreground">No changelog provided for this release.</p>
  );
}

export function ReleaseList({
  instance,
  data,
  canUpdate,
  locked,
  planningId,
  onUpdate,
}: {
  instance: Instance;
  data: InstanceModpack;
  canUpdate: boolean;
  locked: boolean;
  /** Release whose update plan is being fetched. */
  planningId: string | null;
  onUpdate: (release: ModpackRelease, relation: Relation | null) => void;
}) {
  const uid = useId();
  const [showAll, setShowAll] = useState(false);
  const [openChangelog, setOpenChangelog] = useState<string | null>(null);
  const pinnedId = instance.modpack_version_id || data.current.id;
  const releases = data.releases;
  const currentIndex = releases.findIndex((r) => r.id === pinnedId);
  // keep the installed release visible even when it is further down the list
  const visibleCount = showAll ? releases.length : Math.max(INITIAL_VISIBLE, currentIndex + 1);
  const visible = releases.slice(0, visibleCount);
  const provider = (data.provider || instance.modpack_provider) as Provider;
  const projectId = data.project_id || instance.modpack_project_id;

  return (
    <div className="grid gap-1.5">
      <ul className="grid gap-1.5" aria-label="Modpack releases">
        {visible.map((r, idx) => {
          const relation = relationOf(r, idx, currentIndex, data.current, pinnedId);
          const mcChange = !!instance.minecraft_version && r.game_versions.length > 0 && !r.game_versions.includes(instance.minecraft_version);
          const loader = instance.modpack_loader || instance.loader;
          const loaderChange = !!loader && r.loaders.length > 0 && !r.loaders.map((l) => l.toLowerCase()).includes(loader.toLowerCase());
          const url = releaseUrl(instance, r.id);
          const changelogOpen = openChangelog === r.id;
          const label = r.version_number || r.name || r.id;
          const planning = planningId === r.id;
          return (
            <li
              key={r.id}
              className={cn(
                "rounded-lg border bg-card",
                relation === "current" ? "border-primary/50 bg-primary/5 ring-1 ring-primary/20" : "hover:bg-muted/30",
              )}
            >
              <div className="flex flex-col gap-2 p-2.5 sm:flex-row sm:items-start">
                <div className="grid min-w-0 flex-1 gap-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-sm font-medium break-all">{label}</span>
                    <VersionTypeBadge type={r.version_type} />
                    {relation && <RelationBadge relation={relation} />}
                    {idx === 0 && relation !== "current" && (
                      <span className="inline-flex h-5 items-center rounded-md bg-sky-500/10 px-1.5 text-[11px] font-medium text-sky-700 dark:text-sky-300">
                        Latest
                      </span>
                    )}
                    {mcChange && (
                      <span
                        className="inline-flex h-5 items-center rounded-md bg-amber-500/10 px-1.5 text-[11px] font-medium text-amber-700 dark:text-amber-300"
                        title={`This release targets Minecraft ${r.game_versions.join(", ")}; the server runs ${instance.minecraft_version}.`}
                      >
                        Minecraft change
                      </span>
                    )}
                    {loaderChange && (
                      <span className="inline-flex h-5 items-center rounded-md bg-amber-500/10 px-1.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                        Loader change
                      </span>
                    )}
                  </div>
                  {r.name && r.name !== r.version_number && (
                    <p className="truncate text-xs text-muted-foreground" title={r.name}>
                      {r.name}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                    <span className="tabular-nums" title={r.game_versions.join(", ")}>
                      MC <span className="font-medium text-foreground">{r.game_versions.join(", ") || "—"}</span>
                    </span>
                    <LoaderBadges loaders={r.loaders} />
                    {r.loader_version && <span className="tabular-nums">loader {r.loader_version}</span>}
                    {r.published != null && r.published !== "" && (
                      <span className="flex items-center gap-1" title={formatDateTime(r.published)}>
                        <CalendarDays className="size-3" aria-hidden />
                        {relativeTime(r.published)}
                      </span>
                    )}
                    {r.downloads != null && (
                      <span className="flex items-center gap-1 tabular-nums">
                        <Download className="size-3" aria-hidden />
                        {formatNumber(r.downloads)}
                        <span className="sr-only">downloads</span>
                      </span>
                    )}
                    {r.file?.size ? (
                      <span className="flex items-center gap-1 tabular-nums">
                        <FileArchive className="size-3" aria-hidden />
                        {formatBytes(r.file.size)}
                      </span>
                    ) : null}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-1">
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    className="text-muted-foreground"
                    aria-expanded={changelogOpen}
                    aria-controls={`${uid}-cl-${r.id}`}
                    aria-label={`Changelog for ${label}`}
                    onClick={() => setOpenChangelog(changelogOpen ? null : r.id)}
                  >
                    Changelog
                    <ChevronDown className={cn("transition-transform", changelogOpen && "rotate-180")} aria-hidden />
                  </Button>
                  {url && (
                    <Button asChild variant="ghost" size="icon-xs" className="text-muted-foreground">
                      <a href={url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${label} on the provider's site`}>
                        <ExternalLink />
                      </a>
                    </Button>
                  )}
                  {canUpdate && relation !== "current" && (
                    <Button
                      type="button"
                      size="xs"
                      variant={relation === "newer" ? "default" : "outline"}
                      disabled={locked || !!planningId}
                      onClick={() => onUpdate(r, relation)}
                      aria-label={relation === "older" ? `Switch to the older release ${label}` : `Update to ${label}`}
                    >
                      {planning ? <Loader2 className="animate-spin" aria-hidden /> : null}
                      {relation === "older" ? "Switch to this" : "Update to this"}
                    </Button>
                  )}
                </div>
              </div>
              {changelogOpen && (
                <div id={`${uid}-cl-${r.id}`} className="px-2.5 pb-2.5">
                  <Changelog provider={provider} projectId={projectId} release={r} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {releases.length > INITIAL_VISIBLE && (showAll || releases.length > visibleCount) && (
        <Button type="button" variant="ghost" size="sm" className="justify-self-start" onClick={() => setShowAll((s) => !s)}>
          {showAll ? "Show fewer releases" : `Show all ${releases.length} releases`}
        </Button>
      )}
    </div>
  );
}
