/**
 * Live CPU / memory charts built from a client-side rolling buffer of /stats
 * samples. There is no server-side history: the buffer starts empty when the
 * page opens and only ever contains samples this browser received.
 */
import { Activity } from "lucide-react";
import { memo, useId, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Section } from "@/components/PageHeader";
import { formatBytes } from "@/lib/format";
import type { Stats } from "@/lib/types";

export interface StatSample {
  /** client receive time (ms) */
  t: number;
  /** agent timestamp of the sample, used to drop duplicates */
  ts?: string;
  cpu: number;
  mem: number;
  memLimit: number;
  rx: number;
  tx: number;
  /** bytes/s since the previous sample, null for the first sample or after a counter reset */
  rxRate: number | null;
  txRate: number | null;
}

export const SAMPLE_WINDOW_MS = 5 * 60_000;

function nextSamples(prev: StatSample[], stats: Stats, at: number): StatSample[] {
  const last = prev[prev.length - 1];
  // the same agent sample delivered twice (e.g. a refetch that returned identical data)
  if (last && stats.timestamp && last.ts === stats.timestamp) return prev;
  const rx = stats.network_rx_bytes ?? 0;
  const tx = stats.network_tx_bytes ?? 0;
  let rxRate: number | null = null;
  let txRate: number | null = null;
  if (last && at > last.t) {
    const dt = (at - last.t) / 1000;
    // counters restart with the container; a drop means "unknown", not a negative rate
    if (rx >= last.rx) rxRate = (rx - last.rx) / dt;
    if (tx >= last.tx) txRate = (tx - last.tx) / dt;
  }
  const next: StatSample = {
    t: at,
    ts: stats.timestamp,
    cpu: stats.cpu_percent ?? 0,
    mem: stats.memory_bytes ?? 0,
    memLimit: stats.memory_limit_bytes ?? 0,
    rx,
    tx,
    rxRate,
    txRate,
  };
  const cutoff = at - SAMPLE_WINDOW_MS;
  return [...prev.filter((s) => s.t >= cutoff), next];
}

/**
 * Append each new running /stats response (keyed by the query's dataUpdatedAt) to a
 * buffer covering the last ~5 minutes. State is adjusted during render rather than
 * in an effect, so each sample costs a single render.
 */
export function useStatSamples(stats: Stats | undefined, updatedAt: number): StatSample[] {
  const [buf, setBuf] = useState<{ at: number; samples: StatSample[] }>({ at: 0, samples: [] });
  if (stats?.running && stats.cpu_percent !== undefined && updatedAt && updatedAt !== buf.at) {
    setBuf({ at: updatedAt, samples: nextSamples(buf.samples, stats, updatedAt) });
  }
  return buf.samples;
}

const GIB = 1024 ** 3;

function axisBytes(v: number): string {
  if (v === 0) return "0";
  return v >= GIB ? `${(v / GIB).toFixed(1)} GB` : `${Math.round(v / 1024 ** 2)} MB`;
}

function spanLabel(ms: number): string {
  return ms < 60_000 ? `${Math.max(1, Math.round(ms / 1000))} s` : `${Math.round(ms / 60_000)} min`;
}

