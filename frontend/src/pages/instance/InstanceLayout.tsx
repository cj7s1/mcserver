import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, ChevronLeft, Play, RotateCw, Skull, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useOutletContext, useParams } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { JobProgress } from "@/components/JobProgress";
import { PowerActionDialog } from "@/components/PowerActionDialog";
import { PowerCountdownBanner } from "@/components/PowerCountdownBanner";
import { ServerIcon } from "@/components/ServerIcon";
import { ConnectionLostBanner, ErrorState, LoadingState } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { ApiError, errorMessage } from "@/lib/api";
import { gameLabel, hasCapability, isMinecraft, variantLabel } from "@/lib/games";
import { sendPower } from "@/lib/power";
import { hasPerm, useInstance, useInvalidateInstance } from "@/lib/queries";
import { restartRequiredText } from "@/lib/restart-required";
import { instanceSections, sectionPath } from "@/lib/sections";
import type { Instance, PermissionCode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ModpackUpdateBadge } from "./modpack/UpdateBadge";
import { DISMISS_HINT, powerButtons } from "./power-controls";

export interface InstanceContext {
  instance: Instance;
  can: (code: PermissionCode) => boolean;
}

export function useInstanceContext(): InstanceContext {
  return useOutletContext<InstanceContext>();
}

export function softwareLabel(i: Instance): string {
  if (!isMinecraft(i)) return `${gameLabel(i)} ${i.minecraft_version}`.trim();
  if (i.is_modpack) return `${i.modpack_name || "Modpack"} ${i.modpack_version_name}`.trim();
  return `${variantLabel(i.server_type)} ${i.minecraft_version}${i.loader_version ? ` · ${i.loader_version}` : ""}`;
}

/** The game port in the header: Minecraft's number as before; other games' first named port with its protocol. */
function portLabel(i: Instance): string {
  const first = !isMinecraft(i) ? i.ports?.[0] : undefined;
  return first ? `${first.host_port}/${first.protocol}` : String(i.game_port);
}

