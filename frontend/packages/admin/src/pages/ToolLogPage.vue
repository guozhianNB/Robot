<script setup lang="ts">
// 工具日志页：工具调用记录（规格 §6）
import { onMounted, ref } from "vue";

interface ToolLog { id: number; uid: string; name: string; args: string;
  result: string; created_at?: string }

const items = ref<ToolLog[]>([]);

async function load() {
  const res = await fetch("/api/tools/log?limit=100");
  const body = await res.json();
  items.value = body.logs ?? [];
}

onMounted(load);
</script>

<template>
  <div>
    <button @click="load">刷新</button>
    <div v-for="l in items" :key="l.id" class="row">
      <div>
        <b>{{ l.name }}</b>
        <pre>{{ l.args }}</pre>
        <pre class="result">{{ l.result }}</pre>
        <small>（{{ l.uid }}）</small>
      </div>
    </div>
  </div>
</template>

<style scoped>
.row { padding: 10px 14px; margin: 8px 0; background: #1e293b; border-radius: 10px; }
pre { white-space: pre-wrap; margin: 4px 0; font-size: 13px; }
.result { color: #86efac; }
</style>
