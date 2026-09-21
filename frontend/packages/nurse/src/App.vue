<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  type BusEvent,
  listNotices,
  type Notice,
  type NoticeCounts,
  type NotificationEvent,
  parseBusPayload,
} from "shared";
import NursePinGate from "./components/NursePinGate.vue";
import NoticeCard from "./components/NoticeCard.vue";
import PlanPanel from "./components/PlanPanel.vue";
import { beep } from "./lib/beep";

type Filter = "all" | "unread" | "alert" | "cart";
type View = "notices" | "plans";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "unread", label: "未处理" },
  { id: "alert", label: "告警" },
  { id: "cart", label: "小车回报" },
];

const POLL_ONLINE_MS = 30000;
const POLL_OFFLINE_MS = 5000;
const RECONNECT_MS = 3000;
const PLAN_EVENTS = new Set(["plan_created", "plan_updated", "plan_step_changed", "plan_needs_review"]);

const unlocked = ref(false);
const activeView = ref<View>("notices");
const planRefreshToken = ref(0);
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
let workspaceStarted = false;
let workspaceActive = false;

const LEVEL_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 };
const unread = computed(() => counts.value.unread);
const sorted = computed(() => {
  const unacked = notices.value.filter((notice) => !notice.ack_at);
  const acked = notices.value.filter((notice) => !!notice.ack_at);
  unacked.sort((a, b) => (LEVEL_RANK[a.level] ?? 9) - (LEVEL_RANK[b.level] ?? 9)
    || (b.last_at || b.created_at || "").localeCompare(a.last_at || a.created_at || ""));
  acked.sort((a, b) => (b.ack_at || "").localeCompare(a.ack_at || ""));
  return [...unacked, ...acked];
});
const visible = computed(() => sorted.value.filter((notice) => {
  if (filter.value === "unread") return !notice.ack_at;
  if (filter.value === "alert") return notice.level === "critical" || notice.level === "warning";
  if (filter.value === "cart") return notice.source === "cart";
  return true;
}));

