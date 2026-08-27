<script setup lang="ts">
// 提醒卡片：触发时展示 + 确认（规格 §5）
import type { ReminderEvent } from "shared";

defineProps<{ reminder: ReminderEvent | null }>();
const emit = defineEmits<{ (e: "confirm", rid: number): void }>();
</script>

<template>
  <div v-if="reminder" class="banner">
    <div>
      <div class="title">⏰ {{ reminder.title }}</div>
      <div class="content">{{ reminder.content }}</div>
    </div>
    <button class="ok" @click="emit('confirm', reminder.id)">确认</button>
  </div>
</template>

<style scoped>
.banner { display: flex; align-items: center; gap: 16px;
  background: #b45309; color: #fff; padding: 16px 24px; font-size: 20px; }
.banner .title { font-weight: bold; }
.ok { background: #22c55e; color: #fff; border: none; padding: 12px 28px;
  border-radius: 12px; font-size: 20px; margin-left: auto; }
</style>
