import { apiGet, apiPost } from "./client";

export type PlanPriority = "P0" | "P1" | "P2" | "P3";
export type PlanCreatePriority = Exclude<PlanPriority, "P0">;
export type PlanConfirmDecision = "complete" | "mark_succeeded" | "mark_failed" | "retry";
export type JsonObject = Record<string, unknown>;

export interface PlanAttempt {
  id: number;
  plan_id: number;
  step_id: number;
  attempt_no: number;
  idempotency_key: string;
  dispatch_state: string;
  car_task_id: string | null;
  request_json: JsonObject;
  accept_json: JsonObject | null;
  result_json: JsonObject | null;
  started_at: string | null;
  last_checked_at: string | null;
  finished_at: string | null;
  outcome: string | null;
}

export interface PlanStep {
  id: number;
  plan_id: number;
  seq: number;
  step_type: string;
  action: string | null;
  label: string;
  args_json: JsonObject;
  status: string;
  wait_kind: string | null;
  wake_at: string | null;
  map_name: string | null;
  target_json: JsonObject | null;
  tags_fingerprint: string | null;
  timeout_sec: number;
  retry_policy: string;
  max_attempts: number;
  started_at: string | null;
  finished_at: string | null;
  last_progress_at: string | null;
  last_error: string;
  attempts?: PlanAttempt[];
}

export interface PlanSummary {
  id: number;
  display_no: string;
  kind: string;
  title: string;
  priority: PlanPriority;
  preemption: string;
  status: string;
  owner_uid: string | null;
  source_kind: string;
  source_id: string | null;
  creator_uid: string | null;
  creator_role: string | null;
  creator_surface: string | null;
  report_json: JsonObject;
  current_step_id: number | null;
  wake_at: string | null;
  deadline_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface PlanDetail extends PlanSummary {
  steps: PlanStep[];
  attempts?: PlanAttempt[];
}

export interface PlanCounts {
  [status: string]: number;
}

export interface PlanListQuery {
  state?: string | string[];
  limit?: number;
  before_id?: number;
}

export interface PlanListResponse {
  ok: boolean;
  plans: PlanSummary[];
  counts: PlanCounts;
}

export interface PlanResponse {
  ok: boolean;
  plan: PlanDetail;
  /** 仅创建接口返回的人类可读摘要。 */
  summary?: string;
}

export interface PlanCreateInput {
  title: string;
  priority?: PlanCreatePriority;
  owner_uid?: string;
  steps: PlanCreateStep[];
  report?: PlanReport;
}

export interface PlanReport {
  notify?: boolean;
  speak_if_present?: boolean;
}

interface PlanCreateStepBase {
  type: "action" | "wait" | "manual";
  label?: string;
}

export type PlanActionStep = PlanCreateStepBase & (
  | { type: "action"; action: "robot_move"; args: {
    direction: "forward" | "back" | "left" | "right";
    distance_m: number;
  } }
  | { type: "action"; action: "robot_turn"; args: { angle_deg: number } }
  | { type: "action"; action: "robot_goto_point"; args: {
    x: number;
    y: number;
    yaw_deg?: number;
  } }
  | { type: "action"; action: "robot_goto_place"; args: { place: string } }
  | { type: "action"; action: "robot_goto_zone"; args: { zone: string } }
);

export type PlanWaitStep = PlanCreateStepBase & (
  | { type: "wait"; wait_kind: "time"; wake_at: string }
  | { type: "wait"; wait_kind: "device" | "external"; wake_at?: never }
);

export type PlanManualStep = PlanCreateStepBase & {
  type: "manual";
};

export type PlanCreateStep = PlanActionStep | PlanWaitStep | PlanManualStep;

export interface NurseUnlockResponse {
  ok: boolean;
  error?: string;
}

export function listPlans(query: PlanListQuery = {}): Promise<PlanListResponse> {
  const params = new URLSearchParams();
  if (query.state) {
    params.set("state", Array.isArray(query.state) ? query.state.join(",") : query.state);
  }
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.before_id !== undefined) params.set("before_id", String(query.before_id));
  const suffix = params.toString();
  return apiGet<PlanListResponse>(`/api/plans${suffix ? `?${suffix}` : ""}`);
}

export function getPlan(id: number): Promise<PlanResponse> {
  return apiGet<PlanResponse>(`/api/plans/${id}`);
}

export function createPlan(input: PlanCreateInput): Promise<PlanResponse> {
  return apiPost<PlanResponse>("/api/plans", input);
}

export function changePlanPriority(
  id: number,
  priority: PlanPriority,
  version: number,
): Promise<PlanResponse> {
  return apiPost<PlanResponse>(`/api/plans/${id}/priority`, { priority, version });
}

export function cancelPlan(id: number, reason: string, version: number): Promise<PlanResponse> {
  return apiPost<PlanResponse>(`/api/plans/${id}/cancel`, { reason, version });
}

export function confirmPlanStep(
  id: number,
  stepId: number,
  decision: PlanConfirmDecision,
  note: string,
  version: number,
): Promise<PlanResponse> {
  return apiPost<PlanResponse>(`/api/plans/${id}/confirm`, {
    step_id: stepId,
    decision,
    note,
    version,
  });
}

export function retryPlanStep(
  id: number,
  stepId: number,
  note: string,
  version: number,
): Promise<PlanResponse> {
  return apiPost<PlanResponse>(`/api/plans/${id}/retry`, {
    step_id: stepId,
    note,
    version,
  });
}

export function unlockNursePage(pin: string): Promise<NurseUnlockResponse> {
  return apiPost<NurseUnlockResponse>("/api/nurse/page-unlock", { pin });
}
