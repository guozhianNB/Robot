<script setup lang="ts">
// admin 壳：7 页签 + SSE toast（沿用旧前端单页风格，规格 §6）
import { onUnmounted, ref } from "vue";
import { type BusEvent, parseBusPayload } from "shared";
import OverviewPage from "./pages/OverviewPage.vue";
import ChatPage from "./pages/ChatPage.vue";
import MemoriesPage from "./pages/MemoriesPage.vue";
import RemindersPage from "./pages/RemindersPage.vue";
import ToolLogPage from "./pages/ToolLogPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import VoiceStatusPage from "./pages/VoiceStatusPage.vue";

const tabs = [
  { id: "overview", label: "监控总览" },
  { id: "chat", label: "对话" },
  { id: "memories", label: "记忆" },
  { id: "reminders", label: "提醒" },
  { id: "tools", label: "工具日志" },
  { id: "voice", label: "语音状态" },
  { id: "settings", label: "设置" },
];
const active = ref("overview");
const toasts = ref<{ id: number; text: string }[]>([]);
let es: EventSource | null = null;

function pushToast(text: string) {
  toasts.value.push({ id: Date.now(), text });
  setTimeout(() => {
    toasts.value = toasts.value.filter((t) => t.id !== Date.now());
  }, 6000);
}

function onEvent(ev: BusEvent) {
  if (ev.type === "reminder") pushToast(`⏰ ${ev.title}：${ev.content}`);
  if (ev.type === "alarm") pushToast(`🚨 ${ev.type}：${ev.message ?? "告警"}`);
  if (ev.type === "user_changed") pushToast(`👤 当前用户切换为 ${ev.uid}`);
}

function connect() {
  es = new EventSource("/api/events");
  es.onmessage = (msg: MessageEvent) => {
    // EventSource 的 msg.data 已剥离 "data:" 前缀——用 parseBusPayload（parseSseChunk 要求前缀会全丢）
    const ev = parseBusPayload(msg.data as string);
    if (ev) onEvent(ev);
  };
  es.onerror = () => { es?.close(); setTimeout(connect, 3000); };
}

connect();
onUnmounted(() => es?.close());
</script>

<template>
  <div class="admin">
    <nav>
      <button v-for="t in tabs" :key="t.id" :class="{ active: active === t.id }"
              @click="active = t.id">{{ t.label }}</button>
    </nav>
    <main>
      <OverviewPage v-if="active === 'overview'" />
      <ChatPage v-else-if="active === 'chat'" />
      <MemoriesPage v-else-if="active === 'memories'" />
      <RemindersPage v-else-if="active === 'reminders'" />
      <ToolLogPage v-else-if="active === 'tools'" />
      <VoiceStatusPage v-else-if="active === 'voice'" />
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
nav { display: flex; gap: 4px; padding: 10px 16px; background: #111827;
  border-bottom: 1px solid #1f2937; }
nav button { background: none; border: none; color: #94a3b8; padding: 10px 18px;
  border-radius: 10px; font-size: 15px; cursor: pointer; }
nav button.active { background: #1e3a5f; color: #f8fafc; }
main { flex: 1; overflow-y: auto; padding: 20px; }
.toasts { position: fixed; top: 60px; right: 20px; display: flex;
  flex-direction: column; gap: 8px; z-index: 100; }
.toast { background: #1e3a5f; color: #f8fafc; padding: 12px 18px;
  border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,.4); }
</style>
