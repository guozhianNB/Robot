<script setup lang="ts">
// 对话区：识别文本/回复实时上屏 + 手动输入（规格 §5）
// 思维链（reasoning）只在这里展示——后端语音链路只把 content 送 TTS，思考过程不会被念出来。
import { ref } from "vue";

export interface Msg {
  role: "user" | "assistant";
  content: string;
  uid?: string;
  reasoning?: string;   // 思维链（模型 reasoning_content，流式累积；不落库）
}

defineProps<{ messages: Msg[] }>();

const text = ref("");
const emit = defineEmits<{ (e: "send", text: string): void }>();
const showThinking = ref(true);   // 默认展开：现场排查"它到底想了没"最有用

const QUICK = ["我要吃药", "请找家人", "今天天气怎么样", "给我讲讲新闻"];

function send() {
  const t = text.value.trim();
  if (!t) return;
  emit("send", t);
  text.value = "";
}
</script>

<template>
  <div class="chat-area">
    <div class="messages">
      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <span v-if="m.role === 'user'">👴 {{ m.uid ? m.uid + "：" : "" }}{{ m.content }}</span>
        <template v-else>
          <div v-if="m.reasoning" class="thinking">
            <button class="thinking-head" @click="showThinking = !showThinking">
              💭 {{ m.content ? "思考过程" : "正在思考…" }} {{ showThinking ? "（收起）" : "（展开）" }}
            </button>
            <div v-if="showThinking" class="thinking-body">{{ m.reasoning }}</div>
          </div>
          <span>🤖 {{ m.content }}</span>
        </template>
      </div>
    </div>
    <div class="quick">
      <button v-for="q in QUICK" :key="q" @click="emit('send', q)">{{ q }}</button>
    </div>
    <div class="input-row">
      <input v-model="text" placeholder="输入内容…" @keyup.enter="send" />
      <button class="send" @click="send">发送</button>
    </div>
  </div>
</template>

<style scoped>
.chat-area { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.messages { flex: 1; overflow-y: auto; padding: 20px 24px; font-size: 24px; line-height: 1.8; }
.msg { margin-bottom: 16px; }
.msg.user span { background: #1e3a5f; padding: 10px 16px; border-radius: 14px; }
.msg.assistant span { background: #1f2937; padding: 10px 16px; border-radius: 14px; }
.thinking { margin-bottom: 8px; }
.thinking-head { background: none; border: none; color: #9ca3af; font-size: 18px;
  padding: 2px 0; cursor: pointer; }
.thinking-body { margin-top: 6px; padding: 10px 14px; border-left: 3px solid #4b5563;
  background: #111827; color: #9ca3af; font-size: 17px; line-height: 1.7;
  white-space: pre-wrap; border-radius: 8px; max-height: 220px; overflow-y: auto; }
.quick { display: flex; gap: 10px; padding: 0 24px 12px; flex-wrap: wrap; }
.quick button { background: #374151; color: #f9fafb; border: none;
  padding: 12px 18px; border-radius: 12px; font-size: 20px; }
.input-row { display: flex; gap: 10px; padding: 12px 24px 20px; }
.input-row input { flex: 1; font-size: 22px; padding: 12px 16px;
  border-radius: 12px; border: 1px solid #374151; background: #1f2937; color: #f9fafb; }
.send { background: #2563eb; color: #fff; border: none; padding: 12px 28px;
  border-radius: 12px; font-size: 22px; }
</style>
