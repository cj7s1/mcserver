/** Extra port mapping rows and presets for common mods/plugins. */

export interface PortRow {
  key: string;
  label: string;
  container_port: string;
  host_port: string;
  protocol: "tcp" | "udp";
  host_ip: string;
}

export interface PortPreset {
  id: string;
  label: string;
  container_port: number;
  protocol: "tcp" | "udp";
  /** use the game's host port (query shares the game port number on UDP) */
  sameAsGame?: boolean;
  help: string;
}

export const PORT_PRESETS: PortPreset[] = [
  {
    id: "query",
    label: "Query",
    container_port: 25565,
    protocol: "udp",
    sameAsGame: true,
    help: "GameSpy4 query for server lists and status tools. Also turn on enable-query in Settings → Properties.",
  },
  {
    id: "voice",
    label: "Voice chat",
    container_port: 24454,
    protocol: "udp",
    help: "Simple Voice Chat mod/plugin (default UDP 24454).",
  },
  { id: "bluemap", label: "BlueMap", container_port: 8100, protocol: "tcp", help: "BlueMap web map (default TCP 8100)." },
  { id: "dynmap", label: "Dynmap", container_port: 8123, protocol: "tcp", help: "Dynmap web map (default TCP 8123)." },
];

export const MAX_EXTRA_PORTS = 10;
