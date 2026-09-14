<script setup lang="ts">
// 地点面板：列表 + 新增/编辑表单 + 标点校验 + 记录当前位置
import { computed, ref, watch } from "vue";
import { deleteJson, enc, postJson } from "../lib/api";
import type { Destination, DestinationIn, ValidateResp } from "../lib/types";

const props = defineProps<{
  mapName: string;
  items: Destination[];
  selectedUid: string;
  /** 画布「标点」落下的米坐标 */
  draftPoint: { x: number; y: number } | null;
  /** 父组件请求进入标点模式（画布工具条上的「标点」按钮） */
  wantPlacing: boolean;
}>();

const emit = defineEmits<{
  (e: "select", uid: string): void;
  (e: "request-focus", p: { x: number; y: number }): void;
  (e: "placing", v: boolean): void;
  (e: "changed"): void;
}>();

interface Form {
  uid: string;
  name: string;
  aliases: string;
  x: string;
  y: string;
  yaw_deg: string;
  risk: string;
  elder_allowed: string;
  note: string;
}

function emptyForm(): Form {
  return {
    uid: "",
    name: "",
    aliases: "",
    x: "0",
    y: "0",
    yaw_deg: "",
    risk: "low",
    elder_allowed: "1",
    note: "",
  };
}

const list = computed(() => props.items);
const form = ref<Form>(emptyForm());
const validation = ref<ValidateResp | null>(null);
const msg = ref("");
const errMsg = ref("");

const isEdit = computed(() => !!form.value.uid);
// 标点模式由父组件统一持有（画布的工具条按钮也要能进入该模式）
const placing = computed(() => props.wantPlacing);

function aliasesText(d: Destination): string {
  if (Array.isArray(d.aliases_list)) return d.aliases_list.join(", ");
  if (Array.isArray(d.aliases)) return d.aliases.join(", ");
  return String(d.aliases || "");
}

function startNew() {
  form.value = emptyForm();
  validation.value = null;
  msg.value = "";
  errMsg.value = "";
}

function startEdit(d: Destination) {
  form.value = {
    uid: d.uid,
    name: d.name || "",
    aliases: aliasesText(d),
    x: d.x === null || d.x === undefined ? "" : String(d.x),
    y: d.y === null || d.y === undefined ? "" : String(d.y),
    // yaw_deg 的 0 一律当"未设置"：旧缓存表曾把缺字段读成 0，直接把 0 回填会显示"0°"并
    // 让画布画出一条朝正 x 的箭头，用户会以为朝向被限死了。
    yaw_deg: d.yaw_deg === null || d.yaw_deg === undefined || Number(d.yaw_deg) === 0
      ? "" : String(d.yaw_deg),
    risk: d.risk || "low",
    elder_allowed: String(d.elder_allowed ?? 1),
    note: d.note || "",
  };
  validation.value = null;
  msg.value = "";
  errMsg.value = "";
  emit("select", d.uid);
  if (d.x !== null && d.x !== undefined && d.y !== null && d.y !== undefined) {
    emit("request-focus", { x: Number(d.x), y: Number(d.y) });
  }
}