function localStamp(ms = Date.now()): string {
  const date = new Date(ms);
  const part = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${part(date.getMonth() + 1)}-${part(date.getDate())} `
    + `${part(date.getHours())}:${part(date.getMinutes())}:${part(date.getSeconds())}`;
}

async function loadNotices() {
  try {
    const result = await listNotices({ state: "all", limit: 50 });
    notices.value = result.items ?? [];
    counts.value = result.counts ?? { unread: 0, critical: 0 };
    loadErr.value = "";
  } catch (error) {
    console.error("[nurse] 读取通知失败", error);
    loadErr.value = "暂时读不到通知，正在自动重试";
  }
}

function stopRealtime() {
  workspaceActive = false;
  es?.close();
  es = null;
  connected.value = false;
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
  pollTimer = null;
  reconnectTimer = null;
}

function schedulePoll() {
  if (!workspaceActive) return;
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(async () => {
    await loadNotices();
    if (workspaceActive) schedulePoll();
  }, connected.value ? POLL_ONLINE_MS : POLL_OFFLINE_MS);
}

function connect() {
  if (es || !unlocked.value || !workspaceActive) return;
  const source = new EventSource("/api/events");
  es = source;
  source.onopen = () => {
    connected.value = true;
    schedulePoll();
    void loadNotices();
    planRefreshToken.value += 1;
  };
  source.onmessage = (message: MessageEvent) => {
    const event = parseBusPayload(message.data as string);
    if (event) onEvent(event);
  };
  source.onerror = () => {
    source.close();
    if (es === source) es = null;
    connected.value = false;
    if (!workspaceActive) return;
    schedulePoll();
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
    reconnectTimer = window.setTimeout(connect, RECONNECT_MS);
  };
}

function fromEvent(event: NotificationEvent): Notice {
  const old = notices.value.find((notice) => notice.id === event.id);
  return {
    id: event.id, level: event.level, source: event.source, type: event.kind,
    uid: event.uid ?? old?.uid ?? "", uid_name: event.uid_name ?? old?.uid_name ?? "",
    title: event.title ?? old?.title ?? "", body: event.body ?? old?.body ?? "",
    ref: old?.ref ?? "", count: event.count ?? old?.count ?? 1,
    created_at: old?.created_at ?? event.last_at ?? "", last_at: event.last_at ?? old?.last_at ?? "",
    ack_at: old?.ack_at ?? "", ack_by: old?.ack_by ?? "",
  };
}

function onEvent(event: BusEvent) {
  if (PLAN_EVENTS.has(event.type)) {
    planRefreshToken.value += 1;
    return;
  }
  if (event.type === "notification") {
    const index = notices.value.findIndex((notice) => notice.id === event.id);
    const item = fromEvent(event);
    if (index >= 0) notices.value.splice(index, 1, item);
    else {
      notices.value.unshift(item);
      counts.value = {
        unread: counts.value.unread + 1,
        critical: counts.value.critical + (item.level === "critical" ? 1 : 0),
      };
    }
    if (!item.ack_at && item.level === "critical") beep(2);
    return;
  }
  if (event.type !== "notification_ack") return;
  const now = localStamp();
  if (event.all) {
    for (const notice of notices.value) {
      if (!notice.ack_at) { notice.ack_at = now; notice.ack_by = event.by ?? "admin"; }
    }
    counts.value = { unread: 0, critical: 0 };
  } else if (typeof event.id === "number") {
    const notice = notices.value.find((item) => item.id === event.id);
    if (notice && !notice.ack_at) {
      notice.ack_at = now;
      notice.ack_by = event.by ?? "admin";
      counts.value = {
        unread: Math.max(0, counts.value.unread - 1),
        critical: Math.max(0, counts.value.critical - (notice.level === "critical" ? 1 : 0)),
      };
    }
  }
}

function onAcked(id: number) {
  const notice = notices.value.find((item) => item.id === id);
  if (!notice || notice.ack_at) return;
  notice.ack_at = localStamp();
  notice.ack_by = "admin";
  counts.value = {
    unread: Math.max(0, counts.value.unread - 1),
    critical: Math.max(0, counts.value.critical - (notice.level === "critical" ? 1 : 0)),
  };
}

async function startUnlockedWorkspace() {
  if (!unlocked.value) return;
  if (workspaceStarted) return;
  workspaceStarted = true;
  workspaceActive = true;
  const tick = () => { clock.value = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
  tick();
  clockTimer = window.setInterval(tick, 1000);
  nowTimer = window.setInterval(() => { nowMs.value = Date.now(); }, 15000);
  connect();
  await loadNotices();
  if (workspaceActive) schedulePoll();
}

function onUnlocked() {
  unlocked.value = true;
  void startUnlockedWorkspace();
}

watch(unread, (count) => {
  document.title = count > 0 ? `(${count}) 护士台` : "护士台";
}, { immediate: true });

onMounted(() => {
  if (unlocked.value) void startUnlockedWorkspace();
});

onUnmounted(() => {
  stopRealtime();
  if (clockTimer !== null) window.clearInterval(clockTimer);
  if (nowTimer !== null) window.clearInterval(nowTimer);
});
</script>

<template>
  <NursePinGate v-if="!unlocked" @unlocked="onUnlocked" />
  <div v-else class="panel">
    <header class="topbar">
      <strong class="brand">护士台</strong>
      <span class="clock">{{ clock }}</span>
      <span :class="['conn', connected ? 'on' : 'off']">{{ connected ? "● 实时在线" : "○ 连接已断" }}</span>
      <span class="badge">未处理 {{ unread }}</span>
    </header>

    <div v-if="!connected" class="redbar">实时连接断开，正在自动刷新</div>

    <nav class="workspace-tabs" aria-label="护士台模块">
      <button :class="{ active: activeView === 'notices' }" @click="activeView = 'notices'">通知</button>
      <button :class="{ active: activeView === 'plans' }" @click="activeView = 'plans'">计划</button>
    </nav>

    <nav v-show="activeView === 'notices'" class="filters" aria-label="通知筛选">
      <button v-for="item in FILTERS" :key="item.id" :class="['tab', { active: filter === item.id }]"
              @click="filter = item.id">{{ item.label }}</button>
    </nav>

    <main v-show="activeView === 'notices'" class="notice-area">
      <p v-if="loadErr" class="error" role="alert">{{ loadErr }}</p>
      <p v-if="!visible.length && !loadErr" class="empty">目前没有通知</p>
      <NoticeCard v-for="notice in visible" :key="notice.id" :notice="notice" :now-ms="nowMs" @acked="onAcked" />
    </main>
    <main v-show="activeView === 'plans'" class="plan-area">
      <PlanPanel :refresh-token="planRefreshToken" />
    </main>
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { background: #f1f5f9; color: #0f172a; font-family: system-ui, "Microsoft YaHei", sans-serif; font-size: 14px; }
</style>
<style scoped>
.panel { display: flex; flex-direction: column; height: 100vh; }
.topbar { display: flex; align-items: center; gap: 14px; padding: 10px 16px; background: #fff; border-bottom: 1px solid #e2e8f0; }
.brand { font-size: 17px; }.clock { color: #334155; font-size: 15px; font-variant-numeric: tabular-nums; }.conn { font-size: 13px; }.conn.on { color: #15803d; }.conn.off { color: #b91c1c; }
.badge { margin-left: auto; padding: 2px 10px; border-radius: 10px; background: #fee2e2; color: #b91c1c; font-size: 13px; }
.redbar { padding: 6px 16px; text-align: center; border-bottom: 1px solid #fecaca; background: #fee2e2; color: #991b1b; font-size: 13px; }
.workspace-tabs, .filters { display: flex; gap: 6px; padding: 8px 16px; background: #fff; border-bottom: 1px solid #e2e8f0; }
.workspace-tabs button, .filters button { padding: 6px 14px; border: 1px solid transparent; border-radius: 5px; background: transparent; color: #475569; cursor: pointer; font: inherit; }
.workspace-tabs button.active { border-color: #1e3a5f; color: #1e3a5f; font-weight: 600; }.filters button { border-color: #cbd5e1; }.filters button.active { border-color: #1e3a5f; background: #1e3a5f; color: #fff; }
.notice-area, .plan-area { flex: 1; overflow-y: auto; padding: 12px 16px; }.empty { color: #64748b; }.error { color: #b91c1c; font-size: 13px; }
@media (max-width: 560px) { .topbar { gap: 8px; flex-wrap: wrap; }.badge { margin-left: 0; }.workspace-tabs, .filters, .notice-area, .plan-area { padding-left: 10px; padding-right: 10px; } }
</style>
