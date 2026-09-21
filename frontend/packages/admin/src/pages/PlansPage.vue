<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  ApiError,
  buildPlanCreateStep,
  canCancelPlan,
  canChangePlanPriority,
  canRetryPlanStep,
  cancelPlan,
  changePlanPriority,
  confirmPlanStep,
  createPlan,
  getPlan,
  listPlans,
  parseBusPayload,
  planStatusLabel,
  planStepConfirmDecision,
  PLAN_PRIORITY_RANK,
  retryPlanStep,
  type PlanCreateStep,
  type PlanDetail,
  type PlanPriority,
  type PlanStep,
  type PlanStepDraft,
  type PlanSummary,
  validatePlanStepDraft,
} from "shared";

const plans = ref<PlanSummary[]>([]);
const currentPlan = ref<PlanDetail | null>(null);
const selectedStatus = ref("all");
const expandedId = ref<number | null>(null);
const loading = ref(false);
const saving = ref(false);
const errorText = ref("");
const showCreate = ref(false);

const createForm = ref({ title: "", priority: "P1" as Exclude<PlanPriority, "P0">,
  ownerUid: "", notify: true, speak: false });
const stepForm = ref<PlanStepDraft>({ type: "action", label: "",
  action: "robot_move", direction: "forward",
  distance: 1, angle: 90, x: 0, y: 0, yaw: 0, target: "", waitKind: "device" as "time" | "device" | "external", wakeAt: "" });
const createSteps = ref<PlanCreateStep[]>([]);

let eventSource: EventSource | null = null;
let reconnectTimer: number | null = null;

const orderedPlans = computed(() => [...plans.value].sort((a, b) =>
  (PLAN_PRIORITY_RANK[a.priority] ?? 9) - (PLAN_PRIORITY_RANK[b.priority] ?? 9)
  || Date.parse(b.updated_at || b.created_at) - Date.parse(a.updated_at || a.created_at)));

function priorityLabel(priority: string) { return priority; }

function formatTime(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function setLatest(plan: PlanDetail) {
  currentPlan.value = plan;
  plans.value = plans.value.map((item) => item.id === plan.id ? { ...item, ...plan } : item);
}

function conflictPlan(error: unknown): PlanDetail | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const body = error.body as { plan?: PlanDetail } | undefined;
  return body?.plan ?? null;
}

