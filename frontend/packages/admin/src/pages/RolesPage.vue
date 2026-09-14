<script setup lang="ts">
// 身份与权限（admin 页签）：策略矩阵只读 + 口令设置 + 会话/病房上下文可调项。
// 规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md（D12 口令可改、D13 口令门可关）
//
// 为什么口令/策略走裸 fetch 而不是 shared：shared 的 apiGet/apiPost 在非 2xx 时直接抛
// `API <status>: <url>`，会**丢掉响应体里的 error/detail**（该文件不在本次改动范围）。
// 这两个接口的失败语义（403 非管理员 / 400 口令太短 / 口令门冷却）恰恰都靠响应体表达，
// 所以这里自带一层 `req()` 把 X-Surface 头和响应体错误一起处理。
import { onMounted, ref } from "vue";
import { changePassword, getAdminAuth, setAdminAuth } from "shared";

interface RolePolicy {
  prompt_file?: string;
  allowed_tools?: string[] | null;
  data_scope?: string;
  ward_context?: boolean;
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

/** 带 X-Surface: admin 的请求；失败时把后端 `error`/`detail` 原样带回，绝不吞成状态码。 */
async function req<T = any>(path: string, body?: unknown): Promise<{ ok: boolean; data?: T;
                                                                   error?: string }> {
  try {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-Surface": "admin" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const b = await res.json().catch(() => null);
    if (!res.ok) return { ok: false, error: b?.error ?? b?.detail ?? `HTTP ${res.status}` };
    return { ok: true, data: b as T };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function say(text: string, ok = true) { msg.value = text; msgOk.value = ok; }

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
  loaded.value = true;
}

async function savePw() {
  if (!newPw.value) { say("请填新口令", false); return; }
  const r = await changePassword(oldPw.value, newPw.value, "admin");
  if (r.ok) { say("✅ 口令已更新"); oldPw.value = ""; newPw.value = ""; }
  else { say(`❌ ${r.error ?? "改口令失败（非管理员会话会 403）"}`, false); }
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
      三层能力由后端策略包（<code>LLM/policy.py</code>）决定，角色由后端按主体推导 —— 管理台只能看，不能改。
      改后端的提示词片段/工具白名单要动代码，不是本页。
    </p>

    <table>
      <thead>
        <tr><th>角色</th><th>提示词片段</th><th>工具白名单</th><th>数据范围</th><th>读病房上下文</th></tr>
      </thead>
      <tbody>
        <tr v-for="(pol, role) in roles" :key="role">
          <td class="mono">{{ role }}</td>
          <td class="mono">{{ pol.prompt_file }}</td>
          <td class="mono">{{ pol.allowed_tools === null || pol.allowed_tools === undefined
            ? "全部（不按角色裁剪）" : (pol.allowed_tools.join(", ") || "无（空列表）") }}</td>
          <td class="mono">{{ pol.data_scope }}</td>
          <td>{{ pol.ward_context ? "是" : "否" }}</td>
        </tr>
        <tr v-if="loaded && !Object.keys(roles).length"><td colspan="5">读不到策略矩阵</td></tr>
      </tbody>
    </table>

    <h4>口令设置</h4>
    <p v-if="!authRequired" class="warn">
      ⚠️ 口令门当前是关的：任何人都能进管理台（含车前屏点「管理层」）
    </p>
    <div class="row">
      <input v-model="oldPw" type="password" placeholder="旧口令（已设过口令就必填）" />
      <input v-model="newPw" type="password" placeholder="新口令（后端要求 ≥4 位）" />
      <button @click="savePw">改口令</button>
      <button @click="toggleAuth">{{ authRequired ? "关闭口令门" : "开启口令门" }}</button>
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
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
code { background: #1e293b; padding: 1px 5px; border-radius: 4px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.row input { padding: 8px 12px; border-radius: 8px; border: 1px solid #334155;
  background: #1e293b; color: #e2e8f0; font-size: 14px; }
.row input[type="number"] { width: 100px; }
.row label { color: #94a3b8; font-size: 13px; display: flex; gap: 6px; align-items: center; }
.row button { padding: 8px 16px; border-radius: 8px; border: none; background: #1e3a5f;
  color: #e2e8f0; cursor: pointer; font-size: 14px; }
</style>
