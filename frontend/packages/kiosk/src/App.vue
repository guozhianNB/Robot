<script setup lang="ts">
// kiosk 主界面：状态条 + 对话区 + 提醒 + SOS（规格 §5）
import { onMounted, ref } from "vue";
import {
  type BusEvent, type ReminderEvent,
  reportAlarm, getSessionUser, type SessionUser,
  THINKING_MODE_ORDER, THINKING_MODE_HINT,
  normalizeThinkingMode, type ThinkingMode,
} from "shared";
import { useBus } from "./useBus";
import VoiceStatusBar from "./components/VoiceStatusBar.vue";
import ChatArea, { type Msg } from "./components/ChatArea.vue";
import ReminderBanner from "./components/ReminderBanner.vue";
import SosButton from "./components/SosButton.vue";
import UserSwitcher from "./components/UserSwitcher.vue";
import SettingsSheet from "./components/SettingsSheet.vue";

const state = ref("idle");
const uid = ref<string | null>(null);
const locked = ref(false);
const session = ref<SessionUser | null>(null);   // 分层用户体系：角色/病房/TTL 的唯一来源（层级栏与状态条都吃它）
const liveText = ref("");        // 流式 ASR 实时字幕（asr_partial 事件）
const messages = ref<Msg[]>([]);
const reminder = ref<ReminderEvent | null>(null);
const showSwitcher = ref(false);
const showSettings = ref(false);
const asrProvider = ref("cloud");   // local | cloud（识别引擎，重启服务后生效）
const ttsProvider = ref("cloud");   // local | cloud（合成引擎，重启服务后生效）
// 思考档位：auto 快答 / none 不思考 / low 轻度 / high 中度 / max 重度（后三档见规格 D5）。
// 语音轮次不过前端（后端 worker 直接调 chat_stream），所以必须落库到 settings 才生效——
// 见规格 docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md D2。
const thinkingMode = ref<ThinkingMode>("auto");
const showModeMenu = ref(false);   // 点按钮弹出五档选择（老人端按钮大、字也要大）
// 老人端用大白话，不用"轻度/中度/重度"（管理端照旧用档位术语）
const KIOSK_MODE_CN: Record<ThinkingMode, string> = {
  auto: "自动", none: "不思考", low: "想一下", high: "认真想", max: "使劲想",
};
const { connected } = useBus(onEvent);

async function loadSession() {
  try {
    const s = await getSessionUser("kiosk");
    session.value = s;
    uid.value = s.uid;
    locked.value = s.locked;
  } catch { /* 后端未就绪时忽略，保留旧值 */ }
}

async function loadAsrProvider() {
  try {
    const res = await fetch("/api/settings");
    const body = await res.json();
    asrProvider.value = body.settings?.asr_provider ?? "cloud";
    thinkingMode.value = normalizeThinkingMode(body.settings?.thinking_mode);
  } catch { /* 忽略，保留默认 */ }
}

/** 选档并落库：自动 → 不思考 → 轻度 → 中度 → 重度（落库，语音轮次也吃到）。 */
async function pickThinkingMode(m: ThinkingMode) {
  thinkingMode.value = m;
  showModeMenu.value = false;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { thinking_mode: m } }),
    });
  } catch { /* 保存失败也保留本地选择，下一轮仍按本地值说话 */ }
}

async function toggleAsrProvider() {
  const next = asrProvider.value === "cloud" ? "local" : "cloud";
  asrProvider.value = next;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { asr_provider: next } }),
    });
  } catch { /* 保存失败也提示用户 */ }
  alert(`识别引擎已切换为${next === "cloud" ? "云端（火山）" : "本地"}，重启服务后生效`);
}

async function loadTtsProvider() {
  try {
    const res = await fetch("/api/settings");
    const body = await res.json();
    ttsProvider.value = body.settings?.tts_provider ?? "cloud";
  } catch { /* 忽略，保留默认 */ }
}

