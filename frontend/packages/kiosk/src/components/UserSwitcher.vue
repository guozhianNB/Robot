<script setup lang="ts">
// 切换用户弹层：/api/profiles 列表 → 选人即锁定（规格 §5/D11）
import { onMounted, ref } from "vue";

interface Profile { uid: string; name: string; nickname: string; bed: string }

const props = defineProps<{ current: string | null }>();
const emit = defineEmits<{ (e: "pick", uid: string): void; (e: "close"): void }>();

const profiles = ref<Profile[]>([]);
const loading = ref(true);

onMounted(async () => {
  try {
    const res = await fetch("/api/profiles");
    const body = await res.json();
    profiles.value = (body.profiles ?? []) as Profile[];
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="sheet">
      <h2>选择当前老人</h2>
      <p v-if="loading">加载中…</p>
      <ul v-else>
        <li v-for="p in profiles" :key="p.uid"
            :class="{ active: p.uid === current }" @click="emit('pick', p.uid)">
          {{ p.nickname || p.name || p.uid }}
          <small v-if="p.bed">（{{ p.bed }}床）</small>
          <span v-if="p.uid === current">✓</span>
        </li>
      </ul>
      <button class="close" @click="emit('close')">关闭</button>
    </div>
  </div>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6);
  display: flex; align-items: center; justify-content: center; z-index: 50; }
.sheet { background: #1f2937; color: #f9fafb; padding: 28px; border-radius: 20px;
  min-width: 420px; font-size: 22px; }
.sheet h2 { margin-top: 0; }
.sheet li { padding: 14px 16px; border-radius: 12px; cursor: pointer;
  list-style: none; display: flex; gap: 8px; align-items: center; }
.sheet li.active { background: #2563eb; }
.sheet li span { margin-left: auto; color: #22c55e; }
.close { margin-top: 18px; width: 100%; padding: 14px; border-radius: 12px;
  background: #374151; color: #f9fafb; border: none; font-size: 20px; }
</style>
