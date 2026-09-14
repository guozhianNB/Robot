<script setup lang="ts">
// 地图编辑器壳：顶部状态条 + 左画布 / 右面板（地点 / 区域 / 地图文件）
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import MapCanvas from "./pages/MapCanvas.vue";
import PlacePanel from "./pages/PlacePanel.vue";
import ZonePanel from "./pages/ZonePanel.vue";
import MapFiles from "./pages/MapFiles.vue";
import { enc, getJson } from "./lib/api";
import type {
  CurrentMapResp,
  Destination,
  DrawMode,
  IoStatus,
  MapListResp,
  MapMetaFields,
  MetaResp,
  PoseResp,
  Zone,
} from "./lib/types";

type Tab = "places" | "zones" | "files";
type EditorStatusResp = {
  ok: boolean;
  io: IoStatus;
  locator: { pose: PoseResp; current_map: CurrentMapResp };
};

const tabs: { id: Tab; label: string }[] = [
  { id: "places", label: "地点" },
  { id: "zones", label: "区域" },
  { id: "files", label: "地图文件" },
];

const tab = ref<Tab>("places");
const mapName = ref("");
const maps = ref<MapListResp | null>(null);
const meta = ref<MapMetaFields | null>(null);
const places = ref<Destination[]>([]);
const zones = ref<Zone[]>([]);
const pose = ref<PoseResp | null>(null);
const currentMap = ref<CurrentMapResp | null>(null);
const io = ref<IoStatus | null>(null);
const fingerprint = ref<{ changed?: boolean; reasons?: string[] } | null>(null);
const loadNote = ref("");
const canvasStale = ref(false);

const drawMode = ref<DrawMode>("idle");
const selectedUid = ref("");
const showPlaces = ref(true);
const showZones = ref(true);
const draftPoint = ref<{ x: number; y: number } | null>(null);
const draftPoints = ref<number[][] | null>(null);
const donePolygon = ref<number[][] | null>(null);
const doneRect = ref<number[][] | null>(null);
const wantDrawPolygon = ref(0);

const canvasRef = ref<InstanceType<typeof MapCanvas> | null>(null);
let timer: number | null = null;
let statusPending = false;

const poseOk = computed(() => pose.value?.status === "ok");
const yawDeg = computed(() =>
  pose.value?.yaw === null || pose.value?.yaw === undefined
    ? null
    : (Number(pose.value.yaw) * 180) / Math.PI,
);
const curName = computed(() => currentMap.value?.name || "");
/** 车此刻在跑的就是当前打开这张图吗 */
const robotOnThisMap = computed(() => !!curName.value && curName.value === mapName.value);
const curLine = computed(() => {
  const c = currentMap.value;
  if (!c) return "读取中…";
  if (c.name) {
    if (c.name === mapName.value) return `车此刻跑的就是本图「${c.name}」`;
    // ambiguous 是"命中的地图名列表"：把候选直接告诉用户，否则他没法处理这种歧义
    const hits = Array.isArray(c.ambiguous)
      ? `（元数据同时命中：${c.ambiguous.join("、")}）`
      : c.ambiguous ? "（多项命中，可能不准）" : "";
    return `车此刻跑的是「${c.name}」${hits}`;
  }
  return c.detail || "未能识别（导航未运行？看图与标点不受影响）";
});
const fpLine = computed(() => {
  const fp = fingerprint.value;
  if (!fp) return "";
  return fp.changed
    ? `标记指纹与元数据不一致：${(fp.reasons || []).join("；")}`
    : "标记指纹与元数据一致";
});

function round2(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(2) : "—";
}

