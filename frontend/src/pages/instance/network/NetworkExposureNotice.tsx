/** The Network page's warning for a server that is not on the agent's own network (A-6, network-exposure.ts). */
import { ShieldAlert } from "lucide-react";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { controlPorts, onAgentNetwork } from "./network-exposure";

export function NetworkExposureNotice({
  instance,
  network = instance.network,
  className,
}: {
  instance: Pick<Instance, "game" | "server_type" | "network" | "agent_networks">;
  /** The network to judge (default: the saved one; the Network form passes its unsaved value). */
  network?: string;
  className?: string;
}) {
  if (onAgentNetwork(network, instance.agent_networks)) return null;
  const name = network.trim();
  const control = controlPorts(instance);
  const reach = control ? `every port the server listens on, ${control} included,` : "every port the server listens on";
  return (
    <div
      role="note"
      aria-label="Network exposure"
      className={cn(
        "flex gap-2.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-xs text-amber-900 dark:text-amber-100",
        className,
      )}
    >
      <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
      <div className="grid gap-1">
        <p className="font-medium">
          {name === "host" ? "This server uses the host's network" : `This server is on the Docker network “${name}”, not on the panel's own network`}
        </p>
        <p>
          {name === "host"
            ? `Docker publishes and filters nothing here: ${reach} is open on every network interface of the host (LAN, VPN, …), and the server can reach services the host keeps to itself.`
            : `Docker's port publishing does not apply on that network: ${reach} is reachable by everything else on it. On a macvlan or ipvlan network (Unraid's br0) the server has its own address on your LAN, so that means your LAN.`}
          {control ? " The panel never publishes the control port, but on this network its generated password is the only protection." : ""}
        </p>
      </div>
    </div>
  );
}
