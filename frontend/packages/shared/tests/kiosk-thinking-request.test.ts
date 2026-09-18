import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const kioskApp = readFileSync(
  fileURLToPath(new URL("../../kiosk/src/App.vue", import.meta.url)),
  "utf8",
);

describe("kiosk 思考档位请求", () => {
  it("发送聊天时使用当前选择的思考档位", () => {
    expect(kioskApp).toContain("thinking: thinkingMode.value");
    expect(kioskApp).not.toContain('thinking: "auto"');
  });
});
