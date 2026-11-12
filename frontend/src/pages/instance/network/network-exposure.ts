/**
 * A-6 (security review 2026-11-11): the panel never publishes a server's control port (RCON, a REST or
 * admin API), but that only holds on the agent's own instance network. A server on the host's network,
 * or on another Docker network (a macvlan/ipvlan network such as Unraid's br0 gives the container its own
 * LAN address), is reachable on that network on every port it listens on, control port included.
 */
import { gameId } from "@/lib/games";
import type { Instance } from "@/lib/types";

/** The agent's default instance network (agent setting AGENT_INSTANCE_NETWORK). */
export const DEFAULT_INSTANCE_NETWORK = "mcpanel_instances";

/** A network of one server that the agent creates itself (`mcpanel_i_<first 12 hex of the id>`). */
const PER_SERVER_NETWORK = /^mcpanel_i_[0-9a-f]{12}$/;

/** Whether a server with this `network` setting runs on a network the agent manages (empty = its default). */
export function onAgentNetwork(network: string | null | undefined): boolean {
  const name = (network ?? "").trim();
  return !name || name === DEFAULT_INSTANCE_NETWORK || PER_SERVER_NETWORK.test(name);
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
