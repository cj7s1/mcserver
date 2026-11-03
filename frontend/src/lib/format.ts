export function formatBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Math.abs(bytes);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${bytes < 0 ? "-" : ""}${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}

export function formatMemoryMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(mb % 1024 === 0 ? 0 : 1)} GB` : `${mb} MB`;
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function toDate(v: string | number | null | undefined): Date | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function relativeTime(v: string | number | null | undefined, now = Date.now()): string {
  const d = toDate(v);
  if (!d) return "—";
  const diff = (now - d.getTime()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 45) return diff >= 0 ? "just now" : "in a moment";
  const units: [string, number][] = [
    ["year", 31557600],
    ["month", 2629800],
    ["week", 604800],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  const [unit, size] = units.find(([, secs]) => abs >= secs) ?? ["minute", 60];
  const n = Math.max(1, Math.round(abs / size));
  const label = `${n} ${unit}${n === 1 ? "" : "s"}`;
  return diff >= 0 ? `${label} ago` : `in ${label}`;
}

export function formatDateTime(v: string | number | null | undefined): string {
  const d = toDate(v);
  if (!d) return "—";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

export function uptimeSince(v: string | null | undefined, now = Date.now()): string {
  const d = toDate(v);
  if (!d) return "—";
  return formatDuration((now - d.getTime()) / 1000);
}
