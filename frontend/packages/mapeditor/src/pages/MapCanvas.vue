<script setup lang="ts">
// 地图画布：纯 <canvas> 自绘（不引 leaflet/openlayers）。
// 坐标系：米 ←→ 屏幕像素，换算规则与后端 mapserver 严格同口径（y 轴翻转）。
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { metersToPixel, pixelToMeters, pointInPolygon, type Point } from "../lib/coords";
import { classifyPixel, colorFor } from "../lib/colorize";
import { mapImageUrl } from "../lib/api";
import type { Destination, DrawMode, MapMetaFields, PoseResp, Zone } from "../lib/types";

/** 各模式的画布提示语 */
const MODE_HINTS: Record<string, string> = {
  idle: "滚轮缩放 · 拖拽平移 · 双击复位 · 单击选中区域/地点",
  point: "标点模式：单击画布落点（取米坐标）",
  polygon: "画多边形：连点加顶点，双击结束（至少 3 点）",
  rect: "画矩形：按住拖拽出矩形，松手完成",
};

const props = withDefaults(
  defineProps<{
    mapName: string;
    meta: MapMetaFields | null;
    places: Destination[];
    zones: Zone[];
    pose: PoseResp | null;
    robotOnThisMap: boolean;
    drawMode?: DrawMode;
    selectedUid?: string;
    showPlaces?: boolean;
    showZones?: boolean;
  }>(),
  {
    drawMode: "idle",
    selectedUid: "",
    showPlaces: true,
    showZones: true,
  },
);

const emit = defineEmits<{
  (e: "point", p: { x: number; y: number }): void;
  (e: "polygon", poly: number[][]): void;
  (e: "rect", poly: number[][]): void;
  (e: "draft", poly: number[][]): void;
  (e: "select", uid: string): void;
  (e: "mode-change", m: DrawMode): void;
  (e: "stale", v: boolean): void;
}>();

const wrap = ref<HTMLDivElement | null>(null);
const canvas = ref<HTMLCanvasElement | null>(null);
let ctx: CanvasRenderingContext2D | null = null;

// 视图：scale = 屏幕像素 / 图像像素（1 = 原始分辨率）；tx/ty = 图像左上角在屏幕上的位置
const view = ref({ scale: 1, tx: 0, ty: 0 });
const size = ref({ w: 640, h: 480 });
const mouse = ref<{ x: number; y: number } | null>(null);

const imgW = ref(0);
const imgH = ref(0);
const loading = ref(false);
const errText = ref("");
const stale = ref(false);
const cachedAt = ref("");
let off: HTMLCanvasElement | null = null; // 已着色的图像（1:1 图像像素）
let rawBitmap: ImageBitmap | null = null; // 原始灰度位图：阈值变化时重新着色用（不要提前 close）

const res = computed(() => Number(props.meta?.resolution) || null);
const origin = computed<number[] | null>(() => {
  const o = props.meta?.origin;
  return o && o.length >= 2 ? [Number(o[0]), Number(o[1])] : null;
});
const metaH = computed(() => Number(props.meta?.height) || imgH.value || 0);
const worldReady = computed(() => !!(res.value && origin.value));
const cursor = computed<Point | null>(() => {
  const m = mouse.value;
  if (!m || !worldReady.value) return null;
  return pixelToMeters(m.x, m.y, res.value as number, origin.value, metaH.value);
});

// ---------------------------------------------------------------- 坐标换算
const toScreen = (x: number, y: number): Point => [
  view.value.tx + x * view.value.scale,
  view.value.ty + y * view.value.scale,
];
const fromScreen = (px: number, py: number): Point => [
  (px - view.value.tx) / view.value.scale,
  (py - view.value.ty) / view.value.scale,
];
/** 米 → 屏幕 */
function m2s(x: number, y: number): Point {
  const [px, py] = metersToPixel(x, y, res.value as number, origin.value, metaH.value);
  return toScreen(px, py);
}
/** 屏幕 → 米 */
function s2m(px: number, py: number): Point {
  const p = fromScreen(px, py);
  return pixelToMeters(p[0], p[1], res.value as number, origin.value, metaH.value);
}

