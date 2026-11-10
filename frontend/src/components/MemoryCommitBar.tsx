/**
 * "How much host memory is already promised" for the create wizard and the Resources tab. Every
 * running server holds its whole container limit (the heap is pre-touched), so the sum of the
 * running servers' limits plus this one is compared with the host's memory. The figures use the
 * same computation as the Dashboard's "Memory reserved" bar (`memoryCommitment`).
 */
import { AlertTriangle, MemoryStick } from "lucide-react";
import { Progress } from "@/components/ui/progress";
import { formatMemoryMb } from "@/lib/format";
import { cn } from "@/lib/utils";

export function MemoryCommitBar({
  otherRunningMb,
  addMb,
  hostMb,
  subject = "this server",
  className,
}: {
  /** Container limits of the other servers that are running on the same host. */
  otherRunningMb: number;
  /** This server's container limit. */
  addMb: number;
  hostMb: number;
  subject?: string;
  className?: string;
}) {
  const totalMb = otherRunningMb + addMb;
  const pct = hostMb > 0 ? (totalMb / hostMb) * 100 : 0;
  const shown = Math.min(100, Math.max(0, pct));
  const over = totalMb > hostMb;
  const tone =
    pct >= 90 ? "[&_[data-slot=progress-indicator]]:bg-destructive" : pct >= 75 ? "[&_[data-slot=progress-indicator]]:bg-amber-500" : "";
  return (
    <div className={cn("grid gap-1.5 rounded-lg border bg-muted/20 px-3 py-2 text-xs", className)}>
      <div className="flex min-w-0 items-center gap-2">
        <MemoryStick className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span className="font-medium">Host memory already reserved</span>
        <span className="ml-auto tabular-nums text-muted-foreground">{pct.toFixed(0)}%</span>
      </div>
      <Progress value={shown} className={cn("h-1.5", tone)} aria-label={`Host memory reserved with ${subject}: ${pct.toFixed(0)}%`} />
      <p className="text-muted-foreground">
        Running servers reserve <span className="font-medium text-foreground tabular-nums">{formatMemoryMb(otherRunningMb)}</span> of{" "}
        <span className="tabular-nums">{formatMemoryMb(hostMb)}</span> host memory; with {subject} (
        <span className="tabular-nums">{formatMemoryMb(addMb)}</span>) that is{" "}
        <span className="font-medium text-foreground tabular-nums">{formatMemoryMb(totalMb)}</span>.
      </p>
      {over && (
        <p className="flex items-start gap-1.5 text-amber-700 dark:text-amber-300" role="alert">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          Together with the servers that are running now, {subject} would need more memory than the host has. Stop another server
          first, or lower the memory; otherwise the host can run out of memory and the system kills a server (or another
          container) without saving.
        </p>
      )}
    </div>
  );
}
