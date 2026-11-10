/** "How players connect" diagram and the copyable connection addresses for an instance. */
import { ArrowDown, ArrowRight, Check, Copy, Globe, House, Server, ShieldOff, Box } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import type { ConnectionInfo, PortMapping } from "@/lib/types";
import { cn } from "@/lib/utils";


export function CopyButton({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      aria-label={`Copy ${label}`}
      onClick={async () => {
        if (await copyText(value)) {
          setDone(true);
          toast.success(`Copied ${value}`);
          setTimeout(() => setDone(false), 1500);
        } else toast.error("Could not copy. Select the address and copy it manually.");
      }}
    >
      {done ? <Check className="text-emerald-600 dark:text-emerald-400" /> : <Copy />}
    </Button>
  );
}

function FlowNode({ icon, title, detail, tone }: { icon: ReactNode; title: string; detail: ReactNode; tone?: string }) {
  return (
    <div className={cn("flex min-w-0 flex-1 items-start gap-2.5 rounded-lg border bg-background p-3", tone)}>
      <div className="mt-0.5 shrink-0 text-muted-foreground">{icon}</div>
      <div className="min-w-0">
        <p className="text-xs font-medium">{title}</p>
        <div className="mt-0.5 text-xs text-muted-foreground">{detail}</div>
      </div>
    </div>
  );
}

function FlowArrow({ label }: { label: string }) {
  return (
    <div className="flex shrink-0 items-center justify-center gap-1 text-[10px] text-muted-foreground md:flex-col md:px-1">
      <ArrowDown className="size-3.5 md:hidden" aria-hidden />
      <ArrowRight className="hidden size-3.5 md:block" aria-hidden />
      <span className="max-w-24 text-center leading-tight">{label}</span>
    </div>
  );
}

export function ConnectionFlow({
  hostPort,
  bind,
  extraPorts,
  network,
}: {
  hostPort: number;
  bind: string;
  extraPorts: PortMapping[];
  network: string;
}) {
  const allInterfaces = bind === "0.0.0.0" || bind === "::";
  const hostNet = network === "host";
  return (
    <div className="grid gap-3">
      <div className="flex flex-col items-stretch gap-1.5 md:flex-row md:items-center" role="group" aria-label="Connection path">
        <FlowNode
          icon={<Globe className="size-4" />}
          title="Players on the internet"
          detail={
            <>
              Your <strong className="text-foreground">public IP</strong> or a DNS name. Needs a port forward on your router
              (and possibly a DNS record). <em>You</em> set this up; the panel can't.
            </>
          }
          tone="border-dashed"
        />
        <FlowArrow label={`router forwards TCP ${hostPort}`} />
        <FlowNode
          icon={<House className="size-4" />}
          title={`Host machine :${hostPort}`}
          detail={
            hostNet ? (
              "Host networking: the server listens on the host directly, no port mapping."
            ) : (
              <>
                Docker publishes <code className="font-mono text-foreground">{bind}:{hostPort}/tcp</code>
                {allInterfaces ? " on every network interface" : " on this address only"}. LAN players connect here.
              </>
            )
          }
        />
        <FlowArrow label={hostNet ? "same port" : "Docker maps to"} />
        <FlowNode
          icon={<Box className="size-4" />}
          title="Container :25565"
          detail="Minecraft always listens on 25565 inside its container (server-port is managed by the panel)."
        />
      </div>
      <ul className="grid gap-1.5 text-xs text-muted-foreground sm:grid-cols-2">
        <li className="flex gap-2">
          <ShieldOff className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>
            <strong className="text-foreground">RCON (25575) is never published.</strong> The panel talks to it over the internal
            Docker network, so the console works without exposing it.
          </span>
        </li>
        <li className="flex gap-2">
          <Server className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>
            {extraPorts.length > 0
              ? `${extraPorts.length} extra port${extraPorts.length === 1 ? "" : "s"} published for mods/plugins (see below).`
              : "No extra ports published. Add some below for query, voice chat or web maps."}{" "}
            Each one you want reachable from the internet also needs its own router forward.
          </span>
        </li>
      </ul>
    </div>
  );
}

const KIND_ICON = { bind: Server, lan: House, public: Globe } as const;

export function AddressList({ connection, canConfigurePanel }: { connection: ConnectionInfo; canConfigurePanel: boolean }) {
  const { addresses, notes } = connection;
  return (
    <div className="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] lg:items-start">
      {addresses.length > 0 ? (
        <ul className="grid gap-2">
          {addresses.map((a) => {
            const Icon = KIND_ICON[a.kind] ?? Server;
            return (
              <li key={`${a.kind}-${a.address}`} className="flex min-w-0 items-center gap-3 rounded-lg border bg-background px-3 py-2">
                <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-muted-foreground">{a.label}</p>
                  <p className="truncate font-mono text-sm select-all">{a.address}</p>
                </div>
                <CopyButton value={a.address} label={`${a.label} address`} />
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="rounded-lg border border-dashed px-3 py-3 text-xs text-muted-foreground">
          <p className="font-medium text-foreground">No LAN or public address configured</p>
          <p className="mt-1">
            Players can connect to this machine's IP address on port{" "}
            <code className="font-mono text-foreground">{connection.host_port}</code>
            {connection.host_port === 25565 ? " (the default, so the port can be omitted)" : ""}.{" "}
            {canConfigurePanel ? (
              <>
                Set the LAN and public address in{" "}
                <Link to="/admin/system" className="font-medium text-primary hover:underline">
                  System settings
                </Link>{" "}
                to show ready-to-copy addresses here.
              </>
            ) : (
              "An administrator can set the LAN and public address in System settings to show ready-to-copy addresses here."
            )}
          </p>
        </div>
      )}
      {notes.length > 0 && (
        <ul className="grid list-disc gap-1 pl-5 text-xs text-muted-foreground">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
