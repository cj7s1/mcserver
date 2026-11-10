/** Modpack servers: pinned release, release list with explicit update, update history and rollback points. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUpCircle, ExternalLink, History, Info, KeyRound, Loader2, Package, Pin, RefreshCw, Settings2, Undo2, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { JobProgress } from "@/components/JobProgress";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/hooks/use-auth";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { useInvalidateInstance } from "@/lib/queries";
import type { Instance, ModpackUpdateInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { loaderLabel, providerLabel } from "@/pages/modpacks/modpackMeta";
import { useInstanceContext } from "./InstanceLayout";
import { RollbackPoints, UpdateHistory } from "./modpack/HistoryPanels";
import {
  lockReason,
  modpackPrefix,
  projectUrl,
  REVIEW_PARAM,
  updateCheckSummary,
  useInstanceJobList,
  useInstanceModpack,
  useModpackJob,
  type InstanceModpack,
  type ModpackRelease,
  type Relation,
  type UpdatePlan,
} from "./modpack/modpack-api";
import { ReleaseList } from "./modpack/ReleaseList";
import { RemovedPackFiles } from "./modpack/RemovedPackFiles";
import { UpdateDialog } from "./modpack/UpdateDialog";
import { Notice } from "./settings/settings-ui";

type DialogState = { release: ModpackRelease; relation: Relation | null; plan: UpdatePlan; key: number } | null;

function UpdatesBadge({ count }: { count: number }) {
  return count > 0 ? (
    <span className="inline-flex h-5 items-center rounded-md bg-emerald-500/10 px-1.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
      {count} newer release{count === 1 ? "" : "s"}
    </span>
  ) : (
    <span className="inline-flex h-5 items-center rounded-md bg-muted px-1.5 text-[11px] font-medium text-muted-foreground">Up to date</span>
  );
}

/** Result of the periodic "update available" check, with "Review update" and "Check now". */
function UpdateCheckLine({
  instance,
  info,
  canUpdate,
  disabled,
  onReview,
}: {
  instance: Instance;
  info: ModpackUpdateInfo | null | undefined;
  canUpdate: boolean;
  disabled: boolean;
  onReview: (latest: NonNullable<ModpackUpdateInfo["latest"]>) => void;
}) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const check = useMutation({
    mutationFn: () => api.post<ModpackUpdateInfo>(`/instances/${instance.id}/modpack/check`),
    onSuccess: (r) => {
      invalidate(instance.id);
      qc.invalidateQueries({ queryKey: modpackPrefix(instance.id) });
      if (r.throttled) toast.message("Checked moments ago; showing that result");
      else if (r.available && r.latest) toast.success(`Update available: ${r.latest.name}`);
      else if (r.status === "ok") toast.success("No newer compatible release");
      else toast.message(r.detail || "The update check could not run");
    },
    onError: (err) => toast.error(`Could not check for updates: ${errorMessage(err)}`),
  });
  const summary = updateCheckSummary(info);
  const latest = info?.available ? info.latest : null;
  const tone = {
    ok: "text-muted-foreground",
    update: "font-medium text-emerald-700 dark:text-emerald-300",
    muted: "text-muted-foreground",
    error: "text-destructive",
  }[summary.tone];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border bg-muted/30 px-3 py-2 text-xs" aria-live="polite">
      <p className={cn("flex min-w-0 items-center gap-1.5 [overflow-wrap:anywhere]", tone)}>
        {latest && <ArrowUpCircle className="size-3.5 shrink-0" aria-hidden />}
        <span>
          {summary.text}
          {latest?.minecraft_version && <span className="font-normal text-muted-foreground"> · Minecraft {latest.minecraft_version}</span>}
        </span>
      </p>
      {info?.checked_at && (
        <span className="text-muted-foreground" title={formatDateTime(info.checked_at)}>
          Checked {relativeTime(info.checked_at)}
        </span>
      )}
      <div className="ml-auto flex items-center gap-2">
        {latest && canUpdate && (
          <Button size="xs" onClick={() => onReview(latest)} disabled={disabled}>
            Review update
          </Button>
        )}
        <Button size="xs" variant="outline" onClick={() => check.mutate()} disabled={check.isPending}>
          {check.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />} Check now
        </Button>
      </div>
    </div>
  );
}

