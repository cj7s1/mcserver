import { describe, expect, it } from "vitest";
import {
  agentUrlError,
  certValidity,
  checkSummary,
  EMPTY_NODE_FORM,
  formatFingerprint,
  nodePayload,
  nodeToForm,
  normalizeFingerprint,
  sameFingerprint,
  validateNodeForm,
  type PanelNodeRecord,
} from "./nodes-model";

const COLON = "2B:48:C7:C6:11:8B:02:A0:A6:32:45:62:72:03:1A:4B:A0:5F:57:9A:F0:6A:EF:22:92:01:2A:46:65:B2:FA:2B";
const HEX = COLON.replace(/:/g, "").toLowerCase();

const node = (over: Partial<PanelNodeRecord> = {}): PanelNodeRecord => ({
  id: 2,
  name: "node2",
  agent_url: "https://10.0.0.5:8700",
  use_settings_token: false,
  is_default: false,
  public_address: "",
  lan_address: "10.0.0.5",
  tls_fingerprint: COLON,
  transport: "tls_pinned",
  tls_pinned: true,
  security_warning: null,
  last_seen_at: null,
  last_error: "",
  instance_count: 0,
  created_at: "2026-01-01T00:00:00Z",
  ...over,
});

describe("fingerprints", () => {
  it("normalises the formats the backend accepts", () => {
    expect(HEX).toHaveLength(64);
    expect(normalizeFingerprint(COLON)).toBe(HEX);
    expect(normalizeFingerprint(`SHA256 Fingerprint=${COLON}`)).toBe(HEX);
    expect(normalizeFingerprint(`sha256:${HEX}`)).toBe(HEX);
    expect(normalizeFingerprint(COLON.replace(/:/g, " ").toLowerCase())).toBe(HEX);
    expect(normalizeFingerprint(`  ${HEX}\n`)).toBe(HEX);
  });

  it("rejects anything that is not 64 hex digits", () => {
    expect(normalizeFingerprint("")).toBeNull();
    expect(normalizeFingerprint(COLON.slice(0, -3))).toBeNull();
    expect(normalizeFingerprint(`${HEX}00`)).toBeNull();
    expect(normalizeFingerprint(HEX.replace("2b", "zz"))).toBeNull();
  });

  it("formats and compares", () => {
    expect(formatFingerprint(HEX)).toBe(COLON);
    expect(sameFingerprint(COLON, HEX)).toBe(true);
    expect(sameFingerprint(COLON, HEX.replace(/^2b/, "2c"))).toBe(false);
    expect(sameFingerprint("", "")).toBe(false);
  });
});

describe("agentUrlError", () => {
  it("accepts base URLs", () => {
    expect(agentUrlError("https://node2.lan:8700")).toBeNull();
    expect(agentUrlError("http://127.0.0.1:8700/")).toBeNull();
    expect(agentUrlError("https://[fd00::5]:8700")).toBeNull();
  });

  it("rejects other shapes", () => {
    expect(agentUrlError("")).toMatch(/Enter the agent URL/);
    expect(agentUrlError("node2.lan:8700")).not.toBeNull();
    expect(agentUrlError("ftp://node2:21")).toMatch(/http/);
    expect(agentUrlError("https://node2:8700/api")).toMatch(/base URL/);
    expect(agentUrlError("https://node2:8700?x=1")).toMatch(/base URL/);
    expect(agentUrlError("https://user:pw@node2:8700")).toMatch(/base URL/);
  });
});

