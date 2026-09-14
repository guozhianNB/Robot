<script setup lang="ts">
// 区域面板：列表 + 增改删；polygon 支持「画多边形/画矩形」从画布取点
import { computed, ref, watch } from "vue";
import { deleteJson, enc, postJson } from "../lib/api";
import type { Zone, ZoneIn } from "../lib/types";

type Kind = "room" | "ward" | "bed" | "other";
type Shape = "polygon" | "rect";

const KINDS: { v: Kind; t: string }[] = [
  { v: "room", t: "room 房间" },
  { v: "ward", t: "ward 病区" },
  { v: "bed", t: "bed 床位" },
  { v: "other", t: "other 其它" },
];

const props = defineProps<{
  mapName: string;
  items: Zone[];
  selectedUid: string;
  /** 画布多边形模式连点过程中的草稿（米坐标） */
  draftPoints: number[][] | null;
  /** 画布双击结束后的完整多边形（米坐标） */
  donePolygon: number[][] | null;
  /** 画布拖拽完成后的矩形 4 角点（米坐标） */
  doneRect: number[][] | null;
  /** 父组件请求进入「画多边形」模式（每次递增的令牌） */
  wantDrawPolygon: number;
}>();

const emit = defineEmits<{
  (e: "select", uid: string): void;
  (e: "request-focus", p: { x: number; y: number }): void;
  (e: "shape-mode", m: "idle" | "polygon" | "rect"): void;
  (e: "changed"): void;
}>();

interface Form {
  uid: string;
  name: string;
  kind: Kind;
  shape: Shape;
  polygon: number[][];
  parent: string;
  note: string;
}

function emptyForm(): Form {
  return { uid: "", name: "", kind: "room", shape: "polygon", polygon: [], parent: "", note: "" };
}

const list = computed(() => props.items);
const form = ref<Form>(emptyForm());
const msg = ref("");
const errMsg = ref("");
const drawing = ref<"idle" | "polygon" | "rect">("idle");
const pointsText = ref("");

const isEdit = computed(() => !!form.value.uid);
const parents = computed(() => list.value.filter((z) => z.uid !== form.value.uid));

function startNew() {
  form.value = emptyForm();
  pointsText.value = "";
  msg.value = "";
  errMsg.value = "";
}

function startEdit(z: Zone) {
  form.value = {
    uid: z.uid,
    name: z.name || "",
    kind: (z.kind as Kind) || "room",
    shape: (z.shape as Shape) || "polygon",
    polygon: (z.polygon || []).map((p) => [Number(p[0]), Number(p[1])]),
    parent: z.parent || "",
    note: z.note || "",
  };
  pointsText.value = form.value.polygon.map((p) => `${p[0]},${p[1]}`).join("; ");
  msg.value = "";
  errMsg.value = "";
  emit("select", z.uid);
  const c = centroid(form.value.polygon);
  if (c) emit("request-focus", { x: c[0], y: c[1] });
}

function centroid(poly: number[][]): { x: number; y: number } | null {
  if (!poly.length) return null;
  let sx = 0;
  let sy = 0;
  for (const p of poly) {
    sx += Number(p[0]);
    sy += Number(p[1]);
  }
  return { x: sx / poly.length, y: sy / poly.length };
}

/** 顶点文本（每行/分号一组 `x,y`）→ 米坐标数组 */
function parsePoints(text: string): number[][] {
  const out: number[][] = [];
  for (const chunk of String(text).split(/[;\n]+/)) {
    const t = chunk.trim();
    if (!t) continue;
    const parts = t.split(/[,，\s]+/).filter(Boolean);
    if (parts.length < 2) continue;
    const x = Number(parts[0]);
    const y = Number(parts[1]);
    if (Number.isFinite(x) && Number.isFinite(y)) out.push([x, y]);
  }
  return out;
}

async function save() {
  msg.value = "";
  errMsg.value = "";
  const poly = parsePoints(pointsText.value);
  if (!form.value.name.trim()) {
    errMsg.value = "区域必须有名字";
    return;
  }
  if (form.value.shape === "polygon" && poly.length < 3) {
    errMsg.value = "多边形区域至少需要 3 个点（可用「画多边形」在图上取点）";
    return;
  }
  if (form.value.shape === "rect" && poly.length !== 4) {
    errMsg.value = "矩形需要 4 个角点（可用「画矩形」在图上拖拽）";
    return;
  }
  const body: ZoneIn = {
    map_name: props.mapName,
    name: form.value.name.trim(),
    kind: form.value.kind,
    shape: form.value.shape,
    polygon: poly,
    parent: form.value.parent,
    note: form.value.note,
  };
  const url = form.value.uid ? `/api/zones/${enc(form.value.uid)}` : "/api/zones";
  const r = await postJson<any>(url, body);
  if (!r.ok) {
    errMsg.value = r.error || "保存失败";
    return;
  }
  msg.value = `${isEdit.value ? "已更新" : "已新增"}区域 ${form.value.name}（uid=${r.data.uid}）`;
  if (!form.value.uid) form.value.uid = String(r.data.uid || "");
  emit("changed");
  emit("select", String(r.data.uid || ""));
}

