<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
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
  PLAN_PRIORITY_RANK,
  planStatusLabel,
  planStepConfirmDecision,
  retryPlanStep,
  validatePlanStepDraft,
  type PlanCreateStep,
  type PlanDetail,
  type PlanPriority,
  type PlanStep,
  type PlanStepDraft,
  type PlanSummary,
} from "shared";

const props = defineProps<{ refreshToken: number }>();
const plans = ref<PlanSummary[]>([]);
const detail = ref<PlanDetail | null>(null);
const expandedId = ref<number | null>(null);
const selectedStatus = ref("all");
const loading = ref(false);
const saving = ref(false);
const errorText = ref("");
const showCreate = ref(false);
const createSteps = ref<PlanCreateStep[]>([]);
const createForm = ref({ title: "", priority: "P1" as Exclude<PlanPriority, "P0">,
  ownerUid: "", notify: true, speak: false });
const stepForm = ref<PlanStepDraft>({ type: "action", label: "", action: "robot_move",
  direction: "forward", distance: 1, angle: 90, x: 0, y: 0, yaw: 0, target: "",
  waitKind: "device", wakeAt: "" });
let loadGeneration = 0;
let openGeneration = 0;

const orderedPlans = computed(() => [...plans.value].sort((a, b) =>
  (PLAN_PRIORITY_RANK[a.priority] ?? 9) - (PLAN_PRIORITY_RANK[b.priority] ?? 9)
  || Date.parse(b.updated_at || b.created_at) - Date.parse(a.updated_at || a.created_at)));

function formatTime(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function setLatest(plan: PlanDetail) {
  detail.value = plan;
  plans.value = plans.value.map((item) => item.id === plan.id ? { ...item, ...plan } : item);
}

function latestFrom(error: unknown): PlanDetail | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  return (error.body as { plan?: PlanDetail } | undefined)?.plan ?? null;
}

async function load() {
  const requestId = ++loadGeneration;
  openGeneration += 1;
  loading.value = true;
  try {
    const query = selectedStatus.value === "all" ? {} : { state: selectedStatus.value };
    const result = await listPlans(query);
    if (requestId !== loadGeneration) return;
    const nextPlans = result.plans ?? [];
    const ordered = [...nextPlans].sort((a, b) =>
      (PLAN_PRIORITY_RANK[a.priority] ?? 9) - (PLAN_PRIORITY_RANK[b.priority] ?? 9)
      || Date.parse(b.updated_at || b.created_at) - Date.parse(a.updated_at || a.created_at));
    const preferred = detail.value && nextPlans.some((item) => item.id === detail.value?.id)
      ? detail.value.id
      : ordered.find((item) => item.status === "running" || item.status === "needs_review")?.id
        ?? ordered[0]?.id;
    const nextDetail = preferred === undefined ? null : (await getPlan(preferred)).plan ?? null;
    if (requestId !== loadGeneration) return;
    plans.value = nextPlans;
    detail.value = nextDetail;
    errorText.value = "";
  } catch (error) {
    if (requestId !== loadGeneration) return;
    errorText.value = `读取计划失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    if (requestId === loadGeneration) loading.value = false;
  }
}

async function openPlan(id: number) {
  loadGeneration += 1;
  loading.value = false;
  const requestId = ++openGeneration;
  expandedId.value = expandedId.value === id ? null : id;
  if (expandedId.value !== id) return;
  try {
    const response = await getPlan(id);
    if (requestId !== openGeneration || expandedId.value !== id) return;
    if (response.plan) setLatest(response.plan);
  } catch (error) {
    if (requestId !== openGeneration) return;
    errorText.value = `读取计划详情失败：${error instanceof Error ? error.message : String(error)}`;
  }
}

async function mutate(action: () => Promise<{ plan: PlanDetail }>) {
  saving.value = true;
  try {
    const response = await action();
    if (response.plan) setLatest(response.plan);
    await load();
  } catch (error) {
    const latest = latestFrom(error);
    if (latest) {
      setLatest(latest);
      errorText.value = "计划已被其他窗口修改，已刷新最新版本。请重新操作。";
    } else errorText.value = `操作失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    saving.value = false;
  }
}

async function updatePriority(plan: PlanSummary, event: Event) {
  const priority = (event.target as HTMLSelectElement).value as PlanPriority;
  if (priority !== plan.priority) await mutate(() => changePlanPriority(plan.id, priority, plan.version));
}

