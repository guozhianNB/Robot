// 思考档位（手动阶梯）—— 与后端 `/api/chat` 的 `thinking` 字段、settings.thinking_mode 一一对应。
// 规格：docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md
//
// 五档：auto 自动（照思考路由）/ none 不思考 / low 轻度 / high 中度 / max 重度。
// 后端把 low|high|max 映射成 DeepSeek 的 reasoning_effort；`none` 仍保留敏感词安全网
// （命中敏感/健康/情绪词的问题照旧加深，关不掉）。
export type ThinkingMode = "auto" | "none" | "low" | "high" | "max";

/** 档位顺序（下拉/按钮的展示顺序，从省到费）。 */
export const THINKING_MODE_ORDER: readonly ThinkingMode[] =
  ["auto", "none", "low", "high", "max"] as const;

/** 中文短标签（按钮/下拉文字）。 */
export const THINKING_MODE_LABEL: Record<ThinkingMode, string> = {
  auto: "自动",
  none: "不思考",
  low: "轻度",
  high: "中度",
  max: "重度",
};

/** 长说明（tooltip / 设置页小字）。 */
export const THINKING_MODE_HINT: Record<ThinkingMode, string> = {
  auto: "自动：日常问题秒回，健康/敏感/复杂问题自动加深思考",
  none: "不思考：最快；健康/敏感问题仍会自动加深（安全网关不掉）",
  low: "轻度：每次都先想一句再答，速度与稳妥兼顾",
  high: "中度：默认深想（推荐处理健康/用药类问题）",
  max: "重度：最慢最稳，复杂推理或重要决定时用",
};

/** 旧三档值兼容（缓存里的老前端 bundle / 老设置里可能还留着 on/off）。 */
const LEGACY: Record<string, ThinkingMode> = { on: "high", off: "none" };

/** 解析后端/设置里拿到的任意值 → 合法档位（坏值一律回落 auto，不抛异常）。 */
export function normalizeThinkingMode(value: unknown): ThinkingMode {
  if (typeof value === "string") {
    if ((THINKING_MODE_ORDER as readonly string[]).includes(value)) return value as ThinkingMode;
    if (LEGACY[value]) return LEGACY[value];
  }
  return "auto";
}

/** 循环切到下一档：auto → none → low → high → max → auto（按钮点一下换一档）。 */
export function nextThinkingMode(mode: ThinkingMode): ThinkingMode {
  const i = THINKING_MODE_ORDER.indexOf(mode);
  return THINKING_MODE_ORDER[(i + 1) % THINKING_MODE_ORDER.length]!;
}

/** 按钮文字：`🧠 思考：中度`。 */
export function thinkingModeLabel(mode: ThinkingMode): string {
  return `🧠 思考：${THINKING_MODE_LABEL[mode]}`;
}
