<script setup lang="ts">
// 地图编辑器（独立进程 :8010）的按需启停入口。
// 规格：docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md §4.1
// 为什么要有这一页：编辑器（像素修图 + 划线/标点）默认不跑，跑起来是另一个进程；
// 这里只做「启动 / 停止 / 看状态」，编辑器本身在它自己的窗口里。
import { onMounted, onUnmounted, ref } from "vue";
import { getMapEditorService, startMapEditor, stopMapEditor, type MapEditorService } from "shared";

const st = ref<MapEditorService | null>(null);
const busy = ref(false);
const note = ref("");
const err = ref("");
let timer: number | null = null;
let pending = false;
let queued = false;          // 命中 pending 时的"补跑"意图

async function refresh(force = false) {
  if (pending) { if (force) queued = true; return; }
  pending = true;
  try {
    const r = await getMapEditorService("admin");
    if (!r.ok) { err.value = r.error ?? "读取服务状态失败"; return; }
    st.value = r;
    err.value = "";
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    pending = false;
    if (queued) { queued = false; void refresh(); }
  }
}

/** 编辑器 URL：用 location.hostname（不是 127.0.0.1）——从另一台机器看 admin 时也能开对。 */
function editorUrl(port: number) {
  return `http://${window.location.hostname}:${port}/mapeditor/`;
}

/** 兜底入口：被拦截 / 窗口被误关后，仍能从本页把编辑器叫回来。
 *  同样必须是脚本 window.open —— 编辑器里的 window.close() 只对脚本开的窗口生效。 */
function openEditor() {
  if (!st.value?.port) return;
  window.open(editorUrl(st.value.port), "_blank");
}

async function start() {
  busy.value = true; note.value = ""; err.value = "";
  // 先在点击的同步栈里开一个空窗口：这样浏览器不会判为"非用户手势弹窗"。
  // 启动最长要等 20s（后端轮询就绪），远超 transient activation 的 ~5s 窗口。
  const w = window.open("about:blank", "_blank");
  try {
    const r = await startMapEditor("admin");
    st.value = r;
    if (!r.ok) {
      w?.close();
      err.value = r.error ?? "启动失败（详情见后端终端）";
      return;
    }
    note.value = r.source === "external"
      ? "已有外部实例在跑，直接打开。"
      : `已启动（PID ${r.pid ?? "?"}）。`;
    if (w) w.location.href = editorUrl(r.port);
    await refresh(true);
    // 放在 refresh 之后：refresh 成功会清 err，否则这句提示只会闪现几毫秒。
    if (!w) err.value = "浏览器拦截了新窗口 —— 请点下面「打开编辑器窗口」手动打开。";
  } catch (e) {
    w?.close();
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function stop() {
  if (!window.confirm("确定停止地图编辑器服务？（编辑器窗口会连不上，需要重新点启动）")) return;
  busy.value = true; note.value = ""; err.value = "";
  try {
    const r = await stopMapEditor("admin");
    st.value = r;
    if (!r.ok) { err.value = r.error ?? "停止失败"; return; }
    note.value = "已停止。";
    await refresh(true);
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

function label(s: MapEditorService | null) {
  if (!s) return "读取中…";
  if (!s.running) return "未启动";
  const who = s.source === "managed" ? `PID ${s.pid ?? "?"}（managed）` : "外部实例（external）";
  const up = s.uptime_s != null ? `，已运行 ${Math.round(s.uptime_s)} 秒` : "";
  return `运行中（${who}，端口 ${s.port}${up}）`;
}

onMounted(() => {
  void refresh();
  timer = window.setInterval(() => { void refresh(); }, 5000);
});
onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
});
</script>

<template>
  <div class="page">
    <h2>🗺 地图编辑器</h2>
    <p class="hint">
      像素修图与划线/标点都在这里按需启动 —— 编辑器是<b>独立进程</b>（默认端口 8010），
      不用它的时候不占资源；在编辑器里点「保存并退出」会自动把它停掉。
    </p>

    <div class="card">
      <div class="row">
        <span class="state" :class="{ on: st?.running }">{{ label(st) }}</span>
        <button :disabled="busy || st?.running === true" @click="start">
          {{ busy ? "处理中…" : "启动地图编辑器" }}
        </button>
        <button v-if="st?.running && st?.port" :disabled="busy" @click="openEditor()">打开编辑器窗口</button>
        <button class="danger" :disabled="busy || !st?.running" @click="stop">停止服务</button>
        <button :disabled="busy" @click="refresh(true)">刷新状态</button>
      </div>
      <p v-if="note" class="ok">{{ note }}</p>
      <p v-if="err" class="bad">{{ err }}</p>
    </div>

    <div class="card">
      <h3>用法</h3>
      <ol>
        <li>点「启动地图编辑器」→ 自动打开编辑器窗口（新标签页）。</li>
        <li>在编辑器里列图 / 标地点 / 画区域；像素修图点地图文件面板里的「像素修图」。</li>
        <li>干完点「保存并退出」→ 保存 + 停服务 + 关窗。<b>只关窗</b>（点浏览器 ×）不会停服务，
            回本页点「停止服务」即可。</li>
        <li>⚠️ 改完地图<b>必须重启导航</b>才生效：<code>~/tools/nav_screen.sh nav &lt;地图名&gt;</code>。</li>
      </ol>
    </div>
  </div>
</template>

<style scoped>
.page { max-width: 900px; }
h2 { margin: 0 0 8px; font-size: 20px; }
h3 { margin: 0 0 8px; font-size: 15px; color: #cbd5e1; }
.hint { color: #94a3b8; font-size: 13px; line-height: 1.7; }
.card { background: #111827; border: 1px solid #1f2937; border-radius: 10px;
  padding: 14px 16px; margin-bottom: 16px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.state { margin-right: auto; color: #94a3b8; font-size: 14px; }
.state.on { color: #4ade80; }
button { padding: 8px 16px; border-radius: 8px; border: none; background: #1e3a5f;
  color: #e2e8f0; cursor: pointer; font-size: 14px; }
button.danger { background: #7f1d1d; }
button:disabled { opacity: 0.55; cursor: not-allowed; }
.ok { color: #4ade80; font-size: 13px; margin: 10px 0 0; }
.bad { color: #f87171; font-size: 13px; margin: 10px 0 0; }
ol { margin: 0; padding-left: 20px; color: #cbd5e1; font-size: 13px; line-height: 1.9; }
code { background: #1e293b; padding: 1px 5px; border-radius: 4px; }
</style>
