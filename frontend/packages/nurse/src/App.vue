<script setup lang="ts">
// 护士台壳（规格 §5.2 / 简报 §4）：登录门 + 顶栏 + 筛选 + 通知列表 + 断线红条。
//
// 职责边界（D9/D10）：**只读 + 确认** —— 看通知、点「处理了」、一眼看清未处理条数。
// 不放对话、设置、记忆、地图、老人注册、身份与权限 —— 一个都不放进来。
//
// 登录门判据是 `role !== "admin"`（照 admin/src/App.vue 的既定写法）：后端未登录时
// `getSessionUser("admin")` 返回 `role="ward"`（fail-closed），写成 `=== "ward"`
// 会在后端哪天新增角色时漏门。
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  type BusEvent, getSessionUser, listNotices, login, logout,
  type Notice, type NoticeCounts, type NotificationEvent, parseBusPayload, type SessionUser,
} from "shared";
import NoticeCard from "./components/NoticeCard.vue";
import { beep } from "./lib/beep";

type Filter = "all" | "unread" | "alert" | "cart";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "unread", label: "未处理" },
  { id: "alert", label: "告警" },
  { id: "cart", label: "小车回报" },
];

const POLL_ONLINE_MS = 30000;      // 在线：30s 兜底轮询（防 SSE 静默丢帧）
const POLL_OFFLINE_MS = 5000;      // 断线：5s 兜底轮询
const RECONNECT_MS = 3000;         // SSE 断线重连间隔（照 admin）

// ---- 登录门状态 ----
const session = ref<SessionUser | null>(null);
const pw = ref("");
const loginErr = ref("");
const loading = ref(true);
const loggingIn = ref(false);

/** 登录门判据的唯一真相：不是 admin 一律视为未登录 —— 未登录绝不开实时连接。 */
const isAdmin = computed(() => session.value?.role === "admin");

// ---- 面板状态 ----
const notices = ref<Notice[]>([]);
const counts = ref<NoticeCounts>({ unread: 0, critical: 0 });
const loadErr = ref("");
const filter = ref<Filter>("all");
const connected = ref(false);
const clock = ref("");
const nowMs = ref(Date.now());

let es: EventSource | null = null;
let pollTimer: number | null = null;
let clockTimer: number | null = null;
let nowTimer: number | null = null;
let reconnectTimer: number | null = null;

const LEVEL_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 };
const unread = computed(() => counts.value.unread);

function tsOf(n: Notice): string {
  return n.last_at || n.created_at || "";
}

/** 排序（规格 §5.2）：未处理全部在前 —— 先按级别 critical>warning>info，同级按 last_at 倒序；
 *  已处理的沉到底部，按 ack_at 倒序。 */
const sorted = computed(() => {
  const unacked = notices.value.filter((n) => !n.ack_at);
  const acked = notices.value.filter((n) => !!n.ack_at);
  unacked.sort((a, b) =>
    (LEVEL_RANK[a.level] ?? 9) - (LEVEL_RANK[b.level] ?? 9)
    || tsOf(b).localeCompare(tsOf(a)));
  acked.sort((a, b) => (b.ack_at || "").localeCompare(a.ack_at || ""));
  return [...unacked, ...acked];
});

/** 筛选：纯前端过滤，不发新请求（规格 §5.2）。 */
const visible = computed(() => sorted.value.filter((n) => {
  if (filter.value === "unread") return !n.ack_at;
  if (filter.value === "alert") return n.level === "critical" || n.level === "warning";
  if (filter.value === "cart") return n.source === "cart";
  return true;
}));

// ---- 工具 ----
/** 本地时间 → 与后端同形的 `YYYY-MM-DD HH:MM:SS`（relativeTime 直接吃得下）。 */
function localStamp(ms: number = Date.now()): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function isAuthError(e: unknown): boolean {
  return /API (401|403)/.test(String(e));
}

// ---- 列表 ----
async function loadNotices() {
  try {
    const r = await listNotices({ state: "all", limit: 50 });
    notices.value = r.items ?? [];
    counts.value = r.counts ?? { unread: 0, critical: 0 };
    loadErr.value = "";
  } catch (e) {
    if (isAuthError(e)) { dropSession(); return; }   // 401/403 → 回登录门
    console.error("[nurse] 读取通知失败", e);          // 原始错误只进控制台，不上屏
    loadErr.value = "暂时读不到通知，正在自动重试";      // 不静默（规格 §5.2 空态/错误态）
  }
}

