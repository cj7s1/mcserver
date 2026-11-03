import type { Backup } from "@/lib/types";
import { cn } from "@/lib/utils";
import { KIND_INFO } from "./backup-utils";

export function KindBadge({ kind }: { kind: Backup["kind"] }) {
  const k = KIND_INFO[kind] ?? KIND_INFO.manual;
  return (
    <span
      className={cn("inline-flex h-5 items-center rounded-full px-2 text-[11px] font-medium whitespace-nowrap", k.className)}
      title={k.hint}
    >
      {k.label}
    </span>
  );
}
