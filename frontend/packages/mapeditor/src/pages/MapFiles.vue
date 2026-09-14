<script setup lang="ts">
// 地图文件面板：列表 / 改名 / 复制 / 删除 / 下载 / 设为目标地图 / 改元数据 / tags 查看与重建 / 像素修图入口
import { computed, ref, watch } from "vue";
import {
  deleteJson,
  enc,
  getJson,
  mapDownloadUrl,
  mapTagsUrl,
  navCommand,
  pixelEditorUrl,
  postJson,
} from "../lib/api";
import type { MapListResp, MapMetaFields, MetaResp } from "../lib/types";

const props = defineProps<{
  selectedMap: string;
  maps: MapListResp | null;
}>();

const emit = defineEmits<{
  (e: "pick", name: string): void;
  (e: "changed"): void;
}>();

const note = ref("");
const errMsg = ref("");
const busy = ref(false);
const newName = ref("");
const tagsOpen = ref(false);
const tagsJson = ref("");
const copied = ref("");

// 元数据编辑表单
const meta = ref({
  resolution: "",
  ox: "",
  oy: "",
  negate: "0",
  occupied_thresh: "",
  free_thresh: "",
});
const metaMap = ref("");
const metaNote = ref("");
const metaErr = ref("");

// 假位姿注入（无 ROS 时测试「车上图」）
const injectOpen = ref(false);
const injectText = ref('{"x":0,"y":0,"yaw":0}');
const injectMsg = ref("");

const rows = computed(() => props.maps?.maps || []);

function fmt(v: unknown, digits = 3): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toFixed(digits);
}

function originText(o: unknown): string {
  const a = Array.isArray(o) ? o : [];
  if (!a.length) return "—";
  return a.map((v) => fmt(v, 3)).join(", ");
}

async function copy(text: string, tag: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    copied.value = `${tag} 已复制：${text}`;
    setTimeout(() => {
      if (copied.value.startsWith(`${tag} 已复制`)) copied.value = "";
    }, 4000);
  } catch (e: any) {
    errMsg.value = `复制失败：${e?.message || e}（请手动选中复制）`;
  }
}

function needConfirm(data: any): boolean {
  return !!data?.need_confirm;
}

function affectsText(a: any): string {
  const d = Number(a?.destinations ?? 0);
  const z = Number(a?.zones ?? 0);
  return `${d} 个地点 / ${z} 个区域`;
}

// ---------------------------------------------------------------- 元数据
function loadMetaForm(name: string, info?: Partial<MapMetaFields> | null) {
  metaMap.value = name;
  const src = (info || {}) as Partial<MapMetaFields>;
  meta.value = {
    resolution: src.resolution === null || src.resolution === undefined ? "" : String(src.resolution),
    ox: Array.isArray(src.origin) && src.origin.length ? String(src.origin[0]) : "",
    oy: Array.isArray(src.origin) && src.origin.length > 1 ? String(src.origin[1]) : "",
    negate: String(src.negate ?? 0),
    occupied_thresh:
      src.occupied_thresh === null || src.occupied_thresh === undefined
        ? ""
        : String(src.occupied_thresh),
    free_thresh:
      src.free_thresh === null || src.free_thresh === undefined ? "" : String(src.free_thresh),
  };
  metaNote.value = "";
  metaErr.value = "";
}

async function editMeta(name: string) {
  const r = await getJson<MetaResp>(`/api/map/${enc(name)}/meta`);
  if (!r.ok) {
    metaErr.value = r.error || "读取元数据失败";
    metaMap.value = name;
    return;
  }
  loadMetaForm(name, r.data.meta);
  if (r.data.meta?.problems?.length) {
    metaNote.value = `该图 yaml 有问题：${r.data.meta.problems.join("；")}`;
  }
}