async function remove(z: Zone) {
  if (!window.confirm(`确认删除区域「${z.name || z.uid}」？`)) return;
  const r = await deleteJson<any>(`/api/zones/${enc(z.uid)}?map=${enc(props.mapName)}`);
  if (!r.ok) {
    errMsg.value = r.error || "删除失败";
    return;
  }
  const orphaned: string[] = r.data.orphaned || [];
  msg.value = `已删除 ${z.name || z.uid}${orphaned.length ? `｜子区域 parent 已清空：${orphaned.join(", ")}` : ""}`;
  if (form.value.uid === z.uid) startNew();
  emit("changed");
}

function startDraw(m: "polygon" | "rect") {
  if (drawing.value === m) {
    drawing.value = "idle";
    emit("shape-mode", "idle");
    return;
  }
  beginDraw(m);
}

/** 进入绘制态但不做「再点一次取消」的翻转（供父组件请求使用） */
function beginDraw(m: "polygon" | "rect") {
  drawing.value = m;
  form.value.shape = m;
  if (m === "polygon") pointsText.value = "";
  msg.value =
    m === "polygon" ? "在画布上连点加顶点，双击结束" : "在画布上按住拖拽出矩形";
  emit("shape-mode", m);
}

function applyPolygon(poly: number[][]) {
  form.value.polygon = poly.map((p) => [Number(p[0]), Number(p[1])]);
  pointsText.value = form.value.polygon.map((p) => `${p[0]},${p[1]}`).join("; ");
  drawing.value = "idle";
  msg.value = `已取到 ${poly.length} 个顶点，记得点保存`;
  emit("shape-mode", "idle");
}

watch(() => props.mapName, () => {
  startNew();
});

// shape 下拉与真实绘制态必须同步：否则用户先点「画多边形」连了几个点，再从下拉切成 rect，
// 画布仍停在 polygon 模式（旧草稿点还在），保存时报"矩形要 4 个点"，用户完全看不懂。
watch(
  () => form.value.shape,
  (s, old) => {
    if (!old || s === old) return;          // 忽略初始化与 beginDraw 内部赋值
    drawing.value = "idle";
    emit("shape-mode", "idle");
    if (s === "polygon") pointsText.value = "";
  },
);

watch(
  () => props.selectedUid,
  (uid) => {
    if (!uid || uid === form.value.uid) return;
    const z = list.value.find((x) => x.uid === uid);
    if (z) startEdit(z);
  },
);

// 画布连点过程中的实时草稿（只回显，不落盘）
watch(
  () => props.draftPoints,
  (poly) => {
    if (!poly || !poly.length) return;
    if (drawing.value !== "polygon") return;
    pointsText.value = poly.map((p) => `${p[0]},${p[1]}`).join("; ");
  },
);

// 画布双击结束 → 完整多边形
watch(
  () => props.donePolygon,
  (poly) => {
    if (!poly || poly.length < 3) return;
    form.value.shape = "polygon";
    applyPolygon(poly);
  },
);

// 画布拖拽完成 → 矩形 4 角点
watch(
  () => props.doneRect,
  (poly) => {
    if (!poly || poly.length !== 4) return;
    form.value.shape = "rect";
    applyPolygon(poly);
  },
);

// 父组件请求进入「画多边形」模式（令牌递增；已在画则忽略）
watch(
  () => props.wantDrawPolygon,
  (v, old) => {
    if (!v || v === old) return;
    if (drawing.value === "polygon") return;
    beginDraw("polygon");
  },
);

</script>

