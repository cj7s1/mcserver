import { describe, expect, it } from "vitest";
import type { Instance } from "@/lib/types";
import { powerButtons } from "./power-controls";

const of = (status: Instance["status"], container_state = "", desired_state: Instance["desired_state"] = "stopped") =>
  powerButtons({ status, container_state, desired_state });

describe("powerButtons", () => {
  it("offers Restart, Stop and Kill while the server runs", () => {
    for (const status of ["running", "starting", "installing", "unhealthy", "restarting"] as const) {
      expect(of(status, "running", "running")).toEqual({ start: false, restart: true, stop: true, dismiss: false, kill: true });
    }
  });

  it("offers only Start for a stopped server, with or without a container", () => {
    const onlyStart = { start: true, restart: false, stop: false, dismiss: false, kill: false };
    expect(of("stopped", "")).toEqual(onlyStart);
    expect(of("stopped", "exited")).toEqual(onlyStart); // the seconds before its container is removed
    expect(of("created", "")).toEqual(onlyStart);
    expect(of("created", "created")).toEqual(onlyStart);
  });

  it("offers Stop next to Start for a failed server that still has its container", () => {
    // it died while it was meant to run: the container is kept until someone presses Start or Stop
    expect(of("failed", "exited", "running")).toEqual({ start: true, restart: false, stop: false, dismiss: true, kill: false });
    // a crash loop: Docker is restarting the container
    expect(of("failed", "restarting", "running").dismiss).toBe(true);
  });

  it("offers Stop for a server that stopped by itself or waits for the start after a host shutdown", () => {
    expect(of("stopped", "exited", "running").dismiss).toBe(true);
  });

  it("does not offer Stop when there is nothing left to settle", () => {
    // stopped by the panel after a crash loop: already meant to be stopped, its container goes by itself
    expect(of("failed", "exited", "stopped").dismiss).toBe(false);
    // killed for lack of memory and stopped since: no container any more
    expect(of("failed", "", "stopped").dismiss).toBe(false);
    expect(of("failed", "", "running").dismiss).toBe(false);
  });

  it("keeps Kill available while a stop is in progress", () => {
    expect(of("stopping", "running")).toEqual({ start: true, restart: false, stop: false, dismiss: false, kill: true });
  });
});
