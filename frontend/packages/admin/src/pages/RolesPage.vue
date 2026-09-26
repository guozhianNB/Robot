<script setup lang="ts">
// 身份与权限（admin 页签）：**可编辑权限矩阵** + 口令设置 + 会话/病房上下文可调项。
// 规格：docs/superpowers/specs/2026-09-26-permission-matrix-design.md（矩阵：D11 矩阵全可编辑，仅 R3 红锁不可勾）
//      docs/superpowers/specs/2026-09-14-layered-user-roles-design.md（D12 口令可改、D13 口令门可关）
//
// 为什么口令/矩阵走裸 fetch 而不是 shared：shared 的 apiGet/apiPost 在非 2xx 时直接抛
// `API <status>: <url>`，会**丢掉响应体里的 error/detail/results**。本页的失败语义
// （403 非管理员 / 403 红锁逐格结果 / 400 口令太短 / 口令门冷却）恰恰都靠响应体表达，
// 所以这里自带一层 `req()` 把 X-Surface 头和响应体错误一起处理。
import { computed, onMounted, ref } from "vue";
import { changePassword, getAdminAuth, restoreFactoryPassword, setAdminAuth } from "shared";

interface RolePolicy {
  prompt_file?: string;
  allowed_tools?: string[] | null;
  data_scope?: string;
  ward_context?: boolean;
}

/** 矩阵一行（后端 GET /api/permissions/matrix → tools[]）。 */
interface MatrixTool {
  name: string;
  server: string;
  local: boolean;
  /** 注册表里已不存在（工具下线/改名），但库里还留着覆盖行 */
  orphan: boolean;
  /** 整机开关（本地 <名>_enabled / MCP mcp_enabled）当前是否打开 */
  switch_on: boolean;
  factory: Record<string, boolean>;
  allowed: Record<string, boolean>;
  overridden: Record<string, boolean>;
  locked: Record<string, boolean>;
}

const roles = ref<Record<string, RolePolicy>>({});
const authRequired = ref(true);
const oldPw = ref("");
const newPw = ref("");
const msg = ref("");
const msgOk = ref(true);
const ttl = ref(300);
const wardWindow = ref(10);
const loaded = ref(false);

// ---- 权限矩阵状态 ----
const matrix = ref<MatrixTool[]>([]);
const matrixRoles = ref<string[]>([]);
const draft = ref<Record<string, boolean>>({});   // key = `${role}|${tool}`，只放"改动过的格"
const saving = ref(false);

const grouped = computed(() => {
  const out: { server: string; tools: MatrixTool[] }[] = [];
  for (const t of matrix.value) {
    const last = out[out.length - 1];
    if (last && last.server === t.server) last.tools.push(t);
    else out.push({ server: t.server, tools: [t] });
  }
  return out;
});
const dirtyCount = computed(() => Object.keys(draft.value).length);

function cellKey(role: string, tool: string) { return `${role}|${tool}`; }
function isDirty(role: string, tool: string) { return draft.value[cellKey(role, tool)] !== undefined; }
function cellValue(role: string, t: MatrixTool) {
  const k = cellKey(role, t.name);
  return draft.value[k] !== undefined ? draft.value[k] : t.allowed[role];
}
function toggle(role: string, t: MatrixTool, ev: Event) {
  const el = ev.target as HTMLInputElement;
  if (t.locked[role] || t.orphan) { el.checked = cellValue(role, t); return; }  // R3 红锁 / 已下线
  const next = el.checked;
  // 高危二次确认：出厂对该层是关的，现在要放开（典型：把联网/抓取工具给老人层）
  if (next && !t.factory[role] &&
      !confirm(`「${t.name}」（${t.server}）出厂对 ${role} 层是关闭的。\n确认放开这一格？`)) {
    el.checked = cellValue(role, t);        // 用户取消 → 把 DOM 勾选态还原（:checked 是单向绑定）
    return;
  }
  const k = cellKey(role, t.name);
  if (next === t.allowed[role]) delete draft.value[k];   // 改回原值 = 不再是 diff
  else draft.value[k] = next;
  draft.value = { ...draft.value };
}

