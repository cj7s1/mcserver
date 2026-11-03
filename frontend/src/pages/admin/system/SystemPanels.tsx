import {
  Camera,
  CameraOff,
  Container,
  Globe,
  HardDrive,
  Lock,
  Network,
  Package,
  ServerCog,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";
import type { ReactNode } from "react";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatBytes, formatMemoryMb } from "@/lib/format";
import type { SystemInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AdminCallout } from "../admin-shared";
import { agentDocker, agentPolicy, agentStorage, type PortsInfo, type StorageVolume } from "./system-types";

type InfoRow = { label: string; value: ReactNode };

function InfoList({ rows }: { rows: InfoRow[] }) {
  return (
    <dl className="grid grid-cols-[minmax(7rem,auto)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
      {rows.map((r) => (
        <div key={r.label} className="contents">
          <dt className="text-muted-foreground">{r.label}</dt>
          <dd className="min-w-0 break-words">{r.value ?? <span className="text-muted-foreground">—</span>}</dd>
        </div>
      ))}
    </dl>
  );
}

export function HostSection({ info }: { info: SystemInfo }) {
  const agent = info.agent;
  const d = agentDocker(agent);
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <Container className="size-4 text-muted-foreground" aria-hidden /> Docker host
        </span>
      }
      description={`Node “${info.node.name}”${agent.version ? ` · mcagent ${agent.version}` : ""}`}
    >
      {!agent.reachable ? (
        <p className="text-sm text-muted-foreground">Host details are unavailable while the agent can't be reached.</p>
      ) : (
        <InfoList
          rows={[
            {
              label: "Docker Engine",
              value: d.server_version ? (
                <>
                  {d.server_version}
                  {d.api_version && <span className="text-muted-foreground"> · API {d.api_version}</span>}
                </>
              ) : null,
            },
            { label: "Operating system", value: d.os },
            { label: "Kernel", value: d.kernel ? <span className="font-mono text-xs">{d.kernel}</span> : null },
            { label: "Architecture", value: d.architecture },
            { label: "CPUs", value: d.cpus },
            { label: "Memory", value: d.memory_bytes ? formatBytes(d.memory_bytes) : null },
            { label: "Storage driver", value: d.storage_driver },
            { label: "Cgroup version", value: d.cgroup_version ? `v${d.cgroup_version}` : null },
            { label: "Running containers", value: d.containers_running },
            { label: "Host name", value: d.name ? <span className="font-mono text-xs">{d.name}</span> : null },
          ]}
        />
      )}
    </Section>
  );
}

const VOLUME_LABELS: Record<string, string> = { instances: "Server data", backups: "Backups" };

function UsageBar({ volume }: { volume: StorageVolume }) {
  // df-style: used / (used + available) so reserved blocks don't hide a nearly full disk
  const denom = volume.used_bytes + volume.free_bytes;
  const pct = denom > 0 ? Math.min(100, (volume.used_bytes / denom) * 100) : 0;
  const tone = pct >= 90 ? "bg-red-500" : pct >= 80 ? "bg-amber-500" : "bg-primary";
  return (
    <div className="grid gap-1">
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
        aria-label="Disk usage"
      >
        <div className={cn("h-full rounded-full transition-all", tone)} style={{ width: `${pct}%` }} />
      </div>
      <div className="flex flex-wrap justify-between gap-x-3 text-xs text-muted-foreground tabular-nums">
        <span>
          <span className={cn("font-medium", pct >= 90 ? "text-destructive" : "text-foreground")}>{Math.round(pct)}%</span>{" "}
          used · {formatBytes(volume.used_bytes)}
        </span>
        <span>
          {formatBytes(volume.free_bytes)} free · {formatBytes(volume.total_bytes)} volume
        </span>
      </div>
    </div>
  );
}