async function toggleTtsProvider() {
  const next = ttsProvider.value === "cloud" ? "local" : "cloud";
  ttsProvider.value = next;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { tts_provider: next } }),
    });
  } catch { /* 保存失败也提示 */ }
  alert(`合成引擎已切换为${next === "cloud" ? "云端（火山）" : "本地"}，重启服务后生效`);
}

function onEvent(ev: BusEvent) {
  if (ev.type === "voice_state") {
    if (ev.state === "asr_partial") { liveText.value = ev.text ?? ""; return; }
    state.value = ev.state;
    if (ev.uid) uid.value = ev.uid;
    if (ev.state === "recognized" && ev.text) {
      // 语音问答开始：老人语句入气泡 + assistant 占位（等待 chat_partial 渐进）
      liveText.value = "";
      messages.value.push({ role: "user", content: ev.text, uid: uid.value ?? undefined });
      messages.value.push({ role: "assistant", content: "" });
    } else if (ev.state === "idle") {
      liveText.value = "";
    }
    return;
  }
  if (ev.type === "chat_partial") {
    const last = messages.value[messages.value.length - 1];
    if (last && last.role === "assistant") last.content += ev.delta;
    else messages.value.push({ role: "assistant", content: ev.delta });
    return;
  }
  if (ev.type === "chat_reasoning") {
    // 思维链：只上屏（TTS 侧 worker 只把 content 送合成，这里收到的永远不是播报内容）
    const last = messages.value[messages.value.length - 1];
    if (last && last.role === "assistant") last.reasoning = (last.reasoning ?? "") + ev.delta;
    else messages.value.push({ role: "assistant", content: "", reasoning: ev.delta });
    return;
  }
  if (ev.type === "chat_new") {
    liveText.value = "";
    const msgs = messages.value;
    const prev = msgs[msgs.length - 2];
    const last = msgs[msgs.length - 1];
    if (last?.role === "assistant" && prev?.role === "user" && prev.content === ev.user) {
      last.content = ev.assistant;      // 渐进气泡覆盖为终稿（防 SSE 丢帧）
    } else {
      msgs.push({ role: "user", content: ev.user, uid: ev.uid });
      msgs.push({ role: "assistant", content: ev.assistant });
    }
    return;
  }
  if (ev.type === "voice_status" && ev.status === "degraded") {
    state.value = "unavailable";
    liveText.value = "";
  }
  if (ev.type === "reminder") reminder.value = ev;
  if (ev.type === "user_changed") {
    // 载荷是 {uid, locked, source}（role/slot/ward_uid 可选）：先按载荷即时更新，
    // 再拉一次全量会话（角色/病房/TTL 只有 /api/session/user 有）。
    uid.value = ev.uid || null;
    locked.value = ev.locked;
    loadSession();
  }
  if (ev.type === "ward_changed") {
    loadSession();               // 当前病房变了（位置自动切换 / 手动切 / 后台管理改动）
  }
  if (ev.type === "session_expired") {
    loadSession();               // 管理员 TTL 到点 → 该槽位已回落集体层
    if (ev.slot === "kiosk") liveText.value = "管理员会话已超时，已回到集体层";
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
      body: JSON.stringify({
        uid: uid.value ?? "elder_001",
        message: text,
        thinking: thinkingMode.value,
        speak: true,
      }),
    });
    if (!res.ok || !res.body) throw new Error("chat failed");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    // /api/chat 返回的是 chat_stream 事件（meta/reasoning/content/done），不是 bus 广播事件 ——
    // 按 \n\n 组帧后解析 data: 行（一个帧可能被网络切成两个 chunk，逐行解析会静默丢帧）
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut: number;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data:")) continue;
          try {
            const ev = JSON.parse(line.slice(5).trim());
            if (ev.type === "reasoning") last.reasoning = (last.reasoning ?? "") + ev.content;
            if (ev.type === "content") last.content += ev.content;
            if (ev.type === "done") break;
          } catch { /* 坏帧忽略 */ }
        }
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

async function onConfirmReminder(rid: number) {
  try {
    await fetch(`/api/reminders/${rid}/confirm`, { method: "POST" });
    reminder.value = null;
  } catch { /* 忽略 */ }
}

