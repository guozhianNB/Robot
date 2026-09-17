<script setup lang="ts">
// 对话页：历史回读 + SSE 流式打字机（规格 §6，接口契约与旧前端一致）
// 思考模式：手动三档（自动/强制/关闭），思维链（reasoning）在气泡内展示、不进 TTS。
// 规格 docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md
import { onMounted, onUnmounted, ref } from "vue";
import {
  THINKING_MODE_ORDER, THINKING_MODE_LABEL, THINKING_MODE_HINT,
  normalizeThinkingMode, thinkingModeLabel, type ThinkingMode,
} from "shared";

interface Msg { role: "user" | "assistant"; content: string; reasoning?: string }

// 初始 uid 优先取注册向导完成时写入的 localStorage("uid")，其次默认 elder_001
const uid = ref(localStorage.getItem("uid") ?? "elder_001");
const messages = ref<Msg[]>([]);
const text = ref("");
const sending = ref(false);
const thinkingMode = ref<ThinkingMode>("auto");
const showModeMenu = ref(false);   // 五档下拉（点按钮展开）
// 后端 meta 回执：mode=实际生效档位、effort=真正发给模型的强度、method=命中来源。
// 现场排障就看这一小块（"参数到底有没有传到 llm"不用猜）。
const lastRouter = ref<{ on?: boolean; effort?: string | null; mode?: string; method?: string; reason?: string } | null>(null);
let pollTimer: number | undefined;

async function loadThinkingMode() {
  try {
    const res = await fetch("/api/settings");
    const body = await res.json();
    thinkingMode.value = normalizeThinkingMode(body.settings?.thinking_mode);
  } catch { /* 后端未就绪时保留本地值 */ }
}

/** 选档并落库：kiosk 语音轮次不过前端，只有落库的设置才能让语音也吃到手动档位。 */
async function pickThinkingMode(m: ThinkingMode) {
  thinkingMode.value = m;
  showModeMenu.value = false;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { thinking_mode: m } }),
    });
  } catch { /* 保存失败也保留本地选择 */ }
}

async function loadHistory() {
  // 必须带 X-Surface：后端按端槽位取 principal，缺头按 kiosk 槽 → 管理台会以集体层身份说话
  const res = await fetch(`/api/chat/history?uid=${uid.value}&limit=200`,
    { headers: { "X-Surface": "admin" } });
  const body = await res.json();
  messages.value = (body.history ?? []).map((h: any) => ({
    role: h.role, content: h.content,
  }));
}

async function send() {
  const t = text.value.trim();
  if (!t || sending.value) return;
  sending.value = true;
  // I-4：先 push 空 assistant 占位，流式循环更新占位 —— 否则 length-1 是刚 push 的 user 消息，
  // 循环里直接覆盖会导致用户消息丢失（参照 kiosk App.vue sendText 写法）
  messages.value.push({ role: "user", content: t });
  messages.value.push({ role: "assistant", content: "" });
  text.value = "";
  let assistant = "";
  let reasoning = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      // X-Surface: admin —— 管理台说的话是**管理层**的话；缺头会被后端按 kiosk 槽
      // 取 principal，降级成集体层（且这一轮会被按集体层口径处理）
      headers: { "Content-Type": "application/json", "X-Surface": "admin" },
      // thinking 显式上报当前档位：后端仍以「敏感词安全网优先」合成最终深度
      // （手选"关闭"时健康/敏感问题照旧加深，见规格 D1）
      body: JSON.stringify({ uid: uid.value, message: t, thinking: thinkingMode.value }),
    });
    if (!res.body) throw new Error("no body");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    // SSE 必须按 \n\n 组帧：一个 data: 帧可能被网络切成两个 chunk，
    // 逐行 JSON.parse 会在跨界处静默失败、整段思维链就"消失"了（I-6）。
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
          if (!line.startsWith("data:")) continue;   // 心跳注释行
          try {
            const ev = JSON.parse(line.slice(5).trim());
            if (ev.type === "meta") lastRouter.value = ev.router ?? null;
            if (ev.type === "reasoning") reasoning += ev.content;
            if (ev.type === "content") assistant += ev.content;
            if (ev.type === "done") break;
          } catch { /* 坏帧忽略 */ }
        }
      }
      messages.value[messages.value.length - 1] = { role: "assistant", content: assistant, reasoning };
    }
  } catch {
    messages.value[messages.value.length - 1] = { role: "assistant", content: "（发送失败）" };
  } finally {
    sending.value = false;
  }
}

async function clearHistory() {
  await fetch(`/api/chat/history?uid=${uid.value}`,
    { method: "DELETE", headers: { "X-Surface": "admin" } });
  messages.value = [];
}

