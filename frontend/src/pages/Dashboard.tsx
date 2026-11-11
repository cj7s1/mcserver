import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CircleDot,
  Cpu,
  HardDrive,
  MemoryStick,
  Plus,
  Server,
  ServerCrash,
  ServerOff,
  Users,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { BackupWarningBadge } from "@/components/BackupNotice";
import { PageHeader, Section } from "@/components/PageHeader";
import { ServerIcon } from "@/components/ServerIcon";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { useAuth } from "@/hooks/use-auth";
import { formatBytes, formatMemoryMb, relativeTime, formatDateTime } from "@/lib/format";
import { useInstances, useJobs, useStats, useSystemInfo } from "@/lib/queries";
import type { Instance, SystemInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { JobStatusIcon } from "./activity/job-status";
import { jobStatusLabel } from "./activity/job-utils";
import { softwareLabel } from "./instance/InstanceLayout";
import { ServerQuickActions } from "./library/ServerActions";
import { memoryCommitment, statusBucket, statusRank } from "./library/server-utils";

const MAX_STATS = 6;
const MAX_ROWS = 8;

export default function Dashboard() {
  const { user, can } = useAuth();
  const instances = useInstances();
  const system = useSystemInfo();
  const canCreate = can("instance.create");

  const list = instances.data ?? [];
  const sorted = [...list].sort((a, b) => statusRank(a.status) - statusRank(b.status) || a.name.localeCompare(b.name));
  const statsIds = new Set(
    sorted
      .filter((i) => i.status === "running" || i.status === "unhealthy")
      .slice(0, MAX_STATS)
      .map((i) => i.id),
  );

  const greeting = user?.first_name || user?.username;
  return (
    <div className="grid grid-cols-1 gap-4">
      <PageHeader
        className="pb-1"
        title="Dashboard"
        description={
          <>
            {greeting ? `Welcome back, ${greeting}. ` : ""}
            {system.data ? `Node “${system.data.node.name}” · MCPanel ${system.data.version}` : "Your servers at a glance."}
          </>
        }
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <Link to="/servers">
                <Server /> All servers
              </Link>
            </Button>
            {canCreate && (
              <Button size="sm" asChild>
                <Link to="/servers/new">
                  <Plus /> Create server
                </Link>
              </Button>
            )}
          </>
        }
      />

      {instances.isLoading ? (
        <LoadingState label="Loading servers…" />
      ) : instances.error ? (
        <ErrorState error={instances.error} onRetry={() => instances.refetch()} title="Could not load servers" />
      ) : (
        <SummaryTiles instances={list} />
      )}

      {system.data && !system.data.agent.reachable && <AgentWarning agent={system.data.agent} />}

      <div className="grid items-start gap-4 lg:grid-cols-3">
        <div className="grid min-w-0 grid-cols-1 content-start gap-4 lg:col-span-2">
          <Section
            title="Servers"
            description={list.length ? `${list.length} server${list.length === 1 ? "" : "s"} · live status` : undefined}
            actions={
              list.length > 0 && (
                <Button variant="ghost" size="sm" asChild>
                  <Link to="/servers">
                    Library <ArrowRight />
                  </Link>
                </Button>
              )
            }
          >
            {instances.isLoading ? (
              <LoadingState className="py-6" />
            ) : instances.error ? (
              <p className="text-sm text-muted-foreground">Server list unavailable.</p>
            ) : list.length === 0 ? (
              <EmptyState
                icon={<Server className="size-5" />}
                title="No servers yet"
                description={
                  canCreate
                    ? "Create your first Minecraft server: vanilla, Paper, Fabric, Forge or a modpack."
                    : "You do not have access to any servers. Ask an administrator to grant you access."
                }
                action={
                  canCreate && (
                    <Button asChild size="sm">
                      <Link to="/servers/new">
                        <Plus /> Create server
                      </Link>
                    </Button>
                  )
                }
              />
            ) : (
              <div className="-mx-4 -my-4">
                <ul className="divide-y" aria-label="Servers">
                  {sorted.slice(0, MAX_ROWS).map((inst) => (
                    <DashboardServerRow key={inst.id} instance={inst} withStats={statsIds.has(inst.id)} />
                  ))}
                </ul>
                {sorted.length > MAX_ROWS && (
                  <div className="border-t px-4 py-2.5 text-center">
                    <Link to="/servers" className="text-sm text-primary hover:underline">
                      View all {sorted.length} servers
                    </Link>
                  </div>
                )}
              </div>
            )}
          </Section>
          <RecentActivity />
        </div>

        <div className="grid min-w-0 grid-cols-1 content-start gap-4">
          <HostCapacity
            system={system.data}
            isLoading={system.isLoading}
            error={system.error}
            onRetry={() => system.refetch()}
            instances={list}
          />
        </div>
      </div>
    </div>
  );
}