function PinnedRelease({
  instance,
  data,
  loading,
  children,
}: {
  instance: Instance;
  data: InstanceModpack | undefined;
  loading: boolean;
  children?: ReactNode;
}) {
  const url = projectUrl(instance);
  const provider = instance.modpack_provider || data?.provider || "";
  const current = data?.current;
  const loader = instance.modpack_loader || instance.loader;
  const rows: [string, string][] = [
    ["Release", instance.modpack_version_name || current?.version_number || instance.modpack_version_id || "—"],
    ["Minecraft", instance.minecraft_version || "—"],
    ["Loader", [loader ? loaderLabel(loader) : "", instance.loader_version].filter(Boolean).join(" ") || "—"],
    ["Java", instance.java_version ? `Java ${instance.java_version}` : "—"],
    ["Provider", provider ? providerLabel(provider) : "—"],
  ];
  const published = current?.published;
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <Package className="size-4 text-muted-foreground" aria-hidden /> Pinned release
        </span>
      }
      description="The server installs exactly this release and never upgrades itself on restart."
      actions={
        url ? (
          <Button asChild size="sm" variant="outline">
            <a href={url} target="_blank" rel="noopener noreferrer">
              <ExternalLink aria-hidden /> <span className="hidden sm:inline">View on</span> {providerLabel(provider)}
            </a>
          </Button>
        ) : undefined
      }
    >
      <div className="grid gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <p className="min-w-0 text-base font-semibold break-words">{instance.modpack_name || data?.name || instance.modpack_slug || "Modpack"}</p>
          {data ? <UpdatesBadge count={data.updates_available} /> : loading ? <Skeleton className="h-5 w-24" /> : null}
        </div>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3 lg:grid-cols-6">
          {rows.map(([k, v]) => (
            <div key={k} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{k}</dt>
              <dd className="truncate font-medium" title={v}>
                {v}
              </dd>
            </div>
          ))}
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">Published</dt>
            <dd className="truncate font-medium" title={published != null ? formatDateTime(published) : undefined}>
              {published != null && published !== "" ? relativeTime(published) : "—"}
            </dd>
          </div>
        </dl>
        {instance.modpack_version_id && (
          <p className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
            <Pin className="size-3 shrink-0" aria-hidden />
            <span className="truncate">
              Release id <code className="font-mono">{instance.modpack_version_id}</code>
              {instance.modpack_project_id && (
                <>
                  {" "}· project <code className="font-mono">{instance.modpack_project_id}</code>
                </>
              )}
            </span>
          </p>
        )}
        {children}
      </div>
    </Section>
  );
}

function ReleasesError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const auth = useAuth();
  const code = error instanceof ApiError ? error.code : "";
  if (code === "curseforge_key_missing") {
    return (
      <div className="grid gap-3">
        {/* A missing key is configuration, not a failure: no red error box. */}
        <EmptyState
          icon={<KeyRound className="size-5" />}
          title="CurseForge needs an API key to list releases"
          description={errorMessage(error)}
          className="py-8"
        />
        <p className="text-xs text-muted-foreground">
          The server keeps running its pinned release. Release changes, rollback points and the update history appear here once the
          panel can reach CurseForge. “Before update” backups stay restorable from the Backups tab.
        </p>
        {auth.can("system.settings") && (
          <Button asChild size="sm" variant="outline" className="justify-self-start">
            <Link to="/admin/system">
              <KeyRound aria-hidden /> Add a CurseForge API key
            </Link>
          </Button>
        )}
      </div>
    );
  }
  if (code === "not_modpack") return <ErrorState error={error} title="Not a modpack server" />;
  if (code === "project_not_recorded") {
    // A CurseForge pack deployed by URL without an API key: the panel learns which project it is
    // from the server's install manifest, i.e. only after the first start installed the pack.
    return (
      <div className="grid gap-3">
        <EmptyState
          icon={<Package className="size-5" />}
          title="The modpack is not known to the panel yet"
          description="Start the server once and let it install the pack. The panel then reads which CurseForge project and release it runs from the server's install manifest, and the releases, updates and rollback points appear here."
          className="py-8"
        />
        <Button size="sm" variant="outline" className="justify-self-start" onClick={onRetry}>
          <RefreshCw aria-hidden /> Check again
        </Button>
      </div>
    );
  }
  return <ErrorState error={error} title="Could not load the modpack releases" onRetry={onRetry} />;
}

