import type {
  PlanConfirmDecision,
  PlanCreateStep,
  PlanStep,
} from "./api/plans";

export const PLAN_PRIORITY_RANK: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };

const TERMINAL_PLAN_STATES = new Set(["succeeded", "failed", "cancelled", "expired"]);
const MANUAL_CONFIRM_STATES = new Set(["pending", "waiting", "paused"]);
const COORDINATE_LIMIT = 1000;

export interface PlanStepDraft {
  type: PlanCreateStep["type"];
  label: string;
  action: "robot_move" | "robot_turn" | "robot_goto_point" | "robot_goto_place" | "robot_goto_zone";
  direction: "forward" | "back" | "left" | "right";
  distance: number;
  angle: number;
  x: number;
  y: number;
  yaw: number;
  target: string;
  waitKind: "time" | "device" | "external";
  wakeAt: string;
}

export function planStatusLabel(status: string): string {
  return ({
    queued: "排队", running: "执行中", waiting: "等待中", paused: "暂停",
    needs_review: "待复核", cancelling: "取消中", succeeded: "已完成",
    failed: "失败", cancelled: "已取消", expired: "已过期",
  } as Record<string, string>)[status] ?? status;
}

export function canChangePlanPriority(status: string): boolean {
  return !TERMINAL_PLAN_STATES.has(status) && status !== "cancelling";
}

export function canCancelPlan(status: string): boolean {
  return canChangePlanPriority(status);
}

export function planStepConfirmDecision(step: PlanStep): PlanConfirmDecision | null {
  if (step.status === "needs_review") return "mark_succeeded";
  const manual = step.step_type === "manual"
    || (step.step_type === "wait" && step.wait_kind === "manual");
  return manual && MANUAL_CONFIRM_STATES.has(step.status) ? "complete" : null;
}

export function canRetryPlanStep(step: PlanStep): boolean {
  return step.status === "needs_review"
    && step.step_type === "action"
    && step.action === "robot_goto_point"
    && step.retry_policy === "safe_goto_only";
}

export function validatePlanStepDraft(draft: PlanStepDraft): string | null {
  if (draft.type === "wait" && draft.waitKind === "time") {
    if (!draft.wakeAt.trim() || Number.isNaN(Date.parse(draft.wakeAt))) {
      return "定时等待需要填写合法的唤醒时间";
    }
  }
  if (draft.type !== "action") return null;
  if (draft.action === "robot_move") {
    const distance = Number(draft.distance);
    if (!Number.isFinite(distance) || !(distance > 0 && distance <= 5)) {
      return "移动距离必须是 0 到 5 米之间的有限数字";
    }
  }
  if (draft.action === "robot_turn") {
    const angle = Number(draft.angle);
    if (!Number.isFinite(angle) || !(Math.abs(angle) > 0 && Math.abs(angle) <= 360)) {
      return "转向角度必须是 0 到 360 度之间的有限数字";
    }
  }
  if (draft.action === "robot_goto_point") {
    const coordinates = [Number(draft.x), Number(draft.y), Number(draft.yaw)];
    if (!coordinates.every(Number.isFinite)
      || coordinates.slice(0, 2).some((value) => Math.abs(value) > COORDINATE_LIMIT)
      || Math.abs(coordinates[2]) > 360) {
      return `坐标必须是有限数字，范围不超过 ±${COORDINATE_LIMIT}，朝向不超过 ±360 度`;
    }
  }
  if (["robot_goto_place", "robot_goto_zone"].includes(draft.action) && !draft.target.trim()) {
    return "目标地点或区域不能为空";
  }
  return null;
}

export function buildPlanCreateStep(draft: PlanStepDraft): PlanCreateStep {
  const label = draft.label.trim() || "计划步骤";
  if (draft.type === "manual") return { type: "manual", label };
  if (draft.type === "wait") {
    if (draft.waitKind === "time") {
      return { type: "wait", wait_kind: "time", wake_at: draft.wakeAt, label };
    }
    return { type: "wait", wait_kind: draft.waitKind, label };
  }
  if (draft.action === "robot_move") {
    return { type: "action", action: draft.action,
      args: { direction: draft.direction, distance_m: Number(draft.distance) }, label };
  }
  if (draft.action === "robot_turn") {
    return { type: "action", action: draft.action, args: { angle_deg: Number(draft.angle) }, label };
  }
  if (draft.action === "robot_goto_point") {
    return { type: "action", action: draft.action,
      args: { x: Number(draft.x), y: Number(draft.y), yaw_deg: Number(draft.yaw) }, label };
  }
  if (draft.action === "robot_goto_place") {
    return { type: "action", action: draft.action, args: { place: draft.target.trim() }, label };
  }
  return { type: "action", action: draft.action, args: { zone: draft.target.trim() }, label };
}
