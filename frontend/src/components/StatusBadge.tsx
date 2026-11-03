import { cn } from "@/lib/utils";
import type { InstanceStatus } from "@/lib/types";

const STYLES: Record<InstanceStatus, { label: string; dot: string; badge: string; pulse?: boolean }> = {
  created: { label: "Not installed", dot: "bg-zinc-400", badge: "text-zinc-600 dark:text-zinc-300 bg-zinc-500/10" },
  installing: { label: "Installing", dot: "bg-sky-400", badge: "text-sky-700 dark:text-sky-300 bg-sky-500/10", pulse: true },
  starting: { label: "Starting", dot: "bg-amber-400", badge: "text-amber-700 dark:text-amber-300 bg-amber-500/10", pulse: true },
  running: { label: "Running", dot: "bg-emerald-400", badge: "text-emerald-700 dark:text-emerald-300 bg-emerald-500/10" },
  unhealthy: { label: "Unresponsive", dot: "bg-orange-400", badge: "text-orange-700 dark:text-orange-300 bg-orange-500/10", pulse: true },
  stopping: { label: "Stopping", dot: "bg-amber-400", badge: "text-amber-700 dark:text-amber-300 bg-amber-500/10", pulse: true },
  stopped: { label: "Stopped", dot: "bg-zinc-400", badge: "text-zinc-600 dark:text-zinc-300 bg-zinc-500/10" },
  restarting: { label: "Restarting", dot: "bg-amber-400", badge: "text-amber-700 dark:text-amber-300 bg-amber-500/10", pulse: true },
  updating: { label: "Updating", dot: "bg-sky-400", badge: "text-sky-700 dark:text-sky-300 bg-sky-500/10", pulse: true },
  backing_up: { label: "Backing up", dot: "bg-violet-400", badge: "text-violet-700 dark:text-violet-300 bg-violet-500/10", pulse: true },
  restoring: { label: "Restoring", dot: "bg-violet-400", badge: "text-violet-700 dark:text-violet-300 bg-violet-500/10", pulse: true },
  failed: { label: "Failed", dot: "bg-red-500", badge: "text-red-700 dark:text-red-300 bg-red-500/10" },
  unknown: { label: "Unknown", dot: "bg-zinc-500", badge: "text-zinc-600 dark:text-zinc-300 bg-zinc-500/10" },
  archived: { label: "Archived", dot: "bg-zinc-600", badge: "text-zinc-500 bg-zinc-500/10" },
};

export function statusLabel(status: InstanceStatus): string {
  return STYLES[status]?.label ?? status;
}

export function StatusDot({ status, className }: { status: InstanceStatus; className?: string }) {
  const s = STYLES[status] ?? STYLES.unknown;
  return (
    <span className={cn("relative inline-flex size-2.5 shrink-0", className)} aria-hidden>
      {s.pulse && <span className={cn("absolute inline-flex size-full animate-ping rounded-full opacity-60", s.dot)} />}
      <span className={cn("relative inline-flex size-2.5 rounded-full", s.dot)} />
    </span>
  );
}

export function StatusBadge({ status, className, title }: { status: InstanceStatus; className?: string; title?: string }) {
  const s = STYLES[status] ?? STYLES.unknown;
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium", s.badge, className)}
      title={title}
      role="status"
    >
      <StatusDot status={status} />
      {s.label}
    </span>
  );
}
