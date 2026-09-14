<script setup lang="ts">
// admin 壳：登录门 + 10 页签 + SSE toast（规格 §6 / docs/superpowers/specs/2026-09-14-layered-user-roles-design.md）
//
// 登录门（D13）：**判据是 `role !== "admin"`**。未登录时后端 `getSessionUser("admin")` 返回的
// `role` 是 `"ward"`（fail-closed），所以「不是 admin」= 必须显示登录卡 —— 绝不能写成
// `role === "ward"`（哪天后端多一个角色就漏门）。
import { onMounted, onUnmounted, ref } from "vue";
import { type BusEvent, getSessionUser, login, logout, parseBusPayload,
         type SessionUser } from "shared";
import OverviewPage from "./pages/OverviewPage.vue";
import RegisterPage from "./pages/RegisterPage.vue";
import ChatPage from "./pages/ChatPage.vue";
import MemoriesPage from "./pages/MemoriesPage.vue";
import RemindersPage from "./pages/RemindersPage.vue";
import WardsPage from "./pages/WardsPage.vue";
import ToolLogPage from "./pages/ToolLogPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import VoiceStatusPage from "./pages/VoiceStatusPage.vue";
import RolesPage from "./pages/RolesPage.vue";

const tabs = [
  { id: "overview", label: "监控总览" },
  { id: "register", label: "老人注册" },
  { id: "chat", label: "对话" },
  { id: "memories", label: "记忆" },
  { id: "reminders", label: "提醒" },
  { id: "wards", label: "病房管理" },
  { id: "tools", label: "工具日志" },
  { id: "voice", label: "语音状态" },
  { id: "roles", label: "身份与权限" },
  { id: "settings", label: "设置" },
];
const active = ref("overview");

// ---- 登录门状态 ----
const session = ref<SessionUser | null>(null);
const pw = ref("");
const loginErr = ref("");
const loading = ref(true);
const loggingIn = ref(false);

const toasts = ref<{ id: number; text: string }[]>([]);
let es: EventSource | null = null;
let ttlTimer: number | null = null;

function pushToast(text: string) {
  const id = Date.now();
  toasts.value.push({ id, text });
  // 捕获 id 供过滤（不能用 Date.now()——6 秒后已推进，恒不相等导致 toast 永不消失）
  setTimeout(() => {
    toasts.value = toasts.value.filter((t) => t.id !== id);
  }, 6000);
}

function onEvent(ev: BusEvent) {
  const admin = session.value?.role === "admin";
  // 未登录时不弹提醒/告警 toast（登录卡上不该弹）；但**会话类事件永远要处理**——
  // 管理员超时降权正是靠 session_expired/user_changed 立刻把登录门收回来。
  if (admin) {
    if (ev.type === "reminder") pushToast(`⏰ ${ev.title}：${ev.content}`);
    if (ev.type === "alarm") pushToast(`🚨 ${ev.alarm_type ?? ev.type}：${ev.message ?? "告警"}`);
    if (ev.type === "user_changed") pushToast(`👤 当前用户切换为 ${ev.uid}`);
  }
  // 管理员会话超时 / 口令门被改 / 病房增删改 → 重新拉会话（角色、TTL、警示条都靠它）
  if (ev.type === "session_expired" || ev.type === "user_changed"
      || ev.type === "ward_changed" || ev.type === "admin_auth_changed") { loadSession(); }
}

// ---- 会话 ----
async function loadSession() {
  try {
    session.value = await getSessionUser("admin");
    loginErr.value = "";
  } catch (e) {
    // 拉不到会话**不清空**已登录态（网络抖动不该把管理员踢出）；
    // 但首轮失败时 session 仍为 null → 登录门照样显示（fail-closed）。
    if (!session.value) loginErr.value = `读取会话失败：${e}`;
  } finally {
    loading.value = false;
  }
}

async function doLogin() {
  loginErr.value = "";
  loggingIn.value = true;
  try {
    // 口令门关着（auth_required=false）时传 null 直接进
    const r = await login(pw.value ? pw.value : null, "admin");
    if (!r.ok) { loginErr.value = r.error ?? "登录失败"; return; }
    pw.value = "";
    await loadSession();
    connect();                       // 进门前不连 SSE（登录卡上不该弹 toast）
  } catch (e) {
    loginErr.value = `登录失败：${e}`;
  } finally {
    loggingIn.value = false;
  }
}

async function doLogout() {
  try { await logout("admin"); } catch { /* 忽略：下面照样刷新会话 */ }
  es?.close(); es = null;
  await loadSession();
}

function connect() {
  if (es) return;
  es = new EventSource("/api/events");
  es.onmessage = (msg: MessageEvent) => {
    // EventSource 的 msg.data 已剥离 "data:" 前缀——用 parseBusPayload（parseSseChunk 要求前缀会全丢）
    const ev = parseBusPayload(msg.data as string);
    if (ev) onEvent(ev);
  };
  es.onerror = () => { es?.close(); es = null; setTimeout(connect, 3000); };
}

