/**
 * B1 Share card: the address, ports, protection, join steps and checklist of GET /instances/<id>/share, with copy
 * buttons; the password only for users who may configure the server, read on demand and never cached; a friend
 * sees the card without password and fix-it links; Minecraft "Add a friend".
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { qk } from "@/lib/queries";
import type { AuthState, Instance, PermissionCode } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { shareKey, type ShareAnswer } from "./share-api";
import { ShareButton, ShareCard } from "./ShareCard";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

const SECRET = "sesame-42";
const VALHEIM = (over: Partial<ShareAnswer> = {}): ShareAnswer => ({
  game: "valheim",
  game_label: "Valheim",
  address: { host: "play.example.org", source: "panel", note: "" },
  connect: "play.example.org:2456",
  ports: [
    { port: 2456, protocol: "udp", name: "game" },
    { port: 2457, protocol: "udp", name: "query" },
  ],
  protection: { kind: "password", on: true, label: "a join password" },
  steps: ["Start Valheim and choose a character.", "Join Game, Add Server: play.example.org:2456."],
  requirements: ["Valheim, the current version"],
  open_warning: "",
  invite_text: 'Join my Valheim server "Vikings"!\nAddress: play.example.org:2456\nPassword: ask me for it',
  invite_has_password: false,
  checklist: [
    { id: "running", ok: true, label: "The server is running", hint: "" },
    { id: "public_address", ok: true, label: "A public address is set", hint: "" },
    { id: "protection", ok: true, label: "Strangers are kept out by a join password", hint: "" },
    { id: "ports", ok: null, label: "Forward on your router: UDP 2456, UDP 2457", hint: "The panel does not change your router." },
  ],
  password_set: true,
  ...over,
});

const ALL: PermissionCode[] = ["instance.view", "instance.control", "instance.configure", "instance.players"];
const valheim = (over: Partial<Instance> = {}) =>
  makeInstance({ id: "5ae0a5ae-0000-0000-0000-000000000001", name: "Vikings", game: "valheim", game_label: "Valheim", permissions: ALL, ...over });

const auth = (permissions: PermissionCode[]): AuthState => ({
  setup_required: false,
  authenticated: true,
  user: { id: 1, username: "o", email: "", first_name: "", last_name: "", is_owner: false, role_name: null, permissions, must_change_password: false, preferences: {} },
});

let get: ReturnType<typeof vi.spyOn>;
let answer: ShareAnswer;
beforeEach(() => {
  answer = VALHEIM();
  get = vi.spyOn(api, "get").mockImplementation((async (path: string, query?: Record<string, unknown>) => {
    if (path.endsWith("/share")) {
      const a = structuredClone(answer);
      if (query?.with_password === "true") {
        a.invite_text = a.invite_text.replace("Password: ask me for it", `Password: ${SECRET}`);
        a.invite_has_password = true;
        a.password = SECRET;
      }
      return a;
    }
    return new Promise(() => {});
  }) as typeof api.get);
  vi.mocked(copyText).mockClear();
});
afterEach(() => vi.restoreAllMocks());

function renderCard(instance: Instance, globalPerms: PermissionCode[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, auth(globalPerms));
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter>
          <ShareCard instance={instance} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { qc, user: userEvent.setup() };
}

describe("the Share card", () => {
  it("shows the address, ports, protection, steps and checklist; copies the address", async () => {
    const { qc, user } = renderCard(valheim());
    expect(await screen.findByLabelText("Address friends type")).toHaveTextContent("play.example.org:2456");
    expect(screen.getByText("UDP 2456")).toBeInTheDocument();
    expect(screen.getByText("Protected by a join password.")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Join steps" })).getAllByRole("listitem")).toHaveLength(2);
    const checklist = screen.getByRole("list", { name: "Checklist" });
    expect(within(checklist).getAllByText("OK:")).toHaveLength(3);
    expect(within(checklist).getByText("Check yourself:")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Copy address" }));
    expect(copyText).toHaveBeenLastCalledWith("play.example.org:2456");
    expect(JSON.stringify(qc.getQueryData(shareKey(valheim().id)))).not.toContain(SECRET);
  });

  it("copies the password and the invite with it only on demand, for users who may configure", async () => {
    const { qc, user } = renderCard(valheim());
    await user.click(await screen.findByRole("button", { name: "Copy password" }));
    await waitFor(() => expect(copyText).toHaveBeenLastCalledWith(SECRET));
    expect(get).toHaveBeenLastCalledWith(`/instances/${valheim().id}/share`, { with_password: "true" });

    await user.click(screen.getByRole("button", { name: "Copy invite" }));
    await waitFor(() => expect(vi.mocked(copyText).mock.calls.at(-1)?.[0]).toContain("Password: ask me for it"));

    await user.click(screen.getByRole("checkbox", { name: "Include the password" }));
    await user.click(screen.getByRole("button", { name: "Copy invite" }));
    await waitFor(() => expect(vi.mocked(copyText).mock.calls.at(-1)?.[0]).toContain(`Password: ${SECRET}`));
    // nothing with the password stays in the cache
    expect(JSON.stringify(qc.getQueryCache().getAll().map((q) => q.state.data))).not.toContain(SECRET);
  });

  it("follows password_set: unknown when the password cannot be read now", async () => {
    answer = VALHEIM({ password_set: null });
    renderCard(valheim());
    expect(await screen.findByText("Protected by a join password? The panel could not check it right now.")).toBeInTheDocument();
    expect(screen.getByText("The join password could not be read right now.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy password" })).not.toBeInTheDocument();
  });

  it("marks failing items and links to where they are fixed", async () => {
    answer = VALHEIM({
      address: { host: null, source: null, note: "No public address is set: an administrator sets it in System settings." },
      connect: null,
      protection: { kind: "password", on: false, label: "a join password" },
      password_set: false,
      checklist: [
        { id: "running", ok: false, label: "The server is running", hint: "Start it before your friends join." },
        { id: "public_address", ok: false, label: "A public address is set", hint: "No public address is set." },
        { id: "protection", ok: false, label: "Strangers are kept out by a join password", hint: "Anyone can join." },
        { id: "ports", ok: null, label: "Forward on your router: UDP 2456", hint: "" },
      ],
    });
    renderCard(valheim(), ["system.settings"]);
    const checklist = await screen.findByRole("list", { name: "Checklist" });
    expect(within(checklist).getAllByText("Missing:")).toHaveLength(3);
    expect(within(checklist).getByText("Start it before your friends join.")).toBeInTheDocument();
    expect(within(checklist).getByRole("link", { name: "Set it in System settings" })).toHaveAttribute("href", "/admin/system");
    expect(within(checklist).getByRole("link", { name: "Set a password in Secrets" })).toHaveAttribute(
      "href",
      `/servers/${valheim().id}/settings?tab=game-settings`,
    );
    expect(screen.getByText("No join password is set.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy password" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Include the password" })).not.toBeInTheDocument();
    expect(screen.getByText(/No public address is set: an administrator/)).toBeInTheDocument();
  });

  it("a friend sees it without password and without fix-it links", async () => {
    const friendAnswer = VALHEIM({ checklist: [{ id: "public_address", ok: false, label: "A public address is set", hint: "Not set." }] });
    answer = friendAnswer;
    const { user } = renderCard(valheim({ limited: true, permissions: ["instance.control"] }));
    await user.click(await screen.findByRole("button", { name: "Copy invite" }));
    expect(copyText).toHaveBeenLastCalledWith(friendAnswer.invite_text);
    expect(screen.queryByRole("button", { name: "Copy password" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith(expect.anything(), { with_password: "true" });
  });

  it("Minecraft: adds a friend to the whitelist and offers to turn the whitelist on", async () => {
    answer = VALHEIM({
      game: "minecraft",
      game_label: "Minecraft",
      protection: { kind: "whitelist", on: false, label: "the whitelist" },
      open_warning: "Anyone with a Minecraft account can join while the whitelist is off.",
      password_set: false,
    });
    const post = vi.spyOn(api, "post").mockResolvedValue({ via: "file", output: "" } as never);
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ values: { "white-list": "true" } } as never);
    const mc = makeInstance({ id: "5ae0a5ae-0000-0000-0000-000000000002", name: "Lobby", permissions: ALL });
    const { user } = renderCard(mc);
    expect(await screen.findByText(/Anyone with a Minecraft account can join/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Add a friend (their Minecraft name)"), "Steve_42");
    await user.click(screen.getByRole("button", { name: "Add to whitelist" }));
    expect(post).toHaveBeenCalledWith(`/instances/${mc.id}/players`, { action: "whitelist_add", name: "Steve_42" });
    await user.click(screen.getByRole("button", { name: "Turn the whitelist on" }));
    expect(patch).toHaveBeenCalledWith(`/instances/${mc.id}/properties`, { values: { "white-list": "true" } });
  });

  it("Minecraft with online mode off: the checklist item, its fix link, and no whitelist offer", async () => {
    answer = VALHEIM({
      game: "minecraft",
      game_label: "Minecraft",
      protection: { kind: "whitelist", on: false, label: "the whitelist" },
      open_warning: "With online mode off anyone can join under any name.",
      password_set: false,
      checklist: [
        { id: "protection", ok: false, label: "Strangers are kept out by the whitelist", hint: "With online mode off anyone can join under any name." },
        { id: "online_mode", ok: false, label: "Players need a Minecraft account (online mode)", hint: "online-mode is false in server.properties." },
      ],
    });
    const mc = makeInstance({ id: "5ae0a5ae-0000-0000-0000-000000000003", name: "Lobby", permissions: ALL });
    renderCard(mc);
    const checklist = await screen.findByRole("list", { name: "Checklist" });
    expect(within(checklist).getByText("Players need a Minecraft account (online mode)")).toBeInTheDocument();
    expect(within(checklist).getAllByText("Missing:")).toHaveLength(2);
    expect(within(checklist).getByRole("link", { name: "Turn online mode on in Properties" })).toHaveAttribute(
      "href",
      `/servers/${mc.id}/settings?tab=properties`,
    );
    expect(screen.getByText(/^Open:/)).toBeInTheDocument();
    expect(screen.getByText(/Online mode is off: anyone can join under any name/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Turn the whitelist on" })).not.toBeInTheDocument();
  });

  it("the header's Share button opens the same content and reads it only while open", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(qk.auth, auth([]));
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter>
            <ShareButton instance={valheim()} />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(get).not.toHaveBeenCalled();
    await userEvent.setup().click(screen.getByRole("button", { name: "Share" }));
    const dialog = await screen.findByRole("dialog", { name: "Share Vikings" });
    expect(await within(dialog).findByLabelText("Address friends type")).toHaveTextContent("play.example.org:2456");
  });
});