async function cancel(plan: PlanSummary) {
  if (!window.confirm(`确认取消“${plan.title}”？`)) return;
  const reason = window.prompt("取消原因（可选）", "护士台取消") ?? "护士台取消";
  await mutate(() => cancelPlan(plan.id, reason, plan.version));
}

async function confirm(step: PlanStep) {
  const decision = planStepConfirmDecision(step);
  if (!detail.value || !decision || !window.confirm(`确认步骤“${step.label}”已完成？`)) return;
  await mutate(() => confirmPlanStep(detail.value!.id, step.id, decision, "护士确认", detail.value!.version));
}

async function retry(step: PlanStep) {
  if (!detail.value || !window.confirm(`确认重试步骤“${step.label}”？`)) return;
  await mutate(() => retryPlanStep(detail.value!.id, step.id, "护士重试", detail.value!.version));
}

function addStep() {
  const error = validatePlanStepDraft(stepForm.value);
  if (error) { errorText.value = error; return; }
  createSteps.value.push(buildPlanCreateStep(stepForm.value));
  stepForm.value.label = "";
  errorText.value = "";
}

async function submitCreate() {
  if (!createForm.value.title.trim()) { errorText.value = "请填写计划标题"; return; }
  if (!createSteps.value.length) { errorText.value = "请至少添加一个结构化步骤"; return; }
  saving.value = true;
  try {
    await createPlan({
      title: createForm.value.title.trim(), priority: createForm.value.priority,
      owner_uid: createForm.value.ownerUid.trim() || undefined, steps: createSteps.value,
      report: { notify: createForm.value.notify, speak_if_present: createForm.value.speak },
    });
    createForm.value.title = "";
    createSteps.value = [];
    showCreate.value = false;
    await load();
  } catch (error) {
    errorText.value = `创建失败：${error instanceof Error ? error.message : String(error)}`;
  } finally {
    saving.value = false;
  }
}

watch(() => props.refreshToken, () => { void load(); });
onMounted(() => { void load(); });
</script>

