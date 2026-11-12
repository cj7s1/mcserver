/**
 * A-6 (security review 2026-11-11): the panel never publishes a server's control port (RCON, a REST or
 * admin API), but that only holds on the agent's own instance network. A server on the host's network,
 * or on another Docker network (a macvlan/ipvlan network such as Unraid's br0 gives the container its own
 * LAN address), is reachable on that network on every port it listens on, control port included.
 */
import { gameId } from "@/lib/games";
import type { AgentNetworks, Instance } from "@/lib/types";

/** The agent's default instance network (agent setting AGENT_INSTANCE_NETWORK): the guess while the agent does not say. */
export const DEFAULT_INSTANCE_NETWORK = "mcpanel_instances";

/**
 * A network of one server that the agent creates itself: `zz_mcpanel_i_<first 12 hex of the id>` (the prefix
 * sorts after the compose networks, A-1); `mcpanel_i_` is the spelling of the first agents with them.
 */
const PER_SERVER_NETWORK = /^(?:zz_)?mcpanel_i_[0-9a-f]{12}$/;

/**
 * Whether a server with this `network` setting runs on a network the agent manages: empty (its default),
 * the agent's shared network, or one of its per-server networks. The names come from the instance answer's
 * `agent_networks`; only when the agent does not report them (null) are the default names assumed.
 */
export function onAgentNetwork(network: string | null | undefined, agent?: AgentNetworks | null): boolean {
  const name = (network ?? "").trim();
  if (!name) return true;
  if (agent && typeof agent.shared === "string" && agent.shared) {
    const prefix = typeof agent.per_server_prefix === "string" ? agent.per_server_prefix : "";
    return name === agent.shared || (!!prefix && name.startsWith(prefix));
  }
  return name === DEFAULT_INSTANCE_NETWORK || PER_SERVER_NETWORK.test(name);
}

/** The control listeners of each game (and Terraria variant) that the panel keeps unpublished. */
const CONTROL_PORTS: Record<string, string> = {
  minecraft: "RCON (TCP 25575)",
  factorio: "RCON (TCP 27015)",
  "terraria:tshock": "the TShock REST API (TCP 7878)",
  palworld: "the REST API (TCP 8212)",
  "project-zomboid": "RCON (TCP 27015)",
  cs2: "RCON (TCP, on the game port's number)",
  rust: "WebRCON (TCP 28016) and its web page (TCP 8080)",
};

/** The control port(s) of a server in words, or null when its game is controlled through its console only. */
export function controlPorts(instance: Pick<Instance, "game" | "server_type">): string | null {
  const game = gameId(instance);
  return CONTROL_PORTS[`${game}:${String(instance.server_type ?? "").toLowerCase()}`] ?? CONTROL_PORTS[game] ?? null;
}
