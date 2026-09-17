<script setup lang="ts">
// 单条通知卡片（规格 §5.2）：级别色带 / emoji + 老人 + ×count + 相对时间 / 标题 / 正文 / 「处理了」。
// D7：按钮常规尺寸（padding 6px 14px）、正文 14–15px —— 护士台的用户是护士，不搞大按钮大字号。
import { ref } from "vue";
import { ackNotice, type Notice } from "shared";
import { relativeTime } from "../lib/relativeTime";

const props = defineProps<{ notice: Notice; nowMs: number }>();
const emit = defineEmits<{ (e: "acked", id: number): void }>();

const busy = ref(false);
const err = ref("");

const EMOJI: Record<string, string> = { critical: "🔴", warning: "🟠", info: "⚪" };

async function ack() {
  if (busy.value || props.notice.ack_at) return;
  busy.value = true;
  err.value = "";
  try {
    await ackNotice(props.notice.id);
    emit("acked", props.notice.id);          // 本地立刻标记（不等 SSE 回包）
  } catch (e) {
    err.value = `处理失败：${e}`;              // 失败必须说出来，绝不假装成功
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <article :class="['card', `lv-${notice.level}`, { acked: !!notice.ack_at }]">
    <div class="line1">
      <span class="emoji">{{ EMOJI[notice.level] ?? "⚪" }}</span>
      <span v-if="notice.uid_name" class="who">{{ notice.uid_name }}</span>
      <span v-if="notice.count > 1" class="count">×{{ notice.count }}</span>
      <span class="time">{{ relativeTime(notice.last_at || notice.created_at, nowMs) }}</span>
    </div>
    <div class="title">{{ notice.title }}</div>
    <div class="line3">
      <span class="body">{{ notice.body }}</span>
      <span v-if="err" class="err">{{ err }}</span>
      <button v-if="!notice.ack_at" class="ack" :disabled="busy" @click="ack">
        {{ busy ? "处理中…" : "处理了" }}
      </button>
    </div>
  </article>
</template>

<style scoped>
.card {
  position: relative;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-left: 4px solid #64748b;          /* 级别色带（按 lv-* 覆盖） */
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 8px;
}
.card.lv-critical { border-left-color: #dc2626; }
.card.lv-warning { border-left-color: #d97706; }
.card.lv-info { border-left-color: #64748b; }

/* 已处理：整卡变灰沉底（规格 §5.2 / 简报 §4.2） */
.card.acked { opacity: 0.55; }

.line1 { display: flex; align-items: center; gap: 8px; }
.emoji { font-size: 14px; }
.who { font-size: 15px; font-weight: 600; color: #0f172a; }
.count {
  font-size: 13px; color: #b91c1c; background: #fee2e2;
  border-radius: 4px; padding: 1px 6px;
}
.time { margin-left: auto; font-size: 13px; color: #64748b; }

.title { font-size: 16px; color: #0f172a; margin-top: 4px; }
.line3 { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
.body { font-size: 14px; color: #334155; line-height: 1.5; white-space: pre-wrap; }
.err { font-size: 13px; color: #b91c1c; }
.ack {
  margin-left: auto; flex: none;
  padding: 6px 14px;                        /* D7：常规尺寸，不是大按钮 */
  font-size: 14px; border-radius: 6px; cursor: pointer;
  border: 1px solid #1f9d55; background: #1f9d55; color: #fff;
}
.ack:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