async function saveMeta(confirm = false) {
  const name = metaMap.value;
  if (!name) return;
  metaErr.value = "";
  metaNote.value = "";
  const body: Record<string, unknown> = { confirm };
  if (meta.value.resolution !== "") body.resolution = Number(meta.value.resolution);
  if (meta.value.ox !== "" || meta.value.oy !== "") {
    body.origin = [Number(meta.value.ox || 0), Number(meta.value.oy || 0)];
  }
  body.negate = Number(meta.value.negate || 0);
  if (meta.value.occupied_thresh !== "") body.occupied_thresh = Number(meta.value.occupied_thresh);
  if (meta.value.free_thresh !== "") body.free_thresh = Number(meta.value.free_thresh);

  busy.value = true;
  const r = await postJson<any>(`/api/map/${enc(name)}/meta`, body);
  busy.value = false;
  if (!r.ok) {
    if (needConfirm(r.data)) {
      // 409：改元数据会让已标坐标含义改变 → 二次确认
      metaNote.value = `本图有 ${affectsText(r.data.affects)}，保存后坐标含义将改变。确认继续？`;
      const go = window.confirm(
        `本图有 ${affectsText(r.data.affects)}。\n改 resolution/origin 会让它们的坐标含义改变，确认继续？`,
      );
      if (go) await saveMeta(true);
      return;
    }
    metaErr.value = r.error || "保存元数据失败";
    return;
  }
  const changes: string[] = r.data.changed || [];
  metaNote.value = `已保存：${changes.join("；")}${
    r.data.backup?.length ? `（备份 ${r.data.backup.length} 份）` : ""
  }`;
  emit("changed");
}

// ---------------------------------------------------------------- 文件操作
async function rename() {
  const name = props.selectedMap;
  const nv = newName.value.trim();
  if (!name || !nv) {
    errMsg.value = "请先填写新名字";
    return;
  }
  busy.value = true;
  const r = await postJson<any>(`/api/map/${enc(name)}/rename`, { new_name: nv });
  busy.value = false;
  if (!r.ok) {
    errMsg.value = r.error || "改名失败";
    return;
  }
  note.value = `已改名：${r.data.old} → ${r.data.new}`;
  newName.value = "";
  emit("pick", String(r.data.new));
  emit("changed");
}

async function copyMap() {
  const name = props.selectedMap;
  const nv = newName.value.trim();
  if (!name || !nv) {
    errMsg.value = "请先填写新名字";
    return;
  }
  busy.value = true;
  const r = await postJson<any>(`/api/map/${enc(name)}/copy`, { new_name: nv });
  busy.value = false;
  if (!r.ok) {
    errMsg.value = r.error || "复制失败";
    return;
  }
  const warns: string[] = r.data.warnings || [];
  note.value = `已复制到 ${r.data.dst}${r.data.tags_copied ? "（标记随行）" : "（标记未随行）"}${
    warns.length ? `｜${warns.join("；")}` : ""
  }`;
  newName.value = "";
  emit("changed");
}

async function removeMap(name: string, confirm = false) {
  busy.value = true;
  const r = await deleteJson<any>(`/api/map/${enc(name)}?confirm=${confirm ? "true" : "false"}`);
  busy.value = false;
  if (!r.ok) {
    if (needConfirm(r.data)) {
      const go = window.confirm(
        `本图有 ${affectsText(r.data.affects)}，删除会一并移除。确认删除「${name}」？`,
      );
      if (go) await removeMap(name, true);
      return;
    }
    errMsg.value = r.error || "删除失败";
    return;
  }
  note.value = `已删除 ${name}（移除 ${(r.data.removed || []).join("/")}）`;
  emit("changed");
}

async function setCurrent(name: string) {
  busy.value = true;
  // 两种体后端都接受，这里按规格首选嵌套写法
  const r = await postJson<any>("/api/settings", { settings: { current_map: name } });
  busy.value = false;
  if (!r.ok) {
    errMsg.value = r.error || "设置当前地图失败";
    return;
  }
  note.value = `已把「${name}」设为目标地图。真要切图请执行：${navCommand(name)}`;
  emit("changed");
}

async function reindex(name: string) {
  busy.value = true;
  const r = await postJson<any>(`/api/map/${enc(name)}/tags/reindex`);
  busy.value = false;
  if (!r.ok) {
    errMsg.value = r.error || "重建索引缓存失败";
    return;
  }
  note.value = `已重建索引：地点 ${r.data.destinations ?? "?"} 个 / 区域 ${r.data.zones ?? "?"} 个`;
  emit("changed");
}

