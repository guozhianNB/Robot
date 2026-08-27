import { describe, it, expect, vi, afterEach } from "vitest";
import { apiGet, apiPost } from "../src/api/client";

afterEach(() => vi.restoreAllMocks());

describe("REST client", () => {
  it("apiGet 解析 JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ ok: true, uid: "elder_001" }),
    }));
    const res = await apiGet("/api/session/user");
    expect(res.uid).toBe("elder_001");
    expect(fetch).toHaveBeenCalledWith("/api/session/user", expect.any(Object));
  });

  it("apiPost 发送 JSON body", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await apiPost("/api/session/user", { uid: "elder_002", locked: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/session/user");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ uid: "elder_002", locked: true });
  });

  it("非 ok 响应抛错", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 500, json: async () => ({}),
    }));
    await expect(apiGet("/api/xxx")).rejects.toThrow();
  });
});
