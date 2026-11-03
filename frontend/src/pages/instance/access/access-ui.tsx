/** Permission chips and the role reference used by the instance Access page. */
import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { PermissionDef, Role } from "@/lib/types";
import { instancePerms } from "./access-utils";
import { cn } from "@/lib/utils";

export function PermissionChip({ def, code, muted }: { def?: PermissionDef; code: string; muted?: boolean }) {
  const chip = (
    <span
      tabIndex={def ? 0 : undefined}
      className={cn(
        "inline-flex h-5 items-center rounded-md border px-1.5 text-[11px] whitespace-nowrap outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
        muted ? "border-dashed text-muted-foreground" : "bg-muted/50",
        (code === "instance.restore" || code === "instance.delete" || code === "instance.access") &&
          !muted &&
          "border-amber-500/40 text-amber-800 dark:text-amber-200",
      )}
    >
      {def?.label ?? code}
    </span>
  );
  if (!def) return chip;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{chip}</TooltipTrigger>
      <TooltipContent>{def.description}</TooltipContent>
    </Tooltip>
  );
}

export function PermissionChips({
  codes,
  defs,
  limit,
  extra,
}: {
  codes: readonly string[];
  defs: Map<string, PermissionDef>;
  limit?: number;
  extra?: ReactNode;
}) {
  // keep the canonical order from /permissions
  const order = [...defs.keys()];
  const sorted = [...codes].sort((a, b) => order.indexOf(a) - order.indexOf(b));
  const shown = limit ? sorted.slice(0, limit) : sorted;
  const rest = sorted.length - shown.length;
  return (
    <div className="flex flex-wrap gap-1">
      {shown.map((c) => (
        <PermissionChip key={c} code={c} def={defs.get(c)} />
      ))}
      {rest > 0 && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={0} className="inline-flex h-5 cursor-help items-center rounded-md px-1 text-[11px] text-muted-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
              +{rest} more
            </span>
          </TooltipTrigger>
          <TooltipContent>
            {sorted
              .slice(shown.length)
              .map((c) => defs.get(c)?.label ?? c)
              .join(", ")}
          </TooltipContent>
        </Tooltip>
      )}
      {extra}
    </div>
  );
}

export function RoleReference({
  roles,
  defs,
  instanceCodes,
  reasons,
}: {
  roles: Role[];
  defs: Map<string, PermissionDef>;
  instanceCodes: Set<string>;
  /** why a role cannot be granted by the current user, if it cannot */
  reasons: Map<number, string | null>;
}) {
  return (
    <ul className="grid gap-3 md:grid-cols-2">
      {roles.map((r) => {
        const perms = instancePerms(r, instanceCodes);
        const reason = reasons.get(r.id);
        return (
          <li key={r.id} className="grid content-start gap-2 rounded-lg border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">{r.name}</span>
              {r.is_system && <span className="text-[11px] text-muted-foreground">built-in</span>}
              <span className="ml-auto text-[11px] text-muted-foreground tabular-nums">
                {perms.length} server permission{perms.length === 1 ? "" : "s"}
              </span>
            </div>
            {r.description && <p className="text-xs text-muted-foreground">{r.description}</p>}
            <PermissionChips codes={perms} defs={defs} />
            {reason && <p className="text-[11px] text-amber-700 dark:text-amber-300">{reason}</p>}
          </li>
        );
      })}
    </ul>
  );
}