// ---------------------------------------------------------------- 取图 / 着色
async function load() {
  const name = props.mapName;
  if (!name) return;
  loading.value = true;
  errText.value = "";
  try {
    // 用 fetch 而非 <img>：需要读 X-Map-Stale 头判断"离线缓存"
    const url = mapImageUrl(name) + `?t=${Date.now()}`;
    const r = await fetch(url, { headers: { Accept: "image/png" } });
    if (!r.ok) {
      let msg = `取图失败（HTTP ${r.status}）`;
      try {
        const j = await r.json();
        if (j?.error) msg = `${j.error}（HTTP ${r.status}）`;
      } catch {
        /* 非 JSON 错误体：保留默认文案 */
      }
      throw new Error(msg);
    }
    const isStale = r.headers.get("X-Map-Stale") === "1";
    stale.value = isStale;
    cachedAt.value = r.headers.get("X-Map-Cached-At") || "";
    emit("stale", isStale);
    const blob = await r.blob();
    // 保留原始灰度位图（不 close）：改 occupied_thresh/free_thresh 后要能**重新着色**，
    // 否则元数据改了画布颜色不变，用户会以为后端没保存成功。
    rawBitmap?.close?.();
    rawBitmap = await createImageBitmap(blob);
    imgW.value = rawBitmap.width;
    imgH.value = rawBitmap.height;
    colorize(rawBitmap);
    fitted = false;
    setTimeout(() => {
      // 图尺寸就绪后重新对中
      fit();
      fitted = true;
      draw();
    }, 0);
  } catch (e: any) {
    errText.value = e?.message || String(e);
    off = null;
    imgW.value = imgH.value = 0;
    draw();
  } finally {
    loading.value = false;
  }
}

/** 用 coords/colorize 的口径把灰度图着色成 1:1 的离屏画布。 */
function colorize(bitmap: ImageBitmap) {
  const c = document.createElement("canvas");
  c.width = bitmap.width;
  c.height = bitmap.height;
  const cx = c.getContext("2d");
  if (!cx) return;
  cx.drawImage(bitmap, 0, 0);
  const data = cx.getImageData(0, 0, c.width, c.height);
  const buf = data.data;
  const m = props.meta;
  const opts = {
    negate: Number(m?.negate ?? 0),
    occupied_thresh: Number(m?.occupied_thresh ?? 0.65),
    free_thresh: Number(m?.free_thresh ?? 0.25),
  };
  const colors = {
    occupied: colorFor("occupied"),
    free: colorFor("free"),
    unknown: colorFor("unknown"),
  };
  for (let i = 0; i < buf.length; i += 4) {
    const col = colors[classifyPixel(buf[i], 255, opts)];
    buf[i] = col[0];
    buf[i + 1] = col[1];
    buf[i + 2] = col[2];
    buf[i + 3] = col[3];
  }
  cx.putImageData(data, 0, 0);
  off = c;
  // 注意：这里**不** close 位图 —— 调用方持有 rawBitmap，阈值变化时要复用重新着色
}

// ---------------------------------------------------------------- 视图操作
function fit() {
  const w = size.value.w;
  const h = size.value.h;
  const iw = imgW.value || Number(props.meta?.width) || 0;
  const ih = imgH.value || Number(props.meta?.height) || 0;
  if (!iw || !ih || !w || !h) return;
  const s = Math.min(w / iw, h / ih) * 0.94;
  view.value = { scale: s, tx: (w - iw * s) / 2, ty: (h - ih * s) / 2 };
}

function zoomAt(cx: number, cy: number, k: number) {
  const v = view.value;
  const s = Math.min(40, Math.max(0.02, v.scale * k));
  const wx = (cx - v.tx) / v.scale;   // 光标处的图像像素坐标（保持不动）
  const wy = (cy - v.ty) / v.scale;
  view.value = { scale: s, tx: cx - wx * s, ty: cy - wy * s };
  draw();
}

/** 把某个米坐标居中显示（供父组件 focus 使用，父组件通过 ref 调用）。 */
function focusOn(x: number, y: number) {
  if (!worldReady.value) return;
  const v = view.value;
  const [px, py] = metersToPixel(x, y, res.value as number, origin.value, metaH.value);
  view.value = {
    scale: v.scale,
    tx: size.value.w / 2 - px * v.scale,
    ty: size.value.h / 2 - py * v.scale,
  };
  draw();
}

