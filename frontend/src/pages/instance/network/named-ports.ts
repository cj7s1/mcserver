/** Named ports of a non-Minecraft server: rows of the editor, validation and the PATCH body (GameNetwork.tsx). */
import { derivedPorts } from "@/lib/game-ports";
import type { GamePortSpec, NamedPort } from "@/lib/types";

export interface NamedPortRow {
  name: string;
  label: string;
  protocol: "tcp" | "udp";
  container_port: number;
  host_port: string;
}

export function rowsFromPorts(ports: readonly NamedPort[]): NamedPortRow[] {
  return ports.map((p) => ({
    name: p.name,
    label: p.label || p.name,
    protocol: p.protocol,
    container_port: p.container_port,
    host_port: String(p.host_port),
  }));
}

/**
 * Errors per port name: range, and two named ports of this server on the same host port and protocol.
 * With the catalog's `specs`, the ports derived from a row (Valheim: query = game + 1) count as well.
 */
export function validateNamedPorts(rows: readonly NamedPortRow[], specs?: readonly GamePortSpec[]): Record<string, string> {
  const errors: Record<string, string> = {};
  const seen = new Map<string, string>();
  for (const r of rows) {
    const t = r.host_port.trim();
    if (!/^\d+$/.test(t) || Number(t) < 1024 || Number(t) > 65535) {
      errors[r.name] = "A port between 1024 and 65535";
      continue;
    }
    const key = `${Number(t)}/${r.protocol}`;
    const other = seen.get(key);
    if (other) errors[r.name] = `Same port and protocol as ${other}`;
    else seen.set(key, r.label);
    for (const d of derivedPorts(specs, r.name)) {
      const n = Number(t) + (d.derived_from?.offset ?? 0);
      const dk = `${n}/${d.protocol}`;
      if (n > 65535) errors[r.name] ??= `${d.label} would be port ${n}; choose a lower port`;
      else if (seen.has(dk)) errors[r.name] ??= `${d.label} (port ${n}) would have the same port and protocol as ${seen.get(dk)}`;
      seen.set(dk, d.label);
    }
  }
  return errors;
}

/** The PATCH body's `ports`: every named port of the server with its host port. */
export function portsPatch(rows: readonly NamedPortRow[]): { name: string; host_port: number }[] {
  return rows.map((r) => ({ name: r.name, host_port: Number(r.host_port.trim()) }));
}
