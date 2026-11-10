import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, PackageOpen, RefreshCw, TriangleAlert } from "lucide-react";
import { Link } from "react-router";
import { BackupNotice } from "@/components/BackupNotice";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import { qk, useStats } from "@/lib/queries";
import { overviewShortcuts, sectionPath } from "@/lib/sections";
import type { Instance, Stats } from "@/lib/types";
import { useInstanceContext } from "./InstanceLayout";
import { DiagnosticsCard } from "./overview/diagnostics";
import { JobItem, useInstanceJobs } from "./overview/instance-jobs";
import { ConnectionCard, SoftwareCard } from "./overview/info-cards";
import { useNow } from "./overview/overview-utils";
import { useStatSamples } from "./overview/live-samples";
import { ResourceCharts } from "./overview/resource-charts";
import { StatTiles } from "./overview/stat-tiles";

/** Disk usage is expensive to measure (directory walk on the node), so it is polled separately and rarely. */
function useDiskUsage(id: string, enabled: boolean) {
  const qc = useQueryClient();
  const key = [...qk.stats(id), "disk"] as const;
  return useQuery({
    queryKey: key,
    queryFn: async () => {
      const s = await api.get<Stats>(`/instances/${id}/stats`, { disk: "true", players: "false" });
      // The agent omits backup_bytes when it answers from its 60 s cache; keep the last known value.
      const prev = qc.getQueryData<Stats>(key);
      if (s.disk && s.disk.backup_bytes === undefined && prev?.disk?.backup_bytes !== undefined) {
        s.disk = { ...s.disk, backup_bytes: prev.disk.backup_bytes };
      }
      return s;
    },
    enabled,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
    staleTime: 30_000,
    retry: false,
  });
}

function InstallBanner({ instance, canControl }: { instance: Instance; canControl: boolean }) {
  return (
    <div className="flex gap-3 rounded-xl border border-sky-500/30 bg-sky-500/10 p-4 text-sm">
      <PackageOpen className="mt-0.5 size-5 shrink-0 text-sky-600 dark:text-sky-400" aria-hidden />
      <div>
        <p className="font-medium">This server has not been installed yet</p>
        <p className="mt-1 text-muted-foreground">
          The server software{instance.is_modpack ? " and modpack files" : ""} are downloaded and installed automatically the
          first time it starts. The first start can take several minutes
          {instance.is_modpack ? " for large modpacks" : ""}; progress shows above and in the Console.
          {canControl ? " Use Start above when you are ready." : " Ask someone with control permission to start it."}
        </p>
      </div>
    </div>
  );
}

function RecentJobs({ instance, now }: { instance: Instance; now: number }) {
  const jobs = useInstanceJobs(instance.id, instance.active_job);
  return (
    <Section
      title="Recent jobs"
      description="Operations run on this server"
      className="lg:col-span-2"
      actions={
        <Link to={`/servers/${instance.id}/activity`} className="text-xs font-medium text-primary hover:underline">
          View all activity
        </Link>
      }
    >
      {jobs.isLoading ? (
        <LoadingState label="Loading jobs…" className="py-6" />
      ) : jobs.error ? (
        <ErrorState error={jobs.error} onRetry={() => jobs.refetch()} title="Could not load jobs" />
      ) : !jobs.data?.length ? (
        <EmptyState title="No jobs yet" description="Starting, stopping, backups and updates will be listed here." className="py-8" />
      ) : (
        <ul className="grid gap-2">
          {jobs.data.slice(0, 6).map((j) => (
            <JobItem key={j.id} job={j} now={now} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function Shortcuts({ instance }: { instance: Instance }) {
  const items = overviewShortcuts(instance);
  return (
    <Section title="Quick links">
      <nav aria-label="Server shortcuts">
        <ul className="grid gap-2">
          {items.map(({ section, description, icon: Icon }) => (
            <li key={section.key}>
              <Link
                to={sectionPath(instance, section)}
                className="group flex items-center gap-3 rounded-lg border bg-background/40 px-3 py-2.5 outline-none transition-colors hover:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <span className="grid size-8 shrink-0 place-items-center rounded-md bg-primary/10 text-primary [&_svg]:size-4">
                  <Icon />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium">{section.label(instance)}</span>
                  <span className="block truncate text-xs text-muted-foreground">{description}</span>
                </span>
                <ChevronRight className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </Section>
  );
}

function OverviewContent() {
  const { instance, can } = useInstanceContext();
  const now = useNow(1000);
  const statsEnabled = !instance.archived_at && instance.status !== "created";
  const stats = useStats(instance.id, statsEnabled);
  const disk = useDiskUsage(instance.id, !instance.archived_at);
  const samples = useStatSamples(stats.data, stats.dataUpdatedAt);
  const latest = samples[samples.length - 1];
  const running = !!stats.data?.running;

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      {instance.status === "created" && !instance.active_job && <InstallBanner instance={instance} canControl={can("instance.control")} />}
      {instance.archived_at && (
        <div className="flex items-center gap-2 rounded-xl border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
          <TriangleAlert className="size-4 shrink-0" aria-hidden />
          This server is archived. Live stats are not collected while it is archived.
        </div>
      )}
      {stats.error && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-800 dark:text-amber-200"
        >
          <TriangleAlert className="size-4 shrink-0" aria-hidden />
          <span className="min-w-0 flex-1">Live stats unavailable: {errorMessage(stats.error)}</span>
          <Button size="xs" variant="outline" onClick={() => stats.refetch()}>
            <RefreshCw /> Retry
          </Button>
        </div>
      )}

      <BackupNotice instance={instance} />

      <DiagnosticsCard instance={instance} can={can} now={now} />

      <StatTiles instance={instance} stats={stats.data} latest={latest} disk={disk} now={now} />

      {!instance.archived_at && <ResourceCharts instanceId={instance.id} samples={samples} running={running} />}

      <div className="grid gap-4 lg:grid-cols-2">
        <ConnectionCard instance={instance} />
        <SoftwareCard instance={instance} canConfigure={can("instance.configure")} />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <RecentJobs instance={instance} now={now} />
        <Shortcuts instance={instance} />
      </div>
    </div>
  );
}

export default function Overview() {
  const { instance } = useInstanceContext();
  // Keyed by instance so the client-side sample buffer (and chosen history range) never mixes servers.
  return <OverviewContent key={instance.id} />;
}
