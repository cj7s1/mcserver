import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { SecretsResponse } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { SecretsSection } from "./game-settings-tab";

function setup(secrets: SecretsResponse) {
  const instance = makeInstance({ game: "satisfactory" } as never);
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false }, mutations: { retry: false } } });
  qc.setQueryData(qk.secrets(instance.id), secrets);
  const put = vi.spyOn(api, "put").mockResolvedValue(secrets as never);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SecretsSection instance={instance} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { put, instance, user: userEvent.setup() };
}

afterEach(() => vi.restoreAllMocks());

describe("SecretsSection: clearing a secret that opens the server (review 2026-11-10, item 9)", () => {
  it("asks first and sends the explicit access choice", async () => {
    const { put, instance, user } = setup({
      secrets: [{ key: "client_password", label: "Join password", set: true, clear_opens: true }],
    });
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(put).not.toHaveBeenCalled();
    expect(await screen.findByText(/open the server\?/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open the server" }));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith(`/instances/${instance.id}/secrets`, { values: { client_password: null }, access: "open" }),
    );
  });

  it("clears other secrets at once, without an access choice", async () => {
    const { put, instance, user } = setup({ secrets: [{ key: "token", label: "Token", set: true }] });
    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith(`/instances/${instance.id}/secrets`, { values: { token: null } }));
  });
});
