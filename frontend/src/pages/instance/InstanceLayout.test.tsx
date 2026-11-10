import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import { makeInstance } from "@/test/fixtures";
import { InstanceLayout } from "./InstanceLayout";

const SERVER = makeInstance({ status: "stopped", desired_state: "stopped", container_state: "" });

afterEach(() => vi.restoreAllMocks());

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.instance(SERVER.id), SERVER);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/servers/${SERVER.id}/files`]}>
        <Routes>
          <Route path="/servers/:id" element={<InstanceLayout />}>
            <Route path="files" element={<textarea aria-label="Draft" />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { qc, user: userEvent.setup() };
}

describe("InstanceLayout", () => {
  it("keeps the page and its unsaved state when a background poll fails", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(SERVER as never);
    const { qc, user } = setup();
    await user.type(screen.getByRole("textbox", { name: "Draft" }), "unsaved edit");

    get.mockRejectedValue(new ApiError(502, "http_502", "Cannot reach the panel API"));
    await act(async () => {
      await qc.refetchQueries({ queryKey: qk.instance(SERVER.id), exact: true });
    });
    expect(await screen.findByText(/Connection to the panel lost; retrying/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Draft" })).toHaveValue("unsaved edit");

    get.mockResolvedValue(SERVER as never);
    await user.click(screen.getByRole("button", { name: "Retry now" }));
    await waitFor(() => expect(screen.queryByText(/Connection to the panel lost/)).toBeNull());
    expect(screen.getByRole("textbox", { name: "Draft" })).toHaveValue("unsaved edit");
  });

  it("shows the error page when the server is gone", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(SERVER as never);
    const { qc } = setup();
    get.mockRejectedValue(new ApiError(404, "not_found", "Server not found."));
    await act(async () => {
      await qc.refetchQueries({ queryKey: qk.instance(SERVER.id), exact: true });
    });
    expect(await screen.findByText("Server not found.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Draft" })).toBeNull();
  });
});
