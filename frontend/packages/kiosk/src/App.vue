<script setup lang="ts">
// kiosk 主界面：状态条 + 对话区 + 提醒 + SOS（规格 §5）
import { onMounted, ref } from "vue";
import {
  type BusEvent, type ReminderEvent, setSessionUser,
  reportAlarm, getSessionUser,
} from "shared";
import { useBus } from "./useBus";
import VoiceStatusBar from "./components/VoiceStatusBar.vue";
import ChatArea, { type Msg } from "./components/ChatArea.vue";
import ReminderBanner from "./components/ReminderBanner.vue";
import SosButton from "./components/SosButton.vue";

const state = ref("idle");
const uid = ref<string | null>(null);
const locked = ref(false);
const messages = ref<Msg[]>([]);
const reminder = ref<ReminderEvent | null>(null);
const { connected } = useBus(onEvent);

async function loadSession() {
  try {
    const s = await getSessionUser();
    uid.value = s.uid;
    locked.value = s.locked;
  } catch { /* 后端未就绪时忽略 */ }
}

function onEvent(ev: BusEvent) {
  if (ev.type === "voice_state") state.value = ev.state;
  if (ev.type === "chat_new") {
    messages.value.push({ role: "user", content: ev.user, uid: ev.uid });
    messages.value.push({ role: "assistant", content: ev.assistant });
  }
  if (ev.type === "reminder") reminder.value = ev;
  if (ev.type === "user_changed") {
    uid.value = ev.uid;
    locked.value = ev.locked;
  }
}

async function sendText(text: string) {
  messages.value.push({ role: "user", content: text, uid: uid.value ?? undefined });
  messages.value.push({ role: "assistant", content: "" });  // 占位，流式填充
  const last = messages.value[messages.value.length - 1];
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: uid.value ?? "elder_001", message: text, thinking: "auto" }),
    });
    if (!res.ok || !res.body) throw new Error("chat failed");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    // /api/chat 返回的是 chat_stream 事件（reasoning/content/done），
    // 不是 bus 广播事件 —— 直接按 data: 行解析，不能用 parseSseChunk（只认 bus 类型）
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const line of decoder.decode(value, { stream: true }).split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "content") last.content += ev.content;
          if (ev.type === "done") break;
        } catch { /* 坏帧忽略 */ }
      }
    }
    if (!last.content) last.content = "（无回复）";
  } catch {
    last.content = "（发送失败，请重试）";
  }
}

async function onSos() {
  try {
    await reportAlarm("sos", uid.value ?? "", "老人按下紧急呼叫按钮");
  } catch { /* 广播失败也要提示用户 */ }
  alert("已发送紧急呼叫");
}

async function onSwitchUser(nextUid: string) {
  await setSessionUser(nextUid, true);   // 手动切换即锁定（规格 D11）
  uid.value = nextUid;
  locked.value = true;
}

async function onConfirmReminder(rid: number) {
  try {
    await fetch(`/api/reminders/${rid}/confirm`, { method: "POST" });
    reminder.value = null;
  } catch { /* 忽略 */ }
}

onMounted(loadSession);
</script>

<template>
  <div class="kiosk">
    <VoiceStatusBar :state="state" :uid="uid" :locked="locked" />
    <ReminderBanner v-if="reminder" :reminder="reminder" @confirm="onConfirmReminder" />
    <ChatArea :messages="messages" @send="sendText" />
    <div class="bottom">
      <SosButton @sos="onSos" />
      <span class="conn" :class="{ off: !connected }">{{ connected ? "●" : "○ 重连中" }}</span>
    </div>
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { background: #0b1220; color: #f9fafb; font-family: system-ui, sans-serif; }
</style>
<style scoped>
.kiosk { height: 100vh; display: flex; flex-direction: column; }
.bottom { display: flex; align-items: center; gap: 20px; padding: 16px 24px; }
.conn { color: #22c55e; font-size: 20px; }
.conn.off { color: #ef4444; }
</style>
