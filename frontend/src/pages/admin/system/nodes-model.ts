/**
 * Shapes and client-side validation for the System → Nodes section.
 * Mirrors backend/apps/nodes/views.py (NodeSerializer, NodeCheckView, NodeTlsProbeView) and
 * backend/apps/nodes/tls.py (fingerprint normalisation). The backend re-validates everything.
 */

export type NodeTransport = "plain" | "tls_pinned" | "tls_ca";

/** GET /nodes item. Tokens are write-only and never returned. */
export interface PanelNodeRecord {
  id: number;
  name: string;
  agent_url: string;
  /** The node uses MCPANEL_AGENT_TOKEN (and URL / fingerprint) from the panel's environment. */
  use_settings_token: boolean;
  is_default: boolean;
  public_address: string;
  lan_address: string;
  /** "AB:CD:…" (upper-case, colon separated) or "" when not pinned. */
  tls_fingerprint: string;
  transport: NodeTransport;
  tls_pinned: boolean;
  security_warning: string | null;
  last_seen_at: string | null;
  last_error: string;
  instance_count: number;
  created_at: string;
}

/** POST /nodes/tls-probe: the certificate an https:// agent presents (handshake only). */
export interface TlsProbeResult {
  fingerprint: string;
  subject: string;
  issuer: string;
  self_signed: boolean;
  not_before: string;
  not_after: string;
  names: string[];
}

export interface AgentCheckInfo {
  version?: string;
  restic?: string | null;
  docker?: {
    server_version?: string | null;
    cpus?: number | null;
    memory_bytes?: number | null;
    os?: string | null;
    architecture?: string | null;
  } | null;
  tls?: { mode?: string | null; fingerprint?: string | null } | null;
}

/** POST /nodes/{id}/check */
export type NodeCheckResult = { ok: true; info: AgentCheckInfo } | { ok: false; error: string; code?: string };

/** Same key as the Settings → "Move to another node" list, so both share the cache. */
export const nodesKey = ["nodes"] as const;

export const NODE_COMPOSE_FINGERPRINT_CMD =
  "docker compose -f docker-compose.node.yml --env-file node.env exec agent python -m mcagent fingerprint";

export const MIN_TOKEN_LENGTH = 24;

export const TRANSPORT_META: Record<NodeTransport, { label: string; description: string }> = {
  plain: {
    label: "Plain HTTP",
    description: "Unencrypted. The token and console traffic can be read on the network.",
  },
  tls_pinned: {
    label: "TLS pinned",
    description: "HTTPS. The panel trusts exactly the certificate with the pinned fingerprint.",
  },
  tls_ca: {
    label: "TLS (CA)",
    description: "HTTPS verified against trusted certificate authorities and the host name.",
  },
};

/** Accept "AB:CD:…", "ab cd …", "SHA256 Fingerprint=AB:…", "sha256:…" or bare hex; return lower hex or null. */
export function normalizeFingerprint(value: string): string | null {
  let text = value.trim();
  if (text.includes("=")) text = text.slice(text.indexOf("=") + 1);
  if (text.toLowerCase().startsWith("sha256:")) text = text.slice(7);
  const hexed = text.replace(/[\s:]/g, "").toLowerCase();
  return /^[0-9a-f]{64}$/.test(hexed) ? hexed : null;
}

/** lower hex -> "AB:CD:…" */
export function formatFingerprint(hexed: string): string {
  const up = hexed.toUpperCase();
  const out: string[] = [];
  for (let i = 0; i < up.length; i += 2) out.push(up.slice(i, i + 2));
  return out.join(":");
}

export function sameFingerprint(a: string, b: string): boolean {
  const na = normalizeFingerprint(a);
  return na !== null && na === normalizeFingerprint(b);
}