async function viewTags(name: string) {
  tagsJson.value = "加载中…";
  tagsOpen.value = true;
  const r = await getJson<any>(mapTagsUrl(name));
  if (!r.ok) {
    tagsJson.value = `读取失败：${r.error}`;
    return;
  }
  const j = r.data;
  tagsJson.value = JSON.stringify(
    {
      path: j.path,
      exists: j.exists,
      stale: j.stale,
      cached_at: j.cached_at,
      warnings: j.warnings,
      fingerprint: j.fingerprint,
      tags: j.tags,
    },
    null,
    2,
  );
}

// ---------------------------------------------------------------- 假位姿注入
async function injectPose(clear = false) {
  injectMsg.value = "";
  let body: any = {};
  if (!clear) {
    const t = injectText.value.trim();
    if (t) {
      try {
        body = JSON.parse(t);
      } catch (e: any) {
        injectMsg.value = `JSON 解析失败：${e?.message || e}`;
        return;
      }
    }
  }
  const r = await postJson<any>("/api/mapeditor/pose/inject", body);
  if (!r.ok) {
    injectMsg.value = r.error || "注入失败";
    return;
  }
  injectMsg.value = r.data.cleared
    ? "已清除注入的假位姿/假地图"
    : `已注入：pose=${JSON.stringify(r.data.pose)} current_map=${r.data.current_map?.name ?? "unknown"}`;
  emit("changed");
}

function openPixelEditor(name: string) {
  if (!name) {
    errMsg.value = "请先选择一张地图";
    return;
  }
  window.open(pixelEditorUrl(name), "_blank");
}

watch(
  () => props.selectedMap,
  (n) => {
    errMsg.value = "";
    note.value = "";
    if (n) void editMeta(n);
  },
  { immediate: true },
);
</script>