describe("validateNodeForm", () => {
  const valid = {
    ...EMPTY_NODE_FORM,
    name: "node2",
    agent_url: "https://10.0.0.5:8700",
    agent_token: "x".repeat(24),
    tls_fingerprint: COLON,
  };

  it("passes a complete new node", () => {
    expect(validateNodeForm(valid)).toEqual({});
  });

  it("requires a token of at least 24 characters when adding", () => {
    expect(validateNodeForm({ ...valid, agent_token: "" }).agent_token).toMatch(/AGENT_TOKEN/);
    expect(validateNodeForm({ ...valid, agent_token: "short" }).agent_token).toMatch(/24/);
  });

  it("keeps the token when blank on edit", () => {
    expect(validateNodeForm({ ...nodeToForm(node()) }, node())).toEqual({});
    expect(validateNodeForm({ ...nodeToForm(node()), agent_token: "abc" }, node()).agent_token).toMatch(/24/);
  });

  it("checks fingerprints and that pinning needs https", () => {
    expect(validateNodeForm({ ...valid, tls_fingerprint: "AB:CD" }).tls_fingerprint).toMatch(/64 hex/);
    expect(validateNodeForm({ ...valid, agent_url: "http://10.0.0.5:8700" }).tls_fingerprint).toMatch(/https/);
    expect(validateNodeForm({ ...valid, agent_url: "http://10.0.0.5:8700", tls_fingerprint: "" })).toEqual({});
  });

  it("validates addresses", () => {
    expect(validateNodeForm({ ...valid, lan_address: "http://10.0.0.5" }).lan_address).toMatch(/Hostname or IP/);
    expect(validateNodeForm({ ...valid, public_address: "mc.example.com" })).toEqual({});
  });

  it("only checks addresses for settings-managed nodes", () => {
    const local = node({ use_settings_token: true, is_default: true, agent_url: "http://127.0.0.1:8700", tls_fingerprint: "" });
    expect(validateNodeForm({ ...nodeToForm(local), name: "" }, local)).toEqual({});
  });
});

describe("nodePayload", () => {
  it("sends every field when adding, with a normalised fingerprint", () => {
    const body = nodePayload({
      ...EMPTY_NODE_FORM,
      name: " node2 ",
      agent_url: "https://10.0.0.5:8700/",
      agent_token: "t".repeat(30),
      tls_fingerprint: HEX,
    });
    expect(body).toEqual({
      name: "node2",
      agent_url: "https://10.0.0.5:8700",
      agent_token: "t".repeat(30),
      tls_fingerprint: COLON,
      lan_address: "",
      public_address: "",
    });
  });

  it("only sends changed fields when editing", () => {
    const n = node();
    expect(nodePayload(nodeToForm(n), n)).toEqual({});
    // same fingerprint in another notation is not a change
    expect(nodePayload({ ...nodeToForm(n), tls_fingerprint: HEX }, n)).toEqual({});
    expect(nodePayload({ ...nodeToForm(n), tls_fingerprint: "", agent_token: "n".repeat(24) }, n)).toEqual({
      tls_fingerprint: "",
      agent_token: "n".repeat(24),
    });
  });

  it("never sends URL, token or fingerprint for settings-managed nodes", () => {
    const local = node({ use_settings_token: true, agent_url: "http://127.0.0.1:8700", tls_fingerprint: "" });
    const body = nodePayload(
      { ...nodeToForm(local), agent_url: "http://evil:1", agent_token: "z".repeat(30), lan_address: "192.168.1.2" },
      local,
    );
    expect(body).toEqual({ lan_address: "192.168.1.2" });
  });
});

describe("certificate helpers", () => {
  it("reports validity", () => {
    const p = { not_before: "2026-01-01T00:00:00+00:00", not_after: "2027-01-01T00:00:00+00:00" };
    expect(certValidity(p, Date.parse("2026-06-01T00:00:00Z"))).toBe("valid");
    expect(certValidity(p, Date.parse("2025-06-01T00:00:00Z"))).toBe("not_yet_valid");
    expect(certValidity(p, Date.parse("2028-06-01T00:00:00Z"))).toBe("expired");
    expect(certValidity({ not_before: "?", not_after: "?" })).toBe("unknown");
  });

  it("summarises a check", () => {
    expect(
      checkSummary({
        version: "0.1.0",
        docker: { server_version: "29.9.2" },
        restic: "restic 0.19.1 compiled with go1.26.4 on linux/amd64",
        tls: { mode: "auto" },
      }),
    ).toBe("mcagent 0.1.0 · Docker 29.9.2 · restic 0.19.1 · TLS auto");
    expect(checkSummary({ version: "0.1.0", restic: null })).toBe("mcagent 0.1.0 · restic missing");
  });
});
