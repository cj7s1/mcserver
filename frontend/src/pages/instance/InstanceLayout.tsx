import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, ChevronLeft, Play, RotateCw, Skull, Square } from "lucide-react";
import { NavLink, Outlet, useOutletContext, useParams } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { JobProgress } from "@/components/JobProgress";
import { ServerIcon } from "@/components/ServerIcon";
import { ErrorState, LoadingState } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import { hasPerm, useInstance, useInvalidateInstance } from "@/lib/queries";
import type { Instance, PermissionCode } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface InstanceContext {
  instance: Instance;
  can: (code: PermissionCode) => boolean;
}

export function useInstanceContext(): InstanceContext {
  return useOutletContext<InstanceContext>();
}

const RUNNING_LIKE = ["running", "starting", "installing", "unhealthy", "restarting"];

export function softwareLabel(i: Instance): string {
  if (i.is_modpack) return `${i.modpack_name || "Modpack"} ${i.modpack_version_name}`.trim();
  const type = i.server_type.charAt(0) + i.server_type.slice(1).toLowerCase();
  const name = i.server_type === "NEOFORGE" ? "NeoForge" : type;
  return `${name} ${i.minecraft_version}${i.loader_version ? ` · ${i.loader_version}` : ""}`;
}

function PowerControls({ instance, can }: InstanceContext) {
  const invalidate = useInvalidateInstance();
  const power = useMutation({
    mutationFn: (action: "start" | "stop" | "restart" | "kill") => api.post(`/instances/${instance.id}/power`, { action }),
    onSuccess: (_d, action) => {
      toast.success(action === "kill" ? "Server process killed" : `${action[0].toUpperCase()}${action.slice(1)} requested`);
      invalidate(instance.id);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  if (!can("instance.control") || instance.archived_at) return null;
  const busy = !!instance.active_job || power.isPending;
  const running = RUNNING_LIKE.includes(instance.status);
  return (
    <div className="flex flex-wrap items-center gap-2">
      {!running ? (
        <Button size="sm" onClick={() => power.mutate("start")} disabled={busy}>
          <Play /> Start
        </Button>
      ) : (
        <>
          <Button size="sm" variant="outline" onClick={() => power.mutate("restart")} disabled={busy}>
            <RotateCw /> Restart
          </Button>
          <Button size="sm" variant="outline" onClick={() => power.mutate("stop")} disabled={busy}>
            <Square /> Stop
          </Button>
        </>
      )}
      {(running || instance.status === "stopping") && (
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

const TABS: { to: string; label: string; perm?: PermissionCode; when?: (i: Instance) => boolean }[] = [
  { to: "", label: "Overview" },
  { to: "console", label: "Console", perm: "instance.console" },
  { to: "files", label: "Files", perm: "instance.files.read" },
  { to: "content", label: "Mods", when: (i) => i.content_kind === "mods" },
  { to: "content", label: "Plugins", when: (i) => i.content_kind === "plugins" },
  { to: "modpack", label: "Modpack", when: (i) => i.is_modpack },
  { to: "players", label: "Players" },
  { to: "backups", label: "Backups", perm: "instance.backup" },
  { to: "schedules", label: "Schedules" },
  { to: "network", label: "Network" },
  { to: "settings", label: "Settings" },
  { to: "access", label: "Access", perm: "instance.access" },
  { to: "activity", label: "Activity" },
];

export function InstanceLayout() {
  const { id } = useParams();
  const { data: instance, isLoading, error, refetch } = useInstance(id);
  if (isLoading) return <LoadingState label="Loading server…" />;
  if (error || !instance) return <ErrorState error={error ?? new Error("Server not found")} onRetry={() => refetch()} />;
  const can = (code: PermissionCode) => hasPerm(instance.permissions, code);
  const ctx: InstanceContext = { instance, can };
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <div className="min-w-0">
        <NavLink to="/servers" className="mb-3 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
          <ChevronLeft className="size-3.5" /> All servers
        </NavLink>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <ServerIcon url={instance.icon_url} name={instance.name} type={instance.server_type} className="size-12" />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="truncate text-xl font-semibold tracking-tight">{instance.name}</h1>
                <StatusBadge status={instance.status} title={instance.status_detail} />
              </div>
              <p className="mt-0.5 truncate text-sm text-muted-foreground">
                {softwareLabel(instance)} · Java {instance.java_version} · port {instance.game_port}
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
            {instance.status === "stopped" || instance.status === "failed"
              ? "Configuration changed. It is applied the next time the server starts (the container is recreated; world data is kept)."
              : "Configuration changed. Restart the server to apply it (the container is recreated; world data is kept)."}
          </div>
        )}
        {instance.active_job && <JobProgress job={instance.active_job} className="mt-3" />}
      </div>
      <nav className="-mx-3 overflow-x-auto border-b px-3 md:mx-0 md:px-0" aria-label="Server sections">
        <div className="flex min-w-max gap-1">
          {TABS.filter((t) => (!t.perm || can(t.perm)) && (!t.when || t.when(instance))).map((t) => (
            <NavLink
              key={t.label}
              to={t.to ? `/servers/${instance.id}/${t.to}` : `/servers/${instance.id}`}
              end={!t.to}
              className={({ isActive }) =>
                cn(
                  "border-b-2 border-transparent px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground",
                  isActive && "border-primary font-medium text-foreground",
                )
              }
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      </nav>
      <Outlet context={ctx} />
    </div>
  );
}