async function load() {
  loading.value = true;
  try {
    const query = selectedStatus.value === "all" ? {} : { state: selectedStatus.value };
    const result = await listPlans(query);
    plans.value = result.plans ?? [];
    const preferred = currentPlan.value && plans.value.some((p) => p.id === currentPlan.value?.id)
      ? currentPlan.value.id
      : orderedPlans.value.find((p) => p.status === "running" || p.status === "needs_review")?.id
        ?? orderedPlans.value[0]?.id;
    if (preferred !== undefined) {
      const detail = await getPlan(preferred);
      if (detail.plan) currentPlan.value = detail.plan;
    } else currentPlan.value = null;
    errorText.value = "";
  } catch (error) {
    errorText.value = `读取计划失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    loading.value = false;
  }
}

async function openPlan(id: number) {
  expandedId.value = expandedId.value === id ? null : id;
  if (expandedId.value !== id) return;
  try {
    const result = await getPlan(id);
    if (result.plan) setLatest(result.plan);
  } catch (error) {
    errorText.value = `读取计划详情失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function runMutation(action: () => Promise<{ plan: PlanDetail }>) {
  saving.value = true;
  try {
    const result = await action();
    if (result.plan) setLatest(result.plan);
    await load();
  } catch (error) {
    const latest = conflictPlan(error);
    if (latest) {
      setLatest(latest);
      errorText.value = "计划已被其他窗口修改，已刷新最新版本。请重新操作。";
    } else {
      errorText.value = `操作失败：${error instanceof Error ? error.message : String(error)}`;
    }
  } finally {
    saving.value = false;
  }
}

function planFor(id: number) { return currentPlan.value?.id === id ? currentPlan.value : null; }

async function updatePriority(plan: PlanSummary, event: Event) {
  const priority = (event.target as HTMLSelectElement).value as PlanPriority;
  if (priority === plan.priority) return;
  await runMutation(() => changePlanPriority(plan.id, priority, plan.version));
}

async function cancel(id: number) {
  const detail = planFor(id);
  const summary = plans.value.find((item) => item.id === id);
  const version = detail?.version ?? summary?.version;
  if (version === undefined || !window.confirm("确认取消此计划？")) return;
  const reason = window.prompt("取消原因（可选）", "管理员取消") ?? "管理员取消";
  await runMutation(() => cancelPlan(id, reason, version));
}

async function confirmStep(step: PlanStep) {
  const detail = planFor(step.plan_id);
  if (!detail || !window.confirm(`确认步骤“${step.label}”已完成？`)) return;
  const decision = planStepConfirmDecision(step);
  if (!decision) return;
  await runMutation(() => confirmPlanStep(detail.id, step.id, decision, "管理员确认", detail.version));
}

async function retryStep(step: PlanStep) {
  const detail = planFor(step.plan_id);
  if (!detail || !window.confirm(`确认重试步骤“${step.label}”？`)) return;
  await runMutation(() => retryPlanStep(detail.id, step.id, "管理员重试", detail.version));
}

function addStep() {
  const validationError = validatePlanStepDraft(stepForm.value);
  if (validationError) { errorText.value = validationError; return; }
  createSteps.value.push(buildPlanCreateStep(stepForm.value));
  stepForm.value.label = "";
  errorText.value = "";
}

function removeStep(index: number) { createSteps.value.splice(index, 1); }

async function submitCreate() {
  if (!createForm.value.title.trim()) { errorText.value = "请填写计划标题"; return; }
  if (!createSteps.value.length) { errorText.value = "请至少添加一个结构化步骤"; return; }
  const validationError = validatePlanStepDraft(stepForm.value);
  if (validationError) { errorText.value = validationError; return; }
  saving.value = true;
  try {
    await createPlan({ title: createForm.value.title.trim(), priority: createForm.value.priority,
      owner_uid: createForm.value.ownerUid.trim() || undefined, steps: createSteps.value,
      report: { notify: createForm.value.notify, speak_if_present: createForm.value.speak } });
    showCreate.value = false;
    createForm.value.title = "";
    createSteps.value = [];
    await load();
  } catch (error) {
    errorText.value = `创建失败：${error instanceof Error ? error.message : String(error)}`;
  } finally { saving.value = false; }
}

function connectEvents() {
  if (eventSource) return;
  const source = new EventSource("/api/events");
  eventSource = source;
  source.onopen = () => { void load(); };
  source.onmessage = (message) => {
    const event = parseBusPayload(message.data as string);
    if (event && ["plan_created", "plan_updated", "plan_step_changed", "plan_needs_review"].includes(event.type)) void load();
  };
  source.onerror = () => {
    source.close();
    if (eventSource === source) eventSource = null;
    if (reconnectTimer === null) reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connectEvents();
    }, 3000);
  };
}

onMounted(() => { void load(); connectEvents(); });
onUnmounted(() => {
  eventSource?.close();
  eventSource = null;
  if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
});
</script>

