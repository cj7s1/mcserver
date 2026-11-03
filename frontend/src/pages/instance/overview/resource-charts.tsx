/**
 * CPU / memory / players charts for the instance Overview.
 *
 * Primary source is the stored history (GET /instances/{id}/metrics, sampled once a
 * minute by the worker while the server runs). When no stored samples exist for the
 * chosen range, the charts fall back to a client-side rolling buffer of /stats samples
 * that only covers the time this page has been open — and say so.
 */
import { Activity, Loader2, RefreshCw, TriangleAlert } from "lucide-react";
import { memo, useId, useMemo, useState, type ReactNode } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis, type DotItemDotProps } from "recharts";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { errorMessage } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import type { StatSample } from "./live-samples";
import {
  METRICS_RANGES,
  historySeries,
  rangeInfo,
  useMetricsHistory,
  type MetricsRange,
  type SeriesPoint,
} from "./metrics-history";

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

function shortClock(t: number): string {
  return new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function dayClock(t: number): string {
  return new Date(t).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

type SeriesKey = "cpu" | "mem" | "players";

/** How the time axis and tooltip header are labelled for the current data source. */
interface TimeFormat {
  tick: (t: number) => string;
  tooltip: (t: number) => string;
  /** explicit axis ticks (stored history uses a fixed window, so ticks are aligned to clock time) */
  ticks?: number[];
}

const LIVE_TIME: TimeFormat = { tick: clockLabel, tooltip: clockLabel };

function dayLabel(t: number): string {
  return new Date(t).toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
}

/** Ticks on local clock boundaries: 15 min (1h), 1 h (6h), 4 h (24h), midnight (7d). */
function historyTicks(range: MetricsRange, start: number, end: number): number[] {
  const d = new Date(start);
  const out: number[] = [];
  if (range === "7d") {
    d.setHours(0, 0, 0, 0);
    for (; d.getTime() <= end; d.setDate(d.getDate() + 1)) if (d.getTime() >= start) out.push(d.getTime());
    return out;
  }
  const stepMin = range === "1h" ? 15 : range === "6h" ? 60 : 240;
  d.setMinutes(0, 0, 0);
  if (stepMin >= 60) d.setHours(d.getHours() - (d.getHours() % (stepMin / 60)));
  for (; d.getTime() <= end; d.setMinutes(d.getMinutes() + stepMin)) if (d.getTime() >= start) out.push(d.getTime());
  return out;
}

function historyTime(range: MetricsRange, bucketMinutes: number, start: number, end: number): TimeFormat {
  const bucketMs = bucketMinutes * 60_000;
  const from = range === "7d" || range === "24h" ? dayClock : shortClock;
  return {
    tick: range === "7d" ? dayLabel : shortClock,
    tooltip: (t) => (bucketMinutes > 1 ? `${from(t)} – ${shortClock(t + bucketMs)}` : from(t)),
    ticks: historyTicks(range, start, end),
  };
}

interface ChartRow extends SeriesPoint {
  /** this row's value has no neighbours, so the line would be invisible: draw a dot */
  iso: boolean;
}

function ChartTooltip({
  active,
  row,
  dataKey,
  time,
  format,
}: {
  active?: boolean;
  row?: ChartRow;
  dataKey: SeriesKey;
  time: TimeFormat;
  format: (p: SeriesPoint) => string;
}) {
  if (!active || !row) return null;
  return (
    <div className="rounded-md border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-md">
      <div className="text-muted-foreground tabular-nums">{time.tooltip(row.t)}</div>
      <div className="font-medium tabular-nums">{row[dataKey] === null ? "No samples recorded" : format(row)}</div>
    </div>
  );
}

function TimeSeriesChart({
  data,
  dataKey,
  color,
  xDomain,
  time,
  yMax,
  yTick,
  ticks,
  format,
  label,
  step = false,
}: {
  data: SeriesPoint[];
  dataKey: SeriesKey;
  color: string;
  xDomain: [number, number] | ["dataMin", "dataMax"];
  time: TimeFormat;
  yMax: number | ((dataMax: number) => number);
  yTick: (v: number) => string;
  ticks?: number[];
  format: (p: SeriesPoint) => string;
  label: string;
  step?: boolean;
}) {
  const gradId = `grad-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const rows = useMemo<ChartRow[]>(
    () =>
      data.map((p, i) => ({
        ...p,
        iso: p[dataKey] !== null && (data[i - 1]?.[dataKey] ?? null) === null && (data[i + 1]?.[dataKey] ?? null) === null,
      })),
    [data, dataKey],
  );
  const dot = (p: DotItemDotProps) =>
    (p.payload as ChartRow | undefined)?.iso && p.cx !== undefined && p.cy !== undefined ? (
      <circle cx={p.cx} cy={p.cy} r={3} fill={color} stroke="var(--card)" strokeWidth={1} />
    ) : null;
  return (
    <div className="h-40" role="img" aria-label={label}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={rows} margin={{ top: 6, right: 4, left: 0, bottom: 0 }}>
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
            domain={xDomain}
            ticks={time.ticks}
            tickFormatter={(v: number) => time.tick(v)}
            tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
            tickLine={false}
            axisLine={false}
            minTickGap={48}
          />
          <YAxis
            width={60}
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
            content={(p) => (
              <ChartTooltip
                active={p.active}
                row={(p.payload as readonly { payload?: ChartRow }[] | undefined)?.[0]?.payload}
                dataKey={dataKey}
                time={time}
                format={format}
              />
            )}
          />
          <Area
            type={step ? "stepAfter" : "monotone"}
            dataKey={dataKey}
            stroke={color}
            strokeWidth={2}
            fill={`url(#${gradId})`}
            isAnimationActive={false}
            connectNulls={false}
            dot={dot}
            activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function ChartPlaceholder({ children, loading = false }: { children: ReactNode; loading?: boolean }) {
  return (
    <div className="flex h-40 flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-3 text-center text-xs text-muted-foreground">
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Activity className="size-4" aria-hidden />}
      {children}
    </div>
  );
}

function ChartBlock({ title, summary, children }: { title: string; summary?: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <h3 className="shrink-0 text-xs font-medium text-muted-foreground">{title}</h3>
        {summary && <span className="min-w-0 truncate text-xs tabular-nums">{summary}</span>}
      </div>
      {children}
    </div>
  );
}

function values(data: SeriesPoint[], key: SeriesKey | "memLimit"): number[] {
  const out: number[] = [];
  for (const p of data) {
    const v = p[key];
    if (v !== null && Number.isFinite(v)) out.push(v);
  }
  return out;
}

function lastValue(data: SeriesPoint[], key: SeriesKey): number | null {
  for (let i = data.length - 1; i >= 0; i--) {
    const v = data[i][key];
    if (v !== null) return v;
  }
  return null;
}

function liveSeries(samples: StatSample[]): SeriesPoint[] {
  return samples.map((s) => ({ t: s.t, cpu: s.cpu, mem: s.mem, memLimit: s.memLimit || null, players: s.players }));
}

interface Source {
  kind: "history" | "live";
  data: SeriesPoint[];
  xDomain: [number, number] | ["dataMin", "dataMax"];
  time: TimeFormat;
  /** history: per-bucket aggregates (summaries say "peak"/"avg"); live: raw samples */
  aggregated: boolean;
  bucketMinutes: number;
}

/** `key` is null when no series has anything to draw (one placeholder instead of three). */
type Placeholder = (key: SeriesKey | null) => ReactNode;

function Charts({ source, placeholder }: { source: Source; placeholder: Placeholder }) {
  const { data, aggregated, bucketMinutes } = source;
  const cpu = values(data, "cpu");
  const mem = values(data, "mem");
  const players = values(data, "players");
  const memLimit = Math.max(0, ...values(data, "memLimit")) || Math.max(1, ...mem);
  const avgNote = aggregated && bucketMinutes > 1 ? `${bucketMinutes}-min average` : "";
  const peakCpu = cpu.length ? Math.max(...cpu) : null;
  const latestCpu = lastValue(data, "cpu");
  const latestMem = lastValue(data, "mem");
  const latestPlayers = lastValue(data, "players");
  // a line needs two points; stored history also draws lone buckets as dots
  const drawable = (n: number) => (source.kind === "history" ? n >= 1 : n >= 2);

  const cpuSummary = aggregated
    ? peakCpu !== null
      ? `peak ${peakCpu.toFixed(1)}% · avg ${(cpu.reduce((a, b) => a + b, 0) / cpu.length).toFixed(1)}%`
      : undefined
    : latestCpu !== null
      ? `now ${latestCpu.toFixed(1)}%`
      : undefined;
  const memSummary = aggregated
    ? mem.length
      ? `peak ${formatBytes(Math.max(...mem))}`
      : undefined
    : latestMem !== null
      ? `now ${formatBytes(latestMem)}`
      : undefined;
  const playerSummary = aggregated
    ? players.length
      ? `peak ${Math.max(...players)}`
      : undefined
    : latestPlayers !== null
      ? `now ${latestPlayers}`
      : undefined;

  if (!drawable(cpu.length) && !drawable(mem.length) && !drawable(players.length)) {
    return placeholder(null);
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <ChartBlock title="CPU" summary={cpuSummary}>
        {drawable(cpu.length) ? (
          <TimeSeriesChart
            data={data}
            dataKey="cpu"
            color="var(--chart-1)"
            xDomain={source.xDomain}
            time={source.time}
            yMax={(m: number) => Math.max(100, Math.ceil(m / 50) * 50)}
            yTick={(v) => `${v}%`}
            format={(p) => `${(p.cpu ?? 0).toFixed(1)}% CPU${avgNote ? ` (${avgNote})` : ""}`}
            label={`CPU usage chart${cpuSummary ? `, ${cpuSummary}` : ""}. 100 percent equals one core.`}
          />
        ) : (
          placeholder("cpu")
        )}
      </ChartBlock>
      <ChartBlock title="Memory" summary={memSummary}>
        {drawable(mem.length) ? (
          <TimeSeriesChart
            data={data}
            dataKey="mem"
            color="var(--chart-2)"
            xDomain={source.xDomain}
            time={source.time}
            yMax={memLimit}
            ticks={[0, memLimit / 4, memLimit / 2, (memLimit * 3) / 4, memLimit]}
            yTick={axisBytes}
            format={(p) => {
              const used = p.mem ?? 0;
              const limit = p.memLimit;
              return `${formatBytes(used)}${limit ? ` of ${formatBytes(limit)} (${Math.round((used / limit) * 100)}%)` : ""}${avgNote ? ` · ${avgNote}` : ""}`;
            }}
            label={`Memory usage chart${memSummary ? `, ${memSummary}` : ""}`}
          />
        ) : (
          placeholder("mem")
        )}
      </ChartBlock>
      <ChartBlock title="Players online" summary={playerSummary}>
        {drawable(players.length) ? (
          <TimeSeriesChart
            data={data}
            dataKey="players"
            color="var(--chart-3)"
            xDomain={source.xDomain}
            time={source.time}
            yMax={(m: number) => Math.max(1, m)}
            yTick={(v) => String(v)}
            format={(p) => {
              const n = p.players ?? 0;
              const label = `${n} player${n === 1 ? "" : "s"}`;
              return aggregated && bucketMinutes > 1 ? `${label} (peak in ${bucketMinutes} min)` : label;
            }}
            label={`Players online chart${playerSummary ? `, ${playerSummary}` : ""}`}
            step
          />
        ) : (
          placeholder("players")
        )}
      </ChartBlock>
    </div>
  );
}

const SERIES_NOUN: Record<SeriesKey, string> = { cpu: "CPU", mem: "memory", players: "player count" };

export const ResourceCharts = memo(function ResourceCharts({
  instanceId,
  samples,
  running,
}: {
  instanceId: string;
  samples: StatSample[];
  running: boolean;
}) {
  const [range, setRange] = useState<MetricsRange>("1h");
  const info = rangeInfo(range);
  const history = useMetricsHistory(instanceId, range, true);
  const h = history.data;
  const hasHistory = !!h && h.points.length > 0;
  const updatedAt = history.dataUpdatedAt;

  const historySource = useMemo<Source | null>(() => {
    if (!h || !h.points.length) return null;
    const end = updatedAt;
    const start = end - rangeInfo(h.range).ms;
    return {
      kind: "history",
      data: historySeries(h),
      xDomain: [start, end],
      time: historyTime(h.range, h.bucket_minutes, start, end),
      aggregated: true,
      bucketMinutes: h.bucket_minutes,
    };
  }, [h, updatedAt]);

  const live = useMemo<Source>(
    () => ({ kind: "live", data: liveSeries(samples), xDomain: ["dataMin", "dataMax"], time: LIVE_TIME, aggregated: false, bucketMinutes: 0 }),
    [samples],
  );

  const toggle = (
    <ToggleGroup
      type="single"
      size="sm"
      variant="outline"
      spacing={0}
      value={range}
      onValueChange={(v) => v && setRange(v as MetricsRange)}
      aria-label="History range"
    >
      {METRICS_RANGES.map((r) => (
        <ToggleGroupItem key={r.value} value={r.value} aria-label={`Show the ${r.long}`} className="px-2.5 text-xs">
          {r.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );

  let description: ReactNode;
  let body: ReactNode;
  if (history.isLoading) {
    description = `Stored samples · ${info.long}`;
    body = (
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3" role="status" aria-label="Loading stored history">
        {(["CPU", "Memory", "Players online"] as const).map((t) => (
          <ChartBlock key={t} title={t}>
            <ChartPlaceholder loading>Loading stored history…</ChartPlaceholder>
          </ChartBlock>
        ))}
      </div>
    );
  } else if (hasHistory && historySource && h) {
    const stalePlaceholder: Placeholder = (key) => (
      <ChartPlaceholder>
        No {key ? `${SERIES_NOUN[key]} ` : ""}values were recorded in the {info.long}.
      </ChartPlaceholder>
    );
    description = (
      <>
        Stored samples · {info.long} · {h.bucket_minutes === 1 ? "1-minute" : `${h.bucket_minutes}-minute`} buckets
        {h.note ? <> · {h.note}</> : null}
      </>
    );
    body = <Charts source={historySource} placeholder={stalePlaceholder} />;
  } else {
    // No stored samples (or the history request failed): show the in-browser buffer, labelled as such.
    const enough = samples.length >= 2;
    const latest = samples[samples.length - 1];
    const liveDesc = enough
      ? `last ${spanLabel(latest.t - samples[0].t)} · ${samples.length} samples every 5 s`
      : "sampled every 5 s";
    description = (
      <>
        {history.error ? "Stored history unavailable" : `No stored samples in the ${info.long}`} · showing live samples since this
        page opened ({liveDesc})
      </>
    );
    const livePlaceholder: Placeholder = (key) => (
      <ChartPlaceholder>
        {running
          ? samples.length === 0
            ? "Waiting for the first sample…"
            : samples.length === 1
              ? "Collecting samples… the chart appears after the second one."
              : `No ${key ? `${SERIES_NOUN[key]} ` : ""}values received yet.`
          : `No samples in the ${info.long}. Samples are recorded every minute while the server runs.`}
      </ChartPlaceholder>
    );
    body = (
      <div className="grid gap-3">
        {history.error && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
          >
            <TriangleAlert className="size-3.5 shrink-0" aria-hidden />
            <span className="min-w-0 flex-1">Could not load stored history: {errorMessage(history.error)}</span>
            <Button size="xs" variant="outline" onClick={() => history.refetch()}>
              <RefreshCw /> Retry
            </Button>
          </div>
        )}
        <Charts source={live} placeholder={livePlaceholder} />
      </div>
    );
  }

  const stale = !running && !hasHistory && samples.length > 0;
  return (
    <Section
      title="Resource history"
      description={description}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          {stale && <span className="text-xs text-muted-foreground">Server not running</span>}
          {history.isFetching && !history.isLoading && (
            <Loader2 className="size-3.5 animate-spin text-muted-foreground" aria-label="Refreshing history" />
          )}
          {toggle}
        </div>
      }
    >
      {body}
    </Section>
  );
});
