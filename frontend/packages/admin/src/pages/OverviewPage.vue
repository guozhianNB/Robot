<script setup lang="ts">
// 监控总览：模块状态 + 告警列表（规格 §6）
import { onMounted, ref } from "vue";

const modules = ref<any>(null);
const warnings = ref<any[]>([]);

async function load() {
  modules.value = (await (await fetch("/api/modules/status")).json()).modules ?? {};
  warnings.value = (await (await fetch("/api/logs/warnings?limit=20")).json()).logs ?? [];
}

onMounted(load);
</script>

<template>
  <div>
    <button @click="load">刷新</button>
    <div class="card">
      <h3>模块状态</h3>
      <ul>
        <li v-for="(m, name) in modules" :key="String(name)">
          {{ name }}：{{ (m as any).status }}
          <span v-if="(m as any).reason">（{{ (m as any).reason }}）</span>
        </li>
      </ul>
    </div>
    <div class="card">
      <h3>最近告警/错误</h3>
      <div v-for="(w, i) in warnings" :key="i" class="warn">
        {{ w.ts }} · {{ w.event }} · {{ (w as any).action ?? "" }}
        <span v-if="(w as any).error">：{{ (w as any).error }}</span>
      </div>
      <p v-if="!warnings.length">暂无</p>
    </div>
  </div>
</template>

<style scoped>
.card { background: #1e293b; padding: 16px; border-radius: 10px; margin: 12px 0; }
.warn { padding: 6px 0; border-bottom: 1px solid #334155; font-size: 13px; }
</style>
