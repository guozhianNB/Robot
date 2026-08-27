<script setup lang="ts">
// 记忆页：列表 + 状态过滤 + 确认/拒绝/删除（规格 §6）
import { onMounted, ref } from "vue";

interface Memory { id: number; uid: string; type: string; content: string;
  status: string; created_at?: string }

const items = ref<Memory[]>([]);
const status = ref("");

async function load() {
  const q = status.value ? `?status=${status.value}` : "";
  const res = await fetch(`/api/memories${q}`);
  const body = await res.json();
  items.value = body.memories ?? [];
}

async function act(id: number, action: "confirm" | "reject" | "delete") {
  if (action === "delete") {
    await fetch(`/api/memories/${id}`, { method: "DELETE" });
  } else {
    await fetch(`/api/memories/${id}/${action}`, { method: "POST" });
  }
  await load();
}

onMounted(load);
</script>

<template>
  <div>
    <div class="toolbar">
      <select v-model="status" @change="load">
        <option value="">全部</option>
        <option value="confirmed">已确认</option>
        <option value="pending">待处理</option>
      </select>
      <button @click="load">刷新</button>
    </div>
    <div v-for="m in items" :key="m.id" class="row">
      <div>
        <b>[{{ m.type }}]</b> {{ m.content }}
        <small>（{{ m.uid }} · {{ m.status }}）</small>
      </div>
      <div class="actions">
        <button v-if="m.status !== 'confirmed'" @click="act(m.id, 'confirm')">确认</button>
        <button @click="act(m.id, 'reject')">拒绝</button>
        <button class="danger" @click="act(m.id, 'delete')">删除</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.row { display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; margin-bottom: 8px; background: #1e293b; border-radius: 10px; }
.actions { display: flex; gap: 6px; }
.actions button { padding: 6px 12px; border-radius: 6px; border: none;
  background: #334155; color: #e2e8f0; cursor: pointer; }
.actions .danger { background: #7f1d1d; }
</style>
