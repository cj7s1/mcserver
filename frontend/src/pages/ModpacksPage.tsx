import { Package, Plus, Rocket } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { PageHeader, Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useAuth } from "@/hooks/use-auth";
import type { Project, Provider } from "@/lib/types";
import { DEFAULT_MODPACK_FILTERS, ModpackBrowser, type ModpackFilters } from "./modpacks/ModpackBrowser";
import { providerLabel } from "./modpacks/modpackMeta";
import { type ReleaseData, type SortKey, useResolveRelease } from "./modpacks/modpackQueries";
import { ProjectDetail } from "./modpacks/ProjectDetail";
import { ReleasePicker } from "./modpacks/ReleasePicker";
import { ResolvePreview } from "./modpacks/ResolvePreview";

const PROVIDERS: Provider[] = ["modrinth", "curseforge", "ftb"];
const SORTS: SortKey[] = ["relevance", "downloads", "follows", "newest", "updated"];
const PAGE_SIZE = 24;

function parseFilters(sp: URLSearchParams): ModpackFilters {
  const provider = sp.get("provider") as Provider | null;
  const sort = sp.get("sort") as SortKey | null;
  const page = Number(sp.get("page") ?? "1");
  return {
    provider: provider && PROVIDERS.includes(provider) ? provider : DEFAULT_MODPACK_FILTERS.provider,
    q: sp.get("q") ?? "",
    loaders: (sp.get("loaders") ?? "").split(",").filter(Boolean),
    gameVersion: sp.get("mc") ?? "",
    sort: sort && SORTS.includes(sort) ? sort : DEFAULT_MODPACK_FILTERS.sort,
    page: Number.isFinite(page) && page > 1 ? Math.floor(page) - 1 : 0,
  };
}

function toParams(f: ModpackFilters, project: string | null): URLSearchParams {
  const sp = new URLSearchParams();
  if (f.provider !== DEFAULT_MODPACK_FILTERS.provider) sp.set("provider", f.provider);
  if (f.q) sp.set("q", f.q);
  if (f.loaders.length) sp.set("loaders", f.loaders.join(","));
  if (f.gameVersion) sp.set("mc", f.gameVersion);
  if (f.sort !== DEFAULT_MODPACK_FILTERS.sort) sp.set("sort", f.sort);
  if (f.page > 0) sp.set("page", String(f.page + 1));
  if (project) sp.set("project", project);
  return sp;
}

function DeployButton({ provider, projectId, release }: { provider: Provider; projectId: string; release: ReleaseData | null }) {
  const resolve = useResolveRelease(provider, projectId, release?.id);
  const href = release
    ? `/servers/new?${new URLSearchParams({ provider, project: projectId, version: release.id }).toString()}`
    : null;
  if (!href || !release) {
    return (
      <Button disabled className="w-full sm:w-auto">
        <Rocket /> Select a release to deploy
      </Button>
    );
  }
  const blocked = resolve.isError;
  return blocked ? (
    <Button disabled className="w-full sm:w-auto">
      <Rocket /> This release can't be deployed
    </Button>
  ) : (
    <Button asChild className="w-full sm:w-auto">
      <Link to={href}>
        <Rocket /> Create server from {release.version_number || "this release"}
      </Link>
    </Button>
  );
}

function ProjectSheet({
  provider,
  projectId,
  hit,
  onClose,
}: {
  provider: Provider;
  projectId: string | null;
  hit: Project | null;
  onClose: () => void;
}) {
  const { can } = useAuth();
  const canCreate = can("instance.create");
  const [release, setRelease] = useState<ReleaseData | null>(null);
  const [releaseFor, setReleaseFor] = useState<string | null>(projectId);
  if (releaseFor !== projectId) {
    setReleaseFor(projectId);
    setRelease(null);
  }
  const onSelect = useCallback((r: ReleaseData) => setRelease(r), []);
  const title = hit && hit.id === projectId ? hit.title : "Modpack";

  return (
    <Sheet open={!!projectId} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-2xl">
        <SheetHeader className="border-b pr-12">
          <SheetTitle className="truncate">{title}</SheetTitle>
          <SheetDescription>
            {providerLabel(provider)} modpack · choose a pinned release{canCreate ? " to deploy as a server" : ""}.
          </SheetDescription>
        </SheetHeader>
        {projectId && (
          <div className="grid min-h-0 flex-1 content-start gap-4 overflow-y-auto p-4">
            <ProjectDetail provider={provider} projectId={projectId} fallback={hit} />
            <Section title="Releases">
              <ReleasePicker provider={provider} projectId={projectId} selectedId={release?.id} onSelect={onSelect} autoSelectLatest />
            </Section>
            {canCreate && release && <ResolvePreview provider={provider} projectId={projectId} versionId={release.id} />}
          </div>
        )}
        {projectId && canCreate && (
          <SheetFooter className="border-t sm:flex-row sm:justify-end">
            <DeployButton provider={provider} projectId={projectId} release={release} />
          </SheetFooter>
        )}
      </SheetContent>
    </Sheet>
  );
}

export default function ModpacksPage() {
  const { can } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => parseFilters(searchParams), [searchParams]);
  const projectId = searchParams.get("project");
  const [hit, setHit] = useState<Project | null>(null);

  const onFiltersChange = useCallback(
    (f: ModpackFilters) => setSearchParams(toParams(f, projectId), { replace: true }),
    [setSearchParams, projectId],
  );
  const openProject = (p: Project) => {
    setHit(p);
    setSearchParams(toParams(filters, p.id));
  };
  const closeProject = () => setSearchParams(toParams(filters, null), { replace: true });

  return (
    <div className="grid gap-4">
      <PageHeader
        icon={
          <div className="grid size-9 place-items-center rounded-lg bg-primary/15 text-primary">
            <Package className="size-4.5" />
          </div>
        }
        title="Modpacks"
        description="Browse server-compatible modpacks, inspect their releases, and deploy a pinned release as a new server."
        actions={
          can("instance.create") && (
            <Button asChild variant="outline" size="sm">
              <Link to="/servers/new">
                <Plus /> Create server
              </Link>
            </Button>
          )
        }
        className="pb-0"
      />
      <Section>
        <ModpackBrowser
          filters={filters}
          onFiltersChange={onFiltersChange}
          selectedId={projectId}
          onSelect={openProject}
          layout="grid"
          pageSize={PAGE_SIZE}
        />
      </Section>
      <ProjectSheet provider={hit && hit.id === projectId ? hit.provider : filters.provider} projectId={projectId} hit={hit} onClose={closeProject} />
    </div>
  );
}