function aliasesArray(): string[] {
  return form.value.aliases
    .split(/[,，、]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function num(v: string): number | null {
  const t = String(v).trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

async function validate() {
  const x = num(form.value.x);
  const y = num(form.value.y);
  validation.value = null;
  errMsg.value = "";
  msg.value = "";
  if (x === null || y === null) {
    errMsg.value = "校验前请先填写 x / y（米）";
    return;
  }
  const r = await postJson<ValidateResp>("/api/destinations/validate", {
    map_name: props.mapName,
    x,
    y,
  });
  if (!r.ok) {
    errMsg.value = r.error || "校验失败";
    return;
  }
  validation.value = r.data;
}

function buildBody(): DestinationIn {
  return {
    map_name: props.mapName,
    name: form.value.name.trim(),
    aliases: aliasesArray(),
    x: num(form.value.x) ?? 0,
    y: num(form.value.y) ?? 0,
    yaw_deg: num(form.value.yaw_deg),
    risk: form.value.risk,
    elder_allowed: form.value.elder_allowed === "1" ? 1 : 0,
    note: form.value.note,
    learned_by: "editor",
  };
}

async function save() {
  msg.value = "";
  errMsg.value = "";
  if (!form.value.name.trim()) {
    errMsg.value = "地点必须有名字";
    return;
  }
  if (num(form.value.x) === null || num(form.value.y) === null) {
    errMsg.value = "请填写合法的 x / y（米）";
    return;
  }
  // 保存前顺带校验一次（警告不阻止保存，但要在界面上留痕）
  await validate();
  const body = buildBody();
  const url = form.value.uid
    ? `/api/destinations/${enc(form.value.uid)}`
    : "/api/destinations";
  const r = await postJson<any>(url, body);
  if (!r.ok) {
    errMsg.value = r.error || "保存失败";
    return;
  }
  const warns: string[] = r.data.warnings || [];
  msg.value = `${isEdit.value ? "已更新" : "已新增"} ${form.value.name}（uid=${r.data.uid}）${
    warns.length ? `｜后端提示：${warns.join("；")}` : ""
  }`;
  if (!form.value.uid) form.value.uid = String(r.data.uid || "");
  emit("changed");
  emit("select", String(r.data.uid || ""));
}

async function remove(d: Destination) {
  if (!window.confirm(`确认删除地点「${d.name || d.uid}」？`)) return;
  const r = await deleteJson<any>(
    `/api/destinations/${enc(d.uid)}?map=${enc(props.mapName)}`,
  );
  if (!r.ok) {
    errMsg.value = r.error || "删除失败";
    return;
  }
  msg.value = `已删除 ${d.name || d.uid}`;
  if (form.value.uid === d.uid) startNew();
  emit("changed");
}

async function learn() {
  msg.value = "";
  errMsg.value = "";
  if (!form.value.name.trim()) {
    errMsg.value = "「记录当前位置」需要先填地点名字";
    return;
  }
  const r = await postJson<any>("/api/destinations/learn", {
    map_name: props.mapName,
    name: form.value.name.trim(),
    aliases: aliasesArray(),
    risk: form.value.risk,
    elder_allowed: form.value.elder_allowed === "1" ? 1 : 0,
    note: form.value.note,
  });
  if (!r.ok) {
    // 后端 error 原样显示 + 给出替代路径
    errMsg.value = `${r.error || "记录当前位置失败"}　→ 可改为在图上点选：点「标点」后在画布上单击`;
    return;
  }
  const pose = r.data.pose || {};
  msg.value = `已按当前位姿记录 ${form.value.name}（uid=${r.data.uid}，x=${pose.x}，y=${pose.y}）`;
  form.value.uid = String(r.data.uid || "");
  emit("changed");
}

function togglePlacing() {
  if (placing.value) {
    emit("placing", false);
    return;
  }
  msg.value = "标点模式：在画布上单击即可填 x/y";
  emit("select", "");
  emit("placing", true);
}

watch(() => props.mapName, () => {
  startNew();
});

watch(
  () => props.draftPoint,
  (p) => {
    if (!p) return;
    form.value.x = String(p.x);
    form.value.y = String(p.y);
    emit("placing", false);
    void validate();
  },
);

watch(
  () => props.selectedUid,
  (uid) => {
    if (!uid || uid === form.value.uid) return;
    const d = list.value.find((x) => x.uid === uid);
    if (d) startEdit(d);
  },
);

</script>

<template>
  <div class="panel">
    <div class="head">
      <strong>地点</strong>
      <span class="count">{{ list.length }}</span>
      <span class="spacer"></span>
      <button class="mini" @click="emit('changed')">刷新</button>
    </div>

    <div class="hint err" v-if="errMsg">{{ errMsg }}</div>
    <div class="hint ok" v-if="msg">{{ msg }}</div>

    <div class="list">
      <div
        v-for="d in list"
        :key="d.uid"
        class="row"
        :class="{ active: d.uid === form.uid, sel: d.uid === selectedUid }"
        @click="startEdit(d)"
      >
        <div class="row-main">
          <span class="risk" :class="d.risk === 'high' ? 'high' : 'low'">{{ d.risk || "low" }}</span>
          <span class="nm">{{ d.name || d.uid }}</span>
          <span class="uid">{{ d.uid }}</span>
        </div>
        <div class="row-sub">
          ({{ d.x ?? "-" }}, {{ d.y ?? "-" }}) m
          <span v-if="d.yaw_deg !== null && d.yaw_deg !== undefined && Number(d.yaw_deg) !== 0">
            · {{ d.yaw_deg }}°</span>
          <span v-if="aliasesText(d)"> · {{ aliasesText(d) }}</span>
          <span v-if="d.elder_allowed === 0" class="warn"> · 老人不可去</span>
        </div>
        <div class="row-act">
          <button class="mini" @click.stop="emit('request-focus', { x: Number(d.x), y: Number(d.y) })"
                  :disabled="d.x === null || d.x === undefined">定位</button>
          <button class="mini danger" @click.stop="remove(d)">删除</button>
        </div>
      </div>
      <div class="empty" v-if="!list.length">该图暂无地点</div>
    </div>

    <div class="form">
      <div class="form-head">
        <strong>{{ isEdit ? `编辑 ${form.uid}` : "新增地点" }}</strong>
        <span class="spacer"></span>
        <button class="mini" @click="startNew">清空</button>
        <button class="mini" :class="{ on: placing }" @click="togglePlacing">
          {{ placing ? "标点中…点击画布" : "标点" }}
        </button>
      </div>

      <label>名字
        <input v-model="form.name" placeholder="如：护士办公室" />
      </label>
      <label>别名（逗号分隔）
        <input v-model="form.aliases" placeholder="如：护士站, 服务台" />
      </label>
      <div class="grid2">
        <label>x（米）
          <input v-model="form.x" type="number" step="0.001" />
        </label>
        <label>y（米）
          <input v-model="form.y" type="number" step="0.001" />
        </label>
      </div>
      <div class="grid2">
        <label>yaw_deg（可空）
          <input v-model="form.yaw_deg" type="number" step="1" placeholder="留空=不限制朝向" />
        </label>
        <label>risk
          <select v-model="form.risk">
            <option value="low">low</option>
            <option value="high">high</option>
          </select>
        </label>
      </div>
      <label>老人可去（elder_allowed）
        <select v-model="form.elder_allowed">
          <option value="1">1（允许）</option>
          <option value="0">0（禁止）</option>
        </select>
      </label>
      <label>note
        <input v-model="form.note" placeholder="备注" />
      </label>

      <div class="block" v-if="validation">
        <div class="v-title">
          校验结果
          <span class="tag" :class="validation.in_bounds ? 'ok' : 'bad'">
            {{ validation.in_bounds ? "在范围内" : "越界" }}
          </span>
          <span class="tag" v-if="validation.on_obstacle">障碍上</span>
          <span class="tag" v-if="validation.on_unknown">未知区</span>
        </div>
        <div class="v-line">
          像素类型：{{ validation.pixel_kind || "—" }} ·
          余量：{{ validation.clearance_m ?? "∞" }} m ·
          距边界：{{ validation.edge_margin_m ?? "—" }} m
        </div>
        <ul class="reasons" v-if="validation.reasons && validation.reasons.length">
          <li v-for="(rs, i) in validation.reasons" :key="i">{{ rs }}</li>
        </ul>
        <div class="gray small">仅警告，不阻止保存。</div>
      </div>

      <div class="actions">
        <button class="primary" @click="save">{{ isEdit ? "保存修改" : "新增地点" }}</button>
        <button @click="validate">校验坐标</button>
        <button @click="learn">记录当前位置</button>
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
.risk { font-size: 10px; padding: 1px 5px; border-radius: 4px; }
.risk.low { background: #14532d; color: #bbf7d0; }
.risk.high { background: #7c2d12; color: #fed7aa; }
.row-sub { font-size: 11px; color: #94a3b8; margin-top: 3px; }
.row-sub .warn { color: #fbbf24; }
.row-act { margin-top: 5px; display: flex; gap: 6px; }
.empty { color: #64748b; font-size: 12px; text-align: center; padding: 18px 0; }
/* 表单区：不用 max-height 百分比（窗口不高时会把主按钮顶出可视区，且滚动条会被父级裁掉） */
.form { border-top: 1px solid #1f2937; padding-top: 8px; display: flex;
  flex-direction: column; gap: 6px; flex: 0 1 auto; min-height: 0; overflow-y: auto; }
.form-head { display: flex; align-items: center; gap: 6px; }
label { display: flex; flex-direction: column; gap: 3px; font-size: 12px; color: #94a3b8; }
input, select { background: #0b1220; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 5px 7px; font-size: 13px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
button { background: #1e293b; border: 1px solid #334155; color: #e2e8f0;
  border-radius: 6px; padding: 6px 10px; font-size: 12px; cursor: pointer; }
button:hover { background: #273449; }
button:disabled { opacity: .5; cursor: default; }
button.primary { background: #1d4ed8; border-color: #2563eb; }
button.danger { border-color: #7f1d1d; color: #fca5a5; }
button.mini { padding: 3px 7px; font-size: 11px; }
button.on { background: #7c2d12; border-color: #c2410c; color: #fed7aa; }
/* 主按钮常驻表单底部，表单再长也能看到"新增地点 / 保存修改" */
.actions { display: flex; gap: 6px; flex-wrap: wrap; position: sticky; bottom: 0;
  background: #0f172a; padding: 6px 0 2px; border-top: 1px solid #1f2937; }
.block { background: #0b1220; border: 1px solid #1f2937; border-radius: 8px; padding: 8px; }
.v-title { font-size: 12px; color: #cbd5e1; margin-bottom: 4px; }
.tag { font-size: 10px; padding: 1px 5px; border-radius: 4px; margin-left: 4px;
  background: #7f1d1d; color: #fecaca; }
.tag.ok { background: #14532d; color: #bbf7d0; }
.tag.bad { background: #7f1d1d; color: #fecaca; }
.v-line { font-size: 11px; color: #94a3b8; }
.reasons { margin: 6px 0 4px; padding-left: 18px; color: #f87171; font-size: 12px; }
.small { font-size: 11px; }
.gray { color: #94a3b8; }
</style>
