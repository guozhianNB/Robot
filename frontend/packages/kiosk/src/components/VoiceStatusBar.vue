<script setup lang="ts">
// 状态条：语音状态机三色 + 角色徽标（规格 §5）——🛡 管理员剩 m:ss / 🏠 病房 / 👴 老人 + 🔒
import { computed, ref, watch } from "vue";
import { listWards, type SessionUser } from "shared";

const props = defineProps<{
  state: string;                  // idle / listening / speaking / unavailable（wake 已并入 listening）
  session: SessionUser | null;
}>();

const emit = defineEmits<{ (e: "open-switcher"): void }>();

const wards = ref<{ uid: string; name: string }[]>([]);

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

function fmt(sec: number | null) {
  if (sec == null) return "";
  const s = Math.max(0, Math.floor(sec));
  return ` 剩 ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

const badge = computed(() => {
  const s = props.session;
  if (!s) return "👤 未选择";
  if (s.role === "admin") return `🛡 管理员${fmt(s.ttl_remain)}`;
  if (s.role === "elder") return `👴 ${s.uid}${s.locked ? " 🔒" : ""}`;
  const w = wards.value.find(x => x.uid === s.ward_uid);
  return `🏠 ${w?.name || s.ward_uid || "未选病房"}${s.locked ? " 🔒" : ""}`;
});

// 病房名：集体层徽标要显示**名字**（uid 只是兜底）。只在病房/角色变化时拉一次，
// 不做轮询；失败保留上一次（后端不可达时退化成 uid）。
watch(
  () => [props.session?.role, props.session?.ward_uid],
  async () => {
    if (props.session && props.session.role !== "ward") return;   // 只有集体层需要病房名
    try {
      const r = await listWards("kiosk");
      wards.value = (r.wards ?? []).map(w => ({ uid: w.uid, name: w.name }));
    } catch { /* 保留旧值 */ }
  },
  { immediate: true },
);
</script>

<template>
  <div class="status-bar">
    <span class="dot" :style="{ background: color }"></span>
    <span class="label">{{ label }}</span>
    <button class="user" title="切换用户和层级" @click="emit('open-switcher')">
      <span class="identity">{{ badge }}</span>
      <span class="switch-label">切换用户</span>
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
.user { margin-left: auto; display: flex; align-items: center; gap: 12px;
  background: #1f2937; border: 1px solid #4b5563; color: #f9fafb;
  padding: 8px 10px 8px 16px; border-radius: 8px; font-size: 20px; cursor: pointer; }
.identity { white-space: nowrap; }
.switch-label { padding: 6px 10px; border-left: 1px solid #4b5563; color: #bfdbfe;
  font-size: 16px; white-space: nowrap; }
</style>
