<script setup lang="ts">
// 紧急呼叫（规格 D6）：始终一键直达
const emit = defineEmits<{ (e: "sos"): void }>();
</script>

<template>
  <button class="sos" @click="emit('sos')">🆘 紧急呼叫</button>
</template>

<style scoped>
.sos { background: #dc2626; color: #fff; border: none; padding: 20px 32px;
  border-radius: 16px; font-size: 26px; font-weight: bold; }
</style>