onMounted(() => {
  loadHistory();
  loadThinkingMode();
  // 5s 轮询：kiosk 那边按了同一个按钮，管理台要跟着显示真实生效档位（一份设置两处 UI）
  pollTimer = window.setInterval(loadThinkingMode, 5000);
});
onUnmounted(() => { if (pollTimer !== undefined) window.clearInterval(pollTimer); });
</script>

<template>
  <div class="chat-page">
    <div class="toolbar">
      <input v-model="uid" placeholder="老人 uid" @change="loadHistory" />
      <div class="mode-picker">
        <button class="thinking-btn" :class="thinkingMode" @click="showModeMenu = !showModeMenu"
                :title="THINKING_MODE_HINT[thinkingMode]">
          {{ thinkingModeLabel(thinkingMode) }} ▾
        </button>
        <div v-if="showModeMenu" class="mode-menu">
          <button v-for="m in THINKING_MODE_ORDER" :key="m" :class="{ on: m === thinkingMode }"
                  :title="THINKING_MODE_HINT[m]" @click="pickThinkingMode(m)">
            {{ THINKING_MODE_LABEL[m] }}
          </button>
        </div>
      </div>
      <div v-if="lastRouter" class="router-chip" :class="{ on: lastRouter.on }">
        本轮思考：{{ lastRouter.effort ?? "不思考" }}（档位 {{ lastRouter.mode }}／来源 {{ lastRouter.method }}）
        <span v-if="lastRouter.reason">· {{ lastRouter.reason }}</span>
      </div>
      <button @click="loadHistory">刷新</button>
      <button @click="clearHistory">清空</button>
    </div>
    <div class="msgs">
      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <div v-if="m.reasoning" class="thinking">
          <details open>
            <summary>💭 思考过程</summary>
            <pre>{{ m.reasoning }}</pre>
          </details>
        </div>
        <span><b>{{ m.role === "user" ? "👤" : "🤖" }}</b> {{ m.content }}</span>
      </div>
    </div>
    <div class="input-row">
      <input v-model="text" placeholder="输入消息…" @keyup.enter="send" :disabled="sending" />
      <button @click="send" :disabled="sending">{{ sending ? "发送中…" : "发送" }}</button>
    </div>
  </div>
</template>

<style scoped>
.chat-page { display: flex; flex-direction: column; height: 100%; }
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.toolbar input { flex: 1; padding: 8px 12px; border-radius: 8px;
  border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.msgs { flex: 1; overflow-y: auto; margin-bottom: 12px; }
.msg { padding: 10px 14px; margin-bottom: 8px; border-radius: 10px;
  background: #1e293b; white-space: pre-wrap; }
.msg.user { border-left: 3px solid #3b82f6; }
.msg.assistant { border-left: 3px solid #22c55e; }
/* 思考档位：五档下拉（自动/不思考/轻度/中度/重度），按钮颜色一眼看出当前档 */
.mode-picker { position: relative; }
.thinking-btn { padding: 8px 14px; border-radius: 8px; border: 1px solid #334155;
  background: #1e293b; color: #e2e8f0; white-space: nowrap; }
.thinking-btn.high, .thinking-btn.max { border-color: #a855f7; color: #d8b4fe; }
.thinking-btn.none { border-color: #475569; color: #94a3b8; }
.mode-menu { position: absolute; top: 110%; left: 0; z-index: 20; display: flex;
  flex-direction: column; min-width: 120px; padding: 4px; border-radius: 8px;
  border: 1px solid #334155; background: #0f172a; box-shadow: 0 8px 20px rgba(0,0,0,.45); }
.mode-menu button { padding: 6px 10px; text-align: left; border: none; border-radius: 6px;
  background: none; color: #e2e8f0; cursor: pointer; }
.mode-menu button:hover { background: #1e293b; }
.mode-menu button.on { background: #1d4ed8; }
/* 回执条：让人一眼看出"这轮参数到底传了没有" */
.router-chip { flex: 1; align-self: center; font-size: 12px; color: #94a3b8;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.router-chip.on { color: #d8b4fe; }
/* 思维链块：窄色小字，绝不与正式回复混排（且后端从不把它送 TTS） */
.thinking { margin-bottom: 8px; }
.thinking summary { cursor: pointer; color: #9ca3af; font-size: 12px; }
.thinking pre { margin: 6px 0 0; padding: 8px 10px; border-left: 3px solid #475569;
  background: #0f172a; color: #94a3b8; font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; max-height: 220px; overflow-y: auto; }
.input-row { display: flex; gap: 8px; }
.input-row input { flex: 1; padding: 10px 12px; border-radius: 8px;
  border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.input-row button { padding: 10px 20px; border-radius: 8px;
  background: #2563eb; color: #fff; border: none; }
</style>