onMounted(() => {
  loadSession();
  loadAsrProvider();
  loadTtsProvider();
  // 管理员 TTL 倒计时得真的在走：事件只覆盖"状态变化"，剩多少秒只有轮询能刷新。
  // 10s 轮询同时是 session_expired 的兜底（SSE 断线时也能自己发现会话已回落集体层）。
  setInterval(loadSession, 10000);
});
</script>

<template>
  <div class="kiosk">
    <VoiceStatusBar :state="state" :session="session" @open-switcher="showSwitcher = true" />
    <div v-if="liveText" class="live-asr">🗣 {{ liveText }}</div>
    <ReminderBanner v-if="reminder" :reminder="reminder" @confirm="onConfirmReminder" />
    <ChatArea :messages="messages" @send="sendText" />
    <div class="bottom">
      <SosButton @sos="onSos" />
      <button class="settings-btn asr-toggle" @click="toggleAsrProvider">
        识别：{{ asrProvider === "cloud" ? "云端" : "本地" }}
      </button>
      <button class="settings-btn tts-toggle" @click="toggleTtsProvider">
        合成：{{ ttsProvider === "cloud" ? "云端" : "本地" }}
      </button>
      <div class="mode-picker">
        <div v-if="showModeMenu" class="mode-menu">
          <button v-for="m in THINKING_MODE_ORDER" :key="m"
                  :class="{ on: m === thinkingMode }" :title="THINKING_MODE_HINT[m]"
                  @click="pickThinkingMode(m)">
            {{ KIOSK_MODE_CN[m] }}
          </button>
        </div>
        <button class="settings-btn thinking-btn" :class="thinkingMode"
                :title="THINKING_MODE_HINT[thinkingMode]" @click="showModeMenu = !showModeMenu">
          🧠 {{ KIOSK_MODE_CN[thinkingMode] }}
        </button>
      </div>
      <button class="settings-btn" @click="showSettings = true">⚙ 设置</button>
      <span class="conn" :class="{ off: !connected }">{{ connected ? "●" : "○ 重连中" }}</span>
    </div>
    <!-- 左侧层级栏（管理层/集体层/老人层）：组件自己取 /api/profiles 与 /api/wards。
         session 未拉到（后端不可达）时不渲染，避免拿假会话去渲染角色区。 -->
    <UserSwitcher v-if="showSwitcher && session" :session="session"
                  @changed="loadSession" @close="showSwitcher = false" />
    <SettingsSheet v-if="showSettings" @close="showSettings = false" />
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { background: #0b1220; color: #f9fafb; font-family: system-ui, sans-serif; }
</style>
<style scoped>
.kiosk { height: 100vh; display: flex; flex-direction: column; }
.live-asr { padding: 10px 24px; background: #1e293b; color: #fbbf24;
  font-size: 24px; line-height: 1.4; min-height: 44px; }
.bottom { display: flex; align-items: center; gap: 20px; padding: 16px 24px; }
.conn { color: #22c55e; font-size: 20px; }
.conn.off { color: #ef4444; }
.settings-btn { background: #374151; color: #f9fafb; border: none;
  padding: 16px 24px; border-radius: 16px; font-size: 22px; }
.asr-toggle { background: #1d4ed8; }
.tts-toggle { background: #1d4ed8; }
/* 思考档位：底部大按钮 + 向上弹出的五档菜单（老人端也能点准） */
.mode-picker { position: relative; }
.thinking-btn.high, .thinking-btn.max { background: #6d28d9; }
.thinking-btn.none { background: #4b5563; }
.mode-menu { position: absolute; bottom: 110%; left: 0; z-index: 30; display: flex;
  flex-direction: column; min-width: 160px; padding: 6px; border-radius: 14px;
  border: 1px solid #374151; background: #111827; }
.mode-menu button { padding: 12px 16px; border: none; border-radius: 10px; text-align: left;
  background: none; color: #f9fafb; font-size: 22px; }
.mode-menu button.on { background: #1d4ed8; }
</style>
