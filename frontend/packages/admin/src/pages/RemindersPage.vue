<script setup lang="ts">
// 提醒页：列表 + 新增（护士建议）+ 确认/删除（规格 §6）
import { onMounted, ref } from "vue";

interface Reminder { id: number; uid: string; kind: string; title: string;
  content: string; status: string; trigger_type: string; trigger_time: string;
  trigger_date?: string }

const items = ref<Reminder[]>([]);
const form = ref({ uid: "elder_001", content: "", trigger_type: "once",
  trigger_time: "08:00", trigger_date: "" });

async function load() {
  const res = await fetch("/api/reminders");
  const body = await res.json();
  items.value = body.reminders ?? [];
}

async function add() {
  await fetch("/api/reminders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(form.value),
  });
  form.value.content = "";
  await load();
}

async function confirmRid(id: number) {
  await fetch(`/api/reminders/${id}/confirm`, { method: "POST" });
  await load();
}

async function del(id: number) {
  await fetch(`/api/reminders/${id}`, { method: "DELETE" });
  await load();
}

onMounted(load);
</script>

<template>
  <div>
    <div class="add">
      <input v-model="form.uid" placeholder="uid" />
      <input v-model="form.content" placeholder="提醒内容" />
      <select v-model="form.trigger_type">
        <option value="once">一次</option>
        <option value="daily">每日</option>
      </select>
      <input v-if="form.trigger_type === 'once'" v-model="form.trigger_date"
             type="date" placeholder="触发日期" />
      <input v-model="form.trigger_time" placeholder="08:00" />
      <button @click="add">新增</button>
    </div>
    <div v-for="r in items" :key="r.id" class="row">
      <div>
        <b>{{ r.title }}</b>：{{ r.content }}
        <small>（{{ r.uid }} · {{ r.status }} · {{ r.trigger_time }}<template v-if="r.trigger_date">（{{ r.trigger_date }}）</template>）</small>
      </div>
      <div class="actions">
        <button v-if="r.status === 'triggered' || r.status === 'unconfirmed'"
                @click="confirmRid(r.id)">确认</button>
        <button class="danger" @click="del(r.id)">删除</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.add { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.add input, .add select { padding: 8px 10px; border-radius: 8px;
  border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.row { display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; margin-bottom: 8px; background: #1e293b; border-radius: 10px; }
.actions { display: flex; gap: 6px; }
.actions button { padding: 6px 12px; border-radius: 6px; border: none;
  background: #334155; color: #e2e8f0; cursor: pointer; }
.actions .danger { background: #7f1d1d; }
</style>
