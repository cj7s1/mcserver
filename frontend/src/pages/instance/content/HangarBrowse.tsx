/** Search Hangar (hangar.papermc.io, PaperMC's plugin repository) for Paper-family plugins. */
import { Download, Loader2, Search, Star } from "lucide-react";
import { useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatNumber, relativeTime } from "@/lib/format";
import { useDebounced } from "./BrowseContent";
import { HANGAR_PAGE_SIZE, safeHttpUrl, useHangarSearch } from "./content-api";
import { InstallAction, Pager, SourceResultRow, type ContentEnv } from "./content-ui";
import { HangarInstallDialog, type HangarTarget } from "./HangarInstallDialog";

export function HangarBrowse({ env }: { env: ContentEnv }) {
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 350);
  const [page, setPage] = useState({ q, offset: 0 });
  const offset = page.q === q ? page.offset : 0;
  const [target, setTarget] = useState<HangarTarget | null>(null);
  const canSearch = !!env.minecraftVersion;
  const search = useHangarSearch(env.instance.id, { q, offset }, canSearch);
  const data = search.data;
  const total = data?.total ?? 0;

  if (!canSearch) {
    return (
      <EmptyState
        icon={<Search className="size-5" />}
        title="Search needs a specific Minecraft version"
        description="This server follows the newest Minecraft release, which can change on restart, so compatible plugins cannot be chosen. Set a specific Minecraft version in Settings first."
      />
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3">
      <div className="grid gap-1">
        <Label htmlFor="hangar-search">Search Hangar</Label>
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input
            id="hangar-search"
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Search Paper plugins on Hangar…"
            className="pl-8"
            maxLength={100}
            autoComplete="off"
          />
          {search.isFetching && (
            <Loader2 className="absolute top-1/2 right-2.5 size-4 -translate-y-1/2 animate-spin text-muted-foreground" aria-label="Searching" />
          )}
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Plugins with a Paper build for Minecraft {env.minecraftVersion}, most downloaded first. Installs are checked against the SHA-256
        hash Hangar publishes.
      </p>

      {search.isLoading ? (
        <LoadingState label="Searching Hangar…" />
      ) : search.error ? (
        <ErrorState error={search.error} title="Search failed" onRetry={() => search.refetch()} />
      ) : !data || data.results.length === 0 ? (
        <EmptyState
          icon={<Search className="size-5" />}
          title={q ? `No Hangar plugins match “${q}”` : "No Hangar plugins found"}
          description={`Only plugins with a Paper build for Minecraft ${env.minecraftVersion} are listed.`}
        />
      ) : (
        <>
          <ul className={search.isPlaceholderData ? "grid grid-cols-1 gap-2 opacity-60 transition-opacity" : "grid grid-cols-1 gap-2"} aria-busy={search.isFetching}>
            {data.results.map((hit) => {
              const title = hit.title ?? hit.slug;
              return (
                <SourceResultRow
                  key={hit.id}
                  title={title}
                  url={safeHttpUrl(hit.url)}
                  site="Hangar"
                  author={hit.author}
                  description={hit.description}
                  iconUrl={hit.icon_url}
                  meta={
                    <>
                      <span className="inline-flex items-center gap-1" title={`${hit.downloads ?? 0} downloads`}>
                        <Download className="size-3" aria-hidden /> {formatNumber(hit.downloads)}
                        <span className="sr-only">downloads</span>
                      </span>
                      {hit.follows != null && (
                        <span className="inline-flex items-center gap-1" title={`${hit.follows} stars`}>
                          <Star className="size-3" aria-hidden /> {formatNumber(hit.follows)}
                          <span className="sr-only">stars</span>
                        </span>
                      )}
                      {hit.updated && <span>Updated {relativeTime(hit.updated)}</span>}
                    </>
                  }
                  action={
                    <InstallAction
                      env={env}
                      installed={hit.installed}
                      title={title}
                      onInstall={() =>
                        setTarget({
                          slug: hit.slug,
                          title: hit.title,
                          icon_url: hit.icon_url,
                          description: hit.description,
                          url: hit.url,
                          author: hit.author,
                          installed: hit.installed,
                        })
                      }
                    />
                  }
                />
              );
            })}
          </ul>
          <Pager
            label="Hangar result pages"
            summary={`${offset + 1}–${offset + data.results.length} of ${total.toLocaleString()}`}
            canPrev={offset > 0}
            canNext={offset + HANGAR_PAGE_SIZE < total}
            busy={search.isFetching}
            onPrev={() => setPage({ q, offset: Math.max(0, offset - HANGAR_PAGE_SIZE) })}
            onNext={() => setPage({ q, offset: offset + HANGAR_PAGE_SIZE })}
          />
        </>
      )}

      <HangarInstallDialog
        env={env}
        target={target}
        onOpenChange={(o) => !o && setTarget(null)}
        onSearch={(query) => {
          setTarget(null);
          setText(query);
        }}
      />
    </div>
  );
}