/** 带 X-Surface: admin 的请求；失败时把后端 `error`/`detail` 与**响应体**一起带回。 */
async function req<T = any>(path: string, body?: unknown): Promise<{ ok: boolean; data?: T;
                                                                   error?: string }> {
  try {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-Surface": "admin" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const b = await res.json().catch(() => null);
    if (!res.ok) return { ok: false, error: b?.error ?? b?.detail ?? `HTTP ${res.status}`, data: b };
    return { ok: true, data: b as T };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function say(text: string, ok = true) { msg.value = text; msgOk.value = ok; }

async function loadMatrix() {
  const r = await req<{ ok: boolean; scope: string; roles: string[]; tools: MatrixTool[] }>(
    "/api/permissions/matrix");
  if (!r.ok || !r.data) { say(`❌ 读权限矩阵失败：${r.error}`, false); return; }
  matrix.value = r.data.tools;
  matrixRoles.value = r.data.roles;
  draft.value = {};
}

async function load() {
  const [p, a, s] = await Promise.all([
    req<Record<string, RolePolicy>>("/api/policy/roles"),
    getAdminAuth("admin").catch(() => ({ required: true })),
    req<{ ok: boolean; settings: Record<string, any> }>("/api/settings"),
  ]);
  if (p.ok && p.data) roles.value = p.data;
  authRequired.value = a.required;
  // GET /api/settings 的形状是 {ok, settings:{...}}（**不是**顶层平铺），要取 .settings
  const st = s.data?.settings ?? {};
  if (typeof st.admin_session_ttl_s === "number") ttl.value = st.admin_session_ttl_s;
  if (typeof st.ward_context_window === "number") wardWindow.value = st.ward_context_window;
  await loadMatrix();
  loaded.value = true;
}

async function saveMatrix() {
  const changes = Object.entries(draft.value).map(([k, allowed]) => {
    const idx = k.indexOf("|");
    return { role: k.slice(0, idx), tool: k.slice(idx + 1), allowed };
  });
  if (!changes.length) { say("没有改动"); return; }
  saving.value = true;
  try {
    const r = await req<{ ok: boolean; results: any[] }>("/api/permissions/matrix", { changes });
    if (r.ok) {
      say(`✅ 已保存 ${changes.length} 处改动（即时生效，无需重启）`);
    } else {
      const bad = (r.data?.results ?? []).filter((x: any) => !x.ok)
        .map((x: any) => `${x.role}/${x.tool}（${x.reason === "locked" ? "R3 红锁，不可取消" : x.reason}）`)
        .join("、");
      say(`❌ 有格子被拒：${bad || r.error}`, false);
    }
    await loadMatrix();
  } finally { saving.value = false; }
}

async function resetMatrix() {
  if (!confirm("把全部人工改动恢复为出厂默认？（policy.py 的工具白名单 + conf 的 MCP roles）")) return;
  const r = await req("/api/permissions/reset", {});
  if (r.ok) { say("✅ 已恢复出厂默认"); await loadMatrix(); }
  else say(`❌ ${r.error}`, false);
}

async function savePw() {
  if (!newPw.value) { say("请填新口令", false); return; }
  const r = await changePassword(oldPw.value, newPw.value, "admin");
  if (r.ok) { say("✅ 口令已更新"); oldPw.value = ""; newPw.value = ""; }
  else { say(`❌ ${r.error ?? "改口令失败（非管理员会话会 403）"}`, false); }
}

async function restorePw() {
  if (!confirm("确定恢复为 .env 中 PASSWORD 配置的出厂口令？恢复后所有管理员会话都会退出。")) return;
  try {
    const r = await restoreFactoryPassword("admin");
    if (r.ok) say("出厂口令已恢复，管理层会话已退出，请重新登录");
    else say(`恢复失败：${r.error ?? "请检查 .env 的 PASSWORD"}`, false);
  } catch (e) {
    say(`恢复失败：${e}`, false);
  }
}

async function toggleAuth() {
  try {
    const r = await setAdminAuth(!authRequired.value, "admin");
    authRequired.value = r.required;
    say(r.required ? "✅ 口令门已开启" : "⚠️ 口令门已关闭（当前无保护）", r.required);
  } catch (e) {
    say(`❌ 开关口令门失败：${e}（非管理员会话会 403）`, false);
  }
}

async function saveLimits() {
  const r = await req("/api/settings", {
    admin_session_ttl_s: Number(ttl.value),
    ward_context_window: Number(wardWindow.value),
  });
  if (r.ok) say("✅ 已保存");
  else say(`❌ ${r.error}`, false);
}

onMounted(load);
</script>

<template>
  <section class="page">
    <h3>身份与权限</h3>
    <p class="hint">
      角色由后端按主体推导（前端传的 role 一律不可信，R1）。下表是**可编辑的权限矩阵**：
      出厂默认来自 <code>LLM/agent/policy.py</code> 的工具白名单与 <code>LLM/conf.py</code> 里
      MCP 服务器的 <code>roles</code>，勾选写入运行期覆盖层（<code>brain.db.role_tool_grants</code>），
      <b>即时生效、无需重启</b>。
    </p>

    <h4>权限矩阵（可编辑）</h4>
    <table>
      <thead>
        <tr>
          <th>工具</th>
          <th>来源</th>
          <th v-for="r in matrixRoles" :key="r">{{ r }}</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="g in grouped" :key="g.server">
          <tr class="group"><td :colspan="2 + matrixRoles.length">{{ g.server }}</td></tr>
          <tr v-for="t in g.tools" :key="t.name" :class="{ off: !t.switch_on, dead: t.orphan }">
            <td class="mono">
              {{ t.name }}<span v-if="t.orphan" class="tag">已下线</span>
            </td>
            <td class="mono">{{ t.local ? t.server : (t.switch_on ? "" : "开关已关") }}</td>
            <td v-for="r in matrixRoles" :key="r">
              <input type="checkbox" :checked="cellValue(r, t)" :disabled="t.locked[r] || t.orphan"
                     :title="t.locked[r] ? 'R3：急停/呼救不受权限限制，不可取消'
                             : (t.overridden[r] ? '已人工修改（勾=已落库的覆盖）' : '出厂默认')"
                     @change="toggle(r, t, $event)" />
              <span v-if="t.locked[r]" class="lock">🔒</span>
              <span v-else-if="isDirty(r, t.name)" class="dirty">●</span>
              <span v-else-if="t.overridden[r]" class="over">已改</span>
            </td>
          </tr>
        </template>
        <tr v-if="loaded && !matrix.length"><td :colspan="2 + matrixRoles.length">读不到矩阵</td></tr>
      </tbody>
    </table>
    <div class="row">
      <button :disabled="saving || !dirtyCount" @click="saveMatrix">
        保存改动{{ dirtyCount ? `（${dirtyCount}）` : "" }}
      </button>
      <button class="danger" @click="resetMatrix">全部恢复出厂</button>
    </div>
    <p class="hint">
      <b>●</b> = 本次未保存；<b>已改</b> = 已落库的覆盖；灰勾/灰空 = 出厂默认。
      淡色行 = 整机开关关着（去「设置」页打开，勾了也不生效）；<b>已下线</b> = 库里留着覆盖但工具
      已不在注册表（可「恢复出厂」清掉）。🔒 是红线 R3：急停/呼救任何层都不可取消。
    </p>

    <h4>出厂策略参考</h4>
    <p class="hint">
      <span v-for="(pol, role) in roles" :key="role" class="policy">
        <b>{{ role }}</b>：数据范围 <code>{{ pol.data_scope }}</code>、
        病房上下文 {{ pol.ward_context ? "读" : "不读" }}；
      </span>
    </p>

    <h4>口令设置</h4>
    <p v-if="!authRequired" class="warn">
      ⚠️ 口令门当前是关的：任何人都能进管理台（含车前屏点「管理层」）
    </p>
    <div class="row">
      <input v-model="oldPw" type="password" placeholder="旧口令（已设过口令就必填）" />
      <input v-model="newPw" type="password" placeholder="新口令（后端要求 ≥4 位）" />
      <button @click="savePw">改口令</button>
      <button @click="toggleAuth">{{ authRequired ? "关闭口令门" : "开启口令门" }}</button>
      <button class="danger" @click="restorePw">恢复出厂口令</button>
    </div>
    <p class="hint">
      只要系统已设过口令，<b>无论口令门开着还是关着，改口令都必须填旧口令</b>
      （后端实测：门关着时 <code>old=""</code> 会返回「旧口令错误」）。首次设口令才不需要旧口令。
      新口令长度等校验由后端判断，失败原因原样显示。
    </p>

    <h4>可调项</h4>
    <div class="row">
      <label>管理员会话时效(秒) <input v-model.number="ttl" type="number" min="1" /></label>
      <label>病房上下文条数 <input v-model.number="wardWindow" type="number" min="0" /></label>
      <button @click="saveLimits">保存</button>
    </div>
    <p class="hint">
      会话时效 = 管理员提权后无操作自动降权秒数；病房上下文 = 集体层注入最近 N 条（单向，R5）。
    </p>

    <p v-if="msg" :class="msgOk ? 'ok' : 'warn'">{{ msg }}</p>
  </section>
</template>

<style scoped>
.page { max-width: 1000px; }
h3 { margin: 0 0 6px; font-size: 18px; }
h4 { margin: 22px 0 8px; font-size: 15px; color: #cbd5e1; }
.hint { color: #64748b; font-size: 12px; margin: 6px 0; }
.warn { color: #fbbf24; font-size: 13px; }
.ok { color: #4ade80; font-size: 13px; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1f2937; font-size: 13px; }
th { color: #94a3b8; font-weight: normal; }
tr.group td { color: #64748b; font-size: 11px; text-transform: uppercase;
  background: #0f172a; padding: 4px 10px; }
tr.off { opacity: .55; }
tr.dead td { color: #64748b; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
code { background: #1e293b; padding: 1px 5px; border-radius: 4px; }
.tag, .over { font-size: 11px; color: #94a3b8; margin-left: 6px; }
.dirty { color: #fbbf24; margin-left: 6px; }
.lock { margin-left: 4px; }
.policy { margin-right: 14px; }
input[type="checkbox"] { transform: scale(1.15); cursor: pointer; }
input[type="checkbox"]:disabled { cursor: not-allowed; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.row input { padding: 8px 12px; border-radius: 8px; border: 1px solid #334155;
  background: #1e293b; color: #e2e8f0; font-size: 14px; }
.row input[type="number"] { width: 100px; }
.row label { color: #94a3b8; font-size: 13px; display: flex; gap: 6px; align-items: center; }
.row button { padding: 8px 16px; border-radius: 8px; border: none; background: #1e3a5f;
  color: #e2e8f0; cursor: pointer; font-size: 14px; }
.row button:disabled { opacity: .5; cursor: not-allowed; }
.row button.danger { background: #7f1d1d; }
</style>
