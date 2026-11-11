import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Archive, Cpu, RefreshCw, Server, ServerOff, Settings2, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { PageHeader, Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/use-auth";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { qk, useSystemInfo } from "@/lib/queries";
import type { SystemInfo, SystemSettings } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useGames } from "../modpacks/modpackQueries";
import { AdminCallout, AdminNoPermission } from "./admin-shared";
import { NodesSection } from "./system/NodesSection";
import { NotificationsSection } from "./system/NotificationsSection";
import { GameImagesSection } from "./system/GameImagesSection";
import { SettingsForm } from "./system/SettingsForm";
import {
  HostSection,
  PolicySection,
  PortsSection,
  SecurityNotesSection,
  StorageSection,
} from "./system/SystemPanels";
import { agentDocker, agentPolicy, resticVersion, type PortsInfo } from "./system/system-types";

const portsKey = [...qk.system, "ports"] as const;

export default function SystemPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const canSettings = can("system.settings");
  const canNodes = can("system.nodes");
  const allowed = canSettings || canNodes;

  const info = useSystemInfo();
  const ports = useQuery({
    queryKey: portsKey,
    queryFn: () => api.get<PortsInfo>("/system/ports"),
    enabled: allowed,
    refetchInterval: 30_000,
  });
  const settings = useQuery({
    queryKey: qk.settings,
    queryFn: () => api.get<SystemSettings>("/system/settings"),
    enabled: canSettings,
  });
  // per-game runtime images: only when the panel runs more than one game
  const games = useGames(canSettings);

  if (!allowed) return <AdminNoPermission permission="system.settings" what="System settings" />;

  const refreshing = info.isFetching || ports.isFetching;

  return (
    <div className="grid grid-cols-1 gap-4">
      <PageHeader
        className="pb-0"
        icon={<Settings2 className="size-5 text-muted-foreground" />}
        title="System"
        description={
          info.data
            ? `MCPanel ${info.data.version} · node “${info.data.node.name}”. Host health, nodes, panel-wide defaults and notifications.`
            : "Host health, nodes, panel-wide defaults and notifications."
        }
        actions={
          <Button
            variant="outline"
            size="sm"
            disabled={refreshing}
            onClick={() => {
              qc.invalidateQueries({ queryKey: qk.system, exact: true });
              qc.invalidateQueries({ queryKey: portsKey });
            }}
          >
            <RefreshCw className={cn(refreshing && "animate-spin")} /> Refresh
          </Button>
        }
      />

      {info.isLoading ? (
        <LoadingState label="Checking the host…" />
      ) : info.error && !info.data ? (
        <ErrorState error={info.error} title="Couldn't load system information" onRetry={() => info.refetch()} />
      ) : info.data ? (
        <StatusOverview info={info.data} canNodes={canNodes} />
      ) : null}

      {canNodes && <NodesSection />}

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="grid min-w-0 gap-4">
          {canSettings &&
            (settings.isLoading ? (
              <Section title="Panel settings">
                <LoadingState label="Loading settings…" className="py-8" />
              </Section>
            ) : settings.error && !settings.data ? (
              <ErrorState error={settings.error} title="Couldn't load settings" onRetry={() => settings.refetch()} />
            ) : settings.data ? (
              <SettingsForm
                key={JSON.stringify(settings.data.values)}
                settings={settings.data}
                policy={agentPolicy(info.data?.agent)}
              />
            ) : null)}
          {canSettings && settings.data && (games.data ?? []).length > 1 && (
            <GameImagesSection key={JSON.stringify(settings.data.values.runtime_images ?? {})} settings={settings.data} games={games.data ?? []} />
          )}
          {canSettings && <NotificationsSection />}
          <SecurityNotesSection />
        </div>

        <div className="grid min-w-0 gap-4">
          {info.data &&
            (canNodes ? (
              <>
                <HostSection info={info.data} />
                <StorageSection info={info.data} />
                <PolicySection info={info.data} />
              </>
            ) : (
              <AdminCallout tone="info" title="Host details are hidden">
                Docker, storage and agent policy details need the <span className="font-mono">system.nodes</span>{" "}
                permission.
              </AdminCallout>
            ))}
          <PortsSection
            data={ports.data}
            error={ports.error}
            isLoading={ports.isLoading}
            onRetry={() => ports.refetch()}
          />
        </div>
      </div>
    </div>
  );
}

function Tile({
  icon,
  label,
  value,
  sub,
  tone = "default",
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "default" | "good" | "bad" | "warn";
}) {
  const tones = {
    default: "text-muted-foreground bg-muted",
    good: "text-emerald-700 bg-emerald-500/10 dark:text-emerald-300",
    bad: "text-red-700 bg-red-500/10 dark:text-red-300",
    warn: "text-amber-700 bg-amber-500/10 dark:text-amber-300",
  } as const;
  return (
    <div className="flex min-w-0 items-start gap-3 rounded-xl border bg-card p-3">
      <span className={cn("grid size-8 shrink-0 place-items-center rounded-lg [&_svg]:size-4", tones[tone])} aria-hidden>
        {icon}
      </span>
      <div className="min-w-0">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="truncate text-sm font-semibold">{value}</p>
        {sub && <p className="truncate text-xs text-muted-foreground">{sub}</p>}
      </div>
    </div>
  );
}

function StatusOverview({ info, canNodes }: { info: SystemInfo; canNodes: boolean }) {
  const agent = info.agent;
  const d = agentDocker(agent);
  const restic = resticVersion(agent.restic);
  const resticMissing = canNodes && agent.reachable && !agent.restic;
  return (
    <div className="grid gap-3">
      {!agent.reachable && (
        <AdminCallout tone="danger" icon={<ServerOff />} title="The host agent is unreachable">
          <p>
            Servers can't be started, stopped or backed up until the panel can reach mcagent.
            {agent.error && (
              <>
                {" "}
                Error: <span className="font-mono text-xs">{agent.error}</span>
              </>
            )}
          </p>
          <p className="mt-1">Check that the agent container is running and that its token matches the panel's.</p>
        </AdminCallout>
      )}
      {resticMissing && (
        <AdminCallout tone="warning" icon={<TriangleAlert />} title="restic is not installed in the agent">
          Backups and restores won't work until the restic binary is available to the agent container.
        </AdminCallout>
      )}
      <div className={cn("grid grid-cols-1 gap-3 min-[420px]:grid-cols-2", canNodes ? "lg:grid-cols-4" : "lg:grid-cols-3")}>
        <Tile
          icon={agent.reachable ? <Activity /> : <ServerOff />}
          tone={agent.reachable ? "good" : "bad"}
          label="Host agent"
          value={agent.reachable ? "Connected" : "Unreachable"}
          sub={agent.version ? `mcagent ${agent.version}` : `Node ${info.node.name}`}
        />
        <Tile
          icon={<Server />}
          label="Servers"
          value={`${info.counts.running} running`}
          sub={`${info.counts.instances} total (excluding archived)`}
        />
        <Tile
          icon={<Cpu />}
          label="Host capacity"
          value={d.cpus ? `${d.cpus} CPU${d.cpus === 1 ? "" : "s"}` : "—"}
          sub={d.memory_bytes ? `${formatBytes(d.memory_bytes)} memory` : undefined}
        />
        {canNodes && (
          <Tile
            icon={<Archive />}
            tone={!agent.reachable ? "default" : restic ? "good" : "warn"}
            label="Backup engine"
            value={!agent.reachable ? "Unknown" : restic ? `restic ${restic}` : "Not installed"}
            sub={d.server_version ? `Docker ${d.server_version}` : undefined}
          />
        )}
      </div>
    </div>
  );
}