<template>
  <div class="panel">
    <div class="head">
      <strong>区域</strong>
      <span class="count">{{ list.length }}</span>
      <span class="spacer"></span>
      <button class="mini" @click="emit('changed')">刷新</button>
    </div>

    <div class="hint err" v-if="errMsg">{{ errMsg }}</div>
    <div class="hint ok" v-if="msg">{{ msg }}</div>

    <div class="list">
      <div
        v-for="z in list"
        :key="z.uid"
        class="row"
        :class="{ active: z.uid === form.uid, sel: z.uid === selectedUid }"
        @click="startEdit(z)"
      >
        <div class="row-main">
          <span class="kind">{{ z.kind || "room" }}</span>
          <span class="nm">{{ z.name || z.uid }}</span>
          <span class="uid">{{ z.uid }}</span>
        </div>
        <div class="row-sub">
          {{ z.shape || "polygon" }} · {{ (z.polygon || []).length }} 点
          <span v-if="z.parent"> · parent={{ z.parent }}</span>
          <span v-if="z.note"> · {{ z.note }}</span>
        </div>
        <div class="row-act">
          <button
            class="mini"
            @click.stop="emit('request-focus', centroid(z.polygon || []))"
            :disabled="!(z.polygon || []).length"
          >定位</button>
          <button class="mini danger" @click.stop="remove(z)">删除</button>
        </div>
      </div>
      <div class="empty" v-if="!list.length">该图暂无区域</div>
    </div>

    <div class="form">
      <div class="form-head">
        <strong>{{ isEdit ? `编辑 ${form.uid}` : "新增区域" }}</strong>
        <span class="spacer"></span>
        <button class="mini" @click="startNew">清空</button>
      </div>

      <label>名字
        <input v-model="form.name" placeholder="如：101 病房" />
      </label>
      <div class="grid2">
        <label>kind
          <select v-model="form.kind">
            <option v-for="k in KINDS" :key="k.v" :value="k.v">{{ k.t }}</option>
          </select>
        </label>
        <label>shape
          <select v-model="form.shape">
            <option value="polygon">polygon</option>
            <option value="rect">rect</option>
          </select>
        </label>
      </div>
      <label>parent（本图其它区域）
        <select v-model="form.parent">
          <option value="">（无）</option>
          <option v-for="z in parents" :key="z.uid" :value="z.uid">
            {{ z.name || z.uid }}（{{ z.uid }}）
          </option>
        </select>
      </label>
      <label>顶点（米，`x,y` 分号或换行分隔）
        <textarea v-model="pointsText" rows="4" placeholder="1.2,3.4; 2.0,3.4; 2.0,4.1"></textarea>
      </label>
      <div class="draw">
        <button class="mini" :class="{ on: drawing === 'polygon' }" @click="startDraw('polygon')">
          {{ drawing === "polygon" ? "画多边形中…双击结束" : "画多边形" }}
        </button>
        <button class="mini" :class="{ on: drawing === 'rect' }" @click="startDraw('rect')">
          {{ drawing === "rect" ? "画矩形中…拖拽" : "画矩形" }}
        </button>
      </div>

      <div class="actions">
        <button class="primary" @click="save">{{ isEdit ? "保存修改" : "新增区域" }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; gap: 8px; height: 100%; min-height: 0; }
.head { display: flex; align-items: center; gap: 8px; }
.count { color: #94a3b8; font-size: 12px; }
.spacer { flex: 1; }
.hint { font-size: 12px; padding: 6px 8px; border-radius: 6px; }
.hint.gray { color: #94a3b8; background: #111827; border: 1px solid #1f2937; }
.hint.err { color: #fecaca; background: #450a0a; border: 1px solid #b91c1c; white-space: pre-wrap; }
.hint.ok { color: #bbf7d0; background: #052e16; border: 1px solid #15803d; }
.list { flex: 1 1 auto; overflow-y: auto; min-height: 72px; border: 1px solid #1f2937;
  border-radius: 8px; background: #0b1220; }
.row { padding: 8px 10px; border-bottom: 1px solid #111827; cursor: pointer; }
.row:hover { background: #111827; }
.row.active { background: #1e3a5f; }
.row.sel { box-shadow: inset 3px 0 0 #38bdf8; }
.row-main { display: flex; align-items: center; gap: 6px; }
.nm { font-size: 14px; color: #e2e8f0; }
.uid { font-size: 11px; color: #64748b; }
.kind { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: #1e3a5f; color: #bae6fd; }
.row-sub { font-size: 11px; color: #94a3b8; margin-top: 3px; }
.row-act { margin-top: 5px; display: flex; gap: 6px; }
.empty { color: #64748b; font-size: 12px; text-align: center; padding: 18px 0; }
/* 表单区：不再用 max-height 百分比（窗口不高时会把"新增区域/保存修改"按钮顶出可视区，
   且它的滚动条会被父级 overflow:hidden 裁掉）。改为 flex 收缩 + 自身滚动。 */
.form { border-top: 1px solid #1f2937; padding-top: 8px; display: flex;
  flex-direction: column; gap: 6px; flex: 0 1 auto; min-height: 0; overflow-y: auto; }
.form-head { display: flex; align-items: center; gap: 6px; }
label { display: flex; flex-direction: column; gap: 3px; font-size: 12px; color: #94a3b8; }
input, select, textarea { background: #0b1220; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 5px 7px; font-size: 13px; font-family: inherit; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
button { background: #1e293b; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
button:hover { background: #273449; }
button:disabled { opacity: .5; cursor: default; }
button.primary { background: #1d4ed8; border-color: #2563eb; }
button.danger { border-color: #7f1d1d; color: #fca5a5; }
button.mini { padding: 3px 7px; font-size: 11px; }
button.on { background: #7c2d12; border-color: #c2410c; color: #fed7aa; }
.draw { display: flex; gap: 6px; }
/* 主按钮常驻表单底部：即使表单很长，也始终能看到"新增区域 / 保存修改"。
   用不透明背景 + 向上分隔线，避免滚动时按钮和字段正文叠在一起。 */
.actions { display: flex; gap: 6px; position: sticky; bottom: 0;
  background: #0f172a; padding: 6px 0 2px; border-top: 1px solid #1f2937; }
</style>
