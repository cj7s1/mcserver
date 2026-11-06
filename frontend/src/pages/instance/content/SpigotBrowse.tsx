/**
 * Search SpigotMC through Spiget (api.spiget.org). Free resources whose jar is hosted on SpigotMC can be
 * installed from Spiget's mirror; premium, external and archive resources only get a link to SpigotMC.
 */
import { Download, ExternalLink, Loader2, Search, ThumbsUp } from "lucide-react";
import { useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatNumber, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useDebounced } from "./BrowseContent";
import { safeHttpUrl, testedSummary, useSpigotSearch, type SpigotResource } from "./content-api";
import { InstallAction, Pager, SourceResultRow, type ContentEnv } from "./content-ui";
import { SpigotInstallDialog, type SpigotTarget } from "./SpigotInstallDialog";

const REASON_LABEL: Record<string, string> = {
  premium: "Premium",
  external: "External",
  not_a_jar: "Not a jar",
};

/** Small pill with a tooltip; the text is the label, the tooltip the full explanation. */
function Pill({ label, tip, className }: { label: string; tip: string; className: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className={cn(
            "inline-flex h-4 items-center rounded-full px-1.5 text-[10px] font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring",
            className,
          )}
        >
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{tip}</TooltipContent>
    </Tooltip>
  );
}

function ResourceMeta({ res, env }: { res: SpigotResource; env: ContentEnv }) {
  return (
    <>
      <span className="inline-flex items-center gap-1" title={`${res.downloads ?? 0} downloads`}>
        <Download className="size-3" aria-hidden /> {formatNumber(res.downloads)}
        <span className="sr-only">downloads</span>
      </span>
      {res.likes != null && (
        <span className="inline-flex items-center gap-1" title={`${res.likes} likes`}>
          <ThumbsUp className="size-3" aria-hidden /> {formatNumber(res.likes)}
          <span className="sr-only">likes</span>
        </span>
      )}
      {res.updated && <span>Updated {relativeTime(res.updated)}</span>}
      {res.reason && (
        <Pill
          label={res.reason === "premium" && res.price ? `Premium · ${res.price} ${res.currency ?? ""}`.trim() : REASON_LABEL[res.reason]}
          tip={res.reason_message ?? "Cannot be installed automatically"}
          className="bg-muted text-muted-foreground"
        />
      )}
      {res.tested === false && (
        <Pill
          label={`Not tested on ${env.minecraftVersion}`}
          tip={`The author lists these tested Minecraft versions: ${testedSummary(res)}. It may still work, or it may not.`}
          className="bg-amber-500/10 text-amber-700 dark:text-amber-300"
        />
      )}
    </>
  );
}

export function SpigotBrowse({ env }: { env: ContentEnv }) {
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 350);
  const [paging, setPaging] = useState({ q, page: 1 });
  const page = paging.q === q ? paging.page : 1;
  const [target, setTarget] = useState<SpigotTarget | null>(null);
  const canSearch = !!env.minecraftVersion;
  const search = useSpigotSearch(env.instance.id, { q, page }, canSearch);
  const data = search.data;

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
        <Label htmlFor="spigot-search">Search SpigotMC</Label>
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input
            id="spigot-search"
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Search SpigotMC resources by name…"
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
        {q ? "Name matches" : "The most downloaded free resources"} on SpigotMC, via the Spiget API. Only free plugins hosted on SpigotMC can
        be installed here, from Spiget's copy of the newest file; SpigotMC lists tested Minecraft versions per plugin, not per version.
      </p>

      {search.isLoading ? (
        <LoadingState label="Searching SpigotMC…" />
      ) : search.error ? (
        <ErrorState error={search.error} title="Search failed" onRetry={() => search.refetch()} />
      ) : !data || data.results.length === 0 ? (
        <EmptyState
          icon={<Search className="size-5" />}
          title={q ? `No SpigotMC resources match “${q}”` : "No SpigotMC resources found"}
          description="Spiget searches resource names. Try a shorter or different name."
        />
      ) : (
        <>
          <ul className={search.isPlaceholderData ? "grid grid-cols-1 gap-2 opacity-60 transition-opacity" : "grid grid-cols-1 gap-2"} aria-busy={search.isFetching}>
            {data.results.map((res) => {
              const url = safeHttpUrl(res.url);
              const open = () => setTarget({ id: res.id, title: res.title, icon_url: res.icon_url, description: res.description, url });
              return (
                <SourceResultRow
                  key={res.id}
                  title={res.title}
                  url={url}
                  site="SpigotMC"
                  description={res.description}
                  iconUrl={res.icon_url}
                  meta={<ResourceMeta res={res} env={env} />}
                  action={
                    res.installable ? (
                      <InstallAction env={env} installed={res.installed} title={res.title} onInstall={open} />
                    ) : (
                      <Button size="sm" variant="outline" onClick={open} aria-label={`Details for ${res.title}`}>
                        <ExternalLink /> Details…
                      </Button>
                    )
                  }
                />
              );
            })}
          </ul>
          <Pager
            label="SpigotMC result pages"
            summary={`Page ${data.page} of ${Math.max(data.pages, data.page).toLocaleString()}`}
            canPrev={page > 1}
            canNext={page < data.pages}
            busy={search.isFetching}
            onPrev={() => setPaging({ q, page: Math.max(1, page - 1) })}
            onNext={() => setPaging({ q, page: page + 1 })}
          />
        </>
      )}

      <SpigotInstallDialog env={env} target={target} onOpenChange={(o) => !o && setTarget(null)} />
    </div>
  );
}
