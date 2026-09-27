import { describe, it, expect, vi, afterEach } from "vitest";
import { ApiError, apiDelete, apiErrorMessage, apiGet, apiPost } from "../src/api/client";

afterEach(() => vi.restoreAllMocks());

describe("apiErrorMessage 取后端原因", () => {
  it("优先取 FastAPI HTTPException 的字符串 detail", () => {
    const error = new ApiError(422, "/api/plans", { detail: "robot_goto_place 目标解析失败：当前地图未知" });
    expect(apiErrorMessage(error)).toBe("robot_goto_place 目标解析失败：当前地图未知");
  });

  it("把校验错误数组拼成一句话", () => {
    const error = new ApiError(422, "/api/plans", {
      detail: [{ msg: "Field required", loc: ["body", "title"] }, { msg: "Input should be a valid array" }],
    });
    expect(apiErrorMessage(error)).toContain("Field required");
    expect(apiErrorMessage(error)).toContain("Input should be a valid array");
    expect(apiErrorMessage(error)).toContain("参数不合法");
  });

  it("没有可用 detail 时退回 ApiError.message，非 ApiError 退回传入兜底", () => {
    expect(apiErrorMessage(new ApiError(500, "/api/plans", {}))).toBe("API 500: /api/plans");
    expect(apiErrorMessage(new Error("炸了"))).toBe("炸了");
    expect(apiErrorMessage(null, "未知错误")).toBe("未知错误");
  });
});

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

  it("apiDelete 发送 DELETE 与端槽位", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await apiDelete("/api/wards/ward_101", "admin");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/wards/ward_101");
    expect(init.method).toBe("DELETE");
    expect(init.headers["X-Surface"]).toBe("admin");
  });

  it("非 ok 响应抛错", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 500, json: async () => ({}),
    }));
    await expect(apiGet("/api/xxx")).rejects.toThrow();
  });

  it("非 ok 响应保留状态、地址和 JSON body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ ok: false, error: "version_conflict", plan: { version: 4 } }),
    }));

    try {
      await apiPost("/api/plans/7/priority", { priority: "P1", version: 3 });
      throw new Error("expected apiPost to reject");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({
        status: 409,
        url: "/api/plans/7/priority",
        body: { ok: false, error: "version_conflict", plan: { version: 4 } },
      });
    }
  });
});