function SummaryTiles({ instances }: { instances: Instance[] }) {
  const counts = { running: 0, busy: 0, stopped: 0, failed: 0, unhealthy: 0, notInstalled: 0, unknown: 0 };
  for (const i of instances) {
    counts[statusBucket(i.status)]++;
    if (i.status === "unhealthy") counts.unhealthy++;
    if (i.status === "created") counts.notInstalled++;
    if (i.status === "unknown") counts.unknown++;
  }
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatTile
        to="/servers"
        icon={<Server />}
        label="Servers"
        value={instances.length}
        sub={counts.busy ? `${counts.busy} busy (starting, stopping, installing…)` : "Not archived"}
      />
      <StatTile
        to="/servers?status=running"
        icon={<CircleDot />}
        label="Running"
        value={counts.running}
        tone="text-emerald-600 dark:text-emerald-400"
        sub={counts.unhealthy ? `${counts.unhealthy} not responding` : counts.running ? "All responding" : "None online"}
      />
      <StatTile
        to="/servers?status=stopped"
        icon={<ServerOff />}
        label="Stopped"
        value={counts.stopped}
        sub={counts.notInstalled ? `${counts.notInstalled} not installed yet` : "Offline"}
      />
      <StatTile
        to="/servers?status=failed"
        icon={<ServerCrash />}
        label="Failed"
        value={counts.failed}
        tone={counts.failed ? "text-destructive" : undefined}
        sub={counts.failed ? (counts.unknown ? `${counts.unknown} with unknown state` : "Needs attention") : "No problems"}
      />
    </div>
  );
}

function StatTile({
  to,
  icon,
  label,
  value,
  sub,
  tone,
}: {
  to: string;
  icon: ReactNode;
  label: string;
  value: number;
  sub?: string;
  tone?: string;
}) {
  return (
    <Link
      to={to}
      className="group rounded-xl border bg-card p-4 outline-none transition-colors hover:border-primary/40 focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground [&_svg]:size-3.5">
        {icon}
        {label}
        <ArrowRight
          className="ml-auto opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
          aria-hidden
        />
      </div>
      <div className={cn("mt-1.5 text-2xl font-semibold tabular-nums tracking-tight", tone)}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-xs text-muted-foreground">{sub}</div>}
    </Link>
  );
}

function DashboardServerRow({ instance, withStats }: { instance: Instance; withStats: boolean }) {
  const { data: stats } = useStats(instance.id, withStats);
  const live = withStats && stats?.running;
  const job = instance.active_job;
  return (
    <li className="flex items-center gap-3 px-4 py-2.5">
      <ServerIcon url={instance.icon_url} name={instance.name} type={instance.server_type} game={instance.game} className="size-9" />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <Link
            to={`/servers/${instance.id}`}
            className="max-w-full text-sm font-medium hover:underline max-sm:line-clamp-2 max-sm:[overflow-wrap:anywhere] sm:truncate"
            title={instance.name}
          >
            {instance.name}
          </Link>
          <StatusBadge status={instance.status} title={instance.status_detail || undefined} className="shrink-0" />
          <BackupWarningBadge instance={instance} />
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
          <span className="truncate">{softwareLabel(instance)}</span>
          <span className="tabular-nums">:{instance.game_port}</span>
          <span>{formatMemoryMb(instance.memory_mb)} heap</span>
          {live && stats && (
            <>
              {stats.cpu_percent !== undefined && (
                <span className="inline-flex items-center gap-1 tabular-nums" title="CPU usage (100% = one core)">
                  <Cpu className="size-3" aria-hidden />
                  <span className="sr-only">CPU</span>
                  {stats.cpu_percent.toFixed(0)}%
                </span>
              )}
              {stats.memory_bytes !== undefined && (
                <span className="inline-flex items-center gap-1 tabular-nums" title="Container memory usage">
                  <MemoryStick className="size-3" aria-hidden />
                  <span className="sr-only">Memory</span>
                  {formatBytes(stats.memory_bytes)}
                  {stats.memory_limit_bytes ? ` / ${formatBytes(stats.memory_limit_bytes)}` : ""}
                </span>
              )}
              {stats.players && stats.players.online !== null && (
                <span
                  className="inline-flex items-center gap-1 tabular-nums"
                  title={stats.players.players.join(", ") || "Players online"}
                >
                  <Users className="size-3" aria-hidden />
                  <span className="sr-only">Players</span>
                  {stats.players.online}
                  {stats.players.max !== null ? `/${stats.players.max}` : ""}
                </span>
              )}
            </>
          )}
        </div>
        {job && (
          <div className="mt-1.5 flex items-center gap-2" aria-live="polite">
            <Progress value={(job.progress ?? 0) * 100} className="h-1 max-w-48" aria-label={`${job.title} progress`} />
            <span className="truncate text-xs text-muted-foreground">
              {job.title} · {Math.round((job.progress ?? 0) * 100)}%
            </span>
          </div>
        )}
      </div>
      <ServerQuickActions instance={instance} />
    </li>
  );
}