<template>
  <section class="plans-page">
    <div class="toolbar">
      <div class="toolbar-title"><h2>计划</h2><span>{{ plans.length }} 项</span></div>
      <button class="primary" type="button" @click="showCreate = true">＋ 新建</button>
      <button class="icon-button" type="button" title="刷新计划" aria-label="刷新计划" :disabled="loading" @click="load">↻</button>
      <label class="filter">状态
        <select v-model="selectedStatus" @change="load">
          <option value="all">全部</option><option value="queued">排队</option><option value="running">执行中</option>
          <option value="waiting">等待中</option><option value="paused">暂停</option><option value="needs_review">待复核</option><option value="cancelling">取消中</option>
          <option value="succeeded">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option><option value="expired">已过期</option>
        </select>
      </label>
    </div>

    <p v-if="errorText" class="error" role="alert">{{ errorText }}</p>
    <div v-if="currentPlan" class="current-plan">
      <div class="section-heading"><span>当前计划</span><span class="muted">v{{ currentPlan.version }} · {{ formatTime(currentPlan.updated_at) }}</span></div>
      <div class="current-line"><strong>{{ currentPlan.display_no }} · {{ currentPlan.title }}</strong><span class="status">{{ planStatusLabel(currentPlan.status) }}</span><span class="priority">{{ priorityLabel(currentPlan.priority) }}</span></div>
      <div class="current-meta">负责人：{{ currentPlan.owner_uid || "未指定" }}　来源：{{ currentPlan.source_kind || "人工" }}</div>
    </div>

    <div class="plan-list" aria-live="polite">
      <div v-if="!orderedPlans.length && !loading" class="empty">暂无计划</div>
      <article v-for="plan in orderedPlans" :key="plan.id" class="plan-row" :class="{ selected: currentPlan?.id === plan.id }">
        <button class="plan-main" type="button" @click="openPlan(plan.id)">
          <span class="plan-no">{{ plan.display_no }}</span><span class="plan-title">{{ plan.title }}</span>
          <span class="status">{{ planStatusLabel(plan.status) }}</span><span class="priority">{{ plan.priority }}</span>
          <small>{{ formatTime(plan.updated_at) }}</small>
        </button>
        <select class="priority-select" :value="plan.priority" title="调整优先级" aria-label="调整优先级" :disabled="saving || !canChangePlanPriority(plan.status)" @change="updatePriority(plan, $event)">
          <option value="P0">P0</option><option value="P1">P1</option><option value="P2">P2</option><option value="P3">P3</option>
        </select>
        <button v-if="canCancelPlan(plan.status)" class="icon-button danger" type="button" title="取消计划" aria-label="取消计划" :disabled="saving" @click="cancel(plan.id)">×</button>
        <div v-if="expandedId === plan.id && currentPlan?.id === plan.id" class="details">
          <div v-for="step in currentPlan.steps" :key="step.id" class="step-row">
            <div><b>{{ step.seq }}. {{ step.label }}</b><span class="muted">{{ step.action || step.step_type }} · {{ planStatusLabel(step.status) }}</span></div>
            <div class="step-actions">
              <button v-if="planStepConfirmDecision(step)" class="icon-button" type="button" title="确认步骤" aria-label="确认步骤" :disabled="saving" @click="confirmStep(step)">✓</button>
              <button v-if="canRetryPlanStep(step)" class="icon-button" type="button" title="重试步骤" aria-label="重试步骤" :disabled="saving" @click="retryStep(step)">↻</button>
            </div>
            <div v-if="step.attempts.length" class="attempts">
              <span v-for="attempt in step.attempts" :key="attempt.id">尝试 #{{ attempt.attempt_no }} · {{ attempt.outcome || attempt.dispatch_state }}</span>
            </div>
          </div>
        </div>
      </article>
    </div>

    <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate = false">
      <form class="dialog" @submit.prevent="submitCreate">
        <div class="dialog-heading"><h3>新建计划</h3><button class="icon-button" type="button" title="关闭" aria-label="关闭" @click="showCreate = false">×</button></div>
        <label>标题<input v-model="createForm.title" required maxlength="120" /></label>
        <div class="form-grid"><label>优先级<select v-model="createForm.priority"><option value="P1">P1</option><option value="P2">P2</option><option value="P3">P3</option></select></label><label>负责人 UID<input v-model="createForm.ownerUid" placeholder="可选" /></label></div>
        <fieldset><legend>结构化步骤</legend>
          <div class="form-grid"><label>类型<select v-model="stepForm.type"><option value="action">车控动作</option><option value="wait">等待</option><option value="manual">人工确认</option></select></label><label>步骤名称<input v-model="stepForm.label" placeholder="例如：前往护士站" /></label></div>
          <template v-if="stepForm.type === 'action'"><div class="form-grid"><label>动作<select v-model="stepForm.action"><option value="robot_move">直行/倒车</option><option value="robot_turn">转向</option><option value="robot_goto_point">前往坐标</option><option value="robot_goto_place">前往地点</option><option value="robot_goto_zone">前往区域</option></select></label><label v-if="stepForm.action === 'robot_move'">方向<select v-model="stepForm.direction"><option value="forward">前进</option><option value="back">后退</option><option value="left">左移</option><option value="right">右移</option></select></label></div>
             <div v-if="stepForm.action === 'robot_move'" class="form-grid"><label>距离（米）<input v-model.number="stepForm.distance" type="number" min="0.1" max="5" step="0.1" /></label></div>
             <div v-else-if="stepForm.action === 'robot_turn'" class="form-grid"><label>角度（度）<input v-model.number="stepForm.angle" type="number" min="-360" max="360" step="1" /></label></div>
             <div v-else-if="stepForm.action === 'robot_goto_point'" class="form-grid three"><label>X<input v-model.number="stepForm.x" type="number" min="-1000" max="1000" step="0.01" /></label><label>Y<input v-model.number="stepForm.y" type="number" min="-1000" max="1000" step="0.01" /></label><label>朝向<input v-model.number="stepForm.yaw" type="number" min="-360" max="360" step="1" /></label></div>
            <div v-else class="form-grid"><label>目标地点/区域<input v-model="stepForm.target" required /></label></div>
          </template>
          <template v-else-if="stepForm.type === 'wait'"><div class="form-grid"><label>等待方式<select v-model="stepForm.waitKind"><option value="time">指定时间</option><option value="device">设备信号</option><option value="external">外部信号</option></select></label><label v-if="stepForm.waitKind === 'time'">唤醒时间<input v-model="stepForm.wakeAt" type="datetime-local" /></label></div></template>
          <button class="secondary" type="button" @click="addStep">添加步骤</button>
          <ul v-if="createSteps.length" class="pending-steps"><li v-for="(step, index) in createSteps" :key="index"><span>{{ index + 1 }}. {{ step.label || step.type }}</span><button class="icon-button" type="button" title="移除步骤" aria-label="移除步骤" @click="removeStep(index)">×</button></li></ul>
        </fieldset>
        <div class="checks"><label><input v-model="createForm.notify" type="checkbox" /> 通知护士台</label><label><input v-model="createForm.speak" type="checkbox" /> 在场时播报</label></div>
        <div class="dialog-actions"><button class="secondary" type="button" @click="showCreate = false">取消</button><button class="primary" type="submit" :disabled="saving">创建计划</button></div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.plans-page { color: #e2e8f0; max-width: 1180px; margin: 0 auto; }
.toolbar, .section-heading, .current-line, .plan-row, .dialog-heading, .dialog-actions { display: flex; align-items: center; gap: 10px; }
.toolbar { min-height: 42px; border-bottom: 1px solid #334155; margin-bottom: 12px; }
.toolbar-title { display: flex; align-items: baseline; gap: 8px; margin-right: auto; }.toolbar h2 { margin: 0; font-size: 18px; }.toolbar-title span, .muted, small { color: #94a3b8; font-size: 12px; }
button, select, input { font: inherit; }.toolbar button, .dialog button { cursor: pointer; }.primary, .secondary, .icon-button, select, input { border: 1px solid #475569; border-radius: 5px; background: #1e293b; color: #e2e8f0; }
.primary, .secondary { padding: 6px 11px; }.primary { background: #2563eb; border-color: #2563eb; }.icon-button { width: 30px; height: 30px; padding: 0; display: inline-grid; place-items: center; cursor: pointer; }.icon-button:disabled, button:disabled { opacity: .5; cursor: not-allowed; }.danger { color: #fecaca; border-color: #7f1d1d; }
.filter { display: flex; align-items: center; gap: 5px; color: #94a3b8; font-size: 12px; }.filter select, .priority-select { padding: 5px 7px; }
.error { margin: 8px 0; padding: 8px 10px; color: #fecaca; background: #451a1a; border-left: 3px solid #ef4444; font-size: 13px; }.current-plan { border-left: 3px solid #38bdf8; background: #172554; padding: 10px 12px; margin-bottom: 12px; }.section-heading { justify-content: space-between; color: #bae6fd; font-size: 12px; }.current-line { margin-top: 6px; }.current-line strong { margin-right: auto; }.current-meta { color: #bfdbfe; font-size: 12px; margin-top: 6px; }
.status, .priority { display: inline-block; padding: 2px 6px; border-radius: 3px; background: #334155; font-size: 11px; white-space: nowrap; }.priority { color: #fcd34d; }.plan-list { display: grid; gap: 5px; }.plan-row { flex-wrap: wrap; padding: 7px 8px; background: #1e293b; border: 1px solid transparent; }.plan-row.selected { border-color: #2563eb; }.plan-main { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; border: 0; background: none; color: inherit; text-align: left; cursor: pointer; }.plan-no { color: #94a3b8; font-size: 12px; }.plan-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.plan-main small { margin-left: auto; }.priority-select { width: 60px; }.details { width: 100%; border-top: 1px solid #334155; margin-top: 4px; padding: 7px 0 0 40px; }.step-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 10px; padding: 6px 0; border-bottom: 1px solid #273449; }.step-row b { display: block; font-size: 13px; }.step-row .muted { margin-left: 8px; }.step-actions { display: flex; gap: 4px; }.attempts { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 5px; color: #94a3b8; font-size: 11px; }.attempts span { background: #0f172a; padding: 3px 5px; border-radius: 3px; }.empty { color: #94a3b8; text-align: center; padding: 32px; }
.modal-backdrop { position: fixed; inset: 0; z-index: 20; background: rgb(2 6 23 / .72); display: grid; place-items: center; padding: 16px; }.dialog { width: min(620px, 100%); max-height: 92vh; overflow-y: auto; padding: 16px; background: #0f172a; border: 1px solid #475569; box-shadow: 0 10px 30px rgb(0 0 0 / .35); }.dialog-heading { justify-content: space-between; border-bottom: 1px solid #334155; padding-bottom: 8px; margin-bottom: 10px; }.dialog h3 { margin: 0; font-size: 17px; }.dialog label { display: flex; flex-direction: column; gap: 4px; color: #cbd5e1; font-size: 12px; margin-bottom: 8px; }.dialog input, .dialog select { padding: 7px 8px; width: 100%; box-sizing: border-box; }.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }.form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }.dialog fieldset { border: 1px solid #334155; padding: 10px; margin: 10px 0; }.dialog legend { color: #94a3b8; padding: 0 5px; font-size: 12px; }.secondary { background: #1e293b; color: #e2e8f0; }.pending-steps { list-style: none; padding: 0; margin: 8px 0; border-top: 1px solid #334155; }.pending-steps li { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid #273449; font-size: 12px; }.checks { display: flex; gap: 14px; color: #cbd5e1; font-size: 12px; }.checks label { flex-direction: row; align-items: center; }.dialog-actions { justify-content: flex-end; margin-top: 14px; }
@media (max-width: 680px) { .plan-main small { display: none; }.form-grid, .form-grid.three { grid-template-columns: 1fr; }.toolbar { flex-wrap: wrap; padding-bottom: 8px; }.toolbar-title { width: 100%; }.details { padding-left: 10px; } }
</style>