/** 丢掉会话 / 回登录门 / 卸载的唯一收口：关掉实时连接 + 清掉**全部**计时器
 *  （含 3 秒重连计时器）—— 否则 SSE 报错排下的 connect() 会在登出后照旧打开
 *  `/api/events`，违反「未登录不得连 SSE」。 */
function stopRealtime() {
  es?.close(); es = null;
  connected.value = false;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
}

/** 会话失效：退回登录门（重新拉一次会话，让口令门状态也是新的）。 */
function dropSession() {
  stopRealtime();
  session.value = null;
  void loadSession();
}

/** 轮询兜底：在线 30s、断线 5s；每次跑完重新排下一轮（所以间隔能随连接状态变化）。 */
function schedulePoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (!session.value || session.value.role !== "admin") return;
  const delay = connected.value ? POLL_ONLINE_MS : POLL_OFFLINE_MS;
  pollTimer = window.setTimeout(async () => {
    await loadNotices();
    schedulePoll();
  }, delay);
}

// ---- 会话 ----
async function loadSession() {
  try {
    session.value = await getSessionUser("admin");
    loginErr.value = "";
  } catch (e) {
    console.error("[nurse] 读取登录状态失败", e);      // 原始错误只进控制台，不上屏
    if (!session.value) loginErr.value = "读取登录状态失败，请刷新页面重试";
  } finally {
    loading.value = false;
  }
}

async function doLogin() {
  loginErr.value = "";
  loggingIn.value = true;
  try {
    const r = await login(pw.value ? pw.value : null, "admin");
    if (!r.ok) { loginErr.value = r.error ?? "登录失败"; return; }
    pw.value = "";
    await loadSession();
    connect();                       // 未登录不连 SSE（登录卡上不该有告警声）
    await loadNotices();
    schedulePoll();
  } catch (e) {
    console.error("[nurse] 登录失败", e);              // 原始错误只进控制台，不上屏
    loginErr.value = "登录没能成功，请检查网络后重试";
  } finally {
    loggingIn.value = false;
  }
}

async function doLogout() {
  try { await logout("admin"); } catch { /* 忽略：下面照样刷新会话 */ }
  stopRealtime();                     // 关实时连接 + 清轮询/重连计时器（登出后不重连）
  notices.value = [];
  counts.value = { unread: 0, critical: 0 };
  loadErr.value = "";
  await loadSession();
}

// ---- SSE ----
function connect() {
  if (!isAdmin.value) return;          // 未登录不连 SSE（登录卡上不该有告警声）
  if (es) return;
  es = new EventSource("/api/events");
  es.onopen = () => { connected.value = true; schedulePoll(); };
  es.onmessage = (msg: MessageEvent) => {
    // EventSource 的 msg.data 已剥离 "data:" 前缀 —— 必须用 parseBusPayload（不是 parseSseChunk）
    const ev = parseBusPayload(msg.data as string);
    if (ev) onEvent(ev);
  };
  es.onerror = () => {
    es?.close(); es = null;
    connected.value = false;
    schedulePoll();                  // 断线期间切到 5s 兜底轮询
    if (!isAdmin.value) return;      // 会话已丢 → 绝不排重连
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = window.setTimeout(connect, RECONNECT_MS);
  };
}

/** 总线事件 → 本地列表（补 SSE payload 没有的列，保留旧行已有字段）。 */
function fromEvent(ev: NotificationEvent): Notice {
  const old = notices.value.find((n) => n.id === ev.id);
  return {
    id: ev.id,
    level: ev.level,
    source: ev.source,
    type: ev.kind,                                  // ⚠️ 通知类型在 kind 里（D6），不是 type
    uid: ev.uid ?? old?.uid ?? "",
    uid_name: ev.uid_name ?? old?.uid_name ?? "",
    title: ev.title ?? old?.title ?? "",
    body: ev.body ?? old?.body ?? "",
    ref: old?.ref ?? "",
    count: ev.count ?? old?.count ?? 1,
    created_at: old?.created_at ?? ev.last_at ?? "",
    last_at: ev.last_at ?? old?.last_at ?? "",
    ack_at: old?.ack_at ?? "",
    ack_by: old?.ack_by ?? "",
  };
}

