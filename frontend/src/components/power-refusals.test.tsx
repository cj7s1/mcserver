/**
 * Starts the panel refuses (security review 2026-11-11): "Start anyway" past the memory admission only
 * for administrators (D-4, I-5: 403 `overcommit_not_allowed` for anyone else), and a missing required
 * secret (E-4, I-3: 409 `secret_required`) pointing to Settings -> Game settings -> Secrets.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { mayOvercommit, sendPower } from "@/lib/power";
import { qk } from "@/lib/queries";
import type { AuthState, Instance, Job, PermissionCode } from "@/lib/types";
import { ServerQuickActions } from "@/pages/library/ServerActions";
import { makeInstance } from "@/test/fixtures";
import { JobProgress } from "./JobProgress";
import { MemoryOvercommitHost } from "./MemoryOvercommitDialog";
import { SecretRequiredHost, SecretRequiredJobHint } from "./SecretRequiredDialog";

const MESSAGE = "Starting Big Pack needs 14.5 GB; running servers already reserve 52 GB of 62 GB host memory.";
const noMemory = () => new ApiError(409, "insufficient_memory", MESSAGE, { needed_mb: 14848, reserved_mb: 53248, host_mb: 63488 });
const SECRET_MESSAGE = "Valheim needs its join password: set it under Game settings -> Secrets before starting the server.";
const noSecret = () => new ApiError(409, "secret_required", SECRET_MESSAGE, { key: "server_password", label: "Join password" });

const STOPPED = makeInstance({ id: "cccccccc-0000-0000-0000-000000000001", name: "Big Pack", status: "stopped", desired_state: "stopped" });

function auth(permissions: PermissionCode[]): AuthState {
  return {
    setup_required: false,
    authenticated: true,
    user: {
      id: 7,
      username: "helper",
      email: "",
      first_name: "",
      last_name: "",
      is_owner: false,
      role_name: "Operator",
      permissions,
      must_change_password: false,
      preferences: {},
    },
  };
}

let post: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    throw new Error(`unexpected GET ${path}`);
  }) as typeof api.get);
  post = vi.spyOn(api, "post");
});

afterEach(() => vi.restoreAllMocks());

function renderApp(ui: React.ReactNode, { state, instances }: { state?: AuthState; instances?: Instance[] } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  if (state) qc.setQueryData(qk.auth, state);
  for (const i of instances ?? []) qc.setQueryData(qk.instance(i.id), i);
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {ui}
        <MemoryOvercommitHost />
        <SecretRequiredHost />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { user };
}

describe("start anyway only for administrators (D-4)", () => {
  it("reads the global permission from the auth state", () => {
    expect(mayOvercommit(undefined)).toBeNull();
    expect(mayOvercommit({ setup_required: false, authenticated: false, user: null })).toBeNull();
    expect(mayOvercommit(auth(["instance.view", "instance.control"]))).toBe(false);
    expect(mayOvercommit(auth(["system.settings"]))).toBe(true);
  });

  it("a user without system.settings is told to ask an administrator and nothing is sent again", async () => {
    post.mockRejectedValueOnce(noMemory());
    const { user } = renderApp(<ServerQuickActions instance={STOPPED} />, { state: auth(["instance.view", "instance.control"]) });
    await user.click(screen.getByRole("button", { name: "Start Big Pack" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Not enough free memory to start Big Pack" });
    expect(within(dialog).getByText(MESSAGE)).toBeInTheDocument();
    expect(within(dialog).getByText(/Ask an administrator, or stop another server first/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Start anyway" })).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenLastCalledWith(`/instances/${STOPPED.id}/power`, { action: "start" });
    expect(screen.getByRole("button", { name: "Start Big Pack" })).toBeEnabled();
  });

  it("an administrator is offered Start anyway", async () => {
    post.mockRejectedValueOnce(noMemory()).mockResolvedValueOnce({ id: "job-1", title: "Start server" } as never);
    const { user } = renderApp(<ServerQuickActions instance={STOPPED} />, { state: auth(["system.settings", "instance.view"]) });
    await user.click(screen.getByRole("button", { name: "Start Big Pack" }));
    const dialog = await screen.findByRole("alertdialog", { name: /Not enough free memory/ });
    await user.click(within(dialog).getByRole("button", { name: "Start anyway" }));
    await waitFor(() => expect(post).toHaveBeenLastCalledWith(`/instances/${STOPPED.id}/power`, { action: "start", allow_overcommit: true }));
  });

  it("a 403 overcommit_not_allowed (permissions changed meanwhile) reaches the caller with its message", async () => {
    const refused = new ApiError(403, "overcommit_not_allowed", "Starting a server that does not fit into the node's memory needs an administrator");
    post.mockRejectedValueOnce(noMemory()).mockRejectedValueOnce(refused);
    const { user } = renderApp(null, { state: auth(["system.settings"]) });
    const started = sendPower(STOPPED, { action: "start" }).catch((err: unknown) => err);
    await user.click(await screen.findByRole("button", { name: "Start anyway" }));
    expect(await started).toBe(refused);
  });
});

describe("a start refused for a missing secret (E-4)", () => {
  it("names the secret and links to Settings -> Game settings; nothing else is reported", async () => {
    post.mockRejectedValueOnce(noSecret());
    const instance = makeInstance({ ...STOPPED, permissions: ["instance.view", "instance.control", "instance.configure"] });
    const { user } = renderApp(<ServerQuickActions instance={instance} />, { instances: [instance] });
    await user.click(screen.getByRole("button", { name: "Start Big Pack" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Big Pack cannot be started without its join password" });
    expect(within(dialog).getByText(SECRET_MESSAGE)).toBeInTheDocument();
    const link = within(dialog).getByRole("link", { name: "Open Secrets" });
    expect(link).toHaveAttribute("href", `/servers/${instance.id}/settings?tab=game-settings`);
    await user.click(link);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("a user who may not configure the server is told to ask someone who may", async () => {
    post.mockRejectedValueOnce(noSecret());
    const instance = makeInstance({ ...STOPPED, permissions: ["instance.view", "instance.control"] });
    const { user } = renderApp(<ServerQuickActions instance={instance} />, { instances: [instance] });
    await user.click(screen.getByRole("button", { name: "Start Big Pack" }));
    const dialog = await screen.findByRole("alertdialog", { name: /cannot be started without its join password/ });
    expect(within(dialog).getByText(/Ask someone who may configure this server/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("link")).not.toBeInTheDocument();
  });

  it("resolves as cancelled with the notice mounted (a restart says so)", async () => {
    post.mockRejectedValue(noSecret());
    // a restart: the notice says "restarted"
    renderApp(null);
    await expect(sendPower(STOPPED, { action: "restart" })).resolves.toEqual({ status: "cancelled" });
    expect(await screen.findByRole("alertdialog", { name: "Big Pack cannot be restarted without its join password" })).toBeInTheDocument();
  });

  it("without a mounted notice the 409 reaches the caller", async () => {
    post.mockRejectedValue(noSecret());
    await expect(sendPower(STOPPED, { action: "start" })).rejects.toMatchObject({ code: "secret_required", details: { key: "server_password" } });
  });

  it("a failed job that could not start the server for it links there too", () => {
    const job = { id: "j1", title: "Restart server", kind: "restart", status: "failed", progress: 1, message: "", error: SECRET_MESSAGE, error_code: "secret_required", instance: STOPPED.id } as Job;
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <JobProgress job={job} />
          <SecretRequiredJobHint job={{ error_code: "agent_error", instance: STOPPED.id }} />
          <SecretRequiredJobHint job={{ error_code: "secret_required", instance: null }} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const links = screen.getAllByRole("link", { name: "Set it under Settings → Game settings → Secrets" });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", `/servers/${STOPPED.id}/settings?tab=game-settings`);
  });
});
