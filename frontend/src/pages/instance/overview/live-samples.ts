/**
 * Client-side rolling buffer of live /stats samples (about the last 5 minutes). It
 * starts empty when the page opens and only ever holds samples this browser received;
 * the Overview uses it for the network rate tile and as the chart fallback when the
 * server has no stored history yet.
 */
import { useState } from "react";
import type { Stats } from "@/lib/types";

export interface StatSample {
  /** client receive time (ms) */
  t: number;
  /** agent timestamp of the sample, used to drop duplicates */
  ts?: string;
  cpu: number;
  mem: number;
  memLimit: number;
  /** players online from the same /stats response; null when RCON could not answer */
  players: number | null;
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
    players: typeof stats.players?.online === "number" ? stats.players.online : null,
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
