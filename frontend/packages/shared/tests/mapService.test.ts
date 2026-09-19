import { afterEach, describe, expect, it, vi } from "vitest";
import { getMapEditorService, startMapEditor, stopMapEditor } from "../src/api/mapService";

function stub(body: any = { ok: true, running: true, source: "managed", pid: 7, port: 8010 }) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** apiGet 不显式带 method（fetch 默认即 GET，见 client.test.ts 既有约定），统一取方法用于断言。 */
const methodOf = (init: any) => init.method ?? "GET";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("地图编辑器服务客户端", () => {
  it("状态查询走 GET 且带 X-Surface", async () => {
    const f = stub();
    await getMapEditorService("admin");
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/mapeditor/service");
    expect(methodOf(init)).toBe("GET");
    expect(init.headers["X-Surface"]).toBe("admin");
  });

  it("启动走 POST /start", async () => {
    const f = stub();
    await startMapEditor("admin");
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/mapeditor/service/start");
    expect(init.method).toBe("POST");
    expect(init.headers["X-Surface"]).toBe("admin");
  });

  it("停止走 POST /stop", async () => {
    const f = stub();
    await stopMapEditor("admin");
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/mapeditor/service/stop");
    expect(init.method).toBe("POST");
  });
});