// ---------------------------------------------------------------- 加载
async function loadMaps() {
  const r = await getJson<MapListResp>("/api/map/list");
  if (!r.ok) {
    loadNote.value = r.error || "加载地图列表失败";
    return;
  }
  maps.value = r.data;
  if (r.data.status === "unavailable") {
    loadNote.value = r.data.reason || "地图存储不可用";
  } else if (!loadNote.value) {
    loadNote.value = "";
  }
  const list = r.data.maps || [];
  if (!mapName.value) {
    const cur = r.data.current_map || "";
    const pick = list.find((m) => m.name === cur) || list[0];
    if (pick) await pickMap(pick.name);
  } else if (!list.some((m) => m.name === mapName.value)) {
    // 当前图被删/改名 → 退回第一张
    if (list[0]) await pickMap(list[0].name);
    else mapName.value = "";
  } else {
    await Promise.all([loadMeta(), loadPlaces(), loadZones()]);
  }
}

async function pickMap(name: string) {
  if (!name) return;
  mapName.value = name;
  selectedUid.value = "";
  drawMode.value = "idle";
  draftPoint.value = null;
  draftPoints.value = null;
  donePolygon.value = null;
  doneRect.value = null;
  meta.value = { name, width: null, height: null, resolution: null, origin: null,
    negate: null, occupied_thresh: null, free_thresh: null } as MapMetaFields;
  fingerprint.value = null;
  await Promise.all([loadMeta(), loadPlaces(), loadZones()]);
}

async function loadMeta() {
  if (!mapName.value) return;
  const r = await getJson<MetaResp>(`/api/map/${enc(mapName.value)}/meta`);
  if (!r.ok) {
    loadNote.value = r.error || "读取地图元数据失败";
    return;
  }
  if (r.data.meta) meta.value = r.data.meta;
  fingerprint.value = r.data.fingerprint || null;
  if (r.data.tags_warnings?.length) {
    loadNote.value = `标记文件告警：${r.data.tags_warnings.join("；")}`;
  }
}

async function loadPlaces() {
  if (!mapName.value) return;
  const r = await getJson<any>(`/api/destinations?map=${enc(mapName.value)}`);
  places.value = r.ok && Array.isArray(r.data.destinations) ? r.data.destinations : [];
}

async function loadZones() {
  if (!mapName.value) return;
  const r = await getJson<any>(`/api/zones?map=${enc(mapName.value)}`);
  zones.value = r.ok && Array.isArray(r.data.zones) ? r.data.zones : [];
}

/** 顶部状态条轮询：后端串行读取 rosbridge，前端只发一个汇总请求。 */
async function statusTick() {
  if (statusPending) return;
  statusPending = true;
  try {
    const r = await getJson<EditorStatusResp>("/api/mapeditor/status");
    if (r.ok) {
      pose.value = r.data.locator.pose;
      currentMap.value = r.data.locator.current_map;
      io.value = r.data.io;
    }
  } finally {
    statusPending = false;
  }
}

async function refreshAll() {
  await loadMaps();
  await statusTick();
}

onMounted(() => {
  void refreshAll();
  timer = window.setInterval(() => {
    void statusTick();
  }, 2500);
});

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
});

// ---------------------------------------------------------------- 画布事件
function onPoint(p: { x: number; y: number }) {
  draftPoint.value = { ...p };
  drawMode.value = "idle";
  tab.value = "places";
}

function onDraft(poly: number[][]) {
  draftPoints.value = poly.slice();
}

function onPolygonDone(poly: number[][]) {
  donePolygon.value = poly.slice();
  draftPoints.value = null;
  drawMode.value = "idle";
  tab.value = "zones";
}

function onRectDone(poly: number[][]) {
  doneRect.value = poly.slice();
  drawMode.value = "idle";
  tab.value = "zones";
}

function onSelect(uid: string) {
  selectedUid.value = uid;
}

function onCanvasStale(v: boolean) {
  canvasStale.value = v;
}

function setDrawMode(m: DrawMode) {
  drawMode.value = m;
  if (m !== "idle") tab.value = m === "point" ? "places" : "zones";
  // 顶部工具条进入「画多边形」时，把区域面板也切到取点态
  if (m === "polygon") wantDrawPolygon.value += 1;
}

function setPlacing(v: boolean) {
  if (v) {
    setDrawMode("point");
    tab.value = "places";
  } else if (drawMode.value === "point") {
    drawMode.value = "idle";
  }
}

