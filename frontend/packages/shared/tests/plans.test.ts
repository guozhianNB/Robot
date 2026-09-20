import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  cancelPlan,
  changePlanPriority,
  confirmPlanStep,
  createPlan,
  getPlan,
  listPlans,
  retryPlanStep,
  unlockNursePage,
} from "../src/api/plans";
import type {
  PlanCreateInput,
  PlanCreateResponse,
  PlanConflictResponse,
  PlanDetail,
} from "../src/api/plans";
import { ApiError } from "../src/api/client";
import { parseBusPayload } from "../src/events";
import type { PlanUpdatedEvent } from "../src/events";
import { listPlans as listPlansFromBarrel } from "../src";

function stub(body: unknown = { ok: true }) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Plan REST 客户端", () => {
  it("从 shared barrel 导出 Plan API", () => {
    expect(listPlansFromBarrel).toBe(listPlans);
  });

  it("类型覆盖创建接口尚无 attempt 的 Plan 快照与 summary", () => {
    type CreatedHasAttempts = "attempts" extends keyof PlanCreateResponse["plan"] ? true : false;
    type CreatedStepHasAttempts = "attempts" extends keyof PlanCreateResponse["plan"]["steps"][number]
      ? true
      : false;
    type DetailAttemptsRequired = undefined extends PlanDetail["attempts"] ? false : true;
    const createdHasAttempts: CreatedHasAttempts = false;
    const createdStepHasAttempts: CreatedStepHasAttempts = false;
    const detailAttemptsRequired: DetailAttemptsRequired = true;
    expect(createdHasAttempts).toBe(false);
    expect(createdStepHasAttempts).toBe(false);
    expect(detailAttemptsRequired).toBe(true);
  });

  it("创建输入类型拒绝 P0、执行字段和非法 report", () => {
    const valid: PlanCreateInput = {
      title: "去护士站",
      priority: "P1",
      steps: [{ type: "manual", label: "护士确认" }],
      report: { notify: true, speak_if_present: false },
    };
    // @ts-expect-error Task 7 创建入口拒绝 P0。
    const p0: PlanCreateInput = { title: "越权", priority: "P0", steps: valid.steps };
    const injected: PlanCreateInput = {
      title: "注入",
      steps: [{
        type: "manual",
        label: "确认",
        // @ts-expect-error 执行状态不能从创建请求注入。
        status: "succeeded",
      }],
    };
    const badReport: PlanCreateInput = {
      title: "错误回报",
      steps: valid.steps,
      report: {
        // @ts-expect-error report 只接受两个布尔开关。
        notify: "yes",
      },
    };
    expect([valid, p0, injected, badReport]).toHaveLength(4);
  });

  it("按状态分页查询且不附加 X-Surface", async () => {
    const fetchMock = stub({ ok: true, plans: [], counts: {} });
    await listPlans({ state: ["queued", "running"], limit: 20, before_id: 9 });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/plans?state=queued%2Crunning&limit=20&before_id=9");
    expect(init.headers["X-Surface"]).toBeUndefined();
  });

  it("读取详情", async () => {
    const fetchMock = stub({ ok: true, plan: { id: 7 } });
    await getPlan(7);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/plans/7");
  });

  it("创建请求只发送后端白名单字段", async () => {
    const fetchMock = stub({ ok: true, plan: { id: 7 } });
    const payload: PlanCreateInput = {
      title: "去护士站",
      priority: "P1",
      owner_uid: "elder_001",
      steps: [{ type: "manual", label: "护士确认" }],
      report: { notify: true, speak_if_present: false },
    };
    await createPlan(payload);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/plans");
    expect(init.body).toBe(JSON.stringify(payload));
    expect(init.headers["X-Surface"]).toBeUndefined();
  });

  it("修改优先级携带 version", async () => {
    const fetchMock = stub();
    await changePlanPriority(7, "P1", 3);
    expect(fetchMock).toHaveBeenCalledWith("/api/plans/7/priority", expect.objectContaining({
      method: "POST", body: JSON.stringify({ priority: "P1", version: 3 }),
    }));
  });

  it("取消携带 reason 与 version", async () => {
    const fetchMock = stub();
    await cancelPlan(7, "不再需要", 4);
    expect(fetchMock.mock.calls[0][1].body).toBe(
      JSON.stringify({ reason: "不再需要", version: 4 }),
    );
  });

  it("确认步骤携带 decision、note 与 version", async () => {
    const fetchMock = stub();
    await confirmPlanStep(7, 11, "mark_succeeded", "现场确认", 5);
    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({
      step_id: 11, decision: "mark_succeeded", note: "现场确认", version: 5,
    }));
  });

  it("重试步骤携带 note 与 version", async () => {
    const fetchMock = stub();
    await retryPlanStep(7, 11, "障碍已清除", 6);
    expect(fetchMock.mock.calls[0][1].body).toBe(
      JSON.stringify({ step_id: 11, note: "障碍已清除", version: 6 }),
    );
  });

  it("护士台解锁只发送 PIN", async () => {
    const fetchMock = stub({ ok: true });
    await unlockNursePage("2468");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/nurse/page-unlock");
    expect(init.body).toBe(JSON.stringify({ pin: "2468" }));
    expect(init.headers["X-Surface"]).toBeUndefined();
  });

  it("409 仍 reject 且暴露 latest Plan", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        ok: false,
        error: "version_conflict",
        plan: { id: 7, version: 4, status: "queued", steps: [], attempts: [] },
      }),
    }));

    try {
      await changePlanPriority(7, "P1", 1);
      throw new Error("expected changePlanPriority to reject");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      const apiError = error as ApiError<PlanConflictResponse>;
      expect(apiError.message).toBe("API 409: /api/plans/7/priority");
      expect(apiError.status).toBe(409);
      expect(apiError.body?.error).toBe("version_conflict");
      expect(apiError.body?.plan.version).toBe(4);
    }
  });
});