function onEvent(ev: BusEvent) {
  if (ev.type === "notification") {
    const i = notices.value.findIndex((n) => n.id === ev.id);
    const item = fromEvent(ev);
    if (i >= 0) {
      notices.value.splice(i, 1, item);            // 同 id 已存在 → 替换，不新增
    } else {
      notices.value.unshift(item);
      // 计数本地即时更新（精确值由下一次轮询的 counts 校正）
      counts.value = {
        unread: counts.value.unread + 1,
        critical: counts.value.critical + (item.level === "critical" ? 1 : 0),
      };
    }
    // 新/合并的 critical 且未处理 → 两声蜂鸣（规格 §4.3）
    if (!item.ack_at && item.level === "critical") beep(2);
    return;
  }
  if (ev.type === "notification_ack") {
    const now = localStamp();
    if (ev.all) {
      for (const n of notices.value) {
        if (!n.ack_at) { n.ack_at = now; n.ack_by = ev.by ?? "admin"; }
      }
      counts.value = { unread: 0, critical: 0 };
    } else if (typeof ev.id === "number") {
      const n = notices.value.find((x) => x.id === ev.id);
      if (n && !n.ack_at) {
        n.ack_at = now;
        n.ack_by = ev.by ?? "admin";
        counts.value = {
          unread: Math.max(0, counts.value.unread - 1),
          critical: Math.max(0, counts.value.critical - (n.level === "critical" ? 1 : 0)),
        };
      }
    }
  }
}

/** 卡片点「处理了」成功后的本地标记（不等 SSE 回包，多屏同步由 notification_ack 负责）。 */
function onAcked(id: number) {
  const n = notices.value.find((x) => x.id === id);
  if (!n || n.ack_at) return;
  n.ack_at = localStamp();
  n.ack_by = "admin";
  counts.value = {
    unread: Math.max(0, counts.value.unread - 1),
    critical: Math.max(0, counts.value.critical - (n.level === "critical" ? 1 : 0)),
  };
}

// 未处理数 → 浏览器标签标题（规格 §4.3）
watch(unread, (n) => { document.title = n > 0 ? `(${n}) 护士台` : "护士台"; }, { immediate: true });

/** 纪律兜底：`isAdmin` 变假（会话丢了）→ **无条件**收口实时连接。
 *  现有各丢会话路径都已显式调 stopRealtime()，但那是"纪律"不是"结构"——
 *  将来新增"把 session 置空却不走该收口"的路径会漏关 SSE。 */
watch(isAdmin, (ok) => { if (!ok) stopRealtime(); });