onMounted(async () => {
  await loadSession();
  if (session.value?.role === "admin") connect();
  // TTL 倒计时必须真的在走（SSE 只在状态变化时发事件）；顺带兜住 session_expired 丢包
  ttlTimer = window.setInterval(loadSession, 15000);
});
onUnmounted(() => {
  es?.close();
  if (ttlTimer) clearInterval(ttlTimer);
});
</script>

<template>
  <div v-if="loading" class="gate">载入中…</div>

  <!-- 登录门：不是 admin 就整站只显示这张卡（fail-closed） -->
  <div v-else-if="!session || session.role !== 'admin'" class="gate login">
    <h3>🛡 管理员登录</h3>
    <p v-if="session && !session.auth_required" class="warn">
      当前口令门已关闭，直接点「进入」即可（建议尽快到「身份与权限」页开启）
    </p>
    <input v-if="!session || session.auth_required" v-model="pw" type="password"
           placeholder="管理员口令" @keyup.enter="doLogin" />
    <button :disabled="loggingIn" @click="doLogin">{{ loggingIn ? "进入中…" : "进入" }}</button>
    <p class="hint">口令门关闭时无需口令；连续 3 次输错会冷却 10 秒。</p>
    <p v-if="loginErr" class="warn">{{ loginErr }}</p>
  </div>

  <div v-else class="admin">
    <header class="topbar">
      <strong>陪护机器人 · 管理台</strong>
      <span class="who">
        🛡 管理员<span v-if="session.ttl_remain != null">
          （剩 {{ Math.floor(session.ttl_remain / 60) }} 分 {{ session.ttl_remain % 60 }} 秒）</span>
      </span>
      <button @click="doLogout">退出</button>
    </header>
    <div v-if="!session.auth_required" class="redbar">
      ⚠️ 当前无口令保护，任何人都能进管理台
    </div>
    <nav>
      <button v-for="t in tabs" :key="t.id" :class="{ active: active === t.id }"
              @click="active = t.id">{{ t.label }}</button>
    </nav>
    <main>
      <OverviewPage v-if="active === 'overview'" />
      <RegisterPage v-else-if="active === 'register'" />
      <ChatPage v-else-if="active === 'chat'" />
      <MemoriesPage v-else-if="active === 'memories'" />
      <RemindersPage v-else-if="active === 'reminders'" />
      <WardsPage v-else-if="active === 'wards'" />
      <ToolLogPage v-else-if="active === 'tools'" />
      <VoiceStatusPage v-else-if="active === 'voice'" />
      <RolesPage v-else-if="active === 'roles'" />
      <SettingsPage v-else-if="active === 'settings'" />
    </main>
    <div class="toasts">
      <div v-for="t in toasts" :key="t.id" class="toast">{{ t.text }}</div>
    </div>
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { background: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; }
</style>
<style scoped>
.admin { display: flex; flex-direction: column; height: 100vh; }

/* ---- 登录门 ---- */
.gate { height: 100%; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 12px; color: #94a3b8; }
.gate.login { background: #111827; }
.gate.login h3 { margin: 0; color: #f8fafc; font-size: 20px; }
.gate.login input { padding: 10px 14px; border-radius: 8px; border: 1px solid #334155;
  background: #1e293b; color: #e2e8f0; font-size: 15px; width: 240px; text-align: center; }
.gate.login > button { padding: 10px 26px; border-radius: 8px; border: none; background: #1f9d55;
  color: #f8fafc; font-size: 15px; cursor: pointer; }
.gate.login > button:disabled { opacity: 0.6; cursor: not-allowed; }
.gate .warn { color: #fbbf24; font-size: 13px; max-width: 420px; text-align: center; margin: 0; }
.gate .hint { color: #64748b; font-size: 12px; margin: 0; }

/* ---- 顶栏 / 警示条 / 页签 ---- */
.topbar { display: flex; align-items: center; gap: 14px; padding: 10px 16px;
  background: #1e293b; border-bottom: 1px solid #1f2937; }
.topbar .who { margin-left: auto; color: #94a3b8; font-size: 13px; }
.topbar button { padding: 6px 14px; border-radius: 8px; border: 1px solid #334155;
  background: #0f172a; color: #e2e8f0; cursor: pointer; font-size: 13px; }
.redbar { background: #7f1d1d; color: #fee2e2; padding: 8px 16px; font-size: 13px;
  text-align: center; }
nav { display: flex; gap: 4px; padding: 10px 16px; background: #111827;
  border-bottom: 1px solid #1f2937; flex-wrap: wrap; }
nav button { background: none; border: none; color: #94a3b8; padding: 10px 18px;
  border-radius: 10px; font-size: 15px; cursor: pointer; }
nav button.active { background: #1e3a5f; color: #f8fafc; }
main { flex: 1; overflow-y: auto; padding: 20px; }
.toasts { position: fixed; top: 60px; right: 20px; display: flex;
  flex-direction: column; gap: 8px; z-index: 100; }
.toast { background: #1e3a5f; color: #f8fafc; padding: 12px 18px;
  border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,.4); }
</style>
