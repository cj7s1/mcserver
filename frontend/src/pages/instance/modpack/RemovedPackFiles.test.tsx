import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { makeInstance } from "@/test/fixtures";
import { RemovedPackFiles } from "./RemovedPackFiles";

const PACK = makeInstance({ is_modpack: true, server_type: "AUTO_CURSEFORGE" });

afterEach(() => vi.restoreAllMocks());

function show(info: Record<string, unknown>, canManage = true) {
  vi.spyOn(api, "get").mockResolvedValue({ supported: true, release: "5001", applies: true, restart_required: true, ...info } as never);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <RemovedPackFiles instance={PACK} canManage={canManage} locked={false} />
    </QueryClientProvider>,
  );
}

describe("removed pack files on the Modpack tab", () => {
  it("lists the record and lets the pack reinstall the files", async () => {
    show({ paths: ["mods/a.jar", "mods/b.jar"] });
    expect(await screen.findByText("mods/a.jar")).toBeInTheDocument();
    const del = vi.spyOn(api, "delete").mockResolvedValue({
      supported: true,
      release: "5001",
      applies: true,
      restart_required: true,
      paths: [],
      cleared: ["mods/a.jar", "mods/b.jar"],
    } as never);
    // the record is gone afterwards (the tab reads it again)
    vi.mocked(api.get).mockResolvedValue({ supported: true, release: "5001", applies: true, restart_required: true, paths: [] } as never);
    await userEvent.click(screen.getByRole("button", { name: /Let the pack reinstall them/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Reinstall them" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith(`/instances/${PACK.id}/modpack/removed-files`));
    await waitFor(() => expect(screen.queryByText("mods/a.jar")).toBeNull());
  });

  it("shows nothing without a record, and no action without the permission", async () => {
    const { unmount } = show({ paths: [] });
    await waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(screen.queryByText("Removed pack files")).toBeNull();
    unmount();
    show({ paths: ["mods/a.jar"] }, false);
    expect(await screen.findByText("mods/a.jar")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reinstall/i })).toBeNull();
  });
});