onMounted(async () => {
  const tick = () => { clock.value = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
  tick();
  clockTimer = window.setInterval(tick, 1000);                 // 顶栏实时时钟
  nowTimer = window.setInterval(() => { nowMs.value = Date.now(); }, 15000);   // 相对时间刷新
  await loadSession();
  if (isAdmin.value) {
    connect();
    await loadNotices();
    schedulePoll();
  }
});

onUnmounted(() => {
  stopRealtime();                    // 与 dropSession/doLogout 同一口径：连接 + 轮询 + 重连计时器
  if (clockTimer) clearInterval(clockTimer);
  if (nowTimer) clearInterval(nowTimer);
});
</script>

<template>
  <div v-if="loading" class="gate">载入中…</div>

  <!-- 登录门：不是 admin 就整站只显示这张卡（fail-closed） -->
  <div v-else-if="!session || session.role !== 'admin'" class="gate login">
    <h3>🏥 护士台登录</h3>
    <p v-if="session && !session.auth_required" class="warn">
      当前口令门已关闭，直接点「进入」即可
    </p>
    <input v-if="!session || session.auth_required" v-model="pw" type="password"
           placeholder="护士台口令（同管理台）" @keyup.enter="doLogin" />
    <button :disabled="loggingIn" @click="doLogin">{{ loggingIn ? "进入中…" : "进入" }}</button>
    <p class="hint">口令门关闭时无需口令；连续 3 次输错会冷却 10 秒。</p>
    <p v-if="loginErr" class="warn">{{ loginErr }}</p>
  </div>

  <div v-else class="panel">
    <header class="topbar">
      <strong class="brand">护士台</strong>
      <span class="clock">{{ clock }}</span>
      <span :class="['conn', connected ? 'on' : 'off']">
        {{ connected ? "● 实时在线" : "○ 连接已断" }}
      </span>
      <span class="badge">未处理 {{ unread }}</span>
      <button class="ghost" @click="doLogout">退出</button>
    </header>

    <!-- 断线红条（规格 §4.4）：SSE 断了也必须让护士看得出来 -->
    <div v-if="!connected" class="redbar">实时连接断开，正在自动刷新</div>

    <nav class="filters">
      <button v-for="f in FILTERS" :key="f.id" :class="['tab', { active: filter === f.id }]"
              @click="filter = f.id">{{ f.label }}</button>
    </nav>

    <main>
      <p v-if="loadErr" class="warn err">{{ loadErr }}</p>
      <p v-if="!visible.length && !loadErr" class="empty">目前没有通知</p>
      <NoticeCard v-for="n in visible" :key="n.id" :notice="n" :now-ms="nowMs" @acked="onAcked" />
    </main>
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
/* 浅色主题 + 普通 PC 控制台尺寸（D7：不做大按钮、不放大字号） */
body { background: #f1f5f9; color: #0f172a;
  font-family: system-ui, "Microsoft YaHei", sans-serif; font-size: 14px; }
</style>
<style scoped>
.panel { display: flex; flex-direction: column; height: 100vh; }

/* ---- 登录门 ---- */
.gate { height: 100%; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 12px; color: #475569; }
.gate.login h3 { margin: 0; color: #0f172a; font-size: 18px; }
.gate.login input { padding: 8px 12px; border-radius: 6px; border: 1px solid #cbd5e1;
  background: #fff; color: #0f172a; font-size: 15px; width: 240px; text-align: center; }
.gate.login > button { padding: 8px 22px; border-radius: 6px; border: none; background: #1f9d55;
  color: #fff; font-size: 15px; cursor: pointer; }
.gate.login > button:disabled { opacity: 0.6; cursor: not-allowed; }
.gate .warn { color: #b45309; font-size: 13px; max-width: 420px; text-align: center; margin: 0; }
.gate .hint { color: #64748b; font-size: 12px; margin: 0; }

/* ---- 顶栏 / 红条 / 筛选 ---- */
.topbar { display: flex; align-items: center; gap: 14px; padding: 10px 16px;
  background: #ffffff; border-bottom: 1px solid #e2e8f0; }
.brand { font-size: 17px; }
.clock { font-size: 15px; color: #334155; font-variant-numeric: tabular-nums; }
.conn { font-size: 13px; }
.conn.on { color: #15803d; }
.conn.off { color: #b91c1c; }
.badge { font-size: 13px; background: #fee2e2; color: #b91c1c;
  border-radius: 10px; padding: 2px 10px; }
.topbar .ghost:first-of-type { margin-left: auto; }
.topbar .ghost { padding: 6px 14px; border-radius: 6px; border: 1px solid #cbd5e1;
  background: #fff; color: #334155; cursor: pointer; font-size: 13px; }
.redbar { background: #fee2e2; color: #991b1b; padding: 6px 16px; font-size: 13px;
  text-align: center; border-bottom: 1px solid #fecaca; }

.filters { display: flex; gap: 6px; padding: 10px 16px;
  background: #fff; border-bottom: 1px solid #e2e8f0; }
.filters .tab { padding: 6px 14px; border-radius: 6px; border: 1px solid #cbd5e1;
  background: #fff; color: #334155; cursor: pointer; font-size: 14px; }
.filters .tab.active { background: #1e3a5f; border-color: #1e3a5f; color: #fff; }

main { flex: 1; overflow-y: auto; padding: 14px 16px; }
.empty { color: #64748b; font-size: 14px; }
.err { font-size: 13px; }
</style>