function requestFocus(p: { x: number; y: number } | null) {
  if (!p) return;
  canvasRef.value?.focusOn(Number(p.x), Number(p.y));
}

function onSelectMap(e: Event) {
  const v = (e.target as HTMLSelectElement | null)?.value || "";
  if (v) void pickMap(v);
}
</script>

<template>
  <div class="app">
    <!-- 顶部状态条 -->
    <header>
      <div class="brand">🗺 地图编辑器</div>
      <select class="mapsel" :value="mapName" @change="onSelectMap">
        <option v-for="m in maps?.maps || []" :key="m.name" :value="m.name">
          {{ m.name }}{{ m.current ? "（当前）" : "" }}{{ m.status && m.status !== "ok" ? ` · ${m.status}` : "" }}
        </option>
      </select>
      <button class="mini" :class="{ on: drawMode === 'point' }" @click="setDrawMode(drawMode === 'point' ? 'idle' : 'point')">
        标点
      </button>
      <button class="mini" :class="{ on: drawMode === 'polygon' }"
              @click="setDrawMode(drawMode === 'polygon' ? 'idle' : 'polygon')">画多边形</button>
      <button class="mini" :class="{ on: drawMode === 'rect' }"
              @click="setDrawMode(drawMode === 'rect' ? 'idle' : 'rect')">画矩形</button>
      <button class="mini" @click="canvasRef?.resetView()">复位视图</button>
      <label class="chk"><input type="checkbox" v-model="showZones" /> 区域</label>
      <label class="chk"><input type="checkbox" v-model="showPlaces" /> 地点</label>
      <button class="mini" @click="refreshAll()">刷新</button>

      <span class="spacer"></span>

      <span class="pill" :class="io?.mode === 'ssh' ? 'warn' : ''">
        IO {{ io?.mode || "?" }}
        <b :class="io?.available ? 'good' : 'bad'">{{ io?.available ? "可用" : "不可用" }}</b>
      </span>
      <span class="pill" v-if="io?.host">host {{ io.host }}</span>
      <span class="pill" :class="poseOk ? '' : 'gray'">
        rosbridge
        <b :class="poseOk ? 'good' : 'bad'">{{ poseOk ? "已连" : "不可用" }}</b>
        <template v-if="poseOk">
          · 车 ({{ round2(pose?.x) }}, {{ round2(pose?.y) }}) m
          · {{ yawDeg === null ? "—" : yawDeg.toFixed(0) }}°
          <b v-if="pose?.suspect" class="bad">位姿可疑</b>
        </template>
      </span>
      <span class="pill" :class="robotOnThisMap ? 'good-pill' : 'gray'">
        /map 指纹：{{ curName || "未识别" }}
        <template v-if="robotOnThisMap">（就是本图）</template>
        <button v-if="curName && !robotOnThisMap" class="mini" @click="pickMap(curName)">切到它</button>
      </span>
    </header>

    <div class="sub">
      <span>{{ curLine }}</span>
      <span v-if="pose?.status === 'unavailable'" class="gray">· {{ pose?.reason }}</span>
      <span v-if="pose?.note" class="bad">· {{ pose.note }}</span>
      <span v-if="io && !io.available" class="gray">· 地图存储：{{ io.reason }}</span>
      <span v-if="canvasStale" class="bad">· 当前离线，画布显示缓存</span>
      <span v-if="fpLine" class="gray">· {{ fpLine }}</span>
      <span v-if="loadNote" class="bad">· {{ loadNote }}</span>
    </div>

    <!-- 左画布 + 右面板 -->
    <div class="body">
      <MapCanvas
        ref="canvasRef"
        :map-name="mapName"
        :meta="meta"
        :places="places"
        :zones="zones"
        :pose="pose"
        :robot-on-this-map="robotOnThisMap"
        :draw-mode="drawMode"
        :selected-uid="selectedUid"
        :show-places="showPlaces"
        :show-zones="showZones"
        @point="onPoint"
        @draft="onDraft"
        @polygon="onPolygonDone"
        @rect="onRectDone"
        @select="onSelect"
        @mode-change="setDrawMode"
        @stale="onCanvasStale"
      />

      <aside>
        <nav>
          <button v-for="t in tabs" :key="t.id" :class="{ active: tab === t.id }"
                  @click="tab = t.id">{{ t.label }}</button>
        </nav>
        <div class="pane">
          <!-- 三个面板都保持挂载（v-show）：画布上的落点/多边形/矩形要能实时写进对应表单 -->
          <PlacePanel
            v-show="tab === 'places'"
            :map-name="mapName"
            :items="places"
            :selected-uid="selectedUid"
            :draft-point="draftPoint"
            :want-placing="drawMode === 'point'"
            @select="onSelect"
            @request-focus="requestFocus"
            @placing="setPlacing"
            @changed="() => { void loadPlaces(); void loadMeta(); }"
          />
          <ZonePanel
            v-show="tab === 'zones'"
            :map-name="mapName"
            :items="zones"
            :selected-uid="selectedUid"
            :draft-points="draftPoints"
            :done-polygon="donePolygon"
            :done-rect="doneRect"
            :want-draw-polygon="wantDrawPolygon"
            @select="onSelect"
            @request-focus="requestFocus"
            @shape-mode="setDrawMode"
            @changed="() => { void loadZones(); void loadMeta(); }"
          />
          <MapFiles
            v-if="tab === 'files'"
            :selected-map="mapName"
            :maps="maps"
            @pick="pickMap"
            @changed="() => { void refreshAll(); }"
          />
        </div>
      </aside>
    </div>
  </div>
