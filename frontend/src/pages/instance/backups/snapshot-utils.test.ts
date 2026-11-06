import { describe, expect, it } from "vitest";
import { providerLabel, snapshotState, snapshotTime, volumeNoun, type SnapshotCapability } from "./snapshot-utils";

const cap = (over: Partial<SnapshotCapability>): SnapshotCapability => ({
  provider: "btrfs",
  available: true,
  fstype: "btrfs",
  reason: null,
  instance_is_subvolume: false,
  ...over,
});

describe("snapshot helpers", () => {
  it("derives the section state for btrfs and ZFS agents", () => {
    expect(snapshotState(undefined)).toBeNull();
    expect(snapshotState(cap({ available: false }))).toBe("unavailable");
    expect(snapshotState(cap({}))).toBe("convert");
    expect(snapshotState(cap({ instance_is_subvolume: true }))).toBe("ready"); // older agents
    expect(snapshotState(cap({ provider: "zfs", instance_ready: true, instance_is_subvolume: true }))).toBe("ready");
    expect(snapshotState(cap({ provider: "zfs", instance_ready: false, instance_is_subvolume: true }))).toBe("convert");
  });

  it("names the provider", () => {
    expect(providerLabel("zfs")).toBe("ZFS");
    expect(providerLabel("btrfs")).toBe("btrfs");
    expect(providerLabel(null)).toBe("filesystem");
    expect(volumeNoun("zfs")).toBe("ZFS dataset");
    expect(volumeNoun("btrfs")).toBe("btrfs subvolume");
  });

  it("falls back to the timestamp in the snapshot name", () => {
    expect(snapshotTime({ name: "20261106T120102Z-x", label: "", created_at: null })).toBe("2026-11-06T12:01:02Z");
  });
});
