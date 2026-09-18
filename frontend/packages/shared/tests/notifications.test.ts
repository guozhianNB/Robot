import { describe, it, expect } from "vitest";
import { parseBusPayload } from "../src/events";

// 护士台通知事件（规格 §4.5；简报 §6 要求补齐这四个用例）
describe("通知中心事件", () => {
  it("notification 正常解析（通知类型在 kind 里）", () => {
    const ev = parseBusPayload(JSON.stringify({
      type: "notification", id: 7, level: "critical", source: "kiosk", kind: "sos",
      uid: "elder_001", uid_name: "张建国 · 301床", title: "紧急呼叫",
      body: "老人说胸口疼", count: 3, last_at: "2026-09-18 10:00:00",
    }));
    expect(ev?.type).toBe("notification");
    if (ev?.type === "notification") {
      expect(ev.id).toBe(7);
      expect(ev.kind).toBe("sos");
      expect(ev.level).toBe("critical");
      expect(ev.uid_name).toBe("张建国 · 301床");
      expect(ev.count).toBe(3);
    }
  });

  it("notification_ack 正常解析（单条与全量）", () => {
    const one = parseBusPayload(JSON.stringify({ type: "notification_ack", id: 7, by: "admin" }));
    expect(one?.type).toBe("notification_ack");
    if (one?.type === "notification_ack") {
      expect(one.id).toBe(7);
      expect(one.by).toBe("admin");
    }
    const all = parseBusPayload(JSON.stringify({
      type: "notification_ack", all: true, by: "admin", n: 4,
    }));
    expect(all?.type).toBe("notification_ack");
    if (all?.type === "notification_ack") {
      expect(all.all).toBe(true);
      expect(all.n).toBe(4);
    }
  });

  it("未知 type 返回 null（容错不抛异常）", () => {
    expect(parseBusPayload('{"type":"notice","id":1}')).toBeNull();
    expect(parseBusPayload('{"type":"notification_new","id":1}')).toBeNull();
  });

  it("缺 kind 的 notification 不崩（字段由后端保证，前端不因缺字段丢事件）", () => {
    const ev = parseBusPayload('{"type":"notification","id":9}');
    expect(ev).not.toBeNull();
    expect(ev?.type).toBe("notification");
    if (ev?.type === "notification") {
      expect(ev.kind).toBeUndefined();
      expect(ev.level).toBeUndefined();
    }
  });

  it("D6 陷阱：事件 type 必须是 notification —— 通知类型绝不能占用 type 键", () => {
    // 后端 bus.publish 内部构造 {"type": event_type, **payload}；若 payload 用 type 承载
    // 通知类型（如 sos），事件类型会被覆盖成 "sos" → 前端整条丢弃。本用例锁死该不变量。
    const good = parseBusPayload('{"type":"notification","id":1,"kind":"sos","level":"critical"}');
    expect(good?.type).toBe("notification");
    const bad = parseBusPayload('{"type":"sos","id":1,"level":"critical"}');   // 旧写法（错的）
    expect(bad).toBeNull();
  });
});
