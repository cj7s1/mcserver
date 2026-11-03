import { CalendarDays, ChevronDown, Download, FileArchive, Loader2, MemoryStick, Pin } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";
import { EmptyState, ErrorState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { formatBytes, formatDateTime, formatMemoryMb, formatNumber, relativeTime } from "@/lib/format";
import type { Provider } from "@/lib/types";
import { cn } from "@/lib/utils";
import { LoaderBadges, plainText, sortVersionsDesc, VersionTypeBadge } from "./modpackMeta";
import { type ReleaseData, useModpackReleases, useReleaseDetail } from "./modpackQueries";

const ALL = "__all__";
const INITIAL_VISIBLE = 6;

function Changelog({ provider, projectId, release }: { provider: Provider; projectId: string; release: ReleaseData }) {
  const inline = release.changelog;
  const detail = useReleaseDetail(provider, projectId, release.id, !inline);
  if (!inline && detail.isLoading) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Loader2 className="size-3 animate-spin" /> Loading changelog…
      </p>
    );
  }
  if (!inline && detail.isError) return <p className="text-xs text-destructive">Could not load the changelog.</p>;
  const text = plainText(inline || detail.data?.changelog);
  return text ? (
    <div className="max-h-56 overflow-y-auto rounded-md border bg-muted/30 p-2.5 text-xs leading-relaxed whitespace-pre-wrap text-muted-foreground">
      {text}
    </div>
  ) : (
    <p className="text-xs text-muted-foreground">No changelog provided for this release.</p>
  );
}

/**
 * Pinned release list for a modpack project. Releases are always pinned by id: the
 * server installs exactly the selected release and never silently upgrades.
 */