<template>
  <section class="plan-panel">
    <div class="toolbar">
      <span class="summary">{{ plans.length }} 项计划</span>
      <button class="primary" type="button" @click="showCreate = true">＋ 新建</button>
      <button class="icon" type="button" title="刷新计划" aria-label="刷新计划" :disabled="loading" @click="load">↻</button>
      <label>状态
        <select v-model="selectedStatus" @change="load">
          <option value="all">全部</option><option value="queued">排队</option><option value="running">执行中</option>
          <option value="waiting">等待中</option><option value="paused">暂停</option><option value="needs_review">待复核</option>
          <option value="cancelling">取消中</option><option value="succeeded">已完成</option><option value="failed">失败</option>
          <option value="cancelled">已取消</option><option value="expired">已过期</option>
        </select>
      </label>
    </div>

    <p v-if="errorText" class="error" role="alert">{{ errorText }}</p>
    <div v-if="detail" class="current">
      <strong>{{ detail.display_no }} · {{ detail.title }}</strong>
      <span class="state">{{ planStatusLabel(detail.status) }}</span><span class="priority">{{ detail.priority }}</span>
      <small>负责人 {{ detail.owner_uid || "未指定" }} · v{{ detail.version }} · {{ formatTime(detail.updated_at) }}</small>
    </div>

    <div class="plan-list" aria-live="polite">
      <p v-if="!orderedPlans.length && !loading" class="empty">暂无计划</p>
      <article v-for="plan in orderedPlans" :key="plan.id" class="plan-row">
        <button class="plan-main" type="button" @click="openPlan(plan.id)">
          <span class="number">{{ plan.display_no }}</span><strong>{{ plan.title }}</strong>
          <span class="state">{{ planStatusLabel(plan.status) }}</span><span class="priority">{{ plan.priority }}</span>
          <small>{{ formatTime(plan.updated_at) }}</small>
        </button>
        <select :value="plan.priority" title="调整优先级" aria-label="调整优先级"
                :disabled="saving || !canChangePlanPriority(plan.status)" @change="updatePriority(plan, $event)">
          <option value="P0">P0</option><option value="P1">P1</option><option value="P2">P2</option><option value="P3">P3</option>
        </select>
        <button v-if="canCancelPlan(plan.status)" class="icon danger" type="button" title="取消计划"
                aria-label="取消计划" :disabled="saving" @click="cancel(plan)">×</button>
        <div v-if="expandedId === plan.id && detail?.id === plan.id" class="details">
          <div v-for="step in detail.steps" :key="step.id" class="step">
            <div><strong>{{ step.seq }}. {{ step.label }}</strong><small>{{ step.action || step.step_type }} · {{ planStatusLabel(step.status) }}</small></div>
            <div class="actions">
              <button v-if="planStepConfirmDecision(step)" class="icon" type="button" title="确认步骤" aria-label="确认步骤" :disabled="saving" @click="confirm(step)">✓</button>
              <button v-if="canRetryPlanStep(step)" class="icon" type="button" title="重试步骤" aria-label="重试步骤" :disabled="saving" @click="retry(step)">↻</button>
            </div>
            <div v-if="step.attempts.length" class="attempts">
              <span v-for="attempt in step.attempts" :key="attempt.id">尝试 #{{ attempt.attempt_no }} · {{ attempt.outcome || attempt.dispatch_state }}</span>
            </div>
          </div>
        </div>
      </article>
    </div>

    <div v-if="showCreate" class="backdrop" @click.self="showCreate = false">
      <form class="dialog" @submit.prevent="submitCreate">
        <div class="dialog-head"><h2>新建计划</h2><button class="icon" type="button" title="关闭" aria-label="关闭" @click="showCreate = false">×</button></div>
        <label>标题<input v-model="createForm.title" required maxlength="120" /></label>
        <div class="grid"><label>优先级<select v-model="createForm.priority"><option value="P1">P1</option><option value="P2">P2</option><option value="P3">P3</option></select></label><label>负责人 UID<input v-model="createForm.ownerUid" placeholder="可选" /></label></div>
        <fieldset><legend>结构化步骤</legend>
          <div class="grid"><label>类型<select v-model="stepForm.type"><option value="action">车控动作</option><option value="wait">等待</option><option value="manual">人工确认</option></select></label><label>步骤名称<input v-model="stepForm.label" placeholder="例如：前往护士站" /></label></div>
          <template v-if="stepForm.type === 'action'">
            <div class="grid"><label>动作<select v-model="stepForm.action"><option value="robot_move">直行/倒车</option><option value="robot_turn">转向</option><option value="robot_goto_point">前往坐标</option><option value="robot_goto_place">前往地点</option><option value="robot_goto_zone">前往区域</option></select></label><label v-if="stepForm.action === 'robot_move'">方向<select v-model="stepForm.direction"><option value="forward">前进</option><option value="back">后退</option><option value="left">左移</option><option value="right">右移</option></select></label></div>
            <label v-if="stepForm.action === 'robot_move'">距离（米）<input v-model.number="stepForm.distance" type="number" min="0.1" max="5" step="0.1" /></label>
            <label v-else-if="stepForm.action === 'robot_turn'">角度（度）<input v-model.number="stepForm.angle" type="number" min="-360" max="360" /></label>
            <div v-else-if="stepForm.action === 'robot_goto_point'" class="grid three"><label>X<input v-model.number="stepForm.x" type="number" min="-1000" max="1000" step="0.01" /></label><label>Y<input v-model.number="stepForm.y" type="number" min="-1000" max="1000" step="0.01" /></label><label>朝向<input v-model.number="stepForm.yaw" type="number" min="-360" max="360" /></label></div>
            <label v-else>目标地点/区域<input v-model="stepForm.target" /></label>
          </template>
          <div v-else-if="stepForm.type === 'wait'" class="grid"><label>等待方式<select v-model="stepForm.waitKind"><option value="time">指定时间</option><option value="device">设备信号</option><option value="external">外部信号</option></select></label><label v-if="stepForm.waitKind === 'time'">唤醒时间<input v-model="stepForm.wakeAt" type="datetime-local" /></label></div>
          <button class="secondary" type="button" @click="addStep">添加步骤</button>
          <ul v-if="createSteps.length"><li v-for="(step, index) in createSteps" :key="index"><span>{{ index + 1 }}. {{ step.label || step.type }}</span><button class="icon" type="button" title="移除步骤" aria-label="移除步骤" @click="createSteps.splice(index, 1)">×</button></li></ul>
        </fieldset>
        <div class="checks"><label><input v-model="createForm.notify" type="checkbox" /> 通知护士台</label><label><input v-model="createForm.speak" type="checkbox" /> 在场时播报</label></div>
        <div class="dialog-actions"><button class="secondary" type="button" @click="showCreate = false">取消</button><button class="primary" type="submit" :disabled="saving">创建计划</button></div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.plan-panel { max-width: 1180px; margin: 0 auto; }