describe("Plan SSE 契约", () => {
  it("类型接受后端当前不含 kind 的真实事件 payload", () => {
    const event: PlanUpdatedEvent = {
      type: "plan_updated",
      plan_id: 7,
      version: 3,
      plan: { id: 7, display_no: "PL-0007", status: "queued", priority: "P1" },
      step: null,
    };
    expect(event.type).toBe("plan_updated");
  });

  it.each([
    "plan_created",
    "plan_updated",
    "plan_step_changed",
    "plan_needs_review",
  ] as const)("接受 %s 事件并保留 kind", (type) => {
    const event = parseBusPayload(JSON.stringify({
      type,
      kind: "manual",
      plan_id: 7,
      version: 3,
      plan: { id: 7, display_no: "PL-0007", status: "queued", priority: "P1" },
      step: type === "plan_step_changed" ? { id: 11, status: "running" } : null,
    }));
    expect(event?.type).toBe(type);
    if (event?.type === type) expect(event.kind).toBe("manual");
  });

  it("未知事件仍返回 null", () => {
    expect(parseBusPayload('{"type":"plan_unknown","kind":"manual"}')).toBeNull();
  });

  it("业务分类不能覆盖总线 type", () => {
    expect(parseBusPayload('{"type":"manual","plan_id":7}')).toBeNull();
  });
});

describe("管理台 Plan 页面源码契约", () => {
  const source = readFileSync(resolve(__dirname, "../../admin/src/pages/PlansPage.vue"), "utf8");

  it("通过 shared Plan API 提供创建、调级、取消、确认和重试入口", () => {
    for (const symbol of [
      "listPlans", "createPlan", "changePlanPriority", "cancelPlan",
      "confirmPlanStep", "retryPlanStep",
    ]) expect(source).toContain(symbol);
    expect(source).not.toContain('fetch("/api/plans');
  });

  it("人工创建只暴露结构化动作和字段，不接收原始 JSON", () => {
    for (const field of ["robot_move", "robot_turn", "robot_goto_point", "robot_goto_place", "robot_goto_zone", "wait", "manual"])
      expect(source).toContain(field);
    expect(source).not.toMatch(/textarea[\\s\\S]*json|v-model[^\\n]*json/i);
  });

  it("包含四类 Plan SSE 事件、断线重连和卸载关闭", () => {
    for (const type of ["plan_created", "plan_updated", "plan_step_changed", "plan_needs_review"])
      expect(source).toContain(type);
    expect(source).toContain("parseBusPayload");
    expect(source).toContain("3000");
    expect(source).toContain("onUnmounted");
    expect(source).toContain("close");
  });

  it("冲突错误读取 ApiError.body.plan", () => {
    expect(source).toContain("ApiError");
    expect(source).toContain("body?.plan");
  });

  it("按后端状态矩阵展示人工确认和重试入口", () => {
    for (const status of ["pending", "waiting", "paused", "needs_review", "cancelling", "cancelled", "expired"])
      expect(source).toContain(status);
    expect(source).toContain('"complete"');
    expect(source).toContain("safe_goto_only");
    expect(source).not.toContain("step.status === 'failed' || step.status === 'canceled'");
  });

  it("创建步骤校验动作数值范围和定时等待", () => {
    for (const token of ["Number.isFinite", "distance_m", "5", "360", "wakeAt", "isNaN"])
      expect(source).toContain(token);
  });
});