export function ReleasePicker({
  provider,
  projectId,
  selectedId,
  onSelect,
  autoSelectLatest = false,
  className,
}: {
  provider: Provider;
  projectId: string;
  selectedId: string | null | undefined;
  onSelect: (release: ReleaseData) => void;
  /** Select the newest release once the list loads if nothing is selected yet. */
  autoSelectLatest?: boolean;
  className?: string;
}) {
  const uid = useId();
  const [includePrerelease, setIncludePrerelease] = useState(false);
  const [autoWidened, setAutoWidened] = useState(false);
  const [mcFilter, setMcFilter] = useState(ALL);
  const [showAll, setShowAll] = useState(false);
  const [openChangelog, setOpenChangelog] = useState<string | null>(null);
  const query = useModpackReleases(provider, projectId, includePrerelease);
  const releases = useMemo(() => query.data ?? [], [query.data]);

  const mcVersions = useMemo(() => sortVersionsDesc([...new Set(releases.flatMap((r) => r.game_versions))]), [releases]);
  const filtered = useMemo(
    () => (mcFilter === ALL ? releases : releases.filter((r) => r.game_versions.includes(mcFilter))),
    [releases, mcFilter],
  );
  const visible = showAll ? filtered : filtered.slice(0, INITIAL_VISIBLE);
  const selectedHidden = !!selectedId && !visible.some((r) => r.id === selectedId) && filtered.some((r) => r.id === selectedId);
  const hasPrerelease = releases.some((r) => r.version_type && r.version_type !== "release");

  // A deep-linked release may be a beta/alpha hidden by the default filter: widen once.
  const selectedMissing = query.isSuccess && !!selectedId && !releases.some((r) => r.id === selectedId);
  if (selectedMissing && !includePrerelease && !autoWidened) {
    setAutoWidened(true);
    setIncludePrerelease(true);
  }

  useEffect(() => {
    if (autoSelectLatest && query.isSuccess && !selectedId && releases.length) onSelect(releases[0]);
  }, [query.isSuccess, releases, selectedId, autoSelectLatest, onSelect]);

  return (
    <div className={cn("grid min-w-0 gap-3", className)}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-2">
          <Switch id={`${uid}-pre`} size="sm" checked={includePrerelease} onCheckedChange={setIncludePrerelease} />
          <Label htmlFor={`${uid}-pre`} className="text-xs font-normal">
            Include beta &amp; alpha
          </Label>
        </div>
        {mcVersions.length > 1 && (
          <div className="flex items-center gap-2">
            <Label htmlFor={`${uid}-mc`} className="text-xs font-normal text-muted-foreground">
              Minecraft
            </Label>
            <Select value={mcFilter} onValueChange={setMcFilter}>
              <SelectTrigger id={`${uid}-mc`} size="sm" className="min-w-28 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All versions</SelectItem>
                {mcVersions.map((v) => (
                  <SelectItem key={v} value={v}>
                    {v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        {query.isFetching && !query.isLoading && <Loader2 className="size-3.5 animate-spin text-muted-foreground" aria-label="Refreshing" />}
        <span className="ml-auto text-xs text-muted-foreground tabular-nums">
          {query.isSuccess && `${filtered.length} release${filtered.length === 1 ? "" : "s"}`}
        </span>
      </div>

      {query.isLoading ? (
        <div className="grid gap-2" aria-busy>
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-14 w-full rounded-lg" />
          ))}
        </div>
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} title="Could not load releases" />
      ) : !filtered.length ? (
        <EmptyState
          title="No releases"
          description={
            includePrerelease || !hasPrerelease
              ? "This modpack has no downloadable releases matching the filter."
              : "No stable releases match. Try including beta and alpha releases."
          }
        />
      ) : (
        <div role="radiogroup" aria-label="Modpack releases" className="grid gap-1.5">
          {visible.map((r, idx) => {
            const selected = r.id === selectedId;
            const changelogOpen = openChangelog === r.id;
            return (
              <div
                key={r.id}
                className={cn(
                  "rounded-lg border bg-card transition-colors has-[:focus-visible]:border-ring has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50",
                  selected ? "border-primary/60 bg-primary/5 ring-1 ring-primary/30" : "hover:bg-muted/40",
                )}
              >
                <div className="flex items-start gap-2 p-2.5">
                  <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-2.5">
                    <input
                      type="radio"
                      name={`${uid}-release`}
                      value={r.id}
                      checked={selected}
                      onChange={() => onSelect(r)}
                      className="mt-1 size-3.5 shrink-0 accent-(--primary)"
                    />
                    <span className="grid min-w-0 flex-1 gap-1">
                      <span className="flex flex-wrap items-center gap-1.5">
                        <span className="text-sm font-medium">{r.version_number || r.name}</span>
                        <VersionTypeBadge type={r.version_type} />
                        {idx === 0 && mcFilter === ALL && (
                          <span className="inline-flex h-5 items-center rounded-md bg-primary/10 px-1.5 text-[11px] font-medium text-primary">
                            Latest
                          </span>
                        )}
                        {r.name && r.name !== r.version_number && (
                          <span className="min-w-0 truncate text-xs text-muted-foreground" title={r.name}>
                            {r.name}
                          </span>
                        )}
                      </span>
                      <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
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
                          </span>
                        )}
                        {r.file?.size ? (
                          <span className="flex items-center gap-1 tabular-nums">
                            <FileArchive className="size-3" aria-hidden />
                            {formatBytes(r.file.size)}
                          </span>
                        ) : null}
                        {r.recommended_memory_mb ? (
                          <span className="flex items-center gap-1 tabular-nums">
                            <MemoryStick className="size-3" aria-hidden />
                            {formatMemoryMb(r.recommended_memory_mb)} recommended
                          </span>
                        ) : null}
                      </span>
                    </span>
                  </label>
                  <Button
                    type="button"
                    variant="ghost"
                    size="xs"
                    className="shrink-0 text-muted-foreground"
                    aria-expanded={changelogOpen}
                    aria-controls={`${uid}-cl-${r.id}`}
                    onClick={() => setOpenChangelog(changelogOpen ? null : r.id)}
                  >
                    <span className="hidden sm:inline">Changelog</span>
                    <span className="sr-only sm:hidden">Changelog for {r.version_number}</span>
                    <ChevronDown className={cn("transition-transform", changelogOpen && "rotate-180")} />
                  </Button>
                </div>
                {changelogOpen && (
                  <div id={`${uid}-cl-${r.id}`} className="px-2.5 pb-2.5">
                    <Changelog provider={provider} projectId={projectId} release={r} />
                  </div>
                )}
              </div>
            );
          })}
          {filtered.length > INITIAL_VISIBLE && (
            <Button type="button" variant="ghost" size="sm" className="justify-self-start" onClick={() => setShowAll((s) => !s)}>
              {showAll ? "Show fewer releases" : `Show all ${filtered.length} releases`}
              {selectedHidden && !showAll && " (selected release is further down)"}
            </Button>
          )}
        </div>
      )}
      {selectedId && query.isSuccess && (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Pin className="size-3" aria-hidden />
          The server is pinned to the selected release and will not upgrade itself on restart.
        </p>
      )}
    </div>
  );
}