function AgentWarning({ agent }: { agent: SystemInfo["agent"] }) {
  return (
    <div
      role="alert"
      className="flex gap-3 rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-800 dark:text-amber-200"
    >
      <AlertTriangle className="mt-0.5 size-5 shrink-0" aria-hidden />
      <div className="min-w-0">
        <p className="font-medium">The node agent is unreachable</p>
        <p className="mt-1 text-amber-800/90 dark:text-amber-200/80">
          Servers cannot be started, stopped, monitored or backed up until the panel can reach the agent. Check that the agent
          container is running and that the panel's agent URL and token are correct. Status shown below may be stale.
        </p>
        {agent.error && (
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-background/60 px-2 py-1.5 font-mono text-xs text-foreground">
            {agent.error}
          </pre>
        )}
      </div>
    </div>
  );
}

function UsageBar({
  label,
  used,
  total,
  detail,
  icon,
}: {
  label: string;
  used: number;
  total: number;
  detail: string;
  icon: ReactNode;
}) {
  const pct = total > 0 ? Math.min(100, Math.max(0, (used / total) * 100)) : 0;
  const tone =
    pct >= 90
      ? "[&_[data-slot=progress-indicator]]:bg-destructive"
      : pct >= 75
        ? "[&_[data-slot=progress-indicator]]:bg-amber-500"
        : "";
  return (
    <div className="grid grid-cols-1 gap-1.5">
      <div className="flex min-w-0 items-center gap-2 text-sm">
        <span className="text-muted-foreground [&_svg]:size-4" aria-hidden>
          {icon}
        </span>
        <span className="truncate font-medium">{label}</span>
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">{pct.toFixed(0)}%</span>
      </div>
      <Progress value={pct} className={cn("h-1.5", tone)} aria-label={`${label} ${pct.toFixed(0)}% used`} />
      <p className="text-xs text-muted-foreground">{detail}</p>
    </div>
  );
}

function HostCapacity({
  system,
  isLoading,
  error,
  onRetry,
  instances,
}: {
  system: SystemInfo | undefined;
  isLoading: boolean;
  error: unknown;
  onRetry: () => void;
  instances: Instance[];
}) {
  const agent = system?.agent;
  const docker = agent?.docker ?? {};
  const cpus = typeof docker.cpus === "number" ? docker.cpus : null;
  const memBytes = typeof docker.memory_bytes === "number" ? docker.memory_bytes : null;
  const { runningMb: reservedMb, allMb: configuredMb } = memoryCommitment(instances, { nodeId: system?.node.id });
  const storage = mergeVolumes(agent?.storage);

  return (
    <Section title="Host capacity" description={system ? `Node “${system.node.name}”` : undefined}>
      {isLoading ? (
        <LoadingState className="py-6" />
      ) : error || !system ? (
        <ErrorState
          error={error ?? new Error("System information unavailable")}
          onRetry={onRetry}
          className="py-6"
          title="Could not load host info"
        />
      ) : !agent?.reachable ? (
        <p className="flex items-start gap-2 text-sm text-amber-700 dark:text-amber-300">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          Host capacity is unknown while the agent is unreachable.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4">
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div className="rounded-lg bg-muted/50 p-2.5">
              <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Cpu className="size-3.5" aria-hidden /> CPU
              </dt>
              <dd className="mt-0.5 font-semibold tabular-nums">
                {cpus !== null ? `${cpus} core${cpus === 1 ? "" : "s"}` : "—"}
              </dd>
            </div>
            <div className="rounded-lg bg-muted/50 p-2.5">
              <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <MemoryStick className="size-3.5" aria-hidden /> Memory
              </dt>
              <dd className="mt-0.5 font-semibold tabular-nums">{memBytes !== null ? formatBytes(memBytes) : "—"}</dd>
            </div>
          </dl>

          {memBytes !== null && (
            <UsageBar
              icon={<MemoryStick />}
              label="Memory reserved"
              used={reservedMb * 1024 * 1024}
              total={memBytes}
              detail={`${formatMemoryMb(reservedMb)} of ${formatBytes(memBytes)} by running servers · ${formatMemoryMb(configuredMb)} if all ran`}
            />
          )}

          {storage.map(({ key, label, vol }) => (
            <UsageBar
              key={key}
              icon={<HardDrive />}
              label={label}
              used={vol.used_bytes}
              total={vol.used_bytes + vol.free_bytes}
              detail={`${formatBytes(vol.free_bytes)} free · ${formatBytes(vol.used_bytes)} used · ${vol.fstype}${
                vol.snapshot?.available ? ` · ${vol.snapshot.provider ?? "native"} snapshots` : ""
              }`}
            />
          ))}

          {(!!docker.server_version || !!agent.version || agent.restic !== undefined) && (
            <dl className="grid grid-cols-1 gap-1 border-t pt-3 text-xs">
              {!!docker.server_version && (
                <div className="flex justify-between gap-2">
                  <dt className="shrink-0 text-muted-foreground">Docker</dt>
                  <dd className="min-w-0 truncate">
                    {String(docker.server_version)}
                    {typeof docker.containers_running === "number"
                      ? ` · ${docker.containers_running} container${docker.containers_running === 1 ? "" : "s"} running`
                      : ""}
                  </dd>
                </div>
              )}
              {agent.version && (
                <div className="flex justify-between gap-2">
                  <dt className="text-muted-foreground">Agent</dt>
                  <dd>v{agent.version}</dd>
                </div>
              )}
              {agent.restic !== undefined && (
                <div className="flex justify-between gap-2">
                  <dt className="shrink-0 text-muted-foreground">Backups</dt>
                  <dd className={cn("min-w-0 truncate", !agent.restic && "text-amber-700 dark:text-amber-300")}>
                    {agent.restic ? agent.restic.split(" compiled")[0] : "restic not installed"}
                  </dd>
                </div>
              )}
            </dl>
          )}
        </div>
      )}
    </Section>
  );
}

