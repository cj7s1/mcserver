/** Instance networking: how players connect, game port, bind address, extra ports and Docker network. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Loader2, Lock, Network as NetworkIcon, Save, Undo2 } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useAuth } from "@/hooks/use-auth";
import { api, ApiError, errorMessage } from "@/lib/api";
import { customNetworkAllowed, gameLabel, isMinecraft } from "@/lib/games";
import { qk, useInvalidateInstance, useSystemInfo } from "@/lib/queries";
import type { Instance, PortMapping } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useInstanceContext } from "./InstanceLayout";
import { usePorts } from "./backups/backup-utils";
import { useGameInfo } from "./game-api";
import { AddressList, ConnectionFlow } from "./network/ConnectionPanel";
import { ExtraPortsEditor } from "./network/ExtraPortsEditor";
import { GameNetwork } from "./network/GameNetwork";
import { NetworkExposureNotice } from "./network/NetworkExposureNotice";
import { MAX_EXTRA_PORTS, type PortPreset, type PortRow } from "./network/port-presets";

type BindMode = "all" | "localhost" | "custom";

interface Draft {
  game_port: string;
  bindMode: BindMode;
  customIp: string;
  rows: PortRow[];
  network: string;
}

interface Normalized {
  game_port: number;
  host_ip: string;
  extra_ports: PortMapping[];
  network: string;
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const NETWORK_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;

function isIp(v: string): boolean {
  return IPV4.test(v) || (v.includes(":") && /^[0-9a-fA-F:.]+$/.test(v) && v.length <= 45);
}

function fromInstance(i: Instance): Draft {
  const bindMode: BindMode = i.host_ip === "0.0.0.0" ? "all" : i.host_ip === "127.0.0.1" ? "localhost" : "custom";
  return {
    game_port: String(i.game_port),
    bindMode,
    customIp: bindMode === "custom" ? i.host_ip : "",
    rows: (i.extra_ports ?? []).map((p, idx) => ({
      key: `p${idx}`,
      label: p.label ?? "",
      container_port: String(p.container_port),
      host_port: String(p.host_port),
      protocol: p.protocol,
      host_ip: p.host_ip ?? "",
    })),
    network: i.network ?? "",
  };
}

function normalizePorts(list: { container_port: number | string; host_port: number | string; protocol: string; host_ip?: string; label?: string }[]): PortMapping[] {
  return list.map((p) => ({
    container_port: Number(p.container_port),
    host_port: Number(p.host_port),
    protocol: p.protocol === "udp" ? "udp" : "tcp",
    host_ip: (p.host_ip ?? "").trim(),
    label: (p.label ?? "").trim(),
  }));
}

function normalize(d: Draft): Normalized {
  return {
    game_port: Number(d.game_port),
    host_ip: d.bindMode === "all" ? "0.0.0.0" : d.bindMode === "localhost" ? "127.0.0.1" : d.customIp.trim(),
    extra_ports: normalizePorts(d.rows),
    network: d.network.trim(),
  };
}

type RowErrors = Partial<Record<"container_port" | "host_port" | "label" | "host_ip" | "row", string>>;

function validate(d: Draft, others: Map<number, string>) {
  const n = normalize(d);
  const e: { game_port?: string; host_ip?: string; network?: string; rows: Record<string, RowErrors> } = { rows: {} };
  if (!d.game_port || !Number.isInteger(n.game_port) || n.game_port < 1024 || n.game_port > 65535)
    e.game_port = "Use a port between 1024 and 65535";
  else if (others.has(n.game_port)) e.game_port = `Port ${n.game_port} is already used by ${others.get(n.game_port)}`;
  if (d.bindMode === "custom" && !isIp(n.host_ip)) e.host_ip = "Enter an IPv4 or IPv6 address of this host";
  if (n.network && !NETWORK_NAME.test(n.network)) e.network = "Letters, digits, _ . - only; must start with a letter or digit";

  const seen = new Map<string, string>();
  seen.set(`${n.game_port}/tcp`, "the game port");
  d.rows.forEach((r) => {
    const re: RowErrors = {};
    const cp = Number(r.container_port);
    const hp = Number(r.host_port);
    if (!r.container_port || !Number.isInteger(cp) || cp < 1 || cp > 65535) re.container_port = "1–65535";
    else if (cp === 25575) re.container_port = "RCON cannot be published";
    else if (cp === 25565 && r.protocol === "tcp") re.container_port = "Game port is set above";
    if (!r.host_port || !Number.isInteger(hp) || hp < 1024 || hp > 65535) re.host_port = "1024–65535";
    else if (others.has(hp)) re.host_port = `Used by ${others.get(hp)}`;
    else {
      const k = `${hp}/${r.protocol}`;
      if (seen.has(k)) re.host_port = `Also used by ${seen.get(k)}`;
      else seen.set(k, r.label ? `“${r.label}”` : "another mapping");
    }
    if (r.host_ip && !isIp(r.host_ip)) re.host_ip = "Invalid IP";
    if (Object.keys(re).length) e.rows[r.key] = re;
  });
  if (d.rows.length > MAX_EXTRA_PORTS) e.rows[d.rows[MAX_EXTRA_PORTS].key] = { row: `At most ${MAX_EXTRA_PORTS} extra ports` };
  const count = (e.game_port ? 1 : 0) + (e.host_ip ? 1 : 0) + (e.network ? 1 : 0) + Object.keys(e.rows).length;
  return { errors: e, valid: count === 0, normalized: n };
}

function Advanced({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="rounded-xl border bg-card">
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-xl px-4 py-3 text-left text-sm font-semibold hover:bg-muted/40 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", open && "rotate-180")} aria-hidden />
        {title}
        {hint && <span className="truncate text-xs font-normal text-muted-foreground">{hint}</span>}
      </button>
      {open && (
        <div id={id} className="grid gap-4 border-t p-4">
          {children}
        </div>
      )}
    </div>
  );
}

export default function Network() {
  const { instance, can } = useInstanceContext();
  // other games: named ports with their protocols (network/GameNetwork.tsx); Minecraft: as before
  if (!isMinecraft(instance)) return <GameNetwork instance={instance} canEdit={can("instance.configure") && !instance.archived_at} />;
  return <MinecraftNetwork />;
}

function MinecraftNetwork() {
  const { instance, can } = useInstanceContext();
  const auth = useAuth();
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const canEdit = can("instance.configure") && !instance.archived_at;
  const ports = usePorts(instance.node); // the ports of the node this server runs on
  const system = useSystemInfo();
  // a game that runs on the panel's own network only (R-8): no network field, unless one is set (to clear it)
  const networkAllowed = customNetworkAllowed(instance, useGameInfo(instance));
  const [draft, setDraft] = useState<Draft | null>(null);
  const [serverErr, setServerErr] = useState<{ port?: number; message: string } | null>(null);
  const current = draft ?? fromInstance(instance);

  // host ports used by *other* servers / containers (the list also contains this server's own ports)
  const own = new Set<number>([instance.game_port, ...(instance.extra_ports ?? []).map((p) => p.host_port)]);
  // this server's own running container (agent names it mcpanel-<first 12 hex of the id>) may still hold old ports
  const ownContainer = `'mcpanel-${instance.id.replace(/-/g, "").slice(0, 12)}'`;
  const isOwn = (p: { port: number; used_by: string }) => own.has(p.port) || p.used_by.includes(ownContainer);
  const others = new Map<number, string>();
  for (const p of ports.data?.in_use ?? []) if (!isOwn(p)) others.set(p.port, p.used_by);

  const { errors, valid, normalized } = validate(current, others);
  const saved = normalize(fromInstance(instance));
  const changed: Partial<Normalized> = {};
  if (normalized.game_port !== saved.game_port) changed.game_port = normalized.game_port;
  if (normalized.host_ip !== saved.host_ip) changed.host_ip = normalized.host_ip;
  if (JSON.stringify(normalized.extra_ports) !== JSON.stringify(saved.extra_ports)) changed.extra_ports = normalized.extra_ports;
  if (normalized.network !== saved.network) changed.network = normalized.network;
  const changeCount = Object.keys(changed).length;

  const save = useMutation({
    mutationFn: () => api.patch<Instance>(`/instances/${instance.id}`, changed),
    onSuccess: (data) => {
      qc.setQueryData(qk.instance(instance.id), data);
      invalidate(instance.id);
      qc.invalidateQueries({ queryKey: ["system", "ports"] });
      setDraft(null);
      setServerErr(null);
      toast.success(
        data.restart_required ? "Network settings saved. Restart the server to apply them." : "Network settings saved",
      );
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "port_conflict") {
        setServerErr({ port: Number(err.details?.port) || undefined, message: err.message });
        qc.invalidateQueries({ queryKey: ["system", "ports"] });
      } else if (!(err instanceof ApiError && err.code === "validation_error")) toast.error(errorMessage(err));
    },
  });
  const fe = save.error instanceof ApiError && save.error.code === "validation_error" ? save.error.fieldErrors : {};

  const edit = (patch: Partial<Draft>) => {
    setDraft({ ...current, ...patch });
    setServerErr(null);
    if (save.error) save.reset();
  };
  const nextFree = (start: number, protocol: string): number | null => {
    const taken = new Set<string>([`${normalized.game_port}/tcp`, ...current.rows.map((r) => `${r.host_port}/${r.protocol}`)]);
    for (let p = Math.max(1024, start); p <= Math.min(65535, start + 200); p++)
      if (!others.has(p) && !taken.has(`${p}/${protocol}`)) return p;
    return null;
  };
  const addRow = (preset?: PortPreset) => {
    const protocol = preset?.protocol ?? "tcp";
    const host = preset
      ? preset.sameAsGame && Number.isInteger(normalized.game_port)
        ? normalized.game_port
        : nextFree(preset.container_port, protocol)
      : null;
    edit({
      rows: [
        ...current.rows,
        {
          key: `n${Date.now()}`,
          label: preset?.label ?? "",
          container_port: preset ? String(preset.container_port) : "",
          host_port: host ? String(host) : "",
          protocol,
          host_ip: "",
        },
      ],
    });
  };

  const gamePortError =
    errors.game_port ?? fe.game_port ?? (serverErr && (!serverErr.port || serverErr.port === normalized.game_port) ? serverErr.message : undefined);
  const rowErrors = { ...errors.rows };
  if (serverErr?.port && serverErr.port !== normalized.game_port) {
    const row = current.rows.find((r) => Number(r.host_port) === serverErr.port);
    if (row) rowErrors[row.key] = { ...rowErrors[row.key], host_port: serverErr.message };
  }
  const allowedNetworks = (system.data?.agent.policy?.networks as string[] | undefined) ?? null;
  const disabled = !canEdit || save.isPending;
  const running = ["running", "starting", "unhealthy"].includes(instance.status);

  return (
    <div className="grid min-w-0 gap-4">
      <NetworkExposureNotice instance={instance} />
      <Section
        title="How players connect"
        description="Three layers sit between a player and the server. The panel manages the last two."
      >
        <div className="grid gap-5">
          <ConnectionFlow
            hostPort={instance.connection.host_port}
            bind={instance.connection.bind_address}
            extraPorts={instance.extra_ports ?? []}
            network={instance.network}
            agentNetworks={instance.agent_networks}
          />
          <div className="grid content-start gap-2">
            <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Addresses</h3>
            <AddressList connection={instance.connection} canConfigurePanel={auth.can("system.settings")} />
          </div>
        </div>
      </Section>

      {!canEdit && (
        <p className="flex items-center gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <Lock className="size-3.5 shrink-0" aria-hidden />
          {instance.archived_at
            ? "This server is archived. Unarchive it in Settings to change networking."
            : "Read-only: you need the “Configure server” permission to change networking."}
        </p>
      )}

      <Section title="Game port & bind address" description={`The host side of the Docker port mapping for ${gameLabel(instance)} (TCP).`}>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="grid content-start gap-1.5">
            <Label htmlFor="net-game-port">Host port</Label>
            <div className="flex gap-2">
              <Input
                id="net-game-port"
                inputMode="numeric"
                value={current.game_port}
                disabled={disabled}
                onChange={(e) => edit({ game_port: e.target.value.replace(/[^\d]/g, "") })}
                aria-invalid={!!gamePortError || undefined}
                aria-describedby="net-game-port-help"
                className="max-w-36 font-mono tabular-nums"
              />
              {canEdit && gamePortError && ports.data?.suggested && ports.data.suggested !== normalized.game_port && (
                <Button type="button" variant="outline" size="sm" className="h-8" onClick={() => edit({ game_port: String(ports.data!.suggested) })}>
                  Use {ports.data.suggested}
                </Button>
              )}
            </div>
            <p id="net-game-port-help" className={cn("text-xs", gamePortError ? "text-destructive" : "text-muted-foreground")}>
              {gamePortError ??
                (normalized.game_port === 25565
                  ? "25565 is Minecraft's default: players can omit the port."
                  : `Players add “:${current.game_port}” to the address. ${ports.data ? `Auto-assigned range: ${ports.data.range[0]}–${ports.data.range[1]}.` : ""}`)}
            </p>
          </div>

          <div className="grid content-start gap-1.5">
            <Label htmlFor="net-bind">Bind address</Label>
            <Select value={current.bindMode} onValueChange={(v) => edit({ bindMode: v as BindMode })} disabled={disabled}>
              <SelectTrigger id="net-bind" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All interfaces (0.0.0.0)</SelectItem>
                <SelectItem value="localhost">This host only (127.0.0.1)</SelectItem>
                <SelectItem value="custom">A specific IP address…</SelectItem>
              </SelectContent>
            </Select>
            {current.bindMode === "custom" && (
              <>
                <Label htmlFor="net-bind-ip" className="sr-only">
                  Bind IP address
                </Label>
                <Input
                  id="net-bind-ip"
                  value={current.customIp}
                  placeholder="e.g. 192.168.1.20"
                  disabled={disabled}
                  onChange={(e) => edit({ customIp: e.target.value.trim() })}
                  aria-invalid={!!(errors.host_ip ?? fe.host_ip) || undefined}
                  className="font-mono"
                />
              </>
            )}
            <p className={cn("text-xs", errors.host_ip || fe.host_ip ? "text-destructive" : "text-muted-foreground")}>
              {errors.host_ip ??
                fe.host_ip ??
                (current.bindMode === "all"
                  ? "Reachable on every network interface of the host (LAN, VPN, …)."
                  : current.bindMode === "localhost"
                    ? "Only programs on the host can connect, e.g. a proxy like Velocity or a tunnel such as playit.gg."
                    : "Only reachable through this address. It must belong to the host, or the server will fail to start.")}
            </p>
          </div>
        </div>

        <details className="group mt-4 rounded-lg border">
          <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium hover:bg-muted/40 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none">
            <ChevronDown className="size-3.5 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden />
            Host ports already in use
            <span className="font-normal text-muted-foreground">
              {ports.isLoading ? "loading…" : ports.data ? `${ports.data.in_use.length} ports` : "unavailable"}
            </span>
          </summary>
          <div className="border-t px-3 py-2">
            {ports.error ? (
              <p className="text-xs text-destructive">{errorMessage(ports.error)}</p>
            ) : (
              <ul className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3">
                {(ports.data?.in_use ?? []).map((p) => (
                  <li key={p.port} className="flex min-w-0 gap-2">
                    <span className="font-mono tabular-nums">{p.port}</span>
                    <span className={cn("truncate", isOwn(p) ? "text-primary" : "text-muted-foreground")} title={p.used_by}>
                      {isOwn(p) ? "this server" : p.used_by}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </details>
      </Section>

      <Section
        title="Extra ports"
        description="Publish additional container ports for plugins and mods. Each needs a free host port."
      >
        <div className="grid gap-3">
          {current.rows.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No extra ports. Common ones: query (UDP, same number as the game port), Simple Voice Chat (UDP 24454), BlueMap (TCP
              8100) and Dynmap (TCP 8123).
            </p>
          )}
          <ExtraPortsEditor rows={current.rows} errors={rowErrors} disabled={disabled} onChange={(rows) => edit({ rows })} onAdd={addRow} />
          {fe.extra_ports && <p className="text-xs text-destructive">{fe.extra_ports}</p>}
          <p className="text-xs text-muted-foreground">
            A published port only helps if the plugin listens on it inside the container. For query, enable{" "}
            <code className="font-mono">enable-query</code> in{" "}
            <Link to={`/servers/${instance.id}/settings`} className="font-medium text-primary hover:underline">
              Settings → Properties
            </Link>
            . RCON (25575) can never be published.
          </p>
        </div>
      </Section>

      {(networkAllowed || saved.network) && (
        <Advanced title="Advanced" hint="Docker network">
          <div className="grid max-w-xl gap-1.5">
            <Label htmlFor="net-docker-network" className="flex items-center gap-1.5">
              <NetworkIcon className="size-3.5 text-muted-foreground" aria-hidden /> Docker network
            </Label>
            <Input
              id="net-docker-network"
              value={current.network}
              placeholder="Default instance network"
              list={allowedNetworks ? "net-allowed-networks" : undefined}
              disabled={disabled}
              onChange={(e) => edit({ network: e.target.value })}
              aria-invalid={!!(errors.network ?? fe.network) || undefined}
              className="font-mono"
              autoComplete="off"
              spellCheck={false}
            />
            {allowedNetworks && (
              <datalist id="net-allowed-networks">
                {allowedNetworks.map((n) => (
                  <option key={n} value={n} />
                ))}
              </datalist>
            )}
            <p className={cn("text-xs", errors.network || fe.network ? "text-destructive" : "text-muted-foreground")}>
              {errors.network ??
                fe.network ??
                (networkAllowed
                  ? "Leave empty for the agent's default network. A custom network (for example one shared with a Velocity proxy) must be allowed by the host agent's policy (AGENT_ALLOWED_NETWORKS), otherwise the server fails to start."
                  : `${gameLabel(instance)} servers run on the panel's own network only: clear the field to move this one back.`)}
            </p>
            {allowedNetworks && (
              <p className="text-xs text-muted-foreground">
                Allowed by the agent: {allowedNetworks.map((n) => <code key={n} className="mr-1.5 font-mono">{n}</code>)}
              </p>
            )}
            {normalized.network !== saved.network && <NetworkExposureNotice instance={instance} network={normalized.network} />}
            {normalized.network === "host" && (
              <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-800 dark:text-amber-200">
                Host networking skips Docker port mapping: the server listens on the host's port 25565 directly, and the host port
                and extra ports above are ignored.
              </p>
            )}
          </div>
        </Advanced>
      )}

      {canEdit && changeCount > 0 && (
        <div
          className="sticky bottom-3 z-20 flex flex-wrap items-center gap-2 rounded-xl border bg-card/95 px-4 py-2.5 shadow-lg backdrop-blur"
          role="region"
          aria-label="Unsaved network changes"
        >
          <span className="text-sm font-medium">
            {changeCount} unsaved change{changeCount === 1 ? "" : "s"}
          </span>
          <span className="text-xs text-muted-foreground">
            {running ? "Applies after a restart (the container is recreated)." : "Applies the next time the server starts."}
          </span>
          <div className="ml-auto flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={save.isPending}
              onClick={() => {
                setDraft(null);
                setServerErr(null);
                save.reset();
              }}
            >
              <Undo2 /> Discard
            </Button>
            <Button size="sm" disabled={!valid || save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save changes
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
