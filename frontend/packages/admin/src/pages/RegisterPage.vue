<script setup lang="ts">
// 老人注册向导（4 步：基本信息 → 声纹 → 人脸占位 → 完成）
// 迁移自 UI(old)/index.html「➕ 注册老人」，规格 docs/superpowers/specs/2026-08-24-elder-registration-flow-design.md
import { onMounted, onUnmounted, ref } from "vue";

const step = ref(1);
const steps = ["基本信息", "声纹", "人脸", "完成"];

// ---- 步骤 1：基本信息 ----
const uid = ref("");
const name = ref("");
const nickname = ref("");
const bed = ref("");
const age = ref<number | null>(null);
const gender = ref("");
const birthday = ref("");
const call = ref("");
const topicsRaw = ref("");
const style = ref("");
const notes = ref("");
const saving = ref(false);

// ---- 步骤 2：声纹 ----
const recState = ref<"idle" | "recording" | "result" | "error">("idle");
const recordingId = ref<string | null>(null);
const recCount = ref(15);
const segments = ref(0);
const recMsg = ref("");
const voiceDone = ref(false);
let timer: number | null = null;

// ---- 步骤 3：人脸（占位） ----
const faceUnavailable = ref(true);
const faceReason = ref("");

const msg = ref("");

async function json(path: string, opts?: { method?: string; body?: unknown }) {
  const res = await fetch(path, {
    method: opts?.method ?? "GET",
    headers: opts?.body ? { "Content-Type": "application/json" } : undefined,
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json();
}

async function nextElderUid(): Promise<string> {
  try {
    const body = await json("/api/profiles");
    const profiles: { uid?: string }[] = body.profiles ?? [];
    let max = 0;
    for (const p of profiles) {
      const m = /^elder_(\d+)$/.exec(p.uid ?? "");
      if (m) max = Math.max(max, parseInt(m[1], 10));
    }
    return "elder_" + String(max + 1).padStart(3, "0");
  } catch {
    return "elder_001";
  }
}

onMounted(async () => {
  uid.value = await nextElderUid();
  try {
    const st = await json("/api/face/status");
    faceUnavailable.value = (st.status ?? "unavailable") === "unavailable";
    faceReason.value = st.reason ?? "人脸录入尚未接入（占位）";
  } catch {
    faceUnavailable.value = true;
    faceReason.value = "人脸模块状态未知";
  }
});
onUnmounted(() => { if (timer) clearInterval(timer); });

// ---- 步骤 1：保存档案 ----
async function saveProfile() {
  const n = name.value.trim();
  if (!n) { msg.value = "请填写姓名"; return; }
  msg.value = "";
  saving.value = true;
  try {
    const topics = topicsRaw.value.split(/[，,]/).map((s) => s.trim()).filter(Boolean);
    const res = await json("/api/profiles", {
      method: "POST",
      body: {
        uid: uid.value.trim() || uid.value,
        name: n,
        nickname: nickname.value.trim(),
        bed: bed.value.trim(),
        age: age.value ?? 0,
        gender: gender.value.trim(),
        birthday: birthday.value.trim(),
        profile: { 病史: [], 用药: [] },
        preferences: { 称呼: call.value.trim(), 话题: topics },
        style: style.value.trim(),
        notes: notes.value.trim(),
      },
    });
    if (!res.ok) throw new Error(res.error || "保存失败");
    msg.value = "✅ 档案已保存（病史 / 用药可在记忆页补录）";
    step.value = 2;
  } catch (e) {
    msg.value = `保存失败：${e}`;
  } finally {
    saving.value = false;
  }
}

// ---- 步骤 2：声纹录制（两步式：录制暂存 → 试听/重录/保存入档） ----
async function startRecord() {
  if (timer) { clearInterval(timer); timer = null; }
  recState.value = "recording";
  recMsg.value = "";
  recCount.value = 15;
  timer = window.setInterval(() => {
    recCount.value = Math.max(recCount.value - 1, 0);
  }, 1000);
  try {
    const res = await json("/api/voice/record", {
      method: "POST",
      body: { seconds: 15, uid: uid.value },
    });
    if (!res.ok) throw new Error(res.error || "录制失败");
    if (timer) { clearInterval(timer); timer = null; }
    recordingId.value = res.recording_id;
    segments.value = res.segments ?? 0;
    recState.value = "result";
  } catch (e) {
    if (timer) { clearInterval(timer); timer = null; }
    recordingId.value = null;
    recState.value = "error";
    recMsg.value = String(e instanceof Error ? e.message : e);
  }
}

async function listen() {
  if (!recordingId.value) return;
  msg.value = "";
  try {
    const r = await fetch(`/api/voice/record/${recordingId.value}/audio`);
    if (!r.ok) { msg.value = "录音已过期，请重新录制"; return; }
    const url = URL.createObjectURL(await r.blob());
    new Audio(url).play();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (e) {
    msg.value = `试听失败：${e}`;
  }
}

async function commitVoice() {
  if (!recordingId.value) return;
  msg.value = "";
  try {
    const res = await json("/api/voice/enroll", {
      method: "POST",
      body: { uid: uid.value, recording_id: recordingId.value, append: false },
    });
    if (!res.ok) throw new Error(res.error || "保存失败");
    recordingId.value = null;
    voiceDone.value = true;
    msg.value = `✅ 声纹已建档（样本数 ${res.samples}）`;
    step.value = 3;
  } catch (e) {
    msg.value = `保存失败：${e}`;
  }
}

function skipVoice() { step.value = 3; }

// ---- 步骤 3 → 4：完成 ----
function finish() { step.value = 4; }

// 完成：把新 uid 记入 localStorage，对话/记忆页默认选中该老人（规格「注册后切换 currentUid」的 Vue 版）
function done() {
  try { localStorage.setItem("uid", uid.value); } catch { /* 忽略 */ }
  msg.value = `✅ 已切换到 ${uid.value}——「对话」「记忆」页默认选中该老人`;
}

// 注册下一位：重置表单并重算 uid
async function reset() {
  name.value = nickname.value = bed.value = gender.value = birthday.value = "";
  call.value = topicsRaw.value = style.value = notes.value = "";
  age.value = null;
  recordingId.value = null;
  recMsg.value = "";
  recState.value = "idle";
  segments.value = 0;
  voiceDone.value = false;
  msg.value = "";
  step.value = 1;
  uid.value = await nextElderUid();
}
</script>

<template>
  <div>
    <div class="toolbar">
      <h2>➕ 老人注册</h2>
      <span class="hint">为新入住老人建档：基本信息 → 声纹 → 人脸（待接入）</span>
      <span v-if="msg" :class="msg.startsWith('✅') ? 'ok' : 'err'">{{ msg }}</span>
    </div>

    <div class="steps">
      <template v-for="(s, i) in steps" :key="s">
        <span v-if="i" class="sep">──</span>
        <span :class="['step', { active: step === i + 1, done: step > i + 1 }]">
          <span class="num">{{ step > i + 1 ? "✓" : i + 1 }}</span>{{ s }}
        </span>
      </template>
    </div>

    <!-- 步骤 1：基本信息 -->
    <section v-if="step === 1">
      <div class="form-grid">
        <label class="field"><span>UID（自动生成，可改）</span><input v-model="uid" /></label>
        <label class="field"><span>姓名 *</span><input v-model="name" placeholder="张桂芳" /></label>
        <label class="field"><span>称呼</span><input v-model="nickname" placeholder="张奶奶" /></label>
        <label class="field"><span>床位</span><input v-model="bed" placeholder="3-15" /></label>
        <label class="field"><span>年龄</span><input v-model.number="age" type="number" /></label>
        <label class="field"><span>性别</span><input v-model="gender" placeholder="女" /></label>
        <label class="field"><span>生日</span><input v-model="birthday" type="date" /></label>
        <label class="field"><span>偏好称呼（如：闺女）</span><input v-model="call" placeholder="闺女" /></label>
        <label class="field wide"><span>喜欢话题（逗号分隔）</span><input v-model="topicsRaw" placeholder="京剧、孙子、养生" /></label>
        <label class="field wide"><span>说话风格画像</span><input v-model="style" placeholder="轻声细语，爱用『囡囡』称呼" /></label>
        <label class="field wide"><span>备注</span><input v-model="notes" placeholder="喜欢听评书，晚饭后散步" /></label>
      </div>
      <p class="hint">病史 / 用药可在注册完成后于记忆页档案表单补录</p>
      <div class="row-actions">
        <button class="primary" :disabled="saving" @click="saveProfile">
          {{ saving ? "保存中…" : "保存档案 → 下一步" }}
        </button>
      </div>
    </section>

    <!-- 步骤 2：声纹 -->
    <section v-else-if="step === 2">
      <div class="rec-panel">
        <template v-if="recState === 'idle'">
          <div class="big">🎙️</div>
          <div>请老人对着机器人说一段话（15 秒）</div>
          <p class="hint">录制完成后可试听、重录或保存；语音模块不可用时可跳过，稍后在记忆页追加</p>
          <div class="row-actions">
            <button class="primary" @click="startRecord">▶ 开始录制</button>
            <button class="ghost" @click="skipVoice">跳过（稍后追加）</button>
          </div>
        </template>
        <template v-else-if="recState === 'recording'">
          <div class="big">🔴 录音中…</div>
          <div class="countdown">{{ recCount }}</div>
          <p class="hint">请老人对着机器人说话</p>
        </template>
        <template v-else-if="recState === 'result'">
          <div class="big">✅</div>
          <div>录音完成，检测到 <b>{{ segments }}</b> 段有效语音</div>
          <div class="row-actions">
            <button @click="listen">🔊 试听</button>
            <button @click="startRecord">🔁 重录</button>
            <button class="primary" @click="commitVoice">💾 保存（首次建档）</button>
          </div>
          <p class="hint">「重录」丢弃本次录音重新录；「保存」建档后进入下一步</p>
        </template>
        <template v-else>
          <div class="big">⚠️</div>
          <div class="err">{{ recMsg || "录制失败" }}</div>
          <div class="row-actions">
            <button @click="startRecord">重试</button>
            <button class="ghost" @click="skipVoice">跳过（稍后追加）</button>
          </div>
        </template>
      </div>
    </section>

    <!-- 步骤 3：人脸（占位） -->
    <section v-else-if="step === 3">
      <div class="rec-panel">
        <div class="big">📷</div>
        <div>摄像头录入人脸 — 功能尚未接入</div>
        <p class="hint">{{ faceReason }}</p>
        <button class="disabled" disabled>📷 拍照录入（未开放）</button>
      </div>
      <div class="row-actions"><button class="primary" @click="finish">完成注册</button></div>
    </section>

    <!-- 步骤 4：完成 -->
    <section v-else>
      <div class="rec-panel done">
        <div class="big">🎉</div>
        <div class="title">老人注册成功！</div>
        <p class="hint">
          {{ name || "" }}（{{ uid }}）· 声纹{{ voiceDone ? "已建档" : "未建档（可在记忆页追加）" }} · 人脸待接入
        </p>
        <div class="row-actions">
          <button class="primary" @click="done">完成，切换到该老人</button>
          <button class="ghost" @click="reset">注册下一位</button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; flex-wrap: wrap; }
.toolbar h2 { margin: 0; font-size: 18px; }
.ok { color: #4ade80; font-size: 13px; }
.err { color: #f87171; font-size: 13px; }
.hint { color: #64748b; font-size: 12px; }
.steps { display: flex; align-items: center; gap: 6px; margin-bottom: 18px; color: #64748b; font-size: 14px; flex-wrap: wrap; }
.step .num { display: inline-flex; width: 20px; height: 20px; border-radius: 50%;
  background: #1e293b; align-items: center; justify-content: center; margin-right: 6px; font-size: 12px; }
.step.active { color: #f8fafc; font-weight: bold; }
.step.active .num { background: #1f9d55; }
.step.done { color: #94a3b8; }
.step.done .num { background: #1e3a5f; }
.sep { color: #374151; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 18px; margin-bottom: 10px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field.wide { grid-column: 1 / 3; }
.field span { color: #94a3b8; font-size: 13px; }
.field input { padding: 8px 12px; border-radius: 8px; border: 1px solid #334155;
  background: #1e293b; color: #e2e8f0; font-size: 14px; }
.rec-panel { background: #17233f; border: 1px solid #1e3a5f; border-radius: 12px;
  padding: 22px; text-align: center; margin-bottom: 16px; }
.rec-panel.done { border-color: #1f9d55; }
.big { font-size: 30px; margin-bottom: 6px; }
.title { font-size: 16px; margin: 6px 0; }
.countdown { font-size: 30px; color: #f87171; font-weight: bold; margin: 8px 0; }
.row-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 8px; }
.rec-panel .row-actions { justify-content: center; }
.row-actions button { padding: 8px 16px; border-radius: 8px; border: none;
  background: #1e3a5f; color: #e2e8f0; cursor: pointer; font-size: 14px; }
.row-actions button.primary { background: #1f9d55; }
.row-actions button.ghost { background: #334155; }
.row-actions button:disabled, button.disabled { opacity: 0.6; cursor: not-allowed; }
button.disabled { background: #333; color: #888; cursor: not-allowed; padding: 8px 16px;
  border-radius: 8px; border: none; margin-top: 10px; }
</style>
