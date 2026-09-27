import { afterEach, describe, expect, it, vi } from "vitest";
import { existsSync, readFileSync } from "node:fs";
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
import {
  buildPlanCreateStep,
  canCancelPlan,
  canChangePlanPriority,
  canRetryPlanStep,
  planStepConfirmDecision,
  validatePlanStepDraft,
  type PlanStepDraft,
} from "../src/planUi";
import type { PlanStep } from "../src/api/plans";
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

describe("Plan 页面共享业务规则", () => {
  const step = (patch: Partial<PlanStep>): PlanStep => ({
    id: 11, plan_id: 7, seq: 1, step_type: "action", action: "robot_move",
    label: "步骤", args_json: {}, status: "pending", wait_kind: null, wake_at: null,
    map_name: null, target_json: null, tags_fingerprint: null, timeout_sec: 30,
    retry_policy: "none", max_attempts: 1, started_at: null, finished_at: null,
    last_progress_at: null, last_error: "", attempts: [], ...patch,
  });
  const draft = (patch: Partial<PlanStepDraft> = {}): PlanStepDraft => ({
    type: "action", label: "移动", action: "robot_move", direction: "forward",
    distance: 1, angle: 90, x: 0, y: 0, yaw: 0, target: "",
    waitKind: "device", wakeAt: "", ...patch,
  });

  it("终态与取消中禁止调级和取消", () => {
    for (const status of ["succeeded", "failed", "cancelled", "expired", "cancelling"]) {
      expect(canChangePlanPriority(status)).toBe(false);
      expect(canCancelPlan(status)).toBe(false);
    }
    expect(canChangePlanPriority("queued")).toBe(true);
    expect(canCancelPlan("running")).toBe(true);
  });

  it("确认入口严格匹配人工步骤与待复核状态", () => {
    expect(planStepConfirmDecision(step({ step_type: "manual", status: "pending" }))).toBe("complete");
    expect(planStepConfirmDecision(step({ step_type: "wait", wait_kind: "manual", status: "paused" }))).toBe("complete");
    expect(planStepConfirmDecision(step({ status: "needs_review" }))).toBe("mark_succeeded");
    expect(planStepConfirmDecision(step({ step_type: "wait", wait_kind: "time", status: "waiting" }))).toBeNull();
    expect(planStepConfirmDecision(step({ step_type: "action", status: "running" }))).toBeNull();
  });

  it("重试只放行待复核的 safe goto point", () => {
    const retryable = step({ status: "needs_review", action: "robot_goto_point", retry_policy: "safe_goto_only" });
    expect(canRetryPlanStep(retryable)).toBe(true);
    expect(canRetryPlanStep({ ...retryable, status: "failed" })).toBe(false);
    expect(canRetryPlanStep({ ...retryable, action: "robot_move" })).toBe(false);
  });

  it("结构化步骤构建与 admin 数值边界一致", () => {
    expect(validatePlanStepDraft(draft())).toBeNull();
    expect(buildPlanCreateStep(draft())).toEqual({
      type: "action", action: "robot_move", args: { direction: "forward", distance_m: 1 }, label: "移动",
    });
    expect(validatePlanStepDraft(draft({ distance: Number.NaN }))).toContain("0 到 5");
    expect(validatePlanStepDraft(draft({ action: "robot_turn", angle: 0 }))).toContain("0 到 360");
    expect(validatePlanStepDraft(draft({ action: "robot_goto_point", x: 1001 }))).toContain("1000");
    expect(validatePlanStepDraft(draft({ action: "robot_goto_place", target: " " }))).toContain("不能为空");
    expect(validatePlanStepDraft(draft({ type: "wait", waitKind: "time", wakeAt: "bad" }))).toContain("唤醒时间");
  });

  it.each([
    ["移动距离", { action: "robot_move", distance: "" }],
    ["移动距离空白", { action: "robot_move", distance: "   " }],
    ["移动距离数字字符串", { action: "robot_move", distance: "1" }],
    ["转向角度", { action: "robot_turn", angle: "" }],
    ["X 坐标", { action: "robot_goto_point", x: "" }],
    ["Y 坐标", { action: "robot_goto_point", y: "   " }],
    ["朝向", { action: "robot_goto_point", yaw: "0" }],
  ] as const)("%s 拒绝空值、空白或非 number 输入", (_label, patch) => {
    expect(validatePlanStepDraft(draft(patch))).not.toBeNull();
  });

  it("所有数字字段拒绝非有限 number，构建阶段也 fail closed", () => {
    expect(validatePlanStepDraft(draft({ action: "robot_move", distance: Infinity }))).not.toBeNull();
    expect(validatePlanStepDraft(draft({ action: "robot_turn", angle: Number.NaN }))).not.toBeNull();
    expect(validatePlanStepDraft(draft({ action: "robot_goto_point", x: -Infinity }))).not.toBeNull();
    expect(validatePlanStepDraft(draft({ action: "robot_goto_point", y: Number.NaN }))).not.toBeNull();
    expect(validatePlanStepDraft(draft({ action: "robot_goto_point", yaw: Infinity }))).not.toBeNull();
    expect(() => buildPlanCreateStep(draft({ action: "robot_goto_point", x: "" }))).toThrow();
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
    expect(source).toContain("planStepConfirmDecision");
    expect(source).toContain("canRetryPlanStep");
    expect(source).not.toContain("step.status === 'failed' || step.status === 'canceled'");
  });

  it("创建步骤校验动作数值范围和定时等待", () => {
    expect(source).toContain("validatePlanStepDraft");
    expect(source).toContain("buildPlanCreateStep");
  });

  it("添加步骤先显式校验，失败时不追加步骤也不调用 API", () => {
    const body = source.slice(source.indexOf("function addStep"), source.indexOf("function removeStep"));
    expect(body.indexOf("validatePlanStepDraft")).toBeGreaterThan(-1);
    expect(body.indexOf("return")).toBeGreaterThan(body.indexOf("validatePlanStepDraft"));
    expect(body.indexOf("createSteps.value.push")).toBeGreaterThan(body.indexOf("return"));
    expect(body).not.toContain("createPlan(");
  });

  it("窄屏当前计划长标题可收缩换行", () => {
    expect(source).toMatch(/\.current-line strong\s*\{[^}]*min-width:\s*0[^}]*overflow-wrap:\s*anywhere/);
  });

  it("创建失败显示后端原因（detail），不再只报 API 422 状态码", () => {
    expect(source).toContain("apiErrorMessage");
    const body = source.slice(source.indexOf("async function submitCreate"),
                              source.indexOf("function connectEvents"));
    expect(body).toContain("apiErrorMessage(error)");
    expect(body).not.toContain("error.message");
  });

  it("等待目标解析的步骤把后端原因显示出来", () => {
    expect(source).toContain("step.last_error");
  });
});

