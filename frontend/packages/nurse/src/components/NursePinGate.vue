<script setup lang="ts">
import { onMounted, ref } from "vue";
import { unlockNursePage } from "shared";

const emit = defineEmits<{ (event: "unlocked"): void }>();
const SESSION_KEY = "nurse-page-unlocked-v1";
const pin = ref("");
const busy = ref(false);
const errorText = ref("");

onMounted(() => {
  if (sessionStorage.getItem(SESSION_KEY) === "1") emit("unlocked");
});

async function submit() {
  if (!pin.value || busy.value) return;
  busy.value = true;
  errorText.value = "";
  try {
    const result = await unlockNursePage(pin.value);
    pin.value = "";
    if (!result.ok) {
      errorText.value = result.error || "口令不正确";
      return;
    }
    sessionStorage.setItem(SESSION_KEY, "1");
    emit("unlocked");
  } catch (error) {
    errorText.value = error instanceof Error ? error.message : "暂时无法验证口令";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <main class="gate-shell">
    <form class="gate" @submit.prevent="submit">
      <div class="mark" aria-hidden="true">＋</div>
      <h1>护士台</h1>
      <label for="nurse-pin">页面口令</label>
      <div class="entry">
        <input id="nurse-pin" v-model="pin" type="password" inputmode="numeric"
               autocomplete="current-password" autofocus aria-describedby="pin-error" />
        <button type="submit" :disabled="busy || !pin">{{ busy ? "验证中" : "进入" }}</button>
      </div>
      <p v-if="errorText" id="pin-error" class="error" role="alert">{{ errorText }}</p>
    </form>
  </main>
</template>

<style scoped>
.gate-shell { min-height: 100%; display: grid; place-items: center; padding: 20px; box-sizing: border-box; background: #eef2f6; }
.gate { width: min(340px, 100%); padding: 24px; box-sizing: border-box; background: #fff; border: 1px solid #d8e0e8; border-top: 3px solid #1e3a5f; box-shadow: 0 8px 24px rgb(15 23 42 / 0.08); }
.mark { width: 30px; height: 30px; display: grid; place-items: center; background: #b91c1c; color: #fff; border-radius: 4px; font-size: 22px; line-height: 1; }
h1 { margin: 10px 0 18px; font-size: 20px; letter-spacing: 0; }
label { display: block; margin-bottom: 6px; color: #475569; font-size: 13px; }
.entry { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
input, button { height: 36px; box-sizing: border-box; border-radius: 5px; font: inherit; }
input { min-width: 0; border: 1px solid #aebbc8; padding: 0 10px; }
input:focus { outline: 2px solid #93c5fd; border-color: #2563eb; }
button { border: 1px solid #1e3a5f; background: #1e3a5f; color: #fff; padding: 0 16px; cursor: pointer; }
button:disabled { opacity: .55; cursor: not-allowed; }
.error { margin: 10px 0 0; color: #b91c1c; font-size: 13px; }
</style>