</template>

<style>
html, body, #app { height: 100%; margin: 0; }
body { background: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; }
</style>

<style scoped>
.app { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
header { display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 8px 12px; background: #111827; border-bottom: 1px solid #1f2937; }
.brand { font-size: 15px; font-weight: 600; margin-right: 4px; }
.spacer { flex: 1; }
.mapsel { background: #0b1220; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 5px 8px; font-size: 13px; max-width: 220px; }
.pill { font-size: 12px; color: #cbd5e1; background: #0b1220; border: 1px solid #1f2937;
  border-radius: 999px; padding: 3px 10px; display: inline-flex; align-items: center; gap: 5px; }
.pill.gray { color: #94a3b8; }
.pill.warn { border-color: #b45309; }
.pill.good-pill { border-color: #0e7490; }
.pill b { font-weight: 600; }
.good { color: #4ade80; }
.bad { color: #f87171; }
.chk { font-size: 12px; color: #94a3b8; display: inline-flex; align-items: center; gap: 3px; }
button { background: #1e293b; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 5px 10px; font-size: 12px; cursor: pointer; }
button:hover { background: #273449; }
button.mini { padding: 4px 8px; font-size: 11px; }
button.mini.on { background: #7c2d12; border-color: #c2410c; color: #fed7aa; }
.sub { display: flex; gap: 8px; flex-wrap: wrap; font-size: 12px; color: #94a3b8;
  padding: 5px 12px; background: #0b1220; border-bottom: 1px solid #1f2937; }
.sub .gray { color: #94a3b8; }
.sub .bad { color: #f87171; }
.body { flex: 1; display: flex; min-height: 0; }
aside { width: 460px; min-width: 340px; border-left: 1px solid #1f2937; background: #0f172a;
  display: flex; flex-direction: column; min-height: 0; }
aside nav { display: flex; gap: 4px; padding: 8px 10px 0; }
aside nav button { background: none; border: none; color: #94a3b8; padding: 6px 12px;
  border-radius: 8px 8px 0 0; font-size: 13px; }
aside nav button.active { background: #1e3a5f; color: #f8fafc; }
/* 面板容器：自己不开滚动条，但必须允许子级收缩 —— 否则子级的滚动条会被 overflow:hidden 裁掉，
   表单底部的主按钮就永远露不出来（实测踩过：区域面板"看不到保存按钮"）。 */
.pane { flex: 1; min-height: 0; padding: 10px; overflow: hidden; }
.pane > * { flex: 1; min-height: 0; }
</style>