function clockLabel(t: number): string {
  return new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

interface TooltipRow {
  value?: unknown;
  payload?: StatSample;
}

function ChartTooltip({
  active,
  payload,
  format,
}: {
  active?: boolean;
  payload?: readonly TooltipRow[];
  format: (s: StatSample) => string;
}) {
  const s = payload?.[0]?.payload;
  if (!active || !s) return null;
  return (
    <div className="rounded-md border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-md">
      <div className="text-muted-foreground tabular-nums">{clockLabel(s.t)}</div>
      <div className="font-medium tabular-nums">{format(s)}</div>
    </div>
  );
}

function SparkArea({
  samples,
  dataKey,
  color,
  yMax,
  yTick,
  format,
  label,
  ticks,
}: {
  samples: StatSample[];
  dataKey: "cpu" | "mem";
  color: string;
  yMax: number | ((dataMax: number) => number);
  yTick: (v: number) => string;
  ticks?: number[];
  format: (s: StatSample) => string;
  label: string;
}) {
  const gradId = `grad-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  return (
    <div className="h-40" role="img" aria-label={label}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={samples} margin={{ top: 6, right: 4, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.28} />
              <stop offset="100%" stopColor={color} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="3 3" />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            tickFormatter={(v: number) => clockLabel(v)}
            tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            minTickGap={48}
          />
          <YAxis
            width={52}
            domain={[0, yMax]}
            ticks={ticks}
            tickFormatter={yTick}
            tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            allowDecimals={false}
          />
          <Tooltip
            cursor={{ stroke: "var(--muted-foreground)", strokeOpacity: 0.4 }}
            isAnimationActive={false}
            content={(p) => <ChartTooltip active={p.active} payload={p.payload as readonly TooltipRow[]} format={format} />}
          />
          <Area
            type="monotone"
            dataKey={dataKey}
            stroke={color}
            strokeWidth={2}
            fill={`url(#${gradId})`}
            isAnimationActive={false}
            dot={false}
            activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function ChartPlaceholder({ running, count }: { running: boolean; count: number }) {
  return (
    <div className="flex h-40 flex-col items-center justify-center gap-2 rounded-lg border border-dashed text-center text-xs text-muted-foreground">
      <Activity className="size-4" aria-hidden />
      {running
        ? count === 0
          ? "Waiting for the first sample…"
          : "Collecting samples… the chart appears after the second one."
        : "No samples yet. Charts fill in while the server is running and this page is open."}
    </div>
  );
}

export const ResourceCharts = memo(function ResourceCharts({ samples, running }: { samples: StatSample[]; running: boolean }) {
  const enough = samples.length >= 2;
  const latest = samples[samples.length - 1];
  const memLimit = latest?.memLimit || Math.max(...samples.map((s) => s.mem), 1);
  const desc = enough
    ? `Since page opened · last ${spanLabel(latest.t - samples[0].t)} · ${samples.length} samples every 5 s`
    : "Since page opened · sampled every 5 s (no stored history)";
  const stale = !running && samples.length > 0;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Section
        title="CPU usage"
        description={desc}
        actions={stale ? <span className="text-xs text-muted-foreground">Server not running</span> : undefined}
      >
        {enough ? (
          <SparkArea
            samples={samples}
            dataKey="cpu"
            color="var(--chart-1)"
            yMax={(m: number) => Math.max(100, Math.ceil(m / 50) * 50)}
            yTick={(v) => `${v}%`}
            format={(s) => `${s.cpu.toFixed(1)}% CPU`}
            label={`CPU usage chart, latest ${latest.cpu.toFixed(1)} percent`}
          />
        ) : (
          <ChartPlaceholder running={running} count={samples.length} />
        )}
      </Section>
      <Section
        title="Memory usage"
        description={desc}
        actions={stale ? <span className="text-xs text-muted-foreground">Server not running</span> : undefined}
      >
        {enough ? (
          <SparkArea
            samples={samples}
            dataKey="mem"
            color="var(--chart-2)"
            yMax={memLimit}
            ticks={[0, memLimit / 4, memLimit / 2, (memLimit * 3) / 4, memLimit]}
            yTick={axisBytes}
            format={(s) =>
              `${formatBytes(s.mem)}${s.memLimit ? ` of ${formatBytes(s.memLimit)} (${Math.round((s.mem / s.memLimit) * 100)}%)` : ""}`
            }
            label={`Memory usage chart, latest ${formatBytes(latest.mem)}`}
          />
        ) : (
          <ChartPlaceholder running={running} count={samples.length} />
        )}
      </Section>
    </div>
  );
});
