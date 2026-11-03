import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, errorMessage } from "./api";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => new Response(body === undefined ? null : JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "mcpanel_csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("api client", () => {
  it("sends the CSRF token on unsafe methods only", async () => {
    document.cookie = "mcpanel_csrftoken=tok123";
    const fn = mockFetch(200, { ok: true });
    await api.post("/instances/x/power", { action: "start" });
    const [, init] = fn.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CSRFToken"]).toBe("tok123");
    await api.get("/instances/", { archived: "all", empty: "" });
    const [url, init2] = fn.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("/api/instances/?archived=all");
    expect((init2.headers as Record<string, string>)["X-CSRFToken"]).toBeUndefined();
  });

  it("turns backend errors into ApiError with code, message and field errors", async () => {
    mockFetch(400, {
      error: { code: "validation_error", message: "Some fields are invalid.", details: { fields: { name: ["Required"] } } },
    });
    const err = (await api.post("/instances/", {}).catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(400);
    expect(err.code).toBe("validation_error");
    expect(err.fieldErrors).toEqual({ name: "Required" });
    expect(errorMessage(err)).toBe("name: Required");
  });

  it("reports conflicts with their stable code", async () => {
    mockFetch(409, { error: { code: "instance_busy", message: "Another operation is in progress", details: {} } });
    const err = (await api.post("/instances/x/power", { action: "stop" }).catch((e) => e)) as ApiError;
    expect(err.code).toBe("instance_busy");
    expect(errorMessage(err)).toBe("Another operation is in progress");
  });

  it("handles 204 and network failures", async () => {
    mockFetch(204, undefined);
    await expect(api.post("/auth/logout")).resolves.toBeUndefined();
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    const err = (await api.get("/auth/state").catch((e) => e)) as ApiError;
    expect(err.code).toBe("network_error");
  });
});
