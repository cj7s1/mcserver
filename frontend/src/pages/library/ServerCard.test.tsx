import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { RESTART_REQUIRED_NOTE, restartRequiredText } from "@/lib/restart-required";
import type { Instance, Job } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { ServerCard } from "./ServerCard";
import { cardNote } from "./server-utils";

const RESUME = "Stopped because the host was shutting down; it is started again automatically";

function show(over: Partial<Instance>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ServerCard instance={makeInstance(over)} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("cardNote", () => {
  it("says why a stopped server is down", () => {
    expect(cardNote({ status: "stopped", status_detail: RESUME, restart_required: false })).toEqual({ text: RESUME, tone: "muted" });
  });

  it("puts the reason before the restart hint (after an upgrade every server reads restart-required)", () => {
    expect(cardNote({ status: "stopped", status_detail: RESUME, restart_required: true })?.text).toBe(RESUME);
    expect(cardNote({ status: "failed", status_detail: "Crash loop", restart_required: true })).toEqual({ text: "Crash loop", tone: "error" });
  });

  it("falls back to the restart hint, and to nothing", () => {
    expect(cardNote({ status: "stopped", status_detail: "", restart_required: true })).toEqual({ text: RESTART_REQUIRED_NOTE, tone: "warning" });
    expect(cardNote({ status: "running", status_detail: "Health check unavailable; readiness not verified", restart_required: true })?.text).toBe(
      RESTART_REQUIRED_NOTE,
    );
    expect(cardNote({ status: "running", status_detail: "Health check unavailable; readiness not verified", restart_required: false })).toBeNull();
    expect(cardNote({ status: "stopped", status_detail: "", restart_required: false })).toBeNull();
  });
});

describe("ServerCard", () => {
  it("shows that a server stopped for a host shutdown is started again automatically", () => {
    show({ status: "stopped", status_detail: RESUME, restart_required: true });
    expect(screen.getByText(RESUME)).toBeInTheDocument();
    expect(screen.queryByText(RESTART_REQUIRED_NOTE)).toBeNull();
  });

  it("shows the restart hint with wording that also fits an MCPanel upgrade", () => {
    show({ status: "running", restart_required: true });
    expect(screen.getByText(RESTART_REQUIRED_NOTE)).toBeInTheDocument();
    expect(RESTART_REQUIRED_NOTE).toMatch(/changed settings or a newer MCPanel version/);
  });

  it("shows a failed server's reason and keeps a running job in front of every note", () => {
    const { unmount } = show({ status: "failed", status_detail: "Crash loop: container is restarting (exit code 1)" });
    expect(screen.getByText(/Crash loop/)).toHaveClass("text-destructive");
    unmount();
    const job = { id: "j1", kind: "start", title: "Start server", status: "running", progress: 0.3, message: "Starting container" } as unknown as Job;
    show({ status: "stopped", status_detail: RESUME, restart_required: true, active_job: job });
    expect(screen.getByText("Starting container")).toBeInTheDocument();
    expect(screen.queryByText(RESUME)).toBeNull();
  });
});

describe("restartRequiredText", () => {
  it("is true for changed settings and for a container made by an older MCPanel version", () => {
    for (const status of ["running", "stopped", "failed", "unhealthy"] as const) {
      const text = restartRequiredText({ status });
      expect(text).toMatch(/changed settings or a newer MCPanel version/);
      expect(text).toMatch(/World data is kept/);
      expect(text).not.toMatch(/Configuration changed/);
    }
    expect(restartRequiredText({ status: "stopped" })).toMatch(/the next time the server starts/);
    expect(restartRequiredText({ status: "running" })).toMatch(/^Restart required/);
  });
});
