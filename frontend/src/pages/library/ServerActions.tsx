import { ArchiveRestore, Info, Loader2, MoreHorizontal, Play, RotateCw, Settings, Square, Terminal } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { PowerActionDialog } from "@/components/PowerActionDialog";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { hasPerm } from "@/lib/queries";
import type { Instance, PermissionCode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { DISMISS_HINT, powerButtons } from "@/pages/instance/power-controls";
import { isRunningLike, useServerPower, useUnarchive } from "./server-utils";

/**
 * Per-server quick actions: an inline Start/Stop button (when the user may control the server)
 * plus a menu with restart, console, settings and unarchive. Permission-aware via instance.permissions.
 * Stop and Restart open the same confirmation (with the optional in-game warning) as the server page.
 * A server that is down but still meant to run (failed, or stopped by itself) and keeps its container
 * also gets Stop, by the same rule as the server header (`powerButtons(...).dismiss`).
 */
export function ServerQuickActions({
  instance,
  showInline = true,
  inlineLabel = true,
  className,
}: {
  instance: Instance;
  showInline?: boolean;
  inlineLabel?: boolean;
  className?: string;
}) {
  const power = useServerPower(instance);
  const unarchive = useUnarchive(instance);
  const [confirm, setConfirm] = useState<"restart" | "stop" | null>(null);
  const can = (code: PermissionCode) => hasPerm(instance.permissions, code);
  const archived = !!instance.archived_at;
  const canControl = can("instance.control") && !archived;
  const running = isRunningLike(instance);
  const stopping = instance.status === "stopping";
  const busy = !!instance.active_job || power.isPending;
  const dismiss = powerButtons(instance).dismiss;
  const base = `/servers/${instance.id}`;

  return (
    <div className={cn("flex items-center gap-1", className)}>
      {showInline &&
        canControl &&
        !stopping &&
        (running ? (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => setConfirm("stop")}
            aria-label={`Stop ${instance.name}`}
          >
            <Square />
            {inlineLabel && <span className="hidden sm:inline">Stop</span>}
          </Button>
        ) : (
          <Button size="sm" disabled={busy} onClick={() => power.mutate("start")} aria-label={`Start ${instance.name}`}>
            {power.isPending ? <Loader2 className="animate-spin" /> : <Play />}
            {inlineLabel && <span className="hidden sm:inline">Start</span>}
          </Button>
        ))}
      {showInline && archived && can("instance.delete") && (
        <Button size="sm" variant="outline" disabled={unarchive.isPending} onClick={() => unarchive.mutate()}>
          {unarchive.isPending ? <Loader2 className="animate-spin" /> : <ArchiveRestore />}
          {inlineLabel && <span className="hidden sm:inline">Unarchive</span>}
        </Button>
      )}
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon-sm" aria-label={`More actions for ${instance.name}`}>
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-48">
          <DropdownMenuLabel className="truncate">{instance.name}</DropdownMenuLabel>
          {canControl && (
            <>
              {!running && !stopping && (
                <DropdownMenuItem disabled={busy} onSelect={() => power.mutate("start")}>
                  <Play /> Start
                </DropdownMenuItem>
              )}
              {dismiss && (
                // nobody is online, so there is no countdown to offer (same as the header's Stop)
                <DropdownMenuItem disabled={busy} onSelect={() => power.mutate("stop")} title={DISMISS_HINT}>
                  <Square /> Stop
                </DropdownMenuItem>
              )}
              {running && (
                <>
                  {!instance.limited && (
                    <DropdownMenuItem disabled={busy} onSelect={() => setConfirm("restart")}>
                      <RotateCw /> Restart…
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem disabled={busy} onSelect={() => setConfirm("stop")}>
                    <Square /> Stop…
                  </DropdownMenuItem>
                </>
              )}
              <DropdownMenuSeparator />
            </>
          )}
          {archived && can("instance.delete") && (
            <>
              <DropdownMenuItem disabled={unarchive.isPending} onSelect={() => unarchive.mutate()}>
                <ArchiveRestore /> Unarchive
              </DropdownMenuItem>
              <DropdownMenuSeparator />
            </>
          )}
          <DropdownMenuItem asChild>
            <Link to={base}>
              <Info /> Overview
            </Link>
          </DropdownMenuItem>
          {can("instance.console") && (
            <DropdownMenuItem asChild>
              <Link to={`${base}/console`}>
                <Terminal /> Open console
              </Link>
            </DropdownMenuItem>
          )}
          {!instance.limited && (
            <DropdownMenuItem asChild>
              <Link to={`${base}/settings`}>
                <Settings /> Settings
              </Link>
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {confirm && (
        <PowerActionDialog instance={instance} action={confirm} open onOpenChange={(o) => !o && setConfirm(null)} />
      )}
    </div>
  );
}