/** 复位视图（双击画布也走这里）。 */
function resetView() {
  fit();
  draw();
}

defineExpose({ focusOn, resetView, fit });

// ---------------------------------------------------------------- 绘制
let raf = 0;
function draw() {
  if (raf) return;
  raf = requestAnimationFrame(() => {
    raf = 0;
    render();
  });
}

function render() {
  const cv = canvas.value;
  if (!cv || !ctx) return;
  const { w, h } = size.value;
  if (cv.width !== w) cv.width = w;
  if (cv.height !== h) cv.height = h;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0b1220";
  ctx.fillRect(0, 0, w, h);

  const v = view.value;
  // 背景图（只画屏幕上可见的那一块）
  if (off && imgW.value && imgH.value) {
    const ix0 = Math.max(0, v.tx);
    const iy0 = Math.max(0, v.ty);
    const ix1 = Math.min(w, v.tx + imgW.value * v.scale);
    const iy1 = Math.min(h, v.ty + imgH.value * v.scale);
    if (ix1 > ix0 && iy1 > iy0) {
      const sx = (ix0 - v.tx) / v.scale;
      const sy = (iy0 - v.ty) / v.scale;
      const sw = (ix1 - ix0) / v.scale;
      const sh = (iy1 - iy0) / v.scale;
      ctx.imageSmoothingEnabled = v.scale < 1;
      ctx.drawImage(off, sx, sy, sw, sh, ix0, iy0, ix1 - ix0, iy1 - iy0);
      ctx.imageSmoothingEnabled = true;
    }
  } else if (!errText.value) {
    ctx.fillStyle = "#334155";
    ctx.font = "14px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(loading.value ? "正在加载地图…" : "请选择一张地图", w / 2, h / 2);
    ctx.textAlign = "start";
    ctx.textBaseline = "alphabetic";
  }

  if (worldReady.value) {
    drawGrid();
    drawOrigin();
    // 没有 resolution/origin 时米换算无意义，不画标记（避免给出错误位置）
    if (props.showZones) drawZones();
    if (props.showPlaces) drawPlaces();
    if (props.robotOnThisMap && props.pose?.status === "ok") drawRobot();
  }
  drawDraft();
  drawCursor();
}

function drawGrid() {
  if (!ctx) return;
  const { w, h } = size.value;
  const v = view.value;
  const step = pickStep(v.scale);
  const [wx0, wy0] = s2m(0, 0);
  const [wx1, wy1] = s2m(w, h);
  const x0 = Math.floor(Math.min(wx0, wx1) / step) * step;
  const x1 = Math.max(wx0, wx1);
  const y0 = Math.floor(Math.min(wy0, wy1) / step) * step;
  const y1 = Math.max(wy0, wy1);
  ctx.save();
  ctx.lineWidth = 1;
  let n = 0;
  for (let x = x0; x <= x1 && n < 4000; x += step, n++) {
    const p = m2s(x, 0);
    const major = Math.abs(Math.round(x / step) % 5) === 0;
    ctx.strokeStyle = major ? "rgba(96,165,250,.34)" : "rgba(96,165,250,.14)";
    ctx.beginPath();
    ctx.moveTo(Math.round(p[0]) + 0.5, 0);
    ctx.lineTo(Math.round(p[0]) + 0.5, h);
    ctx.stroke();
  }
  n = 0;
  for (let y = y0; y <= y1 && n < 4000; y += step, n++) {
    const p = m2s(0, y);
    const major = Math.abs(Math.round(y / step) % 5) === 0;
    ctx.strokeStyle = major ? "rgba(96,165,250,.34)" : "rgba(96,165,250,.14)";
    ctx.beginPath();
    ctx.moveTo(0, Math.round(p[1]) + 0.5);
    ctx.lineTo(w, Math.round(p[1]) + 0.5);
    ctx.stroke();
  }
  // 网格标注（每格或每 5 格标一次米数）
  ctx.fillStyle = "rgba(148,163,184,.85)";
  ctx.font = "11px ui-monospace, monospace";
  const labelEvery = step * v.scale >= 70 ? 1 : 5;
  n = 0;
  for (let x = x0; x <= x1 && n < 4000; x += step, n++) {
    if (Math.abs(Math.round(x / step) % labelEvery) !== 0) continue;
    const p = m2s(x, 0);
    if (p[0] < 8 || p[0] > w - 8) continue;
    ctx.fillText(`${round2(x)}m`, p[0] + 3, 12);
  }
  n = 0;
  for (let y = y0; y <= y1 && n < 4000; y += step, n++) {
    if (Math.abs(Math.round(y / step) % labelEvery) !== 0) continue;
    const p = m2s(0, y);
    if (p[1] < 10 || p[1] > h - 4) continue;
    ctx.fillText(`${round2(y)}m`, 4, p[1] - 3);
  }
  ctx.restore();
}

