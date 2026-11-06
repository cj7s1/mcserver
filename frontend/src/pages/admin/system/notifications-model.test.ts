import { describe, expect, it } from "vitest";
import {
  channelPayload,
  channelToForm,
  EMPTY_CHANNEL_FORM,
  eventLabels,
  validateChannelForm,
  webhookUrlError,
  type NotificationChannel,
} from "./notifications-model";

const channel = (over: Partial<NotificationChannel> = {}): NotificationChannel => ({
  id: 1,
  name: "Ops",
  kind: "discord",
  url_hint: "https://discord.com/…",
  events: ["job.failed", "backup.failed"],
  instance: null,
  enabled: true,
  last_sent_at: null,
  last_error: "",
  created_at: "2026-01-01T00:00:00Z",
  ...over,
});

describe("webhookUrlError", () => {
  it("accepts Discord webhook URLs only on Discord hosts over https", () => {
    expect(webhookUrlError("discord", "https://discord.com/api/webhooks/1/abc")).toBeNull();
    expect(webhookUrlError("discord", "https://discordapp.com/api/webhooks/1/abc")).toBeNull();
    expect(webhookUrlError("discord", "https://canary.discord.com/api/webhooks/1/abc")).toBeNull();
    expect(webhookUrlError("discord", "http://discord.com/api/webhooks/1/abc")).toMatch(/discord\.com\/api\/webhooks/);
    expect(webhookUrlError("discord", "https://discord.com/channels/1")).toMatch(/discord\.com\/api\/webhooks/);
    expect(webhookUrlError("discord", "https://evil.example/api/webhooks/1")).toMatch(/discord\.com\/api\/webhooks/);
    expect(webhookUrlError("discord", "https://discord.com.evil.example/api/webhooks/1")).not.toBeNull();
    expect(webhookUrlError("discord", "discord.com/api/webhooks/1")).not.toBeNull();
  });

  it("accepts any full http(s) URL for generic webhooks", () => {
    expect(webhookUrlError("webhook", "https://example.org/hook")).toBeNull();
    expect(webhookUrlError("webhook", "http://192.168.1.5:8080/x")).toBeNull();
    expect(webhookUrlError("webhook", "ftp://example.org/hook")).toMatch(/http/);
    expect(webhookUrlError("webhook", "example.org/hook")).toMatch(/http/);
    expect(webhookUrlError("webhook", "")).toMatch(/Enter/);
  });
});

describe("validateChannelForm", () => {
  const valid = { ...EMPTY_CHANNEL_FORM, name: "Ops", url: "https://discord.com/api/webhooks/1/abc" };

  it("passes a complete channel", () => {
    expect(validateChannelForm(valid)).toEqual({});
  });

  it("requires name, URL and at least one event", () => {
    const errs = validateChannelForm({ ...valid, name: " ", url: "", events: [] });
    expect(Object.keys(errs).sort()).toEqual(["events", "name", "url"]);
  });

  it("rejects unknown events", () => {
    expect(validateChannelForm({ ...valid, events: ["nope"] }, undefined, ["job.failed"]).events).toBeDefined();
  });

  it("keeps the stored URL on edit unless the kind changes", () => {
    const ch = channel();
    expect(validateChannelForm(channelToForm(ch), ch)).toEqual({});
    expect(validateChannelForm({ ...channelToForm(ch), kind: "webhook" }, ch).url).toMatch(/new kind/);
    expect(validateChannelForm({ ...channelToForm(ch), url: "https://example.org/x" }, ch).url).toMatch(/Discord/);
  });
});

describe("channelPayload", () => {
  it("sends everything when adding, with null for all servers", () => {
    expect(channelPayload({ ...EMPTY_CHANNEL_FORM, name: " Ops ", url: " https://example.org/hook ", kind: "webhook" })).toEqual({
      name: "Ops",
      kind: "webhook",
      url: "https://example.org/hook",
      events: ["backup.failed", "instance.crash_loop_stopped", "instance.failed"],
      instance: null,
      enabled: true,
    });
  });

  it("only sends changes when editing and never an empty URL", () => {
    const ch = channel();
    expect(channelPayload(channelToForm(ch), ch)).toEqual({});
    expect(channelPayload({ ...channelToForm(ch), events: ["backup.failed", "job.failed"] }, ch)).toEqual({});
    expect(channelPayload({ ...channelToForm(ch), enabled: false, instance: "abc" }, ch)).toEqual({
      enabled: false,
      instance: "abc",
    });
    expect(channelPayload({ ...channelToForm(ch), url: "https://discord.com/api/webhooks/2/x" }, ch)).toEqual({
      url: "https://discord.com/api/webhooks/2/x",
    });
  });
});

describe("eventLabels", () => {
  it("orders by the server's list and falls back to ids", () => {
    const events = [
      { id: "instance.failed", label: "Server failed" },
      { id: "backup.failed", label: "Backup failed" },
    ];
    expect(eventLabels(["backup.failed", "instance.failed", "other"], events)).toEqual(["Server failed", "Backup failed", "other"]);
    expect(eventLabels(["x"], undefined)).toEqual(["x"]);
  });
});
