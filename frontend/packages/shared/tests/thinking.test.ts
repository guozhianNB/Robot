import { describe, it, expect } from "vitest";
import {
  THINKING_MODE_ORDER, nextThinkingMode, normalizeThinkingMode,
  thinkingModeLabel, THINKING_MODE_LABEL, THINKING_MODE_HINT, type ThinkingMode,
} from "../src/thinking";
import { parseBusPayload } from "../src/events";

describe("思考档位（五档阶梯）", () => {
  it("顺序 auto → none → low → high → max → auto", () => {
    expect(nextThinkingMode("auto")).toBe("none");
    expect(nextThinkingMode("none")).toBe("low");
    expect(nextThinkingMode("low")).toBe("high");
    expect(nextThinkingMode("high")).toBe("max");
    expect(nextThinkingMode("max")).toBe("auto");
  });

  it("顺序表覆盖五档且无重复", () => {
    expect([...THINKING_MODE_ORDER].sort()).toEqual(["auto", "high", "low", "max", "none"]);
    expect(new Set(THINKING_MODE_ORDER).size).toBe(5);
  });

  it("中文标签就是用户口径：自动/不思考/轻度/中度/重度", () => {
    expect(THINKING_MODE_ORDER.map((m: ThinkingMode) => THINKING_MODE_LABEL[m]))
      .toEqual(["自动", "不思考", "轻度", "中度", "重度"]);
    expect(thinkingModeLabel("high")).toBe("🧠 思考：中度");
    // 每档都要有 tooltip，别出现 undefined
    for (const m of THINKING_MODE_ORDER) expect(THINKING_MODE_HINT[m]).toBeTruthy();
  });

  it("旧三档值 on/off 兼容成 high/none（老前端 bundle、老设置）", () => {
    expect(normalizeThinkingMode("on")).toBe("high");
    expect(normalizeThinkingMode("off")).toBe("none");
  });

  it("坏值一律回落 auto（后端设置被写坏也不许抛异常）", () => {
    for (const bad of [undefined, null, "", "ON", "ultra", "true", 1, {}]) {
      expect(normalizeThinkingMode(bad)).toBe("auto");
    }
    expect(normalizeThinkingMode("max")).toBe("max");
  });
});

describe("chat_reasoning 总线事件（思维链上屏）", () => {
  it("是已知事件类型，可被 parseBusPayload 解析", () => {
    const ev = parseBusPayload(
      '{"type":"chat_reasoning","uid":"elder_001","delta":"先看血压"}'
    );
    expect(ev?.type).toBe("chat_reasoning");
    if (ev?.type === "chat_reasoning") {
      expect(ev.uid).toBe("elder_001");
      expect(ev.delta).toBe("先看血压");
    }
  });
});