/** Validate an agent base URL like the backend: http(s), a host, no path or query. */
export function agentUrlError(value: string): string | null {
  const v = value.trim();
  if (!v) return "Enter the agent URL, e.g. https://node2.lan:8700";
  let u: URL;
  try {
    u = new URL(v);
  } catch {
    return "Enter a full URL including https://";
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return "Use an http:// or https:// URL";
  if (!u.hostname) return "The URL needs a host name or IP";
  if ((u.pathname !== "/" && u.pathname !== "") || u.search || u.hash || u.username || u.password)
    return "Use the agent base URL only, e.g. https://node2.lan:8700";
  return null;
}

export function isHttpsUrl(value: string): boolean {
  return value.trim().toLowerCase().startsWith("https://") && agentUrlError(value) === null;
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const HOST_RE = /^[A-Za-z0-9.-]+$/;
const isIp = (s: string) => IPV4.test(s) || (s.includes(":") && /^[0-9a-fA-F:.]+$/.test(s));

export interface NodeFormState {
  name: string;
  agent_url: string;
  agent_token: string;
  tls_fingerprint: string;
  lan_address: string;
  public_address: string;
}

export const EMPTY_NODE_FORM: NodeFormState = {
  name: "",
  agent_url: "",
  agent_token: "",
  tls_fingerprint: "",
  lan_address: "",
  public_address: "",
};

export function nodeToForm(node: PanelNodeRecord): NodeFormState {
  return {
    name: node.name,
    agent_url: node.agent_url,
    agent_token: "",
    tls_fingerprint: node.tls_fingerprint,
    lan_address: node.lan_address,
    public_address: node.public_address,
  };
}

/**
 * Validate the add/edit form. `existing` is the node being edited (undefined when adding).
 * Settings-managed nodes (use_settings_token) only edit their addresses.
 */
export function validateNodeForm(f: NodeFormState, existing?: PanelNodeRecord): Record<string, string> {
  const errors: Record<string, string> = {};
  const managed = !!existing?.use_settings_token;
  if (!managed) {
    const name = f.name.trim();
    if (!name) errors.name = "Enter a name for this node";
    else if (name.length > 100) errors.name = "At most 100 characters";

    const urlErr = agentUrlError(f.agent_url);
    if (urlErr) errors.agent_url = urlErr;

    const token = f.agent_token;
    if (!existing && !token) errors.agent_token = "Enter the token configured as AGENT_TOKEN on that node";
    else if (token && token.length < MIN_TOKEN_LENGTH)
      errors.agent_token = `Agent tokens must be at least ${MIN_TOKEN_LENGTH} characters`;
    else if (token.length > 500) errors.agent_token = "At most 500 characters";
    else if (token && token.trim() !== token) errors.agent_token = "Remove spaces at the start or end";

    const fp = f.tls_fingerprint.trim();
    if (fp) {
      if (normalizeFingerprint(fp) === null)
        errors.tls_fingerprint = "Enter the SHA-256 fingerprint: 64 hex digits, optionally separated by colons";
      else if (!urlErr && !isHttpsUrl(f.agent_url))
        errors.tls_fingerprint = "Certificate pinning needs an https:// agent URL";
    }
  }
  for (const key of ["lan_address", "public_address"] as const) {
    const v = f[key].trim();
    if (v.length > 255) errors[key] = "At most 255 characters";
    else if (v && !isIp(v) && !HOST_RE.test(v)) errors[key] = "Hostname or IP only, without scheme or port";
  }
  return errors;
}

/** Request body for POST /nodes (add) or PATCH /nodes/{id} (only changed fields). */
export function nodePayload(f: NodeFormState, existing?: PanelNodeRecord): Record<string, string> {
  const fp = f.tls_fingerprint.trim();
  const next: Record<string, string> = {
    name: f.name.trim(),
    agent_url: f.agent_url.trim().replace(/\/+$/, ""),
    tls_fingerprint: fp ? formatFingerprint(normalizeFingerprint(fp) ?? "") : "",
    lan_address: f.lan_address.trim(),
    public_address: f.public_address.trim(),
  };
  if (!existing) {
    return { ...next, agent_token: f.agent_token };
  }
  const managed = existing.use_settings_token;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(next)) {
    if (managed && k !== "lan_address" && k !== "public_address") continue;
    const old = (existing as unknown as Record<string, string>)[k] ?? "";
    const changed = k === "tls_fingerprint" ? !(v === "" ? old === "" : sameFingerprint(v, old)) : v !== old;
    if (changed) out[k] = v;
  }
  if (!managed && f.agent_token) out.agent_token = f.agent_token;
  return out;
}

export type CertValidity = "valid" | "expired" | "not_yet_valid" | "unknown";

export function certValidity(probe: Pick<TlsProbeResult, "not_before" | "not_after">, now = Date.now()): CertValidity {
  const from = Date.parse(probe.not_before);
  const to = Date.parse(probe.not_after);
  if (Number.isNaN(from) || Number.isNaN(to)) return "unknown";
  if (now < from) return "not_yet_valid";
  if (now > to) return "expired";
  return "valid";
}

/** "mcagent 0.1.0 · Docker 29.9.2 · restic 0.19.1 · TLS auto" */
export function checkSummary(info: AgentCheckInfo): string {
  const parts: string[] = [];
  if (info.version) parts.push(`mcagent ${info.version}`);
  if (info.docker?.server_version) parts.push(`Docker ${info.docker.server_version}`);
  if (info.restic) {
    const m = /restic\s+v?([\d.]+)/i.exec(info.restic);
    parts.push(`restic ${m ? m[1] : info.restic}`);
  } else if (info.restic === null) parts.push("restic missing");
  if (info.tls?.mode) parts.push(`TLS ${info.tls.mode}`);
  return parts.join(" · ");
}
