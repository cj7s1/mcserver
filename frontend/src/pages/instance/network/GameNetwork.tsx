/**
 * Network page of a non-Minecraft server: how players connect on each named port (with its
 * protocol), the host port of each named port and the bind address (PATCH /instances/{id} with
 * `ports` [{name, host_port}] and `host_ip`, backend contract B4). The container ports are fixed by
 * the game; the panel checks host ports per protocol (TCP 7777 and UDP 7777 are different ports).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Save, Undo2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError, errorMessage } from "@/lib/api";
import { gameLabel } from "@/lib/games";
import { qk, useInvalidateInstance } from "@/lib/queries";
import type { ConnectionPort, Instance } from "@/lib/types";
import { usePorts } from "../backups/backup-utils";
import { CopyButton } from "./ConnectionPanel";
import { portsPatch, rowsFromPorts, validateNamedPorts, type NamedPortRow } from "./named-ports";

export function NamedPortsEditor({
  rows,
  onChange,
  errors = {},
  warnings = {},
  disabled,
}: {
  rows: readonly NamedPortRow[];
  onChange: (name: string, hostPort: string) => void;
  errors?: Record<string, string>;
  warnings?: Record<string, string>;
  disabled?: boolean;
}) {
  return (
    <ul className="grid gap-3" aria-label="Ports">
      {rows.map((r) => {
        const id = `named-port-${r.name}`;
        return (
          <li key={r.name} className="grid gap-1.5 sm:grid-cols-[minmax(0,1fr)_10rem] sm:items-end">
            <div className="grid gap-0.5">
              <Label htmlFor={id} className="flex flex-wrap items-center gap-2">
                {r.label}
                <Badge variant="outline" className="font-mono uppercase">
                  {r.protocol}
                </Badge>
              </Label>
              <p className="text-xs text-muted-foreground">
                Host port → container port <span className="font-mono">{r.container_port}</span>/{r.protocol}
              </p>
            </div>
            <Input
              id={id}
              inputMode="numeric"
              value={r.host_port}
              onChange={(e) => onChange(r.name, e.target.value)}
              disabled={disabled}
              aria-invalid={!!errors[r.name] || undefined}
              aria-label={`${r.label} host port (${r.protocol.toUpperCase()})`}
            />
            {errors[r.name] ? (
              <p className="text-xs text-destructive sm:col-span-2" role="alert">
                {errors[r.name]}
              </p>
            ) : warnings[r.name] ? (
              <p className="text-xs text-amber-700 sm:col-span-2 dark:text-amber-300">{warnings[r.name]}</p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function PortAddresses({ port }: { port: ConnectionPort }) {
  return (
    <li className="grid gap-1.5 rounded-lg border p-3">
      <p className="flex items-center gap-2 text-sm font-medium">
        {port.label}
        <Badge variant="outline" className="font-mono uppercase">
          {port.protocol}
        </Badge>
        <span className="ml-auto font-mono text-xs text-muted-foreground">
          host {port.host_port} → {port.container_port}
        </span>
      </p>
      {port.addresses.length ? (
        <ul className="grid gap-1">
          {port.addresses.map((a) => (
            <li key={`${a.kind}-${a.address}`} className="flex items-center gap-2 text-sm">
              <span className="w-20 shrink-0 text-xs text-muted-foreground">{a.label}</span>
              <span className="min-w-0 truncate font-mono">{a.address}</span>
              <CopyButton value={a.address} label={`${port.label} ${a.label} address`} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">
          No LAN or public address is set: players use this machine&apos;s IP and port {port.host_port} ({port.protocol.toUpperCase()}).
        </p>
      )}
    </li>
  );
}

/** Connection info per named port (from instance.connection.ports, else from instance.ports). */
export function ConnectionPorts({ instance }: { instance: Instance }) {
  const ports: ConnectionPort[] =
    instance.connection.ports ??
    (instance.ports ?? []).map((p) => ({
      name: p.name,
      label: p.label || p.name,
      protocol: p.protocol,
      container_port: p.container_port,
      host_port: p.host_port,
      addresses: [],
    }));
  return (
    <div className="grid gap-3">
      <ul className="grid gap-2">
        {ports.map((p) => (
          <PortAddresses key={p.name} port={p} />
        ))}
      </ul>
      {instance.connection.notes.length > 0 && (
        <ul className="grid list-disc gap-0.5 pl-5 text-xs text-muted-foreground">
          {instance.connection.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

const IP_RE = /^(\d{1,3}(\.\d{1,3}){3}|[0-9a-fA-F:.]{2,45})$/;

export function GameNetwork({ instance, canEdit }: { instance: Instance; canEdit: boolean }) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const saved = rowsFromPorts(instance.ports ?? []);
  const [rows, setRows] = useState<NamedPortRow[] | null>(null);
  const [hostIp, setHostIp] = useState<string | null>(null);
  const current = rows ?? saved;
  const bind = hostIp ?? instance.host_ip;
  const errors = validateNamedPorts(current);
  if (bind.trim() && !IP_RE.test(bind.trim())) errors.__bind = "Enter an IPv4 or IPv6 address (0.0.0.0 = all interfaces)";
  const nodePorts = usePorts(instance.node);
  const own = new Set(saved.map((r) => Number(r.host_port)));
  const warnings: Record<string, string> = {};
  for (const r of current) {
    const n = Number(r.host_port);
    if (own.has(n)) continue;
    const used = nodePorts.data?.in_use.find((p) => p.port === n);
    if (used) warnings[r.name] = `Port ${n} is in use by ${used.used_by} (fine if that is the other protocol).`;
  }
  const portsChanged = JSON.stringify(portsPatch(current)) !== JSON.stringify(portsPatch(saved));
  const bindChanged = bind.trim() !== instance.host_ip;
  const dirty = portsChanged || bindChanged;
  const save = useMutation({
    mutationFn: () =>
      api.patch<Instance>(`/instances/${instance.id}`, {
        ...(portsChanged ? { ports: portsPatch(current) } : {}),
        ...(bindChanged ? { host_ip: bind.trim() } : {}),
      }),
    onSuccess: (data) => {
      qc.setQueryData(qk.instance(instance.id), data);
      invalidate(instance.id);
      qc.invalidateQueries({ queryKey: ["system", "ports"] });
      setRows(null);
      setHostIp(null);
      toast.success(data.restart_required ? "Ports saved. Restart the server to apply them." : "Ports saved");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.display : errorMessage(err)),
  });
  const valid = Object.keys(errors).length === 0;
  return (
    <div className="grid min-w-0 gap-4">
      <Section title="How players connect" description={`Addresses of this ${gameLabel(instance)} server, per port`}>
        <ConnectionPorts instance={instance} />
      </Section>
      <Section title="Ports" description="The container ports are fixed by the game; choose the host port players connect to.">
        <div className="grid gap-4">
          <NamedPortsEditor
            rows={current}
            onChange={(name, hostPort) => setRows(current.map((r) => (r.name === name ? { ...r, host_port: hostPort } : r)))}
            errors={errors}
            warnings={warnings}
            disabled={!canEdit || save.isPending}
          />
          <div className="grid gap-1.5 sm:max-w-sm">
            <Label htmlFor="game-bind-ip">Bind address</Label>
            <Input
              id="game-bind-ip"
              value={bind}
              onChange={(e) => setHostIp(e.target.value)}
              disabled={!canEdit || save.isPending}
              aria-invalid={!!errors.__bind || undefined}
            />
            {errors.__bind ? (
              <p className="text-xs text-destructive" role="alert">
                {errors.__bind}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">0.0.0.0 = every interface of the host.</p>
            )}
          </div>
          {dirty && canEdit && (
            <div className="flex justify-end gap-2 border-t pt-3">
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setRows(null);
                  setHostIp(null);
                }}
                disabled={save.isPending}
              >
                <Undo2 /> Discard
              </Button>
              <Button size="sm" onClick={() => save.mutate()} disabled={!valid || save.isPending}>
                {save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save
              </Button>
            </div>
          )}
        </div>
      </Section>
    </div>
  );
}