function PowerControls({ instance, can }: InstanceContext) {
  const invalidate = useInvalidateInstance();
  const [confirm, setConfirm] = useState<"restart" | "stop" | null>(null);
  const power = useMutation({
    mutationFn: (action: "start" | "stop" | "kill") => sendPower(instance, { action }),
    onSuccess: (outcome, action) => {
      if (outcome.status === "cancelled") return;
      toast.success(action === "kill" ? "Server process killed" : `${action[0].toUpperCase()}${action.slice(1)} requested`);
      invalidate(instance.id);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  if (!can("instance.control") || instance.archived_at) return null;
  const busy = !!instance.active_job || power.isPending;
  const show = powerButtons(instance);
  return (
    <div className="flex flex-wrap items-center gap-2">
      {show.start && (
        <Button size="sm" onClick={() => power.mutate("start")} disabled={busy}>
          <Play /> Start
        </Button>
      )}
      {show.dismiss && (
        // Not running but still meant to run (it failed, or stopped by itself): the server keeps its
        // container until someone decides, with Start or with this Stop. Nobody is online, so there
        // is no countdown to offer.
        <Button size="sm" variant="outline" onClick={() => power.mutate("stop")} disabled={busy} title={DISMISS_HINT}>
          <Square /> Stop
        </Button>
      )}
      {show.restart && (
        <Button size="sm" variant="outline" onClick={() => setConfirm("restart")} disabled={busy}>
          <RotateCw /> Restart
        </Button>
      )}
      {show.stop && (
        <Button size="sm" variant="outline" onClick={() => setConfirm("stop")} disabled={busy}>
          <Square /> Stop
        </Button>
      )}
      {confirm && (
        <PowerActionDialog instance={instance} action={confirm} open onOpenChange={(o) => !o && setConfirm(null)} />
      )}
      {show.kill && (
        <ConfirmDialog
          trigger={
            <Button size="sm" variant="destructive" disabled={power.isPending} aria-label="Kill server process">
              <Skull />
              <span className="hidden sm:inline">Kill</span>
            </Button>
          }
          title="Kill the server process?"
          description="This immediately terminates the server without saving. Chunks modified since the last autosave may be lost. Prefer Stop unless the server is stuck."
          confirmLabel="Kill now"
          destructive
          onConfirm={async () => {
            await power.mutateAsync("kill");
          }}
        />
      )}
    </div>
  );
}

export function InstanceLayout() {
  const { id } = useParams();
  const { pathname } = useLocation();
  const { data: instance, isLoading, error, refetch } = useInstance(id);
  const tabsRef = useRef<HTMLElement>(null);
  const loaded = !!instance;
  // On narrow screens the section tabs scroll sideways: keep the current one in view (horizontally
  // only, so the page itself does not jump).
  useEffect(() => {
    const nav = tabsRef.current;
    const active = nav?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!nav || !active) return;
    const n = nav.getBoundingClientRect();
    const a = active.getBoundingClientRect();
    if (a.left < n.left) nav.scrollLeft += a.left - n.left - 12;
    else if (a.right > n.right) nav.scrollLeft += a.right - n.right + 12;
  }, [pathname, loaded]);
  if (isLoading) return <LoadingState label="Loading server…" />;
  // Only without data: a failed background poll keeps the page (and its unsaved edits, open dialogs)
  // mounted and shows a banner until the next poll succeeds.
  // A 403/404 means the server is gone or no longer visible: the cached data no longer applies.
  const gone = error instanceof ApiError && (error.status === 403 || error.status === 404);
  if (!instance || gone) return <ErrorState error={error ?? new Error("Server not found")} onRetry={() => refetch()} />;
  const can = (code: PermissionCode) => hasPerm(instance.permissions, code);
  const ctx: InstanceContext = { instance, can };
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      {error && <ConnectionLostBanner error={error} onRetry={() => refetch()} />}
      <div className="min-w-0">
        <NavLink to="/servers" className="mb-3 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
          <ChevronLeft className="size-3.5" /> All servers
        </NavLink>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <ServerIcon url={instance.icon_url} name={instance.name} type={instance.server_type} game={instance.game} className="size-12" />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h1 className="min-w-0 text-xl font-semibold tracking-tight [overflow-wrap:anywhere]">{instance.name}</h1>
                <StatusBadge status={instance.status} title={instance.status_detail} />
                <ModpackUpdateBadge instance={instance} />
              </div>
              <p className="mt-0.5 text-sm text-muted-foreground [overflow-wrap:anywhere]">
                {softwareLabel(instance)}
                {hasCapability(instance, "jvm") && ` · Java ${instance.java_version}`} · port {portLabel(instance)}
              </p>
            </div>
          </div>
          <PowerControls {...ctx} />
        </div>
        {instance.status_detail && !["running"].includes(instance.status) && (
          <p className={cn("mt-2 text-sm", instance.status === "failed" ? "text-destructive" : "text-muted-foreground")}>
            {instance.status_detail}
          </p>
        )}
        {instance.restart_required && !instance.active_job && (
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
            <AlertTriangle className="size-4 shrink-0" />
            {restartRequiredText(instance)}
          </div>
        )}
        {instance.active_job && <JobProgress job={instance.active_job} className="mt-3" />}
        {!instance.archived_at && (
          <PowerCountdownBanner instance={instance} canControl={can("instance.control")} className="mt-3" />
        )}
      </div>
      <nav ref={tabsRef} className="-mx-3 overflow-x-auto border-b px-3 md:mx-0 md:px-0" aria-label="Server sections">
        <div className="flex min-w-max gap-1">
          {instanceSections(instance).map((t) => (
            <NavLink
              key={t.key}
              to={sectionPath(instance, t)}
              end={!t.path}
              className={({ isActive }) =>
                cn(
                  "border-b-2 border-transparent px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground",
                  isActive && "border-primary font-medium text-foreground",
                )
              }
            >
              {t.label(instance)}
            </NavLink>
          ))}
        </div>
      </nav>
      <Outlet context={ctx} />
    </div>
  );
}
