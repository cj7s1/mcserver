/** Named ports that share one host port number over two protocols (`same_port_as`; Satisfactory's game on UDP and its API on TCP). */
import type { GamePortSpec } from "@/lib/types";

/** "udp" or "udp + tcp": the protocols a named port's host number is used for (itself and the ports that share it). */
export function sharedPortsLabel(ports: readonly GamePortSpec[], port: GamePortSpec): string {
  return [port, ...ports.filter((p) => p.same_port_as === port.name)].map((p) => p.protocol).join(" + ");
}
