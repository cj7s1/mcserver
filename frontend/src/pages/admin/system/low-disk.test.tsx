/**
 * A-7 (security review 2026-11-11): the system setting `low_disk_space_gb` (0 = off) next to the memory
 * reserve, and the notification event `node.low_disk_space` among the events a channel can choose.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { Instance, SystemSettings } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { ChannelDialog } from "./ChannelDialog";
import { SettingsForm } from "./SettingsForm";

const SETTINGS = (over: Partial<SystemSettings["values"]> = {}): SystemSettings => ({
  values: {
    runtime_image_channel: "stable",
    default_memory_mb: 4096,
    port_range_start: null,
    port_range_end: null,
    default_bind_ip: "0.0.0.0",
    backup_default_retention: { keep_last: 10, keep_daily: 7, keep_weekly: 4, keep_monthly: 0 },
    pre_restore_backup: true,
    pre_update_backup: true,
    public_address: "",
    lan_address: "",
    timezone: "",
    memory_reserve_mb: 4096,
    ...over,
  },
  secrets_configured: {},
  env: { port_range_default: [25565, 25665] },
});

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
  return userEvent.setup();
}

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
  Element.prototype.hasPointerCapture ??= () => false;
});
afterEach(() => vi.restoreAllMocks());

describe("low disk space setting", () => {
  it("is edited next to the memory reserve and saved as a whole number", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue(SETTINGS({ low_disk_space_gb: 50 }) as never);
    const user = wrap(<SettingsForm settings={SETTINGS({ low_disk_space_gb: 20 })} />);
    const field = screen.getByLabelText("Warn when a node has less free disk space than (GB)");
    expect(field).toHaveValue("20");
    expect(screen.getByText(/0 turns it off\. Default 20 GB/)).toBeInTheDocument();
    await user.clear(field);
    await user.type(field, "50");
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith("/system/settings", { values: { low_disk_space_gb: 50 } });
  });

  it("refuses what is no whole number in range, and 0 means off", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue(SETTINGS({ low_disk_space_gb: 0 }) as never);
    const user = wrap(<SettingsForm settings={SETTINGS({ low_disk_space_gb: 20 })} />);
    const field = screen.getByLabelText(/less free disk space/);
    await user.clear(field);
    await user.type(field, "200000");
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(screen.getByText("At most 100,000 GB")).toBeInTheDocument();
    expect(patch).not.toHaveBeenCalled();
    await user.clear(field);
    await user.type(field, "0");
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith("/system/settings", { values: { low_disk_space_gb: 0 } });
  });

  it("an answer without the setting (older panel) shows no field and sends none", () => {
    wrap(<SettingsForm settings={SETTINGS()} />);
    expect(screen.queryByLabelText(/less free disk space/)).not.toBeInTheDocument();
  });
});

describe("the node event in a channel", () => {
  const EVENTS = [
    { id: "instance.failed", label: "Server failed" },
    { id: "node.low_disk_space", label: "Low disk space on a node (server or backup storage)" },
  ];

  it("can be chosen; a channel for one server says node events reach only channels for all servers", async () => {
    const servers: Instance[] = [makeInstance({ id: "s1", name: "Lobby" })];
    const user = wrap(<ChannelDialog open onOpenChange={() => {}} events={EVENTS} servers={servers} />);
    const box = screen.getByRole("checkbox", { name: /Low disk space on a node/ });
    await user.click(box);
    expect(box).toBeChecked();
    expect(screen.getByText("Events from every server are sent, including servers added later.")).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Servers" }));
    await user.click(await screen.findByRole("option", { name: "Lobby" }));
    expect(screen.getByText(/Node events \(low disk space\) belong to no server/)).toBeInTheDocument();
  });
});