export function StorageSection({ info }: { info: SystemInfo }) {
  const storage = agentStorage(info.agent);
  const entries = Object.entries(storage);
  const inst = storage.instances;
  const bak = storage.backups;
  const sameDisk =
    inst && bak && (inst.source ?? inst.mount_point) && (inst.source ?? inst.mount_point) === (bak.source ?? bak.mount_point);
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <HardDrive className="size-4 text-muted-foreground" aria-hidden /> Storage
        </span>
      }
      description="Where the agent keeps server files and restic repositories."
    >
      {!info.agent.reachable ? (
        <p className="text-sm text-muted-foreground">Storage details are unavailable while the agent can't be reached.</p>
      ) : entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">The agent reported no storage paths.</p>
      ) : (
        <div className="grid gap-4">
          {entries.map(([key, v]) => (
            <div key={key} className="grid gap-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium">{VOLUME_LABELS[key] ?? key}</p>
                  <p className="truncate font-mono text-xs text-muted-foreground" title={v.path}>
                    {v.path}
                  </p>
                </div>
                <div className="flex flex-wrap gap-1">
                  <Badge variant="outline" className="font-mono">
                    {v.fstype}
                  </Badge>
                  {v.mount_point && (
                    <Badge variant="outline" className="font-mono" title={v.source ? `Device ${v.source}` : undefined}>
                      on {v.mount_point}
                    </Badge>
                  )}
                </div>
              </div>
              <UsageBar volume={v} />
              {v.snapshot.available ? (
                <p className="inline-flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-300">
                  <Camera className="size-3.5" aria-hidden /> {v.snapshot.provider} snapshots available
                </p>
              ) : (
                <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
                  <CameraOff className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <span>
                    No filesystem snapshots{v.snapshot.provider ? ` (${v.snapshot.provider})` : ""}
                    {v.snapshot.reason ? `: ${v.snapshot.reason}` : ""}. Backups still work through restic.
                  </span>
                </p>
              )}
            </div>
          ))}
          {sameDisk && (
            <AdminCallout tone="warning" icon={<TriangleAlert />} title="Backups share a disk with server data">
              A single disk failure would lose both. Point <span className="font-mono">MCPANEL_BACKUP_DIR</span> at another
              disk, or copy the restic repositories off-site.
            </AdminCallout>
          )}
        </div>
      )}
    </Section>
  );
}

function Chips({ items }: { items: string[] | undefined }) {
  if (!items?.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((i) => (
        <Badge key={i} variant="secondary" className="h-auto max-w-full font-mono break-all whitespace-normal">
          {i}
        </Badge>
      ))}
    </div>
  );
}

export function PolicySection({ info }: { info: SystemInfo }) {
  const p = agentPolicy(info.agent);
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <Lock className="size-4 text-muted-foreground" aria-hidden /> Agent policy
        </span>
      }
      description="Hard limits enforced by the agent, set through its environment. The panel can't override them."
    >
      {!p ? (
        <p className="text-sm text-muted-foreground">The agent did not report a policy.</p>
      ) : (
        <InfoList
          rows={[
            { label: "Allowed images", value: <Chips items={p.allowed_images} /> },
            { label: "Networks", value: <Chips items={p.networks} /> },
            {
              label: "Host ports",
              value:
                p.port_min !== undefined && p.port_max !== undefined ? (
                  <span className="tabular-nums">
                    {p.port_min}–{p.port_max}
                  </span>
                ) : null,
            },
            { label: "Max memory / server", value: p.max_memory_mb ? formatMemoryMb(p.max_memory_mb) : null },
            {
              label: "Runtime UID:GID",
              value:
                p.runtime_uid !== undefined ? (
                  <span>
                    <span className="font-mono">
                      {p.runtime_uid}:{p.runtime_gid}
                    </span>
                    <span className="block text-xs text-muted-foreground">Owner of server files on the host</span>
                  </span>
                ) : null,
            },
          ]}
        />
      )}
    </Section>
  );
}