function pickStep(scale: number): number {
  const seq = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50];
  for (const s of seq) if (s * scale >= 44) return s;
  return seq[seq.length - 1];
}

function drawOrigin() {
  if (!ctx) return;
  const [ox, oy] = origin.value as number[];
  const p = m2s(ox, oy);
  ctx.save();
  ctx.strokeStyle = "rgba(248,113,113,.75)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(p[0] - 8, p[1]);
  ctx.lineTo(p[0] + 8, p[1]);
  ctx.moveTo(p[0], p[1] - 8);
  ctx.lineTo(p[0], p[1] + 8);
  ctx.stroke();
  ctx.fillStyle = "rgba(248,113,113,.9)";
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillText(`原点(${round2(ox)}, ${round2(oy)})`, p[0] + 10, p[1] + 14);
  ctx.restore();
}

function drawZones() {
  if (!ctx) return;
  ctx.save();
  for (const z of props.zones) {
    const poly = z.polygon || [];
    if (poly.length < 2) continue;
    const sel = z.uid === props.selectedUid;
    ctx.beginPath();
    poly.forEach((pt, i) => {
      const p = m2s(Number(pt[0]), Number(pt[1]));
      if (i === 0) ctx.moveTo(p[0], p[1]);
      else ctx.lineTo(p[0], p[1]);
    });
    ctx.closePath();
    ctx.fillStyle = sel ? "rgba(56,189,248,.34)" : "rgba(16,185,129,.20)";
    ctx.fill();
    ctx.strokeStyle = sel ? "#38bdf8" : "rgba(16,185,129,.85)";
    ctx.lineWidth = sel ? 2.5 : 1.5;
    ctx.stroke();
    ctx.fillStyle = sel ? "#e0f2fe" : "#a7f3d0";
    ctx.font = "12px system-ui, sans-serif";
    const c = centroid(poly);
    const p = m2s(c[0], c[1]);
    ctx.fillText(`${z.name || z.uid}${z.kind ? `·${z.kind}` : ""}`, p[0] + 4, p[1] - 4);
  }
  ctx.restore();
}

