<script setup lang="ts">
// 护士台壳（规格 §5.2 / D11）：顶栏 + 筛选 + 通知列表 + 断线红条。**无登录门**。
//
// 职责边界（D9/D10）：**只读 + 确认** —— 看通知、点「处理了」、一眼看清未处理条数。
// 不放对话、设置、记忆、地图、老人注册、身份与权限 —— 一个都不放进来。
//
// D11（2026-09-18 用户拍板「护士台不再需要登录、也不会被弹」）：打开 `/nurse` 直接就是
// 通知列表 —— 不调 `login`/`getSessionUser`/`logout`、不连会话、没有"会话过期"概念，
// 因此也不存在被弹回登录页的问题。对应的三条读/确认端点在后端也已免鉴权。
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  type BusEvent, listNotices,
  type Notice, type NoticeCounts, type NotificationEvent, parseBusPayload,
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

// ---- 列表 ----
async function loadNotices() {
  try {
    const r = await listNotices({ state: "all", limit: 50 });
    notices.value = r.items ?? [];
    counts.value = r.counts ?? { unread: 0, critical: 0 };
    loadErr.value = "";
  } catch (e) {
    console.error("[nurse] 读取通知失败", e);          // 原始错误只进控制台，不上屏
    loadErr.value = "暂时读不到通知，正在自动重试";      // 不静默（规格 §5.2 空态/错误态）
  }
}

/** 关实时连接 + 清掉**全部**计时器（含 3 秒重连计时器）的唯一收口。
 *  D11 后没有"会话丢了"这条路径，改由**组件卸载**调用（onUnmounted）——
 *  否则 EventSource 报错排下的 connect() 会在页面卸载后照旧打开 `/api/events`。 */
function stopRealtime() {
  es?.close(); es = null;
  connected.value = false;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
}

/** 轮询兜底：在线 30s、断线 5s；每次跑完重新排下一轮（所以间隔能随连接状态变化）。 */
function schedulePoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  const delay = connected.value ? POLL_ONLINE_MS : POLL_OFFLINE_MS;
  pollTimer = window.setTimeout(async () => {
    await loadNotices();
    schedulePoll();
  }, delay);
}

// ---- SSE ----
function connect() {
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

onMounted(async () => {
  const tick = () => { clock.value = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
  tick();
  clockTimer = window.setInterval(tick, 1000);                 // 顶栏实时时钟
  nowTimer = window.setInterval(() => { nowMs.value = Date.now(); }, 15000);   // 相对时间刷新
  connect();                         // D11：打开页面即连实时（无登录门、无会话前置条件）
  await loadNotices();
  schedulePoll();
});

onUnmounted(() => {
  stopRealtime();                    // 卸载收口：关连接 + 清轮询/重连计时器（D11 后无登出路径）
  if (clockTimer) clearInterval(clockTimer);
  if (nowTimer) clearInterval(nowTimer);
});
</script>

<template>
  <!-- D11：无登录门 —— 打开页面直接就是通知列表 -->
  <div class="panel">
    <header class="topbar">
      <strong class="brand">护士台</strong>
      <span class="clock">{{ clock }}</span>
      <span :class="['conn', connected ? 'on' : 'off']">
        {{ connected ? "● 实时在线" : "○ 连接已断" }}
      </span>
      <span class="badge">未处理 {{ unread }}</span>
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

/* ---- 顶栏 / 红条 / 筛选 ---- */
.topbar { display: flex; align-items: center; gap: 14px; padding: 10px 16px;
  background: #ffffff; border-bottom: 1px solid #e2e8f0; }
.brand { font-size: 17px; }
.clock { font-size: 15px; color: #334155; font-variant-numeric: tabular-nums; }
.conn { font-size: 13px; }
.conn.on { color: #15803d; }
.conn.off { color: #b91c1c; }
/* 未处理徽章靠右（原先由「退出」按钮的 margin-left:auto 撑开，D11 去掉按钮后自己撑） */
.badge { font-size: 13px; background: #fee2e2; color: #b91c1c;
  border-radius: 10px; padding: 2px 10px; margin-left: auto; }
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
