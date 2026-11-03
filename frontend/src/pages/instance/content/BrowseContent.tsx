/** Search Modrinth for mods/plugins compatible with this server. */
import { CheckCircle2, ChevronLeft, ChevronRight, Download, ExternalLink, Heart, Loader2, Search, Users } from "lucide-react";
import { useEffect, useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatNumber, relativeTime } from "@/lib/format";
import { SEARCH_PAGE_SIZE, useContentSearch, type ContentSearchHit, type SearchSort } from "./content-api";
import { Notice, ProjectIcon, type ContentEnv } from "./content-ui";
import { InstallContentDialog, type InstallTarget } from "./InstallContentDialog";

function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

const SORTS: { value: SearchSort; label: string }[] = [
  { value: "relevance", label: "Relevance" },
  { value: "downloads", label: "Downloads" },
  { value: "follows", label: "Follows" },
  { value: "newest", label: "Newest" },
  { value: "updated", label: "Recently updated" },
];

function sideNote(hit: ContentSearchHit, env: ContentEnv): string | null {
  if (env.kind === "plugins") return null;
  if (hit.client_side === "required") return "Players must install it too";
  if (hit.client_side === "unsupported") return "Server-side only";
  return null;
}

function ResultRow({ hit, env, onInstall }: { hit: ContentSearchHit; env: ContentEnv; onInstall: () => void }) {
  const note = sideNote(hit, env);
  const title = hit.title ?? hit.slug ?? hit.id;
  return (
    <li className="flex flex-col gap-3 rounded-lg border bg-card p-3 sm:flex-row sm:items-start">
      <div className="flex min-w-0 flex-1 gap-3">
        <ProjectIcon url={hit.icon_url} className="size-12 rounded-lg" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
            {hit.url ? (
              <a
                href={hit.url}
                target="_blank"
                rel="noopener noreferrer"
                className="truncate font-medium hover:underline focus-visible:underline"
                aria-label={`${title} on Modrinth (opens in a new tab)`}
              >
                {title}
              </a>
            ) : (
              <span className="truncate font-medium">{title}</span>
            )}
            {hit.author && <span className="text-xs text-muted-foreground">by {hit.author}</span>}
          </div>
          {hit.description && <p className="mt-0.5 line-clamp-2 text-sm text-muted-foreground">{hit.description}</p>}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1" title={`${hit.downloads ?? 0} downloads`}>
              <Download className="size-3" aria-hidden /> {formatNumber(hit.downloads)}
              <span className="sr-only">downloads</span>
            </span>
            <span className="inline-flex items-center gap-1" title={`${hit.follows ?? 0} followers`}>
              <Heart className="size-3" aria-hidden /> {formatNumber(hit.follows)}
              <span className="sr-only">followers</span>
            </span>
            {hit.updated && <span>Updated {relativeTime(hit.updated)}</span>}
            {note && (
              <span className="inline-flex items-center gap-1">
                <Users className="size-3" aria-hidden /> {note}
              </span>
            )}
            {hit.categories.slice(0, 3).map((c) => (
              <Badge key={c} variant="secondary" className="h-4 px-1.5 text-[10px] capitalize">
                {c.replace(/-/g, " ")}
              </Badge>
            ))}
          </div>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2 sm:flex-col sm:items-end">
        {hit.installed ? (
          <span className="inline-flex h-7 items-center gap-1 rounded-lg bg-emerald-500/10 px-2.5 text-xs font-medium text-emerald-700 dark:text-emerald-300">
            <CheckCircle2 className="size-3.5" aria-hidden /> Installed
          </span>
        ) : env.canEdit ? (
          env.locked ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span tabIndex={0}>
                  <Button size="sm" disabled>
                    <Download /> Install
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent>{env.locked}</TooltipContent>
            </Tooltip>
          ) : (
            <Button size="sm" onClick={onInstall} aria-label={`Install ${title}`}>
              <Download /> Install…
            </Button>
          )
        ) : null}
        {hit.url && (
          <Button asChild variant="ghost" size="icon-sm" className="sm:hidden">
            <a href={hit.url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${title} on Modrinth`}>
              <ExternalLink />
            </a>
          </Button>
        )}
      </div>
    </li>
  );
}

export function BrowseContent({ env, initialQuery }: { env: ContentEnv; initialQuery: string }) {
  const [text, setText] = useState(initialQuery);
  const [sort, setSort] = useState<SearchSort>("relevance");
  const q = useDebounced(text.trim(), 350);
  // the page offset belongs to one query+sort; any change of either starts again at the top
  const pageKey = `${q}|${sort}`;
  const [page, setPage] = useState({ key: pageKey, offset: 0 });
  const offset = page.key === pageKey ? page.offset : 0;
  const [target, setTarget] = useState<InstallTarget | null>(null);

  const canSearch = !!env.minecraftVersion;
  const search = useContentSearch(env.instance.id, { q, offset, sort }, canSearch);
  const data = search.data;
  const total = data?.total ?? 0;
  const ownWarnings = (data?.warnings ?? []).filter((w) => w.startsWith("Could not"));

  if (!canSearch) {
    return (
      <EmptyState
        icon={<Search className="size-5" />}
        title="Search needs a specific Minecraft version"
        description={`This server follows the newest Minecraft release, which can change on restart, so compatible ${env.noun.many} cannot be chosen. Set a specific Minecraft version in Settings first.`}
      />
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="grid flex-1 gap-1">
          <Label htmlFor="content-search">Search Modrinth</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              id="content-search"
              type="search"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={`Search ${env.platform} ${env.noun.many}…`}
              className="pl-8"
              maxLength={200}
              autoComplete="off"
            />
            {search.isFetching && (
              <Loader2 className="absolute top-1/2 right-2.5 size-4 -translate-y-1/2 animate-spin text-muted-foreground" aria-label="Searching" />
            )}
          </div>
        </div>
        <div className="grid gap-1">
          <Label htmlFor="content-search-sort">Sort by</Label>
          <Select value={sort} onValueChange={(v) => setSort(v as SearchSort)}>
            <SelectTrigger id="content-search-sort" className="w-full sm:w-44">
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
      </div>
      <p className="text-xs text-muted-foreground">
        Showing {env.noun.many} for {env.platform} on Minecraft {env.minecraftVersion} that run on servers. Only versions built for
        this exact setup can be installed.
      </p>
      {ownWarnings.map((w) => (
        <Notice key={w} tone="warning">
          {w}
        </Notice>
      ))}

      {search.isLoading ? (
        <LoadingState label="Searching Modrinth…" />
      ) : search.error ? (
        <ErrorState error={search.error} title="Search failed" onRetry={() => search.refetch()} />
      ) : !data || data.results.length === 0 ? (
        <EmptyState
          icon={<Search className="size-5" />}
          title={q ? `No compatible ${env.noun.many} match “${q}”` : `No compatible ${env.noun.many} found`}
          description={`Only ${env.noun.many} with a ${env.platform} build for Minecraft ${env.minecraftVersion} are listed. Try another search term.`}
        />
      ) : (
        <>
          <ul className={search.isPlaceholderData ? "grid grid-cols-1 gap-2 opacity-60 transition-opacity" : "grid grid-cols-1 gap-2"} aria-busy={search.isFetching}>
            {data.results.map((hit) => (
              <ResultRow
                key={hit.id}
                hit={hit}
                env={env}
                onInstall={() =>
                  setTarget({ id: hit.id, title: hit.title, icon_url: hit.icon_url, description: hit.description, url: hit.url })
                }
              />
            ))}
          </ul>
          <nav className="flex flex-wrap items-center justify-between gap-2" aria-label="Search result pages">
            <span className="text-xs text-muted-foreground tabular-nums">
              {offset + 1}–{offset + data.results.length} of {total.toLocaleString()}
            </span>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={offset === 0 || search.isFetching}
                onClick={() => setPage({ key: pageKey, offset: Math.max(0, offset - SEARCH_PAGE_SIZE) })}
              >
                <ChevronLeft /> Previous
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={offset + SEARCH_PAGE_SIZE >= total || search.isFetching}
                onClick={() => setPage({ key: pageKey, offset: offset + SEARCH_PAGE_SIZE })}
              >
                Next <ChevronRight />
              </Button>
            </div>
          </nav>
        </>
      )}

      <InstallContentDialog env={env} target={target} onOpenChange={(o) => !o && setTarget(null)} />
    </div>
  );
}
