/**
 * After the user's own password change the session key rotates and sockets opened before it are
 * closed with 4401 although the tab stays signed in: both live sockets reconnect then, and only
 * then (a real sign-out leaves them closed).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConsoleSocket } from "@/pages/instance/console/use-console-socket";
import { api } from "./api";
import { useLiveEvents } from "./events";
import { qk, useAuthState } from "./queries";
import type { AuthState } from "./types";

class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static all: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeSocket.all.push(this);
  }
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  send() {}
  close() {
    this.readyState = FakeSocket.CLOSED;
  }
  serverClose(code: number) {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.({ code });
  }
}

const SIGNED_IN: AuthState = {
  setup_required: false,
  authenticated: true,
  user: {
    id: 1,
    username: "colton",
    email: "",
    first_name: "",
    last_name: "",
    is_owner: true,
    role_name: null,
    permissions: [],
    must_change_password: false,
    preferences: {},
  },
};
const SIGNED_OUT: AuthState = { setup_required: false, authenticated: false, user: null };

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function client() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, SIGNED_IN);
  return qc;
}

beforeEach(() => {
  FakeSocket.all = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe.each([
  // useAuthState: the auth gate's observer, so invalidating the auth state refetches it
  [
    "live events",
    (qc: QueryClient) =>
      renderHook(
        () => {
          useAuthState();
          return useLiveEvents(true);
        },
        { wrapper: wrapperFor(qc) },
      ),
  ],
  [
    "console",
    (qc: QueryClient) =>
      renderHook(
        () => {
          useAuthState();
          return useConsoleSocket("abc", () => {});
        },
        { wrapper: wrapperFor(qc) },
      ),
  ],
])("%s socket after a 4401 close", (_name, mount) => {
  it("reconnects while the tab is still signed in (own password change)", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(SIGNED_IN as never);
    const qc = client();
    const view = mount(qc);
    act(() => FakeSocket.all[0].open());
    act(() => FakeSocket.all[0].serverClose(4401));
    await settle();
    expect(get).toHaveBeenCalledWith("/auth/state");
    await settle(1_100);
    expect(FakeSocket.all).toHaveLength(2);
    view.unmount();
  });

  it("stays closed when the session really ended", async () => {
    vi.spyOn(api, "get").mockResolvedValue(SIGNED_OUT as never);
    const qc = client();
    const view = mount(qc);
    act(() => FakeSocket.all[0].open());
    act(() => FakeSocket.all[0].serverClose(4401));
    await settle(60_000);
    expect(FakeSocket.all).toHaveLength(1);
    expect(qc.getQueryData(qk.auth)).toEqual(SIGNED_OUT);
    view.unmount();
  });

  it("does not retry a 4403 (access revoked)", async () => {
    vi.spyOn(api, "get").mockResolvedValue(SIGNED_IN as never);
    const qc = client();
    const view = mount(qc);
    act(() => FakeSocket.all[0].open());
    act(() => FakeSocket.all[0].serverClose(4403));
    await settle(60_000);
    expect(FakeSocket.all).toHaveLength(1);
    view.unmount();
  });
});
