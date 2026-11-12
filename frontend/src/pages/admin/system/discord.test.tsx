/**
 * B3 Discord: System → Discord (write-only token, server id, default roles and announcement channel as lists
 * once the bot is connected and as ids before, the bot's state and the invite link) and each server's
 * Settings → General → Discord (available, own roles, share the password).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState, Instance, PermissionCode } from "@/lib/types";
import { DiscordServerSection } from "@/pages/instance/settings/discord-section";
import { makeInstance } from "@/test/fixtures";
import { describeState, idsError, parseIds, type DiscordSettings } from "./discord-model";
import { DiscordSection } from "./DiscordSection";

const ROLE = "123456789012345678";
const ROLE2 = "223456789012345678";
const GUILD = "987654321098765432";
const CHANNEL = "555555555555555555";

const settings = (over: Partial<DiscordSettings> = {}, status: Partial<DiscordSettings["status"]> = {}): DiscordSettings => ({
  enabled: false,
  guild_id: "",
  roles: [],
  channel_id: "",
  token_set: false,
  ...over,
  status: {
    state: "disabled",
    connected: false,
    last_error: "",
    updated_at: null,
    bot_name: "",
    guild: null,
    guild_roles: [],
    guild_channels: [],
    application_id: null,
    invite_url: null,
    ...status,
  },
});

const auth = (permissions: PermissionCode[]): AuthState => ({
  setup_required: false,
  authenticated: true,
  user: { id: 1, username: "o", email: "", first_name: "", last_name: "", is_owner: true, role_name: null, permissions, must_change_password: false, preferences: {} },
});

let current: DiscordSettings;
beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
  Element.prototype.hasPointerCapture ??= () => false;
});
beforeEach(() => {
  current = settings();
  vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    if (path === "/system/discord") return structuredClone(current);
    return new Promise(() => {});
  }) as typeof api.get);
});
afterEach(() => vi.restoreAllMocks());

function wrap(ui: React.ReactNode, perms: PermissionCode[] = ["system.settings"]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, auth(perms));
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("discord model", () => {
  it("reads ids and says the state in words", () => {
    expect(parseIds(` ${ROLE}, ${ROLE2} ${ROLE}`)).toEqual([ROLE, ROLE2]);
    expect(idsError([ROLE])).toBeNull();
    expect(idsError(["12"])).toMatch(/not a Discord id/);
    expect(describeState(settings({}, { state: "connected", bot_name: "MCPanel", guild: { id: GUILD, name: "Friends" } }).status).label).toBe(
      "Connected as MCPanel in Friends",
    );
    expect(describeState(settings({}, { state: "error", last_error: "Discord refused the bot token" }).status)).toEqual({
      label: "Error: Discord refused the bot token",
      tone: "error",
    });
    expect(describeState(settings({}, { state: "no_token" }).status).label).toBe("Needs the bot token");
  });
});

describe("System → Discord", () => {
  it("before the bot connects: token, server id and role ids typed in; saved together", async () => {
    const patch = vi.spyOn(api, "patch").mockImplementation((async (_p: string, body: Record<string, unknown>) => {
      current = settings({ enabled: true, guild_id: GUILD, roles: [ROLE], token_set: true }, { state: "connecting" });
      expect(body).toEqual({ enabled: true, guild_id: GUILD, roles: [ROLE], token: "secret-bot-token" });
      return structuredClone(current);
    }) as never);
    const user = wrap(<DiscordSection />);
    expect(await screen.findByRole("status", { name: "Bot state" })).toHaveTextContent("Off");
    expect(screen.getByText("· not set")).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Bot on" }));
    await user.type(screen.getByLabelText(/Bot token/), "secret-bot-token");
    await user.type(screen.getByLabelText("Discord server id"), "123");
    expect(screen.getByText("A Discord server id is 15 to 22 digits")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save/ })).toBeDisabled();
    await user.clear(screen.getByLabelText("Discord server id"));
    await user.type(screen.getByLabelText("Discord server id"), GUILD);
    await user.type(screen.getByLabelText("Role ids"), ROLE);
    await user.click(screen.getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("· set")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Bot state" })).toHaveTextContent("Connecting to Discord");
    expect(screen.getByLabelText(/Bot token/)).toHaveValue("");
  });

  it("once connected: roles and channel from the Discord server's lists, the invite link, clear the token", async () => {
    current = settings(
      { enabled: true, guild_id: GUILD, roles: [ROLE], token_set: true },
      {
        state: "connected",
        connected: true,
        bot_name: "MCPanel",
        guild: { id: GUILD, name: "Friends" },
        guild_roles: [
          { id: ROLE, name: "Admins" },
          { id: ROLE2, name: "Players" },
        ],
        guild_channels: [{ id: CHANNEL, name: "general" }],
        application_id: "111111111111111111",
        invite_url: "https://discord.com/oauth2/authorize?client_id=111111111111111111&scope=bot+applications.commands&permissions=0",
      },
    );
    const patch = vi.spyOn(api, "patch").mockImplementation((async (_p: string, body: Record<string, unknown>) => {
      const next = structuredClone(current);
      if ("roles" in body) next.roles = body.roles as string[];
      if ("token" in body) next.token_set = false;
      return next;
    }) as never);
    const user = wrap(<DiscordSection />);
    expect(await screen.findByRole("status", { name: "Bot state" })).toHaveTextContent("Connected as MCPanel in Friends");
    expect(screen.getByRole("link", { name: /Invite the bot/ })).toHaveAttribute("href", current.status.invite_url);
    expect(screen.getByRole("checkbox", { name: "Admins" })).toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "Players" }));
    await user.click(screen.getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(patch).toHaveBeenLastCalledWith("/system/discord", { roles: [ROLE, ROLE2] }));
    await user.click(screen.getByRole("button", { name: /Clear/ }));
    await waitFor(() => expect(patch).toHaveBeenLastCalledWith("/system/discord", { token: "" }));
    expect(screen.getByRole("combobox", { name: /Announcement channel/ })).toHaveTextContent("None");
  });

  it("the panel's 400 is shown at its field", async () => {
    vi.spyOn(api, "patch").mockRejectedValue(new ApiError(400, "validation_error", "Some fields are invalid.", { fields: { channel_id: ["Not a channel id"] } }));
    const user = wrap(<DiscordSection />);
    await user.type(await screen.findByLabelText(/Announcement channel/), "555555555555555555");
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(await screen.findByText("Not a channel id")).toBeInTheDocument();
  });
});

describe("a server's Discord settings", () => {
  const server = (over: Partial<Instance> = {}) =>
    makeInstance({ discord_available: false, discord_roles: [], discord_share_password: false, permissions: ["instance.view", "instance.configure"], ...over });

  it("switches it on, with own roles and the password for /join (with a warning)", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue(server({ discord_available: true }) as never);
    const user = wrap(<DiscordServerSection instance={server()} canEdit />, []);
    await user.click(screen.getByRole("switch", { name: "Available in Discord" }));
    await user.type(screen.getByLabelText("Discord role ids for this server"), "12");
    expect(screen.getByText(/“12” is not a Discord id/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Discord role ids for this server"));
    await user.type(screen.getByLabelText("Discord role ids for this server"), ROLE);
    await user.click(screen.getByRole("switch", { name: "Let /join show the password" }));
    expect(screen.getByText(/will be able to read this server's join password/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${server().id}`, { discord_available: true, discord_share_password: true, discord_roles: [ROLE] });
    // a user without system.settings never reads the bot's settings
    expect(api.get).not.toHaveBeenCalledWith("/system/discord");
  });

  it("an administrator picks the roles by name", async () => {
    current = settings({}, { state: "connected", guild_roles: [{ id: ROLE, name: "Admins" }] });
    const user = wrap(<DiscordServerSection instance={server()} canEdit />);
    await user.click(await screen.findByRole("checkbox", { name: "Admins" }));
    const patch = vi.spyOn(api, "patch").mockResolvedValue(server() as never);
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${server().id}`, { discord_roles: [ROLE] });
    expect(within(screen.getByRole("group", { name: /Roles that may start and stop it/ })).getAllByRole("checkbox")).toHaveLength(1);
  });

  it("an older panel's answer shows nothing", () => {
    wrap(<DiscordServerSection instance={makeInstance()} canEdit />);
    expect(screen.queryByText("Available in Discord")).not.toBeInTheDocument();
  });
});