.toolbar, .current, .plan-row, .dialog-head, .dialog-actions { display: flex; align-items: center; gap: 8px; }
.toolbar { min-height: 38px; margin-bottom: 10px; }.summary { margin-right: auto; color: #64748b; font-size: 13px; }
button, input, select { font: inherit; }.primary, .secondary, .icon, select, input { border: 1px solid #bdc8d4; border-radius: 5px; background: #fff; color: #1e293b; box-sizing: border-box; }
.primary, .secondary { padding: 6px 11px; cursor: pointer; }.primary { background: #1e3a5f; border-color: #1e3a5f; color: #fff; }.secondary { background: #f8fafc; }
.icon { width: 30px; height: 30px; padding: 0; display: inline-grid; place-items: center; cursor: pointer; }.icon:disabled, button:disabled { opacity: .5; cursor: not-allowed; }.danger { color: #b91c1c; border-color: #fecaca; }
.toolbar label { display: flex; align-items: center; gap: 5px; color: #64748b; font-size: 12px; }.toolbar select, .plan-row > select { height: 30px; padding: 0 7px; }
.error { margin: 8px 0; padding: 7px 10px; border-left: 3px solid #dc2626; background: #fef2f2; color: #991b1b; font-size: 13px; }
.current { flex-wrap: wrap; padding: 9px 11px; margin-bottom: 10px; background: #eaf2f8; border-left: 3px solid #2878a5; }.current strong { margin-right: auto; }.current small { width: 100%; color: #526475; }
.state, .priority { padding: 2px 6px; border-radius: 3px; background: #e2e8f0; font-size: 11px; white-space: nowrap; }.priority { color: #854d0e; background: #fef9c3; }
.plan-list { display: grid; gap: 5px; }.plan-row { flex-wrap: wrap; padding: 7px 8px; background: #fff; border: 1px solid #dde5ed; border-left: 3px solid #8fa4b8; }
.plan-main { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; border: 0; background: none; color: inherit; text-align: left; cursor: pointer; }.plan-main strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.number { color: #64748b; font-size: 12px; }.plan-main small { margin-left: auto; color: #64748b; }
.details { width: 100%; padding: 5px 0 0 36px; border-top: 1px solid #e2e8f0; }.step { display: grid; grid-template-columns: 1fr auto; gap: 4px 8px; padding: 6px 0; border-bottom: 1px solid #edf2f7; }.step strong { font-size: 13px; }.step small { margin-left: 8px; color: #64748b; }.actions { display: flex; gap: 4px; }.attempts { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 4px; color: #64748b; font-size: 11px; }.attempts span { background: #f1f5f9; padding: 2px 5px; }.empty { padding: 28px; color: #64748b; text-align: center; }
.backdrop { position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 16px; background: rgb(15 23 42 / .55); }.dialog { width: min(620px, 100%); max-height: 92vh; overflow-y: auto; padding: 16px; background: #fff; border: 1px solid #94a3b8; box-shadow: 0 14px 36px rgb(15 23 42 / .2); box-sizing: border-box; }.dialog-head { justify-content: space-between; border-bottom: 1px solid #e2e8f0; padding-bottom: 8px; margin-bottom: 10px; }.dialog h2 { margin: 0; font-size: 17px; }.dialog label { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; color: #475569; font-size: 12px; }.dialog input, .dialog select { width: 100%; padding: 7px 8px; }.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }.grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }.dialog fieldset { border: 1px solid #d8e0e8; padding: 10px; margin: 10px 0; }.dialog legend { padding: 0 5px; color: #64748b; font-size: 12px; }.dialog ul { list-style: none; padding: 0; margin: 8px 0 0; }.dialog li { display: flex; align-items: center; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #e2e8f0; }.checks { display: flex; gap: 14px; }.checks label { flex-direction: row; align-items: center; }.checks input { width: auto; }.dialog-actions { justify-content: flex-end; margin-top: 12px; }
@media (max-width: 680px) { .toolbar { flex-wrap: wrap; }.summary { width: 100%; }.plan-main small { display: none; }.details { padding-left: 6px; }.grid, .grid.three { grid-template-columns: 1fr; } }
</style>
