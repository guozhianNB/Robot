<script setup lang="ts">
// 设置页：功能开关（一键开关，持久化，规格 §6）
import { onMounted, ref } from "vue";

const settings = ref<Record<string, unknown>>({});

async function load() {
  const res = await fetch("/api/settings");
  const body = await res.json();
  settings.value = body.settings ?? {};
}

async function save(key: string, value: unknown) {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: { [key]: value } }),
  });
  await load();
}

const BOOLS = [
  ["voice_enabled", "语音链路总开关"],
  ["asr_enabled", "语音识别"],
  ["tts_enabled", "语音播报"],
  ["reminder_enabled", "定时提醒"],
  ["mcp_enabled", "MCP 外部工具（联网/新闻等，改后重启后端生效）"],
  ["thinking_router_enabled", "思考路由"],
  ["memory_consolidation_enabled", "记忆整理"],
  ["alarm_enabled", "报警上报"],
] as const;

onMounted(load);
</script>

<template>
  <div>
    <div v-for="[key, label] in BOOLS" :key="key" class="row">
      <span>{{ label }}（{{ key }}）</span>
      <input type="checkbox" :checked="!!settings[key]"
             @change="save(key, ($event.target as HTMLInputElement).checked)" />
    </div>
  </div>
</template>

<style scoped>
.row { display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; margin-bottom: 8px; background: #1e293b; border-radius: 10px; }
</style>
