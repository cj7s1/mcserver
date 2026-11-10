import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PowerActionDialog } from "@/components/PowerActionDialog";
import { api, ApiError } from "@/lib/api";
import { isInsufficientMemoryError, sendPower } from "@/lib/power";
import { powerToast } from "@/lib/power-warning";
import { ServerQuickActions } from "@/pages/library/ServerActions";
import { makeInstance } from "@/test/fixtures";
import { MemoryOvercommitHost } from "./MemoryOvercommitDialog";

const MESSAGE = "Starting Big Pack needs 14.5 GB; running servers already reserve 52 GB of 62 GB host memory.";
const refused = () => new ApiError(409, "insufficient_memory", MESSAGE, { needed_mb: 14848, reserved_mb: 53248, host_mb: 63488 });

const STOPPED = makeInstance({ id: "bbbbbbbb-0000-0000-0000-000000000001", name: "Big Pack", status: "stopped", desired_state: "stopped" });
const RUNNING = makeInstance({ id: "bbbbbbbb-0000-0000-0000-000000000002", name: "Big Pack", status: "running" });

let post: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    throw new Error(`unexpected GET ${path}`);
  }) as typeof api.get);
  post = vi.spyOn(api, "post");
});

afterEach(() => vi.restoreAllMocks());

function renderWithHost(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {ui}
        <MemoryOvercommitHost />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { user };
}

describe("memory admission (409 insufficient_memory)", () => {
  it("recognises only a 409 with the stable code", () => {
    expect(isInsufficientMemoryError(refused())).toBe(true);
    expect(isInsufficientMemoryError(new ApiError(409, "instance_busy", "busy"))).toBe(false);
    expect(isInsufficientMemoryError(new ApiError(400, "insufficient_memory", "?"))).toBe(false);
    expect(isInsufficientMemoryError(new Error(MESSAGE))).toBe(false);
  });

  it("asks before starting anyway and repeats the same request with allow_overcommit", async () => {
    post.mockRejectedValueOnce(refused()).mockResolvedValueOnce({ id: "job-1", title: "Start server" } as never);
    const { user } = renderWithHost(<ServerQuickActions instance={STOPPED} />);
    await user.click(screen.getByRole("button", { name: "Start Big Pack" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Not enough free memory to start Big Pack" });
    expect(within(dialog).getByText(MESSAGE)).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenLastCalledWith(`/instances/${STOPPED.id}/power`, { action: "start" });

    await user.click(within(dialog).getByRole("button", { name: "Start anyway" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post).toHaveBeenLastCalledWith(`/instances/${STOPPED.id}/power`, { action: "start", allow_overcommit: true });
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("sends nothing more when the user cancels", async () => {
    post.mockRejectedValueOnce(refused());
    const { user } = renderWithHost(<ServerQuickActions instance={STOPPED} />);
    await user.click(screen.getByRole("button", { name: "Start Big Pack" }));
    const dialog = await screen.findByRole("alertdialog", { name: /Not enough free memory/ });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(post).toHaveBeenCalledTimes(1);
    // the Start button is usable again
    expect(screen.getByRole("button", { name: "Start Big Pack" })).toBeEnabled();
  });

  it("covers a restart from the Stop/Restart dialog, keeping its warning fields", async () => {
    localStorage.setItem("mcpanel.power.warnMinutes", "5");
    post.mockRejectedValueOnce(refused()).mockResolvedValueOnce({ id: "job-2", title: "Restart server (5 min warning)" } as never);
    const { user } = renderWithHost(<PowerActionDialog instance={RUNNING} action="restart" open onOpenChange={() => {}} />);
    const confirm = screen.getByRole("alertdialog", { name: "Restart Big Pack?" });
    await user.click(within(confirm).getByRole("button", { name: "Restart in 5 min" }));
    const ask = await screen.findByRole("alertdialog", { name: "Not enough free memory to restart Big Pack" });
    await user.click(within(ask).getByRole("button", { name: "Start anyway" }));
    await waitFor(() =>
      expect(post).toHaveBeenLastCalledWith(`/instances/${RUNNING.id}/power`, { action: "restart", warn_minutes: 5, allow_overcommit: true }),
    );
  });

  it("without a confirmation host (or for stop/kill) the error reaches the caller unchanged", async () => {
    post.mockRejectedValue(refused());
    await expect(sendPower(STOPPED, { action: "start" })).rejects.toBeInstanceOf(ApiError);
    await expect(sendPower(STOPPED, { action: "stop" })).rejects.toMatchObject({ code: "insufficient_memory" });
    // an explicit overcommit that is still refused is not asked about again
    await expect(sendPower(STOPPED, { action: "start", allow_overcommit: true })).rejects.toMatchObject({ status: 409 });
  });

  it("an older panel that never answers 409 works as before", async () => {
    post.mockResolvedValueOnce({ id: "job-3" } as never);
    await expect(sendPower(STOPPED, { action: "start" })).resolves.toEqual({ status: "done", data: { id: "job-3" }, overcommitted: false });
  });
});

describe("power toast", () => {
  it("never promises a countdown the backend may skip", () => {
    expect(powerToast("Lobby", "Restart", 10, { title: "Restart server (10 min warning)" })).toBe(
      "Restart of Lobby requested; anyone online gets a 10 min warning first",
    );
    // the server was not up, so the backend acted at once
    expect(powerToast("Lobby", "Restart", 10, { title: "Restart server" })).toBe("Restart requested for Lobby");
    expect(powerToast("Lobby", "Stop", 0, null)).toBe("Stop requested for Lobby");
  });
});

describe("the same confirmation for the other requests that start a server", () => {
  it("repeats an update with allow_overcommit after Start anyway, and says so when declined", async () => {
    const { postOrDecline, OvercommitDeclined } = await import("@/lib/power");
    const { user } = renderWithHost(null);
    const path = `/instances/${STOPPED.id}/software`;
    post.mockImplementation((async (_p: string, body?: Record<string, unknown>) => {
      if (!body?.allow_overcommit) throw refused();
      return { id: "job-1" };
    }) as typeof api.post);
    const started = postOrDecline(path, STOPPED, { minecraft_version: "1.21.1", start: true });
    await user.click(await screen.findByRole("button", { name: "Start anyway" }));
    await expect(started).resolves.toEqual({ id: "job-1" });
    expect(post).toHaveBeenLastCalledWith(path, { minecraft_version: "1.21.1", start: true, allow_overcommit: true });

    post.mockClear();
    const declined = postOrDecline(path, STOPPED, { minecraft_version: "1.21.1", start: true }).catch((err: unknown) => err);
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await declined).toBeInstanceOf(OvercommitDeclined);
    expect(post).toHaveBeenCalledTimes(1);
  });
});