type StorageVolume = NonNullable<SystemInfo["agent"]["storage"]>[string];

const VOLUME_LABELS: Record<string, string> = { instances: "Server storage", backups: "Backup storage" };

/** One usage bar per filesystem: volumes that share a mount (same totals) are shown together. */
function mergeVolumes(storage: SystemInfo["agent"]["storage"]): { key: string; label: string; vol: StorageVolume }[] {
  const out: { key: string; label: string; vol: StorageVolume }[] = [];
  for (const [name, vol] of Object.entries(storage ?? {})) {
    const key = `${vol.mount_point ?? vol.path}|${vol.total_bytes}`;
    const label = VOLUME_LABELS[name] ?? name;
    const existing = out.find((o) => o.key === key);
    if (existing) existing.label = `${existing.label.replace(/ storage$/, "")} & ${label.toLowerCase()}`;
    else out.push({ key, label, vol });
  }
  return out;
}

function RecentActivity() {
  const jobs = useJobs({ limit: 8 });
  return (
    <Section
      title="Recent activity"
      actions={
        <Button variant="ghost" size="sm" asChild>
          <Link to="/activity">
            All activity <ArrowRight />
          </Link>
        </Button>
      }
    >
      {jobs.isLoading ? (
        <LoadingState className="py-6" />
      ) : jobs.error ? (
        <ErrorState error={jobs.error} onRetry={() => jobs.refetch()} className="py-6" title="Could not load activity" />
      ) : !jobs.data?.length ? (
        <EmptyState
          icon={<Activity className="size-5" />}
          title="No activity yet"
          description="Installs, power actions, backups and updates appear here."
          className="py-8"
        />
      ) : (
        <ol className="-my-1 grid grid-cols-1" aria-label="Recent jobs">
          {jobs.data.map((job) => (
            <li key={job.id} className="flex items-start gap-2.5 py-1.5">
              <span className="mt-0.5" title={jobStatusLabel(job.status)}>
                <JobStatusIcon status={job.status} />
                <span className="sr-only">{jobStatusLabel(job.status)}</span>
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm">{job.title}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {job.instance && job.instance_name ? (
                    <Link to={`/servers/${job.instance}`} className="hover:text-foreground hover:underline">
                      {job.instance_name}
                    </Link>
                  ) : (
                    "Panel"
                  )}
                  {job.user_name ? ` · ${job.user_name}` : ""}
                  {job.status === "running" ? ` · ${Math.round(job.progress * 100)}%` : ""}
                  {job.status === "failed" && job.error ? ` · ${job.error}` : ""}
                </p>
              </div>
              <time
                dateTime={job.created_at}
                title={formatDateTime(job.created_at)}
                className="shrink-0 text-xs text-muted-foreground"
              >
                {relativeTime(job.created_at)}
              </time>
            </li>
          ))}
        </ol>
      )}
    </Section>
  );
}