export default function Modpack() {
  const { instance, can } = useInstanceContext();
  const [prerelease, setPrerelease] = useState(false);
  const [dialog, setDialog] = useState<DialogState>(null);
  const hasProject = !!instance.modpack_provider && !!instance.modpack_project_id;
  const query = useInstanceModpack(instance.id, prerelease, instance.is_modpack && hasProject);
  const data = query.data;
  const jobs = useInstanceJobList(instance.id);
  const tracked = useModpackJob(instance);
  const canUpdate = can("instance.content");
  const canRollback = canUpdate && can("instance.restore");
  const lock = lockReason(instance);
  const locked = !!lock || tracked.running;

  const plan = useMutation({
    mutationFn: (v: { release: ModpackRelease; relation: Relation | null }) =>
      api.get<UpdatePlan>(`/instances/${instance.id}/modpack/plan/${encodeURIComponent(v.release.id)}`),
    onSuccess: (p, v) => {
      if (p.same_release) {
        toast.message(`${v.release.version_number || v.release.name} is already installed`);
        return;
      }
      setDialog({ release: v.release, relation: v.relation, plan: p, key: Date.now() });
    },
    onError: (err) => toast.error(`Could not prepare the update: ${errorMessage(err)}`),
  });
  const planningId = plan.isPending ? (plan.variables?.release.id ?? null) : null;

  /** Open the update plan for the release found by the update check (or a ?review= link). */
  const review = (versionId: string, name?: string) => {
    const listed = data?.releases.find((r) => r.id === versionId);
    const info = instance.modpack_update?.latest;
    const release: ModpackRelease = listed ?? {
      provider: (instance.modpack_provider || "modrinth") as ModpackRelease["provider"],
      id: versionId,
      project_id: instance.modpack_project_id,
      name: name ?? (info?.id === versionId ? info.name : versionId),
      version_number: name ?? (info?.id === versionId ? info.name : versionId),
      version_type: "release",
      game_versions: info?.id === versionId && info.minecraft_version ? [info.minecraft_version] : [],
      loaders: [],
    };
    plan.mutate({ release, relation: "newer" });
  };

  // "Update available" badge in the server header links here with ?review=<version id>
  const [params, setParams] = useSearchParams();
  const reviewId = params.get(REVIEW_PARAM);
  const releasesSettled = !hasProject || !query.isLoading;
  useEffect(() => {
    if (!reviewId || !releasesSettled) return;
    const next = new URLSearchParams(params);
    next.delete(REVIEW_PARAM);
    setParams(next, { replace: true });
    if (!canUpdate) {
      toast.message("You need the “Manage mods/plugins” permission on this server to update its modpack.");
      return;
    }
    if (locked) {
      toast.message(lock ?? "Another operation is running; review the update when it finishes.");
      return;
    }
    review(reviewId);
    // run once per ?review= value, after the release list settled
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewId, releasesSettled]);

  if (!instance.is_modpack) {
    return (
      <EmptyState
        icon={<Package className="size-5" />}
        title="Not a modpack server"
        description="This server runs standard server software. Change its Minecraft or loader version in Settings → Software."
        action={
          <Button asChild size="sm" variant="outline">
            <Link to={`/servers/${instance.id}/settings?tab=software`}>
              <Settings2 aria-hidden /> Open software settings
            </Link>
          </Button>
        }
      />
    );
  }

  const pinnedId = instance.modpack_version_id || data?.current.id || "";
  const currentListed = !!data && data.releases.some((r) => r.id === pinnedId);
  const rollbackIds = new Set((data?.rollback_points ?? []).map((p) => p.backup_id));

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      {tracked.jobId && (
        <div className="flex items-start gap-2">
          <JobProgress jobId={tracked.jobId} showLog={tracked.finished} className="min-w-0 flex-1" />
          {tracked.finished && (
            <Button variant="ghost" size="icon-sm" onClick={tracked.dismiss} aria-label="Dismiss job result">
              <X />
            </Button>
          )}
        </div>
      )}
      {lock && !tracked.running && canUpdate && <Notice tone="info">{lock}</Notice>}

      <PinnedRelease instance={instance} data={data} loading={query.isLoading}>
        <UpdateCheckLine
          instance={instance}
          info={instance.modpack_update ?? data?.update_check}
          canUpdate={canUpdate}
          disabled={locked || plan.isPending}
          onReview={(latest) => review(latest.id, latest.name)}
        />
      </PinnedRelease>

      <Section
        title="Releases"
        description={
          canUpdate
            ? "Pick a release to see exactly what changes before anything happens."
            : "You need the “Manage mods/plugins” permission on this server to change the release."
        }
        actions={
          hasProject ? (
            <div className="flex items-center gap-2">
              {query.isFetching && !query.isLoading && (
                <Loader2 className="size-3.5 animate-spin text-muted-foreground" aria-label="Refreshing releases" />
              )}
              <Switch id="mp-prerelease" size="sm" checked={prerelease} onCheckedChange={setPrerelease} />
              <Label htmlFor="mp-prerelease" className="text-xs font-normal">
                Include beta &amp; alpha
              </Label>
            </div>
          ) : undefined
        }
        className="min-w-0"
      >
        {!hasProject ? (
          <EmptyState
            icon={<Info className="size-5" />}
            title="The modpack project is not recorded yet"
            description="This server was deployed without provider details (for example a CurseForge pack URL without an API key). The project and release are recorded after the first successful install. Start the server once, then come back here."
          />
        ) : query.isLoading ? (
          <div className="grid gap-1.5" aria-busy="true" aria-label="Loading releases">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-16 w-full rounded-lg" />
            ))}
          </div>
        ) : query.error || !data ? (
          <ReleasesError error={query.error ?? new Error("No data")} onRetry={() => query.refetch()} />
        ) : data.releases.length === 0 ? (
          <EmptyState
            title="No releases found"
            description={prerelease ? "The provider lists no downloadable releases for this modpack." : "No stable releases found. Try including beta and alpha releases."}
          />
        ) : (
          <div className="grid gap-3">
            {!currentListed && (
              <Notice tone="info">
                The installed release ({instance.modpack_version_name || pinnedId || "unknown"}) is not in this list
                {prerelease ? "; the provider may have removed it." : ". It may be a beta or alpha release: include beta & alpha to see it."}
              </Notice>
            )}
            <ReleaseList
              instance={instance}
              data={data}
              canUpdate={canUpdate}
              locked={locked}
              planningId={planningId}
              onUpdate={(release, relation) => plan.mutate({ release, relation })}
            />
          </div>
        )}
      </Section>

      {data && (
        <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
          <Section
            title={
              <span className="inline-flex items-center gap-2">
                <Undo2 className="size-4 text-muted-foreground" aria-hidden /> Rollback points
              </span>
            }
            description="Return to the release and files from before an update."
            className="min-w-0"
          >
            <RollbackPoints
              instance={instance}
              points={data.rollback_points}
              canRollback={canRollback}
              canViewBackups={can("instance.backup")}
              locked={locked}
              onStarted={tracked.track}
            />
          </Section>
          <Section
            title={
              <span className="inline-flex items-center gap-2">
                <History className="size-4 text-muted-foreground" aria-hidden /> Update history
              </span>
            }
            description="Release changes made through the panel, newest first."
            className="min-w-0"
          >
            <UpdateHistory history={data.history} jobs={jobs.data} rollbackIds={rollbackIds} />
          </Section>
        </div>
      )}

      {/* independent of the provider: the record can always be undone */}
      <RemovedPackFiles instance={instance} canManage={canUpdate} locked={!!lock} />

      <section className="grid gap-1.5 rounded-xl border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="flex items-center gap-1.5 font-medium text-foreground">
          <Info className="size-3.5" aria-hidden /> How modpack updates work
        </p>
        <p>
          Updating stops the server, takes a “Before update” backup when “Create a backup first” is ticked in the update dialog, pins the new release and recreates the
          container. On the next start the installer downloads the new release and removes the files that belonged to the old one;
          worlds are kept. If the new release uses another Minecraft version, the world is upgraded on first start and cannot be opened
          by the old version afterwards.
        </p>
        <p>
          A rollback restores the “Before update” backup and the previous release together, after taking a safety backup of the
          current state.
        </p>
      </section>

      {dialog && (
        <UpdateDialog
          key={dialog.key}
          instance={instance}
          release={dialog.release}
          relation={dialog.relation}
          plan={dialog.plan}
          onOpenChange={(o) => !o && setDialog(null)}
          onStarted={tracked.track}
        />
      )}
    </div>
  );
}