<template>
  <div class="panel">
    <div class="head">
      <strong>地图文件</strong>
      <span class="spacer"></span>
      <span class="io" :class="props.maps?.mode === 'ssh' ? 'ssh' : 'local'">
        IO: {{ props.maps?.mode || "?" }}
      </span>
      <span class="root" :title="props.maps?.root || ''">{{ props.maps?.root || "" }}</span>
    </div>

    <div class="hint gray" v-if="props.maps?.status === 'unavailable'">
      {{ props.maps.reason || "地图存储当前不可用" }}
    </div>
    <div class="hint err" v-if="errMsg">{{ errMsg }}</div>
    <div class="hint ok" v-if="note">{{ note }}</div>
    <div class="hint ok" v-if="copied">{{ copied }}</div>

    <div class="list">
      <div
        v-for="m in rows"
        :key="m.name"
        class="row"
        :class="{ active: m.name === selectedMap, current: m.current }"
        @click="emit('pick', m.name)"
      >
        <div class="row-main">
          <span class="nm">{{ m.name }}</span>
          <span class="tag cur" v-if="m.current">当前</span>
          <span class="tag bad" v-if="m.status && m.status !== 'ok'">{{ m.status }}</span>
          <span class="tag" v-if="m.tags_exists">tags ✓</span>
          <span class="tag bad" v-else-if="m.has_yaml">无 tags</span>
        </div>
        <div class="row-sub">
          {{ m.width ?? "?" }}×{{ m.height ?? "?" }} px ·
          res {{ fmt(m.resolution, 3) }} m/px ·
          origin [{{ originText(m.origin) }}] ·
          未知 {{ m.unknown_ratio === null || m.unknown_ratio === undefined ? "—" : `${(Number(m.unknown_ratio) * 100).toFixed(1)}%` }}
        </div>
        <div class="row-sub">
          地点 {{ m.counts?.destinations ?? 0 }} · 区域 {{ m.counts?.zones ?? 0 }}
          <span v-if="m.problems?.length" class="bad"> · {{ m.problems.join("；") }}</span>
        </div>
        <div class="row-act" @click.stop>
          <button class="mini" @click="copy(navCommand(m.name), '换图命令')">复制换图命令</button>
          <button class="mini" @click="setCurrent(m.name)">设为目标地图</button>
          <a class="mini link" :href="mapDownloadUrl(m.name, 'yaml')">yaml</a>
          <a class="mini link" :href="mapDownloadUrl(m.name, 'pgm')">pgm</a>
          <a class="mini link" :href="mapDownloadUrl(m.name, 'tags')">tags</a>
          <button class="mini" @click="viewTags(m.name)">查看 tags</button>
          <button class="mini" @click="reindex(m.name)">重建索引缓存</button>
          <button class="mini" @click="openPixelEditor(m.name)">像素修图</button>
          <button class="mini danger" @click="removeMap(m.name)">删除</button>
        </div>
      </div>
      <div class="empty" v-if="!rows.length">没有可用地图</div>
    </div>

    <div class="form">
      <div class="form-head">
        <strong>选中：{{ selectedMap || "（未选）" }}</strong>
      </div>

      <div class="line">
        <input v-model="newName" placeholder="新地图名" />
        <button @click="rename" :disabled="busy">改名</button>
        <button @click="copyMap" :disabled="busy">复制</button>
        <button class="danger" @click="removeMap(selectedMap)" :disabled="busy || !selectedMap">
          删除
        </button>
      </div>

      <div class="line">
        <code class="cmd">{{ navCommand(selectedMap || "<地图名>") }}</code>
        <button class="mini" @click="copy(navCommand(selectedMap), '换图命令')">一键复制</button>
      </div>
      <div class="gray small">改完地图/换图必须重启导航才生效。</div>

      <div class="meta">
        <div class="meta-head">
          <strong>元数据</strong>
          <span class="gray small">（{{ metaMap || "—" }}）</span>
        </div>
        <div class="grid2">
          <label>resolution（m/px）
            <input v-model="meta.resolution" type="number" step="0.001" />
          </label>
          <label>negate
            <select v-model="meta.negate">
              <option value="0">0</option>
              <option value="1">1</option>
            </select>
          </label>
          <label>origin_x
            <input v-model="meta.ox" type="number" step="0.001" />
          </label>
          <label>origin_y
            <input v-model="meta.oy" type="number" step="0.001" />
          </label>
          <label>occupied_thresh
            <input v-model="meta.occupied_thresh" type="number" step="0.01" />
          </label>
          <label>free_thresh
            <input v-model="meta.free_thresh" type="number" step="0.01" />
          </label>
        </div>
        <div class="hint gray" v-if="metaNote">{{ metaNote }}</div>
        <div class="hint err" v-if="metaErr">{{ metaErr }}</div>
        <div class="line actions">
          <button class="primary" @click="saveMeta(false)" :disabled="busy || !metaMap">
            保存元数据
          </button>
          <button @click="editMeta(selectedMap)" :disabled="!selectedMap">重新读取</button>
          <button @click="openPixelEditor(selectedMap)" :disabled="!selectedMap">像素修图</button>
        </div>
        <div class="gray small">
          改 resolution/origin 会让本图已有地点的坐标含义改变；后端会先返回需要确认的数量。
        </div>
      </div>

      <div class="inject">
        <div class="line">
          <strong>开发用：注入假位姿</strong>
          <button class="mini" @click="injectOpen = !injectOpen">
            {{ injectOpen ? "收起" : "展开" }}
          </button>
        </div>
        <template v-if="injectOpen">
          <textarea v-model="injectText" rows="3" placeholder='{"x":0,"y":0,"yaw":0,"width":100,"height":100,"resolution":0.05,"origin":[-2.5,-2.5,0]}'></textarea>
          <div class="line">
            <button @click="injectPose(false)">注入</button>
            <button @click="injectPose(true)">清除（{}）</button>
          </div>
          <div class="hint gray" v-if="injectMsg">{{ injectMsg }}</div>
          <div class="gray small">
            无 ROS 时用来测试「车此刻在跑哪张图」。只给 x 只注入位姿；给 width 才会注入假地图元数据；
            空对象 <code>{}</code> 清除。
          </div>
        </template>
      </div>
    </div>

    <div class="modal" v-if="tagsOpen" @click.self="tagsOpen = false">
      <div class="modal-box">
        <div class="modal-head">
          <strong>tags.json 原文</strong>
          <span class="spacer"></span>
          <button class="mini" @click="tagsOpen = false">关闭</button>
        </div>
        <pre>{{ tagsJson }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; gap: 8px; height: 100%; min-height: 0; position: relative; }
