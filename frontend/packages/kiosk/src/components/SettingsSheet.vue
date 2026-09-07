<script setup lang="ts">
// 设置弹层：老人相关项（语音/音量/亮度/唤醒词显示），共享后端设置（规格 §5）
import { onMounted, ref } from "vue";

const emit = defineEmits<{ (e: "close"): void }>();

const settings = ref<Record<string, unknown>>({});
const loaded = ref(false);

onMounted(async () => {
  try {
    const res = await fetch("/api/settings");
    const body = await res.json();
    settings.value = body.settings ?? {};
  } finally {
    loaded.value = true;
  }
});

async function save(key: string, value: unknown) {
  settings.value[key] = value;
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: { [key]: value } }),
  });
}
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="sheet">
      <h2>设置</h2>
      <template v-if="loaded">
        <label>
          <input type="checkbox" :checked="!!settings.voice_enabled"
                 @change="save('voice_enabled', ($event.target as HTMLInputElement).checked)" />
          语音交互
        </label>
        <label>
          <input type="checkbox" :checked="!!settings.tts_enabled"
                 @change="save('tts_enabled', ($event.target as HTMLInputElement).checked)" />
          语音播报
        </label>
        <div class="group">
          <div class="group-title">识别引擎（重启服务后生效）</div>
          <label>
            <input type="radio" name="asr_provider" value="local"
                   :checked="settings.asr_provider !== 'cloud'"
                   @change="save('asr_provider', 'local')" />
            本地识别（离线）
          </label>
          <label>
            <input type="radio" name="asr_provider" value="cloud"
                   :checked="settings.asr_provider === 'cloud'"
                   @change="save('asr_provider', 'cloud')" />
            云端识别（火山）
          </label>
        </div>
        <div class="group">
          <div class="group-title">合成引擎（重启服务后生效）</div>
          <label>
            <input type="radio" name="tts_provider" value="local"
                   :checked="settings.tts_provider !== 'cloud'"
                   @change="save('tts_provider', 'local')" />
            本地合成（离线）
          </label>
          <label>
            <input type="radio" name="tts_provider" value="cloud"
                   :checked="settings.tts_provider === 'cloud'"
                   @change="save('tts_provider', 'cloud')" />
            云端合成（火山）
          </label>
        </div>
        <label>
          唤醒词：<b>{{ settings.wakeword ?? "小机器人" }}</b>
        </label>
      </template>
      <button class="close" @click="emit('close')">关闭</button>
    </div>
  </div>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6);
  display: flex; align-items: center; justify-content: center; z-index: 50; }
.sheet { background: #1f2937; color: #f9fafb; padding: 28px; border-radius: 20px;
  min-width: 380px; font-size: 22px; display: flex; flex-direction: column; gap: 18px; }
.sheet h2 { margin-top: 0; }
.sheet label { display: flex; gap: 12px; align-items: center; }
.sheet input[type="checkbox"] { width: 26px; height: 26px; }
.sheet input[type="radio"] { width: 22px; height: 22px; }
.sheet .group { display: flex; flex-direction: column; gap: 8px;
  padding: 10px 14px; border: 1px solid #374151; border-radius: 12px; }
.sheet .group-title { font-size: 16px; color: #9ca3af; margin-bottom: 2px; }
.close { padding: 14px; border-radius: 12px; background: #374151;
  color: #f9fafb; border: none; font-size: 20px; }
</style>
