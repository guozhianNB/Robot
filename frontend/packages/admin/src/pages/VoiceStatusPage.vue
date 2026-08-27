<script setup lang="ts">
// 语音状态页：心跳 + 子模块状态 + 降级原因 + 声纹档案（规格 §6）
import { onMounted, ref } from "vue";

const status = ref<any>(null);
const speakers = ref<string[]>([]);
const details = ref<Record<string, { samples: number }>>({});

async function load() {
  const s = await (await fetch("/api/voice/status")).json();
  status.value = s;
  const sp = await (await fetch("/api/voice/speakers")).json();
  speakers.value = sp.speakers ?? [];
  details.value = sp.details ?? {};
}

onMounted(load);
</script>

<template>
  <div>
    <button @click="load">刷新</button>
    <div v-if="status" class="card">
      <h3>状态：{{ status.status }}</h3>
      <p v-if="status.reason">{{ status.reason }}</p>
      <pre>{{ JSON.stringify(status.modules ?? {}, null, 2) }}</pre>
    </div>
    <div class="card">
      <h3>声纹档案</h3>
      <ul>
        <li v-for="uid in speakers" :key="uid">
          {{ uid }}（样本 {{ details[uid]?.samples ?? 0 }}）
        </li>
        <li v-if="!speakers.length">无档案</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.card { background: #1e293b; padding: 16px; border-radius: 10px; margin: 12px 0; }
pre { white-space: pre-wrap; font-size: 13px; }
</style>
