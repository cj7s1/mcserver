/** Kind badge for snapshots (manual / scheduled / before rollback). */
import { cn } from "@/lib/utils";
import { SNAPSHOT_KIND_INFO, UNKNOWN_SNAPSHOT_KIND, type Snapshot } from "./snapshot-utils";

export function SnapshotKindBadge({ kind }: { kind: Snapshot["kind"] }) {
  const k = (kind && SNAPSHOT_KIND_INFO[kind]) || UNKNOWN_SNAPSHOT_KIND;
  return (
    <span
      className={cn("inline-flex h-5 items-center rounded-full px-2 text-[11px] font-medium whitespace-nowrap", k.className)}
      title={k.hint}
    >
      {k.label}
    </span>
  );
}
