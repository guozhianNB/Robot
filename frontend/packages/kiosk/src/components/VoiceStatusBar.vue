<script setup lang="ts">
// 状态条：语音状态机三色 + active_uid + 锁定标记（规格 §5）
import { computed } from "vue";

const props = defineProps<{
  state: string;          // idle / listening / speaking / unavailable（wake 已并入 listening）
  uid: string | null;
  locked: boolean;
}>();

const emit = defineEmits<{ (e: "open-switcher"): void }>();

const label = computed(() => {
  switch (props.state) {
    case "listening": return "正在听…";
    case "speaking": return "播报中…";
    case "unavailable": return "语音不可用";
    default: return "◉ 待机";
  }
});

const color = computed(() => {
  if (props.state === "listening") return "#3b82f6";
  if (props.state === "speaking") return "#22c55e";
  if (props.state === "unavailable") return "#ef4444";
  return "#9ca3af";
});
</script>

<template>
  <div class="status-bar">
    <span class="dot" :style="{ background: color }"></span>
    <span class="label">{{ label }}</span>
    <button class="user" @click="emit('open-switcher')">
      👤 {{ uid ?? "未选择" }} {{ locked ? "🔒" : "" }}
    </button>
  </div>
</template>

<style scoped>
.status-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 16px 24px; background: #111827; color: #f9fafb;
  font-size: 22px;
}
.dot { width: 14px; height: 14px; border-radius: 50%; }
.user { margin-left: auto; background: none; border: 1px solid #374151;
  color: #f9fafb; padding: 8px 16px; border-radius: 12px; font-size: 20px; }
</style>
