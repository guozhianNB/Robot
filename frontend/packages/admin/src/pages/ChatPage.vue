<script setup lang="ts">
// 对话页：历史回读 + SSE 流式打字机（规格 §6，接口契约与旧前端一致）
import { onMounted, ref } from "vue";

interface Msg { role: "user" | "assistant"; content: string }

const uid = ref("elder_001");
const messages = ref<Msg[]>([]);
const text = ref("");
const sending = ref(false);

async function loadHistory() {
  const res = await fetch(`/api/chat/history?uid=${uid.value}&limit=200`);
  const body = await res.json();
  messages.value = (body.history ?? []).map((h: any) => ({
    role: h.role, content: h.content,
  }));
}

async function send() {
  const t = text.value.trim();
  if (!t || sending.value) return;
  sending.value = true;
  messages.value.push({ role: "user", content: t });
  text.value = "";
  let assistant = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: uid.value, message: t, thinking: "auto" }),
    });
    if (!res.body) throw new Error("no body");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      // 旧协议：chat_stream 事件直接 data: {...}（type: content/reasoning/done）
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "content") assistant += ev.content;
          if (ev.type === "done") break;
        } catch { /* 坏帧忽略 */ }
      }
      messages.value[messages.value.length - 1] = { role: "assistant", content: assistant };
    }
  } catch {
    messages.value.push({ role: "assistant", content: "（发送失败）" });
  } finally {
    sending.value = false;
  }
}

async function clearHistory() {
  await fetch(`/api/chat/history?uid=${uid.value}`, { method: "DELETE" });
  messages.value = [];
}

onMounted(loadHistory);
</script>

<template>
  <div class="chat-page">
    <div class="toolbar">
      <input v-model="uid" placeholder="老人 uid" @change="loadHistory" />
      <button @click="loadHistory">刷新</button>
      <button @click="clearHistory">清空</button>
    </div>
    <div class="msgs">
      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <b>{{ m.role === "user" ? "👤" : "🤖" }}</b> {{ m.content }}
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
.input-row { display: flex; gap: 8px; }
.input-row input { flex: 1; padding: 10px 12px; border-radius: 8px;
  border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.input-row button { padding: 10px 20px; border-radius: 8px;
  background: #2563eb; color: #fff; border: none; }
</style>