export function PortsSection({
  data,
  error,
  isLoading,
  onRetry,
}: {
  data: PortsInfo | undefined;
  error: unknown;
  isLoading: boolean;
  onRetry: () => void;
}) {
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <Network className="size-4 text-muted-foreground" aria-hidden /> Host ports in use
        </span>
      }
      description={
        data ? (
          <>
            Allocation range <span className="tabular-nums">{data.range[0]}–{data.range[1]}</span>
            {data.suggested ? (
              <>
                {" "}
                · next free <span className="font-mono">{data.suggested}</span>
              </>
            ) : (
              " · no free ports left in the range"
            )}
          </>
        ) : (
          "Ports taken by servers and other Docker containers."
        )
      }
    >
      {isLoading ? (
        <LoadingState className="py-6" label="Loading ports…" />
      ) : error && !data ? (
        <ErrorState error={error} title="Couldn't load ports" onRetry={onRetry} className="py-6" />
      ) : !data || data.in_use.length === 0 ? (
        <EmptyState title="No ports in use" description="New servers get the first free port in the range." className="py-6" />
      ) : (
        <div className="max-h-80 overflow-y-auto">
          <Table className="text-[13px]">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-24 text-xs text-muted-foreground">Port</TableHead>
                <TableHead className="text-xs text-muted-foreground">Used by</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.in_use.map((p) => {
                const outside = p.port < data.range[0] || p.port > data.range[1];
                return (
                  <TableRow key={p.port}>
                    <TableCell className="py-1.5 font-mono tabular-nums">{p.port}</TableCell>
                    <TableCell className="py-1.5 whitespace-normal">
                      <span className="mr-2">{p.used_by}</span>
                      {outside && (
                        <Badge variant="outline" className="h-4 px-1.5 text-[10px]" title="Outside the allocation range">
                          outside range
                        </Badge>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </Section>
  );
}

export function SecurityNotesSection() {
  const notes: { icon: ReactNode; title: string; body: ReactNode }[] = [
    {
      icon: <ServerCog />,
      title: "Keep the panel on your LAN or localhost",
      body: (
        <>
          Bind it to <span className="font-mono">127.0.0.1</span> or a LAN address (
          <span className="font-mono">MCPANEL_BIND</span>) and never port-forward the panel port. Only game ports need
          forwarding for public servers.
        </>
      ),
    },
    {
      icon: <Globe />,
      title: "Use HTTPS for remote access",
      body: (
        <>
          The panel speaks plain HTTP. Put an HTTPS reverse proxy (Caddy, Traefik, Nginx Proxy Manager) in front and set{" "}
          <span className="font-mono">MCPANEL_BEHIND_TLS_PROXY</span> and{" "}
          <span className="font-mono">MCPANEL_SECURE_COOKIES</span>, or better, reach it through a VPN such as WireGuard
          or Tailscale.
        </>
      ),
    },
    {
      icon: <Package />,
      title: "Mods and plugins are untrusted code",
      body: "They run with full access to the server's files and network. Install only from sources you trust, and give content permissions only to people you trust.",
    },
    {
      icon: <Container />,
      title: "Docker is not a strong sandbox",
      body: "Containers share the host kernel. Keep Docker and the kernel updated, don't store sensitive data on the same host, and remember that anyone who controls the agent controls Docker.",
    },
  ];
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <ShieldAlert className="size-4 text-muted-foreground" aria-hidden /> Security notes
        </span>
      }
    >
      <ul className="grid gap-3 sm:grid-cols-2">
        {notes.map((n) => (
          <li key={n.title} className="flex gap-3">
            <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground [&_svg]:size-4" aria-hidden>
              {n.icon}
            </span>
            <div className="min-w-0">
              <p className="text-sm font-medium">{n.title}</p>
              <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{n.body}</p>
            </div>
          </li>
        ))}
      </ul>
    </Section>
  );
}
