import { ChevronLeft, ChevronRight, Download, Loader2, PackageSearch, Search, X } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { ServerIcon } from "@/components/ServerIcon";
import { EmptyState, ErrorState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useAuth } from "@/hooks/use-auth";
import { formatDateTime, formatNumber, relativeTime } from "@/lib/format";
import type { Project, Provider } from "@/lib/types";
import { cn } from "@/lib/utils";
import { compactVersions, LoaderBadges, loaderLabel, MODPACK_LOADERS } from "./modpackMeta";
import { type SortKey, useGameVersions, useModpackSearch, useProviders } from "./modpackQueries";
import { VersionCombobox } from "./VersionCombobox";

export interface ModpackFilters {
  provider: Provider;
  q: string;
  loaders: string[];
  gameVersion: string;
  sort: SortKey;
  page: number;
}

export const DEFAULT_MODPACK_FILTERS: ModpackFilters = {
  provider: "modrinth",
  q: "",
  loaders: [],
  gameVersion: "",
  sort: "relevance",
  page: 0,
};

const SORTS: { value: SortKey; label: string }[] = [
  { value: "relevance", label: "Relevance" },
  { value: "downloads", label: "Downloads" },
  { value: "follows", label: "Follows" },
  { value: "newest", label: "Newest" },
  { value: "updated", label: "Recently updated" },
];

/** Provider tabs; providers that are not configured (CurseForge without a key) are disabled with their note. */
export function ProviderTabs({ value, onChange }: { value: Provider; onChange: (p: Provider) => void }) {
  const { data: providers, isLoading } = useProviders();
  const { can } = useAuth();
  const list = providers ?? [];
  const unavailable = list.filter((p) => !p.available && p.note);
  return (
    <div className="grid gap-1.5">
      <Tabs value={value} onValueChange={(v) => onChange(v as Provider)}>
        <TabsList aria-label="Modpack provider">
          {isLoading && !list.length ? (
            <span className="flex items-center gap-1.5 px-2 text-xs">
              <Loader2 className="size-3 animate-spin" /> Loading providers…
            </span>
          ) : (
            list.map((p) => (
              <TabsTrigger key={p.id} value={p.id} disabled={!p.available} title={p.note ?? undefined}>
                {p.label}
              </TabsTrigger>
            ))
          )}
        </TabsList>
      </Tabs>
      {unavailable.map((p) => (
        <p key={p.id} className="text-xs text-muted-foreground">
          <span className="font-medium text-foreground/80">{p.label} unavailable:</span> {p.note}{" "}
          {p.requires_key && can("system.settings") && (
            <Link to="/admin/system" className="text-primary underline-offset-4 hover:underline">
              Open System settings
            </Link>
          )}
        </p>
      ))}
    </div>
  );
}

