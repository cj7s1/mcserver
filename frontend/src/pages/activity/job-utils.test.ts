import { describe, expect, it } from "vitest";
import { canAbandonJob, isJobActive, jobDurationLabel } from "./job-utils";

describe("canAbandonJob", () => {
  const control = ["instance.view", "instance.control"];

  it("only allows queued or running jobs", () => {
    for (const status of ["succeeded", "failed", "cancelled"] as const) {
      expect(canAbandonJob({ status, instance: "a" }, { instance: control, global: ["system.settings"] })).toBe(false);
    }
    expect(canAbandonJob({ status: "queued", instance: "a" }, { instance: control })).toBe(true);
    expect(canAbandonJob({ status: "running", instance: "a" }, { instance: control })).toBe(true);
  });

  it("needs instance.control on the job's server", () => {
    expect(canAbandonJob({ status: "running", instance: "a" }, { instance: ["instance.view"] })).toBe(false);
    expect(canAbandonJob({ status: "running", instance: "a" }, { instance: null, global: ["system.settings"] })).toBe(false);
    // global system.settings does not replace the server permission
    expect(canAbandonJob({ status: "running", instance: "a" }, { global: ["system.settings", "instance.control"] })).toBe(false);
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
