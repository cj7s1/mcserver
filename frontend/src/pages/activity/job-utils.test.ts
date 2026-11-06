import { describe, expect, it } from "vitest";
import { canAbandonJob, isJobActive, jobDurationLabel } from "./job-utils";

describe("canAbandonJob", () => {
  const control = ["instance.view", "instance.control"];

  it("only allows queued or running jobs", () => {
    for (const status of ["succeeded", "failed", "cancelled"] as const) {
      expect(canAbandonJob({ status, instance: "a" }, { instance: control, global: ["system.settings"] })).toBe(false);
    }
    expect(canAbandonJob({ status: "queued", instance: "a", kind: "start" }, { instance: control })).toBe(true);
    expect(canAbandonJob({ status: "running", instance: "a", kind: "stop" }, { instance: control })).toBe(true);
  });

  it("needs instance.control on the job's server for power jobs", () => {
    expect(canAbandonJob({ status: "running", instance: "a", kind: "restart" }, { instance: ["instance.view"] })).toBe(false);
    expect(canAbandonJob({ status: "running", instance: "a", kind: "restart" }, { instance: control })).toBe(true);
    // a global instance.control alone does not count for this server
    expect(canAbandonJob({ status: "running", instance: "a", kind: "restart" }, { global: ["instance.control"] })).toBe(false);
  });

  it("needs the permission of the job's kind (backend ABANDON_PERMISSIONS)", () => {
    const job = (kind: string) => ({ status: "running" as const, instance: "a", kind });
    expect(canAbandonJob(job("restore"), { instance: control })).toBe(false);
    expect(canAbandonJob(job("restore"), { instance: [...control, "instance.restore"] })).toBe(true);
    expect(canAbandonJob(job("delete"), { instance: control })).toBe(false);
    expect(canAbandonJob(job("world_replace"), { instance: ["instance.restore"] })).toBe(false);
    expect(canAbandonJob(job("world_replace"), { instance: ["instance.restore", "instance.files.write"] })).toBe(true);
    // panel-wide codes come from the global role
    expect(canAbandonJob(job("migrate"), { instance: ["instance.configure", "system.nodes"] })).toBe(false);
    expect(canAbandonJob(job("migrate"), { instance: ["instance.configure"], global: ["system.nodes"] })).toBe(true);
    expect(canAbandonJob(job("restore_new"), { instance: ["instance.restore"], global: ["instance.create"] })).toBe(true);
    // unknown kinds need system.settings, which may always abandon
    expect(canAbandonJob(job("something_new"), { instance: control })).toBe(false);
    expect(canAbandonJob(job("restore"), { instance: [], global: ["system.settings"] })).toBe(true);
  });

  it("needs system.settings for jobs without a server", () => {
    expect(canAbandonJob({ status: "running", instance: null }, { global: ["system.settings"] })).toBe(true);
    expect(canAbandonJob({ status: "running", instance: null }, { global: ["instance.control"], instance: control })).toBe(false);
  });
});

describe("job helpers", () => {
  it("detects active jobs", () => {
    expect(isJobActive({ status: "queued" })).toBe(true);
    expect(isJobActive({ status: "running" })).toBe(true);
    expect(isJobActive({ status: "failed" })).toBe(false);
  });

  it("labels durations", () => {
    expect(jobDurationLabel({ started_at: null, finished_at: null })).toBe("—");
    expect(
      jobDurationLabel({ started_at: "2026-01-01T00:00:00Z", finished_at: "2026-01-01T00:01:05Z" }),
    ).toBe("1m 5s");
  });
});
