/**
 * Create wizard pieces for games without a version to choose (catalog `fixed_version`; Satisfactory
 * follows Steam's stable branch and checks for an update at every start), and for named ports that
 * share one number over two protocols (`same_port_as`: Satisfactory's game on UDP and API on TCP).
 */
import { RefreshCw } from "lucide-react";
import type { GameInfo } from "@/lib/types";

export function FixedVersion({ info }: { info: NonNullable<GameInfo["fixed_version"]> }) {
  return (
    <div className="grid gap-1.5" role="group" aria-labelledby="game-fixed-version">
      <p id="game-fixed-version" className="text-sm font-medium">
        Version
      </p>
      <p className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2 text-sm">
        <RefreshCw className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        {info.label}
      </p>
      {info.help && <p className="text-xs text-muted-foreground">{info.help}</p>}
    </div>
  );
}