function drawPlaces() {
  if (!ctx) return;
  ctx.save();
  for (const d of props.places) {
    if (d.x === null || d.x === undefined || d.y === null || d.y === undefined) continue;
    const sel = d.uid === props.selectedUid;
    const p = m2s(Number(d.x), Number(d.y));
    ctx.beginPath();
    ctx.arc(p[0], p[1], sel ? 7 : 5, 0, Math.PI * 2);
    ctx.fillStyle = d.risk === "high" ? "#f97316" : "#fbbf24";
    ctx.fill();
    ctx.strokeStyle = sel ? "#fff" : "rgba(15,23,42,.9)";
    ctx.lineWidth = sel ? 2.5 : 1.5;
    ctx.stroke();
    // yaw 指示短线（yaw_deg 为 0/缺省 = 不限制朝向，不画箭头，免得被误读成"朝正 x"）
    if (d.yaw_deg !== null && d.yaw_deg !== undefined && Number(d.yaw_deg) !== 0) {
      const rad = (Number(d.yaw_deg) * Math.PI) / 180;
      ctx.beginPath();
      ctx.moveTo(p[0], p[1]);
      ctx.lineTo(p[0] + Math.cos(rad) * 16, p[1] - Math.sin(rad) * 16);
      ctx.strokeStyle = "#fbbf24";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.fillStyle = sel ? "#fef3c7" : "#fde68a";
    ctx.font = `${sel ? "bold " : ""}12px system-ui, sans-serif`;
    ctx.fillText(d.name || d.uid, p[0] + 8, p[1] - 7);
  }
  ctx.restore();
}

function drawRobot() {
  if (!ctx) return;
  const pose = props.pose;
  const x = Number(pose?.x);
  const y = Number(pose?.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  const p = m2s(x, y);
  const yaw = Number(pose?.yaw) || 0;
  ctx.save();
  ctx.translate(p[0], p[1]);
  ctx.rotate(-yaw); // 屏幕 y 轴向下 → 世界 yaw 逆时针为正，故取负
  ctx.beginPath();
  ctx.moveTo(0, -16);
  ctx.lineTo(11, 12);
  ctx.lineTo(0, 6);
  ctx.lineTo(-11, 12);
  ctx.closePath();
  ctx.fillStyle = pose?.suspect ? "#94a3b8" : "#22d3ee";
  ctx.fill();
  ctx.strokeStyle = "#0e7490";
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.restore();
  ctx.fillStyle = "#67e8f9";
  ctx.font = "12px ui-monospace, monospace";
  ctx.fillText(
    `车 (${round2(x)}, ${round2(y)}) ${Math.round((yaw * 180) / Math.PI)}°${pose?.suspect ? " ⚠" : ""}`,
    p[0] + 12,
    p[1] + 20,
  );
}

function drawDraft() {
  if (!ctx) return;
  if (drag.value && drag.value.moved) {
    const poly = rectPolygon(drag.value);
    strokePolygon(poly, "#f472b6", true);
  } else if (polyPts.value.length) {
    strokePolygon(polyPts.value, "#f472b6", true, false);
    for (const pt of polyPts.value) {
      const p = m2s(pt[0], pt[1]);
      ctx.fillStyle = "#f472b6";
      ctx.beginPath();
      ctx.arc(p[0], p[1], 4, 0, Math.PI * 2);
      ctx.fill();
    }
  }
}

function strokePolygon(poly: number[][], color: string, close: boolean, useCursor = true) {
  if (!ctx || poly.length < 1) return;
  ctx.save();
  ctx.beginPath();
  poly.forEach((pt, i) => {
    const p = m2s(pt[0], pt[1]);
    if (i === 0) ctx.moveTo(p[0], p[1]);
    else ctx.lineTo(p[0], p[1]);
  });
  if (useCursor && mouse.value) {
    const m = s2m(mouse.value.x, mouse.value.y);
    const p = m2s(m[0], m[1]);
    ctx.lineTo(p[0], p[1]);
  }
  if (close) ctx.closePath();
  ctx.fillStyle = "rgba(244,114,182,.18)";
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.setLineDash([6, 4]);
  ctx.stroke();
  ctx.restore();
}

function drawCursor() {
  if (!ctx || !mouse.value) return;
  const m = mouse.value;
  ctx.save();
  ctx.strokeStyle = "rgba(226,232,240,.45)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(m.x, 0);
  ctx.lineTo(m.x, size.value.h);
  ctx.moveTo(0, m.y);
  ctx.lineTo(size.value.w, m.y);
  ctx.stroke();
  ctx.restore();
}

function centroid(poly: number[][]): number[] {
  let sx = 0;
  let sy = 0;
  for (const p of poly) {
    sx += Number(p[0]);
    sy += Number(p[1]);
  }
  return [sx / poly.length, sy / poly.length];
}

const round2 = (n: number) => Math.round(Number(n) * 100) / 100;

// ---------------------------------------------------------------- 交互
const drag = ref<{ x0: number; y0: number; x1: number; y1: number; moved: boolean } | null>(null);
const polyPts = ref<number[][]>([]);
let lastClick = 0;

function pos(e: MouseEvent): Point {
  const r = (canvas.value as HTMLCanvasElement).getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

function onWheel(e: WheelEvent) {
  e.preventDefault();
  const [x, y] = pos(e);
  zoomAt(x, y, e.deltaY < 0 ? 1.15 : 1 / 1.15);
}

function onDown(e: MouseEvent) {
  const [x, y] = pos(e);
  if (props.drawMode === "rect" && worldReady.value) {
    drag.value = { x0: x, y0: y, x1: x, y1: y, moved: false };
    return;
  }
  drag.value = { x0: x, y0: y, x1: x, y1: y, moved: false };
}

function onMove(e: MouseEvent) {
  const [x, y] = pos(e);
  mouse.value = { x, y };
  if (drag.value) {
    const d = drag.value;
    if (Math.abs(x - d.x0) + Math.abs(y - d.y0) > 3) d.moved = true;
    d.x1 = x;
    d.y1 = y;
    if (props.drawMode !== "rect") {
      // 平移
      view.value = {
        scale: view.value.scale,
        tx: view.value.tx + (x - d.x0),
        ty: view.value.ty + (y - d.y0),
      };
      d.x0 = x;
      d.y0 = y;
    }
  } else if (polyPts.value.length) {
    draw();
  }
  draw();
}

function onUp() {
  const d = drag.value;
  drag.value = null;
  if (!d) return;
  if (props.drawMode === "rect" && d.moved) {
    const poly = rectPolygon(d);
    polyPts.value = [];
    emit("rect", poly);
    emit("mode-change", "idle");
    draw();
  }
}

function rectPolygon(d: { x0: number; y0: number; x1: number; y1: number }): number[][] {
  const a = s2m(d.x0, d.y0);
  const b = s2m(d.x1, d.y1);
  // 4 个角点（米坐标，顺序闭合）；y 方向已由 s2m 翻转，这里只需按矩形周长排列
  return [
    [a[0], a[1]],
    [b[0], a[1]],
    [b[0], b[1]],
    [a[0], b[1]],
  ];
}

function onLeave() {
  mouse.value = null;
  draw();
}

function onDblClick(e: MouseEvent) {
  e.preventDefault();
  if (props.drawMode === "polygon") {
    if (polyPts.value.length >= 3) {
      const poly = polyPts.value.slice();
      polyPts.value = [];
      emit("polygon", poly);
      emit("mode-change", "idle");
    } else {
      polyPts.value = [];
    }
    draw();
    return;
  }
  // 非绘制模式：双击复位视图
  resetView();
  lastClick = 0;
}

function onClick(e: MouseEvent) {
  const [x, y] = pos(e);
  const now = Date.now();
  if (now - lastClick < 320) {
    // 交给 dblclick 处理（多数浏览器 dblclick 在第二次 click 之后触发，这里避免重复落点）
    lastClick = 0;
    return;
  }
  lastClick = now;
  if (!worldReady.value) return;
  const m = s2m(x, y);
  if (props.drawMode === "point") {
    emit("point", { x: round4(m[0]), y: round4(m[1]) });
    return;
  }
  if (props.drawMode === "polygon") {
    polyPts.value = [...polyPts.value, [round4(m[0]), round4(m[1])]];
    emit("draft", polyPts.value.slice());
    draw();
    return;
  }
  // idle：命中测试选中标记（区域优先，其次地点）
  const hit = hitTest(x, y);
  if (hit) emit("select", hit);
}

function hitTest(sx: number, sy: number): string {
  if (!worldReady.value) return "";
  const m = s2m(sx, sy);
  for (const z of props.zones) {
    const poly = z.polygon || [];
    if (poly.length >= 3 && pointInPolygon(m[0], m[1], poly)) return z.uid;
  }
  let best = "";
  let bestD = 14 / view.value.scale; // 图像像素容差
  for (const d of props.places) {
    if (d.x === null || d.x === undefined || d.y === null || d.y === undefined) continue;
    const [px, py] = metersToPixel(
      Number(d.x),
      Number(d.y),
      res.value as number,
      origin.value,
      metaH.value,
    );
    const [mx, my] = fromScreen(sx, sy);
    const dist = Math.hypot(px - mx, py - my);
    if (dist < bestD) {
      bestD = dist;
      best = d.uid;
    }
  }
  return best;
}

const round4 = (n: number) => Math.round(Number(n) * 10000) / 10000;

// ---------------------------------------------------------------- 生命周期
let ro: ResizeObserver | null = null;

let fitted = false;

function measure() {
  const el = wrap.value;
  if (!el) return;
  const r = el.getBoundingClientRect();
  const w = Math.max(120, Math.floor(r.width));
  const h = Math.max(120, Math.floor(r.height));
  if (w === size.value.w && h === size.value.h) return;
  size.value = { w, h };
  const cv = canvas.value;
  if (cv) {
    cv.width = w;
    cv.height = h;
    cv.style.width = `${w}px`;
    cv.style.height = `${h}px`;
  }
  if (!fitted && imgW.value) {
    fitted = true;
    fit();
  }
  draw();
}

onMounted(() => {
  ctx = canvas.value?.getContext("2d") || null;
  measure();
  ro = new ResizeObserver(measure);
  if (wrap.value) ro.observe(wrap.value);
  load();
});

onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
});

watch(() => props.mapName, () => {
  polyPts.value = [];
  drag.value = null;
  off = null;
  rawBitmap?.close?.();
  rawBitmap = null;
  load();
});

watch(() => [props.meta?.resolution, props.meta?.origin, props.meta?.negate,
             props.meta?.occupied_thresh, props.meta?.free_thresh], () => {
  if (rawBitmap) colorize(rawBitmap);
  draw();
});
watch(() => [props.places, props.zones, props.pose, props.selectedUid, props.drawMode], () => draw(), {
  deep: true,
});
// 退出多边形绘制态时丢弃未完成的草稿：否则从下拉把 shape 切成 rect 后，
// 画布上还留着多边形旧顶点，继续点击仍在往多边形里加（用户会看不懂）。
watch(() => props.drawMode, (m) => {
  if (m !== "polygon" && polyPts.value.length) {
    polyPts.value = [];
    emit("draft", []);
    drag.value = null;
    draw();
  }
});
watch(
  () => [props.showPlaces, props.showZones],
  () => draw(),
);
</script>

<template>
  <div ref="wrap" class="canvas-wrap">
    <canvas
      ref="canvas"
      @wheel="onWheel"
      @mousedown="onDown"
      @mousemove="onMove"
      @mouseup="onUp"
      @mouseleave="onLeave"
      @click="onClick"
      @dblclick="onDblClick"
      :class="{ point: drawMode === 'point', cross: drawMode === 'polygon' || drawMode === 'rect' }"
    ></canvas>

    <div class="hud-tl">
      <div class="badge" v-if="stale">
        ⚠ 当前离线，显示缓存<span v-if="cachedAt"> · {{ cachedAt }}</span>
      </div>
      <div class="badge warn" v-if="!worldReady">该图 yaml 缺少 resolution/origin，无法做米换算</div>
      <div class="badge">
        视图 {{ view.scale.toFixed(3) }} px/像素
        <button @click="resetView">复位</button>
      </div>
    </div>

    <div class="hud-br">
      <div v-if="cursor && worldReady" class="coord">
        ({{ cursor[0].toFixed(3) }}, {{ cursor[1].toFixed(3) }}) m
      </div>
      <div v-else-if="mouse" class="coord">鼠标在画布外</div>
      <div class="hint">{{ MODE_HINTS[drawMode] }}</div>
    </div>

    <div class="err" v-if="errText">取图失败：{{ errText }}</div>
  </div>
</template>

<style scoped>
.canvas-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: #0b1220;
}
canvas {
  display: block;
  cursor: grab;
}
canvas.point {
  cursor: crosshair;
}
canvas.cross {
  cursor: cell;
}
.hud-tl {
  position: absolute;
  top: 8px;
  left: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
  pointer-events: none;
}
.badge {
  background: rgba(15, 23, 42, 0.86);
  border: 1px solid #1f2937;
  color: #cbd5e1;
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 6px;
  pointer-events: auto;
}
.badge.warn {
  border-color: #b45309;
  color: #fbbf24;
}
.badge button {
  margin-left: 8px;
  background: #1e293b;
  border: 1px solid #334155;
  color: #e2e8f0;
  border-radius: 4px;
  font-size: 11px;
  padding: 1px 6px;
  cursor: pointer;
}
.hud-br {
  position: absolute;
  right: 8px;
  bottom: 8px;
  text-align: right;
  pointer-events: none;
}
.coord {
  font: 13px ui-monospace, monospace;
  background: rgba(15, 23, 42, 0.86);
  border: 1px solid #1f2937;
  color: #7dd3fc;
  display: inline-block;
  padding: 3px 8px;
  border-radius: 6px;
}
.hint {
  margin-top: 6px;
  font-size: 12px;
  color: #94a3b8;
  text-shadow: 0 1px 2px #000;
}
.err {
  position: absolute;
  left: 8px;
  bottom: 8px;
  max-width: 60%;
  background: rgba(127, 29, 29, 0.9);
  border: 1px solid #b91c1c;
  color: #fee2e2;
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 6px;
}
</style>
