import { describe, it, expect } from "vitest";
import { parseBusEvent, parseBusPayload } from "../src/events";

describe("parseBusEvent", () => {
  it("解析 reminder 事件", () => {
    const ev = parseBusEvent(
      'data: {"type":"reminder","id":1,"uid":"elder_001","title":"吃药"}'
    );
    expect(ev?.type).toBe("reminder");
    if (ev?.type === "reminder") {
      expect(ev.uid).toBe("elder_001");
    }
  });

  it("解析 user_changed 事件", () => {
    const ev = parseBusEvent(
      'data: {"type":"user_changed","uid":"elder_002","locked":true,"source":"manual"}'
    );
    expect(ev?.type).toBe("user_changed");
    if (ev?.type === "user_changed") {
      expect(ev.uid).toBe("elder_002");
      expect(ev.locked).toBe(true);
    }
  });

  it("解析 voice_state 事件", () => {
    const ev = parseBusEvent(
      'data: {"type":"voice_state","state":"listening"}'
    );
    expect(ev?.type).toBe("voice_state");
  });

  it("忽略心跳注释行", () => {
    expect(parseBusEvent(": keep-alive")).toBeNull();
  });

  it("未知类型返回 null 不抛异常", () => {
    expect(parseBusEvent('data: {"type":"unknown_event","x":1}')).toBeNull();
  });
});

describe("parseBusPayload（EventSource msg.data 场景，已剥离 data: 前缀）", () => {
  it("解析纯 JSON payload", () => {
    const ev = parseBusPayload(
      '{"type":"reminder","id":1,"uid":"elder_001","title":"吃药"}'
    );
    expect(ev?.type).toBe("reminder");
  });

  it("坏 JSON 返回 null 不抛异常", () => {
    expect(parseBusPayload("not-json{")).toBeNull();
  });

  it("未知类型返回 null", () => {
    expect(parseBusPayload('{"type":"unknown_event"}')).toBeNull();
  });
});
