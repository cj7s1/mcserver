/**
 * Stored resource history: GET /instances/{id}/metrics?range=… (backend/apps/metrics/views.py).
 *
 * The worker samples running servers once a minute; the API averages the samples
 * into fixed buckets per range. Buckets only exist while the server ran, so a
 * jump between two buckets is a period without samples (server offline).
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";

export type MetricsRange = "1h" | "6h" | "24h" | "7d";

export const METRICS_RANGES: { value: MetricsRange; label: string; long: string; ms: number }[] = [
  { value: "1h", label: "1h", long: "last hour", ms: 3_600_000 },
  { value: "6h", label: "6h", long: "last 6 hours", ms: 6 * 3_600_000 },
  { value: "24h", label: "24h", long: "last 24 hours", ms: 24 * 3_600_000 },
  { value: "7d", label: "7d", long: "last 7 days", ms: 7 * 24 * 3_600_000 },
];

export function rangeInfo(range: MetricsRange) {
  return METRICS_RANGES.find((r) => r.value === range) ?? METRICS_RANGES[0];
}

export interface MetricsPoint {
  /** bucket start, unix seconds */
  t: number;
  cpu_percent: number | null;
  memory_bytes: number | null;
  memory_limit_bytes: number | null;
  /** highest player count seen in the bucket */
  players_online: number | null;
  network_rx_bps: number | null;
  network_tx_bps: number | null;
}

export interface MetricsHistory {
  range: MetricsRange;
  bucket_minutes: number;
  points: MetricsPoint[];
  note: string;
}

/** One chart row; shared by stored history and the live in-browser buffer. Values are null in gaps. */
export interface SeriesPoint {
  /** ms since epoch */
  t: number;
  cpu: number | null;
  mem: number | null;
  memLimit: number | null;
  players: number | null;
}

export function useMetricsHistory(instanceId: string, range: MetricsRange, enabled: boolean) {
  return useQuery({
    queryKey: [...qk.instance(instanceId), "metrics", range] as const,
    queryFn: () => api.get<MetricsHistory>(`/instances/${instanceId}/metrics`, { range }),
    enabled,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
    staleTime: 30_000,
    retry: false,
  });
}

/**
 * Convert API points to chart rows, inserting an all-null row wherever buckets are
 * missing so the charts show a gap instead of interpolating across downtime.
 */
export function historySeries(h: MetricsHistory): SeriesPoint[] {
  const bucketMs = Math.max(1, h.bucket_minutes) * 60_000;
  const out: SeriesPoint[] = [];
  let prevT: number | null = null;
  for (const p of [...h.points].sort((a, b) => a.t - b.t)) {
    const t = p.t * 1000;
    // a single late worker run can skip one bucket; two or more missing buckets is real downtime
    if (prevT !== null && t - prevT > 2 * bucketMs) {
      out.push({ t: prevT + bucketMs, cpu: null, mem: null, memLimit: null, players: null });
    }
    out.push({
      t,
      cpu: p.cpu_percent,
      mem: p.memory_bytes,
      memLimit: p.memory_limit_bytes,
      players: p.players_online,
    });
    prevT = t;
  }
  return out;
}