describe("护士台 PIN 门与 Plan 面板源码契约", () => {
  const nurseRoot = resolve(__dirname, "../../nurse/src");
  const pinPath = resolve(nurseRoot, "components/NursePinGate.vue");
  const panelPath = resolve(nurseRoot, "components/PlanPanel.vue");
  const appPath = resolve(nurseRoot, "App.vue");
  const sourceOf = (path: string) => existsSync(path) ? readFileSync(path, "utf8") : "";

  it("提供独立 PIN 门并只用当前 tab 的固定解锁标记", () => {
    expect(existsSync(pinPath)).toBe(true);
    const source = sourceOf(pinPath);
    expect(source).toContain("unlockNursePage");
    expect(source).toContain("nurse-page-unlocked-v1");
    expect(source).toContain("sessionStorage.setItem");
    expect(source).not.toContain("localStorage");
    expect(source).not.toMatch(/ttl|expires|expiry|setTimeout/i);
    expect(source.match(/sessionStorage\.setItem/g)).toHaveLength(1);
    expect(source).toMatch(/if \(!result\.ok\)[\s\S]*return;[\s\S]*sessionStorage\.setItem/);
    expect(source.indexOf("sessionStorage.setItem")).toBeGreaterThan(source.indexOf("result.ok"));
  });

  it("PlanPanel 通过 shared API 提供完整人工操作且不裸调接口", () => {
    expect(existsSync(panelPath)).toBe(true);
    const source = sourceOf(panelPath);
    for (const symbol of [
      "listPlans", "createPlan", "changePlanPriority", "cancelPlan",
      "confirmPlanStep", "retryPlanStep",
    ]) expect(source).toContain(symbol);
    expect(source).not.toContain('fetch("/api/plans');
    expect(source).not.toContain("X-Surface");
    expect(source).not.toContain("EventSource");
    expect(source).toContain("loadGeneration");
    expect(source).toContain("openGeneration");
    expect(source).toMatch(/requestId !== loadGeneration/);
    expect(source).toMatch(/requestId !== openGeneration/);
    expect(source).toMatch(/async function openPlan[\s\S]*loadGeneration \+= 1[\s\S]*loading\.value = false/);
  });

  it("两端共用人工状态矩阵和结构化步骤校验规则", () => {
    const admin = sourceOf(resolve(__dirname, "../../admin/src/pages/PlansPage.vue"));
    const nurse = sourceOf(panelPath);
    for (const symbol of [
      "buildPlanCreateStep", "planStepConfirmDecision", "canRetryPlanStep",
      "canCancelPlan", "canChangePlanPriority",
    ]) {
      expect(admin).toContain(symbol);
      expect(nurse).toContain(symbol);
    }
  });

  it("添加步骤先显式校验，失败时不追加步骤也不调用 API", () => {
    const source = sourceOf(panelPath);
    const body = source.slice(source.indexOf("function addStep"), source.indexOf("async function submitCreate"));
    expect(body.indexOf("validatePlanStepDraft")).toBeGreaterThan(-1);
    expect(body.indexOf("return")).toBeGreaterThan(body.indexOf("validatePlanStepDraft"));
    expect(body.indexOf("createSteps.value.push")).toBeGreaterThan(body.indexOf("return"));
    expect(body).not.toContain("createPlan(");
  });

  it("窄屏当前计划长标题可收缩换行", () => {
    const source = sourceOf(panelPath);
    expect(source).toMatch(/\.current strong\s*\{[^}]*min-width:\s*0[^}]*overflow-wrap:\s*anywhere/);
  });

  it("护士台创建失败同样显示后端原因，并显示等待解析的步骤原因", () => {
    const source = sourceOf(panelPath);
    expect(source).toContain("apiErrorMessage");
    const body = source.slice(source.indexOf("async function submitCreate"),
                              source.indexOf("watch("));
    expect(body).toContain("apiErrorMessage(error)");
    expect(body).not.toContain("error.message");
    expect(source).toContain("step.last_error");
  });

  it("App 在 PIN 门之后才挂载工作台、通知加载和唯一 SSE 连接", () => {
    const source = sourceOf(appPath);
    expect(source).toContain("NursePinGate");
    expect(source).toContain("PlanPanel");
    expect(source).toContain('v-if="!unlocked"');
    expect(source).toContain("startUnlockedWorkspace");
    expect(source).toMatch(/if \(!unlocked\.value\) return/);
    expect(source).toContain("loadNotices");
    expect(source).toContain("connect");
    expect(source).toContain("plan_created");
    expect(source).toContain("plan_needs_review");
    expect(source.match(/new EventSource/g)).toHaveLength(1);
    expect(source).toMatch(/function connect\(\)[\s\S]*!unlocked\.value[\s\S]*new EventSource/);
    expect(source).toContain("workspaceActive");
    expect(source).toMatch(/await loadNotices\(\);[\s\S]*if \(workspaceActive\) schedulePoll\(\)/);
  });
});
