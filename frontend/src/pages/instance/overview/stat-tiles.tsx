/** Real-time stat tiles for the instance Overview (status, players, CPU, memory, network, disk). */
import type { UseQueryResult } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, CheckCircle2, Cpu, HardDrive, Loader2, MemoryStick, Network, Power, TriangleAlert, Users } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { StatusBadge } from "@/components/StatusBadge";
import { errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, formatDuration, formatMemoryMb, relativeTime, toDate, uptimeSince } from "@/lib/format";
import type { Instance, Stats } from "@/lib/types";
import { cn } from "@/lib/utils";
import type { StatSample } from "./live-samples";

function Tile({
  icon,
  label,
  value,
  sub,
  meter,
  action,
  className,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  meter?: number | null;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5 rounded-xl border bg-card p-3 text-card-foreground sm:p-4", className)}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground [&_svg]:size-3.5">
          {icon}
          {label}
        </div>
        {action}
      </div>
      <div className="min-w-0 text-lg leading-tight font-semibold tabular-nums">{value}</div>
      {meter !== undefined && meter !== null && <Meter fraction={meter} />}
      {sub && <div className="min-w-0 text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}

function Meter({ fraction }: { fraction: number }) {
  const f = Math.max(0, Math.min(1, fraction));
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden>
      <div
        className={cn(
          "h-full rounded-full bg-primary transition-[width] duration-500",
          f >= 0.8 && "bg-amber-500",
          f >= 0.95 && "bg-red-500",
        )}
        style={{ width: `${f * 100}%` }}
      />
    </div>
  );
}

function Muted({ children }: { children: ReactNode }) {
  return <span className="text-muted-foreground">{children}</span>;
}

function StatusTile({ instance, now }: { instance: Instance; now: number }) {
  const s = instance.status;
  const up = ["running", "unhealthy", "starting"].includes(s) && instance.started_at;
  const started = toDate(instance.started_at);
  const ready = toDate(instance.ready_at);
  const readyThisRun = !!(ready && started && ready.getTime() >= started.getTime());
  let sub: ReactNode = null;
  if (s === "running" || s === "unhealthy" || s === "starting") {
    sub = readyThisRun ? (
      <span className="flex items-center gap-1" title={`Ready at ${formatDateTime(instance.ready_at)}`}>
        <CheckCircle2 className="size-3.5 text-emerald-500" aria-hidden />
        Ready · took ~{formatDuration((ready!.getTime() - started!.getTime()) / 1000)} to start
      </span>
    ) : (
      <span className="flex items-center gap-1">
        <Loader2 className="size-3.5 animate-spin" aria-hidden />
        Loading — not accepting players yet
      </span>
    );
    if (s === "unhealthy") {
      sub = (
        <span className="flex items-center gap-1 text-orange-700 dark:text-orange-300">
          <TriangleAlert className="size-3.5" aria-hidden />
          Not responding to health checks
        </span>
      );
    }
  } else if (s === "created") {
    sub = "Not installed yet — installs on first start";
  } else if (instance.oom_killed) {
    sub = (
      <span className="flex items-center gap-1 text-destructive">
        <TriangleAlert className="size-3.5" aria-hidden />
        Killed: out of memory
      </span>
    );
  } else if (["stopped", "failed"].includes(s)) {
    const code = instance.last_exit_code;
    sub = (
      <>
        {instance.started_at ? <>Last started {relativeTime(instance.started_at, now)}</> : "Never started"}
        {code !== null && code !== 0 && <span className="text-destructive"> · exit code {code}</span>}
      </>
    );
  }
  return (
    <Tile
      icon={<Power />}
      label="Status"
      value={
        <span className="flex flex-wrap items-center gap-2">
          <StatusBadge status={s} title={instance.status_detail} />
          {up && <span className="text-base">Up {uptimeSince(instance.started_at, now)}</span>}
        </span>
      }
      sub={sub}
    />
  );
}

function PlayersTile({ instance, stats }: { instance: Instance; stats?: Stats }) {
  const p = stats?.players;
  const manage = (
    <Link to={`/servers/${instance.id}/players`} className="text-xs text-primary hover:underline">
      Manage
    </Link>
  );
  if (!stats?.running) {
    return <Tile icon={<Users />} label="Players" value={<Muted>—</Muted>} sub="Server is not running" action={manage} />;
  }
  if (p === null || p === undefined) {
    return (
      <Tile
        icon={<Users />}
        label="Players"
        value={<Muted>Unavailable</Muted>}
        sub="Player list could not be read over RCON (the server may still be loading)."
        action={manage}
      />
    );
  }
  if (p.online === null) {
    return <Tile icon={<Users />} label="Players" value={<Muted>Unknown</Muted>} sub={p.raw || "Unrecognised response to /list"} action={manage} />;
  }
  const names = p.players ?? [];
  const shown = names.slice(0, 8);
  return (
    <Tile
      icon={<Users />}
      label="Players online"
      value={
        <>
          {p.online}
          <span className="text-sm font-normal text-muted-foreground"> / {p.max ?? "?"}</span>
        </>
      }
      meter={p.max ? p.online / p.max : null}
      action={manage}
      sub={
        names.length === 0 ? (
          "Nobody online"
        ) : (
          <ul className="flex flex-wrap gap-1" aria-label="Online players">
            {shown.map((n) => (
              <li key={n} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground">
                {n}
              </li>
            ))}
            {names.length > shown.length && <li className="px-1 py-0.5">+{names.length - shown.length} more</li>}
          </ul>
        )
      }
    />
  );
}

function CpuTile({ instance, stats }: { instance: Instance; stats?: Stats }) {
  if (!stats?.running || stats.cpu_percent === undefined) {
    return <Tile icon={<Cpu />} label="CPU" value={<Muted>—</Muted>} sub="Measured while the server is running" />;
  }
  const cores = instance.cpu_limit ?? stats.cpu_count ?? 1;
  return (
    <Tile
      icon={<Cpu />}
      label="CPU"
      value={`${stats.cpu_percent.toFixed(1)}%`}
      meter={stats.cpu_percent / (cores * 100)}
      sub={
        <>
          {instance.cpu_limit ? `Limited to ${instance.cpu_limit} cores` : `${stats.cpu_count ?? "?"} cores available`} · 100% = 1 core
          {typeof stats.pids === "number" && ` · ${stats.pids} threads`}
        </>
      }
    />
  );
}

function MemoryTile({ instance, stats }: { instance: Instance; stats?: Stats }) {
  const heap = `Java heap ${formatMemoryMb(instance.memory_mb)}`;
  if (!stats?.running || stats.memory_bytes === undefined) {
    return (
      <Tile
        icon={<MemoryStick />}
        label="Memory"
        value={<Muted>—</Muted>}
        sub={`${heap} · container limit ${formatMemoryMb(instance.container_memory_mb)}`}
      />
    );
  }
  const limit = stats.memory_limit_bytes || 0;
  return (
    <Tile
      icon={<MemoryStick />}
      label="Memory"
      value={
        <>
          {formatBytes(stats.memory_bytes)}
          {limit > 0 && <span className="text-sm font-normal text-muted-foreground"> / {formatBytes(limit)}</span>}
        </>
      }
      meter={limit ? stats.memory_bytes / limit : null}
      sub={`${heap} · JVM reserves its heap, so high usage is normal`}
    />
  );
}

function rate(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${formatBytes(v)}/s`;
}

function NetworkTile({ stats, latest }: { stats?: Stats; latest?: StatSample }) {
  if (!stats?.running || stats.network_rx_bytes === undefined) {
    return <Tile icon={<Network />} label="Network" value={<Muted>—</Muted>} sub="Measured while the server is running" />;
  }
  return (
    <Tile
      icon={<Network />}
      label="Network"
      value={
        <span className="flex flex-wrap items-center gap-x-3 text-base">
          <span className="flex items-center gap-0.5" aria-label={`Receiving ${rate(latest?.rxRate)}`}>
            <ArrowDown className="size-3.5 text-sky-500" aria-hidden />
            {rate(latest?.rxRate)}
          </span>
          <span className="flex items-center gap-0.5" aria-label={`Sending ${rate(latest?.txRate)}`}>
            <ArrowUp className="size-3.5 text-violet-500" aria-hidden />
            {rate(latest?.txRate)}
          </span>
        </span>
      }
      sub={`Since container start: ${formatBytes(stats.network_rx_bytes)} in · ${formatBytes(stats.network_tx_bytes)} out`}
    />
  );
}

function DiskTile({ disk, now }: { disk: UseQueryResult<Stats>; now: number }) {
  const d = disk.data?.disk;
  if (disk.isLoading) {
    return <Tile icon={<HardDrive />} label="Disk" value={<Muted>Measuring…</Muted>} sub="Calculating the server folder size" />;
  }
  if (disk.error || !d) {
    return (
      <Tile
        icon={<HardDrive />}
        label="Disk"
        value={<Muted>Unavailable</Muted>}
        sub={disk.error ? errorMessage(disk.error) : "The node agent did not report disk usage"}
      />
    );
  }
  return (
    <Tile
      icon={<HardDrive />}
      label="Disk"
      value={formatBytes(d.bytes)}
      sub={
        <>
          Server folder
          {d.backup_bytes !== undefined && <> · backups {formatBytes(d.backup_bytes)}</>} · measured{" "}
          <span title={formatDateTime(d.measured_at)}>{relativeTime(d.measured_at, now)}</span>
        </>
      }
    />
  );
}

export function StatTiles({
  instance,
  stats,
  latest,
  disk,
  now,
}: {
  instance: Instance;
  stats?: Stats;
  latest?: StatSample;
  disk: UseQueryResult<Stats>;
  now: number;
}) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:gap-4 lg:grid-cols-3">
      <StatusTile instance={instance} now={now} />
      <PlayersTile instance={instance} stats={stats} />
      <CpuTile instance={instance} stats={stats} />
      <MemoryTile instance={instance} stats={stats} />
      <NetworkTile stats={stats} latest={latest} />
      <DiskTile disk={disk} now={now} />
    </div>
  );
}
