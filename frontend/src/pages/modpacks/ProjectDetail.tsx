import { Collapsible } from "radix-ui";
import { AlertTriangle, CalendarClock, ChevronRight, Download, ExternalLink, Heart, Scale } from "lucide-react";
import { type ReactNode, useState } from "react";
import { ServerIcon } from "@/components/ServerIcon";
import { ErrorState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, formatNumber, relativeTime } from "@/lib/format";
import type { Project, Provider } from "@/lib/types";
import { cn } from "@/lib/utils";
import { followsLabel, LoaderBadges, plainText, providerLabel, sortVersionsDesc } from "./modpackMeta";
import { type ProjectDetailData, useModpackProject } from "./modpackQueries";

function Stat({ icon, label, value, title }: { icon: ReactNode; label: string; value: ReactNode; title?: string }) {
  return (
    <div className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground" title={title}>
      <span className="shrink-0 [&_svg]:size-3.5" aria-hidden>
        {icon}
      </span>
      <span className="truncate">
        <span className="font-medium tabular-nums text-foreground">{value}</span> {label}
      </span>
    </div>
  );
}

/** Modpack project header + metadata. Shows the search hit immediately while the detail loads. */
export function ProjectDetail({
  provider,
  projectId,
  fallback,
  actions,
  className,
}: {
  provider: Provider;
  projectId: string;
  /** The search hit, rendered while the full project loads (and for fields the detail lacks, e.g. Modrinth author). */
  fallback?: Project | null;
  actions?: ReactNode;
  className?: string;
}) {
  const query = useModpackProject(provider, projectId);
  const [showAllVersions, setShowAllVersions] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const hit = fallback && fallback.id === projectId ? fallback : null;
  const p: ProjectDetailData | Project | null = query.data ?? hit;

  if (!p) {
    if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} title="Could not load modpack" />;
    return (
      <div className={cn("grid gap-3", className)} aria-busy>
        <div className="flex gap-3">
          <Skeleton className="size-14 rounded-xl" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-3 w-1/4" />
          </div>
        </div>
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  const author = p.author || hit?.author;
  const versions = sortVersionsDesc(p.game_versions ?? []);
  const shownVersions = showAllVersions ? versions : versions.slice(0, 8);
  const about = plainText(p.body);
  const gallery = (p.gallery ?? []).slice(0, 4);
  const categories = (p.categories ?? []).filter((c) => !["fabric", "forge", "neoforge", "quilt"].includes(c));

  return (
    <div className={cn("grid min-w-0 gap-3", className)}>
      <div className="flex min-w-0 items-start gap-3">
        <ServerIcon url={p.icon_url} name={p.title} className="size-14 rounded-xl" />
        <div className="min-w-0 flex-1">
          <h3 className="text-base leading-tight font-semibold break-words">{p.title}</h3>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
            {author && <span>by {author}</span>}
            <span>on {providerLabel(provider)}</span>
          </div>
          {p.description && <p className="mt-1.5 text-sm text-muted-foreground">{p.description}</p>}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 sm:grid-cols-4">
        {p.downloads != null && (
          <Stat icon={<Download />} label="downloads" value={formatNumber(p.downloads)} title={p.downloads.toLocaleString()} />
        )}
        {p.follows != null && <Stat icon={<Heart />} label={followsLabel(provider)} value={formatNumber(p.follows)} />}
        {p.updated != null && p.updated !== "" && (
          <Stat icon={<CalendarClock />} label="" value={`Updated ${relativeTime(p.updated)}`} title={formatDateTime(p.updated)} />
        )}
        {p.license && <Stat icon={<Scale />} label="" value={p.license.replace(/^LicenseRef-/, "").replace(/-/g, " ")} />}
      </div>

      <dl className="grid gap-2 text-xs sm:grid-cols-[7rem_minmax(0,1fr)]">
        <dt className="text-muted-foreground">Loaders</dt>
        <dd>{p.loaders.length ? <LoaderBadges loaders={p.loaders} /> : "—"}</dd>
        <dt className="text-muted-foreground">Minecraft</dt>
        <dd className="flex flex-wrap items-center gap-1">
          {shownVersions.length
            ? shownVersions.map((v) => (
                <span key={v} className="rounded-md bg-muted px-1.5 py-0.5 tabular-nums">
                  {v}
                </span>
              ))
            : "—"}
          {versions.length > 8 && (
            <Button type="button" variant="link" size="xs" className="h-5 px-1" onClick={() => setShowAllVersions((s) => !s)}>
              {showAllVersions ? "Show fewer" : `+${versions.length - 8} more`}
            </Button>
          )}
        </dd>
        {categories.length > 0 && (
          <>
            <dt className="text-muted-foreground">Categories</dt>
            <dd className="flex flex-wrap gap-1">
              {categories.map((c) => (
                <span key={c} className="rounded-md border px-1.5 py-0.5 capitalize">
                  {c.replace(/-/g, " ")}
                </span>
              ))}
            </dd>
          </>
        )}
        {p.server_side && (
          <>
            <dt className="text-muted-foreground">Server side</dt>
            <dd className="capitalize">{p.server_side}</dd>
          </>
        )}
      </dl>

      {p.server_side === "unsupported" && (
        <div className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-800 dark:text-amber-200">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          This modpack is marked client-only by its author and cannot run as a server.
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {actions}
        {p.url && (
          <Button asChild variant="outline" size="sm">
            <a href={p.url} target="_blank" rel="noreferrer noopener">
              <ExternalLink /> View on {providerLabel(provider)}
            </a>
          </Button>
        )}
      </div>

      {(about || gallery.length > 0) && (
        <Collapsible.Root open={aboutOpen} onOpenChange={setAboutOpen}>
          <Collapsible.Trigger asChild>
            <Button type="button" variant="ghost" size="sm" className="-ml-2 text-muted-foreground">
              <ChevronRight className={cn("transition-transform", aboutOpen && "rotate-90")} />
              About this modpack
            </Button>
          </Collapsible.Trigger>
          <Collapsible.Content className="mt-2 grid gap-3">
            {gallery.length > 0 && (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {gallery.map((src, i) => (
                  <img
                    key={src}
                    src={src}
                    alt={`${p.title} screenshot ${i + 1}`}
                    loading="lazy"
                    className="aspect-video w-full rounded-md bg-muted object-cover"
                  />
                ))}
              </div>
            )}
            {about && (
              <div className="max-h-64 overflow-y-auto rounded-lg border bg-muted/30 p-3 text-xs leading-relaxed whitespace-pre-wrap text-muted-foreground">
                {about}
              </div>
            )}
          </Collapsible.Content>
        </Collapsible.Root>
      )}
    </div>
  );
}