.head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.spacer { flex: 1; }
.io { font-size: 11px; padding: 2px 6px; border-radius: 4px; background: #1e293b; color: #cbd5e1; }
.io.ssh { background: #78350f; color: #fde68a; }
.root { font-size: 11px; color: #64748b; max-width: 46%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.hint { font-size: 12px; padding: 6px 8px; border-radius: 6px; white-space: pre-wrap; }
.hint.gray { color: #94a3b8; background: #111827; border: 1px solid #1f2937; }
.hint.err { color: #fecaca; background: #450a0a; border: 1px solid #b91c1c; }
.hint.ok { color: #bbf7d0; background: #052e16; border: 1px solid #15803d; }
.list { flex: 1 1 auto; overflow-y: auto; min-height: 96px; border: 1px solid #1f2937;
  border-radius: 8px; background: #0b1220; }
.row { padding: 8px 10px; border-bottom: 1px solid #111827; cursor: pointer; }
.row:hover { background: #111827; }
.row.active { background: #1e3a5f; }
.row.current { box-shadow: inset 3px 0 0 #22d3ee; }
.row-main { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.nm { font-size: 14px; color: #e2e8f0; }
.row-sub { font-size: 11px; color: #94a3b8; margin-top: 3px; }
.row-sub .bad { color: #f87171; }
.row-act { margin-top: 6px; display: flex; gap: 5px; flex-wrap: wrap; }
.tag { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: #1e293b; color: #cbd5e1; }
.tag.cur { background: #164e63; color: #a5f3fc; }
.tag.bad { background: #7f1d1d; color: #fecaca; }
.empty { color: #64748b; font-size: 12px; text-align: center; padding: 18px 0; }
/* 同 PlacePanel/ZonePanel：不用 max-height 百分比，否则窗口不高时底部按钮会被顶出可视区 */
.form { border-top: 1px solid #1f2937; padding-top: 8px; display: flex;
  flex-direction: column; gap: 6px; flex: 0 1 auto; min-height: 0; overflow-y: auto; }
.form-head { display: flex; align-items: center; gap: 6px; }
label { display: flex; flex-direction: column; gap: 3px; font-size: 12px; color: #94a3b8; }
input, select, textarea { background: #0b1220; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 5px 7px; font-size: 13px; font-family: inherit; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.line { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
/* 元数据保存那一行常驻表单底部（`.line` 在多处复用，所以只给带 .actions 的那一行加） */
.line.actions { position: sticky; bottom: 0; background: #0f172a; padding: 6px 0 2px;
  border-top: 1px solid #1f2937; }
.line input { flex: 1; min-width: 100px; }
button { background: #1e293b; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
button:hover { background: #273449; }
button:disabled { opacity: .5; cursor: default; }
button.primary { background: #1d4ed8; border-color: #2563eb; }
button.danger { border-color: #7f1d1d; color: #fca5a5; }
button.mini { padding: 3px 7px; font-size: 11px; }
a.mini { padding: 3px 7px; font-size: 11px; border: 1px solid #334155; border-radius: 6px;
  color: #e2e8f0; text-decoration: none; background: #1e293b; }
.meta, .inject { border: 1px solid #1f2937; border-radius: 8px; padding: 8px;
  display: flex; flex-direction: column; gap: 6px; background: #0b1220; }
.meta-head { display: flex; align-items: center; gap: 6px; }
.cmd { background: #0b1220; border: 1px solid #334155; border-radius: 6px;
  padding: 4px 8px; font-size: 12px; color: #7dd3fc; }
.small { font-size: 11px; }
.gray { color: #94a3b8; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.6); display: flex;
  align-items: center; justify-content: center; z-index: 200; }
.modal-box { background: #0f172a; border: 1px solid #334155; border-radius: 10px;
  width: min(760px, 90vw); max-height: 82vh; display: flex; flex-direction: column; }
.modal-head { display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  border-bottom: 1px solid #1f2937; }
.modal-box pre { margin: 0; padding: 12px; overflow: auto; font-size: 12px; color: #cbd5e1; }
</style>