function ResultCard({
  project,
  selected,
  onSelect,
  layout,
}: {
  project: Project;
  selected: boolean;
  onSelect: (p: Project) => void;
  layout: "grid" | "list";
}) {
  const versions = compactVersions(project.game_versions, layout === "grid" ? 3 : 2);
  return (
    <button
      type="button"
      onClick={() => onSelect(project)}
      aria-pressed={selected}
      className={cn(
        "group flex w-full min-w-0 gap-3 rounded-lg border bg-card text-left transition-colors outline-none hover:border-primary/40 hover:bg-muted/40 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50",
        layout === "grid" ? "p-3" : "p-2.5",
        selected && "border-primary/60 bg-primary/5 ring-1 ring-primary/30",
      )}
    >
      <ServerIcon url={project.icon_url} name={project.title} className={layout === "grid" ? "size-12" : "size-10"} />
      <div className="min-w-0 flex-1">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium" title={project.title}>
              {project.title}
            </div>
            {project.author && <div className="truncate text-xs text-muted-foreground">by {project.author}</div>}
          </div>
          {project.downloads != null && (
            <span
              className="flex shrink-0 items-center gap-1 text-xs tabular-nums text-muted-foreground"
              title={`${project.downloads.toLocaleString()} downloads`}
            >
              <Download className="size-3" aria-hidden />
              <span className="sr-only">Downloads:</span>
              {formatNumber(project.downloads)}
            </span>
          )}
        </div>
        {project.description && (
          <p className={cn("mt-1 text-xs text-muted-foreground", layout === "grid" ? "line-clamp-2" : "line-clamp-1")}>
            {project.description}
          </p>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
          <LoaderBadges loaders={project.loaders} />
          <span className="tabular-nums" title={project.game_versions.join(", ")}>
            MC {versions}
          </span>
          {project.updated != null && project.updated !== "" && (
            <span title={formatDateTime(project.updated)}>· updated {relativeTime(project.updated)}</span>
          )}
        </div>
      </div>
    </button>
  );
}

function ResultsSkeleton({ layout, count }: { layout: "grid" | "list"; count: number }) {
  return (
    <div className={layout === "grid" ? "grid gap-3 sm:grid-cols-2 xl:grid-cols-3" : "grid gap-2"} aria-hidden>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="flex gap-3 rounded-lg border p-3">
          <Skeleton className="size-11 rounded-lg" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-3.5 w-2/3" />
            <Skeleton className="h-3 w-1/3" />
            <Skeleton className="h-3 w-full" />
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * Searchable, filterable, paginated modpack list. Filter state is controlled by the
 * parent so the modpacks page can keep it in the URL and the wizard in local state.
 */
export function ModpackBrowser({
  filters,
  onFiltersChange,
  selectedId,
  onSelect,
  layout = "grid",
  pageSize = 20,
  className,
}: {
  filters: ModpackFilters;
  onFiltersChange: (f: ModpackFilters) => void;
  selectedId?: string | null;
  onSelect: (project: Project) => void;
  layout?: "grid" | "list";
  pageSize?: number;
  className?: string;
}) {
  const uid = useId();
  const providers = useProviders();
  const providerInfo = providers.data?.find((p) => p.id === filters.provider);
  const providerAvailable = providerInfo ? providerInfo.available : true;
  const mcVersions = useGameVersions("VANILLA", false);

  // Search text is debounced locally; `syncedQ` is the last value exchanged with the
  // parent so an external change (URL navigation, reset) updates the box without
  // clobbering what the user is still typing.
  const [qInput, setQInput] = useState(filters.q);
  const [syncedQ, setSyncedQ] = useState(filters.q);
  if (filters.q !== syncedQ) {
    setSyncedQ(filters.q);
    setQInput(filters.q);
  }
  const latest = useRef({ filters, onFiltersChange });
  useEffect(() => {
    latest.current = { filters, onFiltersChange };
  });
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const onQueryInput = (value: string) => {
    setQInput(value);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      const { filters: f, onFiltersChange: change } = latest.current;
      if (f.q === value) return;
      setSyncedQ(value);
      change({ ...f, q: value, page: 0 });
    }, 350);
  };

  const params = useMemo(
    () => ({
      q: filters.q.trim(),
      loaders: filters.loaders,
      gameVersion: filters.gameVersion,
      sort: filters.sort,
      offset: filters.page * pageSize,
      limit: pageSize,
    }),
    [filters, pageSize],
  );
  const search = useModpackSearch(filters.provider, params, providerAvailable);
  const data = search.data;
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total ? params.offset + 1 : 0;
  const to = Math.min(params.offset + (data?.results.length ?? 0), total);
  const update = (patch: Partial<ModpackFilters>) => onFiltersChange({ ...filters, page: 0, ...patch });
  const hasFilters = filters.loaders.length > 0 || !!filters.gameVersion || !!filters.q;

  const versionOptions = useMemo(
    () => (mcVersions.data?.versions ?? []).map((v) => ({ version: v.version })),
    [mcVersions.data],
  );

  return (
    <div className={cn("grid min-w-0 gap-3", className)}>
      <ProviderTabs value={filters.provider} onChange={(provider) => update({ provider })} />

      <div className="relative">
        <Label htmlFor={`${uid}-q`} className="sr-only">
          Search modpacks
        </Label>
        <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input
          id={`${uid}-q`}
          type="search"
          value={qInput}
          onChange={(e) => onQueryInput(e.target.value)}
          placeholder={`Search ${providerInfo?.label ?? "modpacks"} (e.g. Better MC)`}
          className="pl-8"
          autoComplete="off"
        />
        {search.isFetching && (
          <Loader2 className="absolute top-1/2 right-2.5 size-4 -translate-y-1/2 animate-spin text-muted-foreground" aria-label="Searching" />
        )}
      </div>

      <div className={cn("flex flex-wrap items-end gap-x-3 gap-y-2", layout === "list" && "gap-x-2")}>
        <div className="grid gap-1">
          <span id={`${uid}-loaders`} className="text-xs font-medium text-muted-foreground">
            Loader
          </span>
          <ToggleGroup
            type="multiple"
            variant="outline"
            size="sm"
            spacing={0}
            value={filters.loaders}
            onValueChange={(loaders) => update({ loaders })}
            aria-labelledby={`${uid}-loaders`}
          >
            {MODPACK_LOADERS.map((l) => (
              <ToggleGroupItem key={l} value={l} className="px-2 text-xs">
                {loaderLabel(l)}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <div className="grid min-w-32 flex-1 gap-1 sm:max-w-44">
          <Label htmlFor={`${uid}-mc`} className="text-xs font-medium text-muted-foreground">
            Minecraft version
          </Label>
          <VersionCombobox
            id={`${uid}-mc`}
            value={filters.gameVersion}
            onChange={(gameVersion) => update({ gameVersion })}
            options={versionOptions}
            clearLabel="Any version"
            loading={mcVersions.isLoading}
            className="h-7 text-xs"
          />
        </div>
        <div className="grid gap-1">
          <Label htmlFor={`${uid}-sort`} className="text-xs font-medium text-muted-foreground">
            Sort by
          </Label>
          <Select value={filters.sort} onValueChange={(v) => update({ sort: v as SortKey })}>
            <SelectTrigger id={`${uid}-sort`} size="sm" className="min-w-32 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SORTS.map((s) => (
                <SelectItem key={s.value} value={s.value}>
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {hasFilters && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              clearTimeout(timer.current);
              setQInput("");
              setSyncedQ("");
              update({ q: "", loaders: [], gameVersion: "" });
            }}
          >
            <X /> Clear
          </Button>
        )}
      </div>

      {filters.provider === "ftb" && (filters.loaders.length > 0 || filters.gameVersion) && (
        <p className="text-xs text-muted-foreground">
          Feed The Beast search has no server-side filters; loader and version filters are applied to each page of results.
        </p>
      )}

      <div className="min-w-0" aria-live="polite" aria-busy={search.isFetching}>
        {!providerAvailable ? (
          <EmptyState
            icon={<PackageSearch className="size-5" />}
            title={`${providerInfo?.label ?? "This provider"} is not available`}
            description={providerInfo?.note ?? "This provider is not configured."}
          />
        ) : search.isLoading ? (
          <ResultsSkeleton layout={layout} count={layout === "grid" ? 6 : 5} />
        ) : search.isError ? (
          <ErrorState error={search.error} onRetry={() => search.refetch()} title="Search failed" />
        ) : !data?.results.length ? (
          <EmptyState
            icon={<PackageSearch className="size-5" />}
            title="No modpacks found"
            description={
              hasFilters ? "Try a different search term or remove some filters." : "This provider returned no modpacks."
            }
          />
        ) : (
          <div
            className={cn(
              layout === "grid" ? "grid gap-3 sm:grid-cols-2 xl:grid-cols-3" : "grid gap-2",
              search.isPlaceholderData && "opacity-60 transition-opacity",
            )}
          >
            {data.results.map((p) => (
              <ResultCard key={`${p.provider}:${p.id}`} project={p} selected={p.id === selectedId} onSelect={onSelect} layout={layout} />
            ))}
          </div>
        )}
      </div>

      {providerAvailable && data && total > 0 && (
        <nav className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground" aria-label="Search results pages">
          <span className="tabular-nums">
            {from}–{to} of {total.toLocaleString()}
          </span>
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              aria-label="Previous page"
              disabled={filters.page === 0 || search.isFetching}
              onClick={() => onFiltersChange({ ...filters, page: filters.page - 1 })}
            >
              <ChevronLeft />
            </Button>
            <span className="px-2 tabular-nums">
              Page {filters.page + 1} of {pages.toLocaleString()}
            </span>
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              aria-label="Next page"
              disabled={filters.page + 1 >= pages || search.isFetching}
              onClick={() => onFiltersChange({ ...filters, page: filters.page + 1 })}
            >
              <ChevronRight />
            </Button>
          </div>
        </nav>
      )}
    </div>
  );
}
