<script setup lang="ts">
// 病房管理（admin 页签）：病房 = 一个"病房用户"（uid 形如 ward_101）。
//
// 几何真相：区域多边形的唯一真相在地图文件夹的 `<图名>.tags.json`
// （规格 docs/superpowers/specs/2026-09-14-map-editor-design.md §4），本页只负责：
//   1) 建病房档案；2) 把病房**关联**到已画好的区域（地图名 + 区域 uid）；
//   3) 便捷入口「记录当前房间为病房区域」= 以小车当前位姿为圆心采样 16 边形写进该图 tags.json；
//   4) 老人归入/移出病房（写 profiles.ward_id）。
// 精确形状（多边形/矩形）请到本壳的「地图编辑器」页签里画（类型选「ward 病区」）。
import { onMounted, ref } from "vue";
import {
  assignElderWard, deleteWard, listWards, recordWardZone, upsertWard, type Ward,
} from "shared";

// 跳到同壳的「地图编辑器」页签（admin 没有路由表，只有 App.vue 的 active ref）。
// 不能再用硬链 `<a href>` 指向编辑器：主后端已不再挂载该路径，硬链会 404。
const emit = defineEmits<{ (e: "goto-mapeditor"): void }>();

interface Profile { uid: string; name?: string; nickname?: string; bed?: string; ward_id?: string }

const wards = ref<Ward[]>([]);
const elders = ref<Profile[]>([]);
const newUid = ref("");
const newName = ref("");
const bindMap = ref("");
const bindZone = ref("");
const msg = ref("");
const err = ref("");
const busy = ref(false);
const loaded = ref(false);

/** shared 的 client 在非 2xx 时只抛 `API <status>: <url>`，会丢掉响应体的 `error`/`detail`
 *  （该文件不在本次改动范围）。按 kiosk 任务已有的做法：失败时对同一 URL 重发一次把响应体
 *  取回来（只发生在已失败的写请求上，此时必为 4xx，无副作用），好把后端的 `error` 原样显示。 */
async function fail(e: unknown, fallback: string): Promise<string> {
  const text = e instanceof Error ? e.message : String(e);
  const m = /^API (\d{3}):\s*(\S+)$/.exec(text);
  if (!m) return text || fallback;
  try {
    const res = await fetch(m[2], {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Surface": "admin" },
      body: "{}",
    });
    const j = (await res.json().catch(() => null)) as { error?: string; detail?: string } | null;
    return j?.error || j?.detail || `${fallback}（HTTP ${res.status}）`;
  } catch {
    return `${fallback}（HTTP ${m[1]}）`;
  }
}

async function load() {
  try {
    const [w, p] = await Promise.all([
      listWards("admin"),
      fetch("/api/profiles", { headers: { "X-Surface": "admin" } }).then(r => r.json()),
    ]);
    // elders 兜个空数组：模板里要 .includes()，后端给 null 就会炸
    wards.value = (w.wards ?? []).map(x => ({ ...x, elders: x.elders ?? [] }));
    // 后端 /api/profiles **默认只返回 kind="elder"**（要看病房走 /api/wards），这里仍按 kind 筛一遍
    elders.value = ((p?.profiles ?? []) as Profile[])
      .filter(x => (x as any).kind === undefined || (x as any).kind === "elder");
  } catch (e) {
    err.value = `读取病房/老人列表失败：${e}`;
  } finally {
    loaded.value = true;
  }
}

async function create() {
  err.value = ""; msg.value = "";
  const uid = newUid.value.trim();
  if (!/^ward_[0-9A-Za-z_]+$/.test(uid)) {
    err.value = "病房 uid 要形如 ward_101（下拉里的建议值就是按床位号来的）";
    return;
  }
  busy.value = true;
  try {
    // 空串 = 后端保持原值（所以这里不会清掉已有区域关联）
    const r = await upsertWard(uid, newName.value.trim(), "", "", "admin");
    if (!r.ok) { err.value = r.error ?? "创建失败"; return; }
    newUid.value = ""; newName.value = "";
    msg.value = `✅ 已保存病房 ${uid}`;
  } catch (e) {
    err.value = await fail(e, "创建失败");
  } finally {
    busy.value = false;
    await load();
  }
}

/** 把病房关联到地图编辑器里**已画好**的区域（区域 uid 从编辑器的区域列表抄）。 */
async function bind(w: Ward) {
  err.value = ""; msg.value = "";
  const map = bindMap.value.trim(), zone = bindZone.value.trim();
  if (!map || !zone) {
    err.value = "地图名与区域 uid 都要填（区域 uid 到地图编辑器的区域列表里抄，形如 z1）";
    return;
  }
  busy.value = true;
  try {
    const r = await upsertWard(w.uid, w.name, map, zone, "admin");
    if (!r.ok) { err.value = r.error ?? "关联失败"; return; }
    msg.value = `✅ ${w.name || w.uid} 已关联到 ${map} / ${zone}`;
  } catch (e) {
    err.value = await fail(e, "关联失败");
  } finally {
    busy.value = false;
    await load();
  }
}

/** 便捷入口：以小车当前位姿为圆心、`ward_zone_default_r` 为半径采样 16 边形写进该图 tags.json。
 *  注意：本接口用**小车当前所在图**（后端 `running_map_name()`），不读病房已有的 ward_map，
 *  所以「第一次记录位置」的病房无需先填地图名。 */
async function markHere(w: Ward) {
  err.value = ""; msg.value = "";
  busy.value = true;
  try {
    const r = await recordWardZone(w.uid, "admin");
    if (!r.ok) { err.value = r.error ?? "记录失败"; return; }
    msg.value = `✅ 已记下 ${r.map} / ${r.zone_uid}`;
  } catch (e) {
    // 失败时把后端 error 原样显示（拿不到位姿 / 认不出地图都会走这里）
    err.value = await fail(e, "记录失败");
  } finally {
    busy.value = false;
    await load();
  }
}

async function remove(w: Ward) {
  const label = w.name || w.uid;
  if (!window.confirm(`确定删除病房“${label}”吗？\n该病房内的老人会自动移出，地图区域会保留。`)) return;
  err.value = ""; msg.value = ""; busy.value = true;
  try {
    const r = await deleteWard(w.uid, "admin");
    if (!r.ok) { err.value = r.error ?? "删除失败"; return; }
    msg.value = `已删除病房 ${label}`;
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
    await load();
  }
}

async function assign(elderUid: string, wardId: string, ev?: Event) {
  err.value = ""; msg.value = "";
  busy.value = true;
  try {
    await assignElderWard(elderUid, wardId, "admin");
    msg.value = wardId ? `✅ ${elderUid} → ${wardId}` : `✅ ${elderUid} 已移出病房`;
  } catch (e) {
    err.value = await fail(e, "分配失败");
    // 失败时把下拉拨回原值：load() 后选项的 :value 由数据决定，但用户手动选过的
    // DOM value 不会自动回退，会造成"看着改了其实没改"
    const sel = ev?.target as HTMLSelectElement | undefined;
    if (sel) sel.value = wardOf(elderUid) ?? "";
  } finally {
    busy.value = false;
    await load();
  }
}

function wardOf(elderUid: string): string | null {
  return wards.value.find(w => w.elders.includes(elderUid))?.uid ?? null;
}

function elderLabel(p: Profile): string {
  return p.nickname || p.name || p.uid;
}

onMounted(load);
</script>

<template>
  <section class="page">
    <h3>病房管理（集体层）</h3>
    <p class="hint">
      一个病房 = 一个「病房用户」（uid 形如 <code>ward_101</code>），它既是集体主体，也承载"车在哪间屋"
      的位置判定。区域几何的<b>唯一真相</b>在地图文件夹的 <code>&lt;图名&gt;.tags.json</code>：
      「记录当前房间为病房区域」按当前位姿生成 16 边形近似圆，精确形状请到
      <button class="link" @click="emit('goto-mapeditor')">地图编辑器</button> 画多边形/矩形（类型选「ward 病区」），
      本页负责把病房关联到那个区域。
    </p>

    <h4>新建病房</h4>
    <div class="row">
      <input v-model="newUid" placeholder="uid（ward_101）" @keyup.enter="create" />
      <input v-model="newName" placeholder="名称（101 病房）" @keyup.enter="create" />
      <button :disabled="busy" @click="create">新建 / 保存</button>
    </div>

    <h4>病房列表</h4>
    <table>
      <thead>
        <tr><th>uid</th><th>名称</th><th>关联区域</th><th>归属老人</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="w in wards" :key="w.uid">
          <td class="mono">{{ w.uid }}</td>
          <td>{{ w.name || "—" }}</td>
          <td class="mono">
            <template v-if="w.ward_zone">
              <div>{{ w.ward_map }} / {{ w.ward_zone }}</div>
              <div class="sub">
                <template v-if="w.zone">{{ w.zone.name || "（无名）" }}
                  <span v-if="w.zone.kind">· {{ w.zone.kind }}</span></template>
                <template v-else>{{ w.ward_map }} 里查不到这个区域</template>
              </div>
            </template>
            <template v-else>未关联</template>
          </td>
          <td class="mono">{{ w.elders.join(", ") || "—" }}</td>
          <td class="acts">
            <button :disabled="busy" @click="markHere(w)">记录当前房间为病房区域</button>
            <button :disabled="busy" @click="bind(w)">关联已画区域</button>
            <button class="danger" :disabled="busy" @click="remove(w)">删除</button>
          </td>
        </tr>
        <tr v-if="loaded && !wards.length"><td colspan="5">还没有病房，先用上面的表单建一个</td></tr>
      </tbody>
    </table>

    <h4>关联已画好的区域</h4>
    <p class="hint">
      先在下面填「地图名 + 区域 uid」（区域在<button class="link" @click="emit('goto-mapeditor')">地图编辑器</button>里画好后从它的列表里抄 uid），
      再点目标病房那一行的「关联已画区域」。
    </p>
    <div class="row">
      <input v-model="bindMap" placeholder="地图名（my_map）" />
      <input v-model="bindZone" placeholder="区域 uid（z1）" />
    </div>

    <h4>老人归属</h4>
    <p class="hint">每个老人只能属于一个病房；下拉选「（移出病房）」即解除归属。</p>
    <table>
      <thead><tr><th>老人</th><th>床位</th><th>当前病房</th><th>改为</th></tr></thead>
      <tbody>
        <tr v-for="e in elders" :key="e.uid">
          <td>{{ elderLabel(e) }}<span class="sub"> · {{ e.uid }}</span></td>
          <td class="mono">{{ e.bed || "—" }}</td>
          <td class="mono">{{ wardOf(e.uid) || "—" }}</td>
          <td>
            <select :value="wardOf(e.uid) ?? ''" :disabled="busy"
                    @change="assign(e.uid, ($event.target as HTMLSelectElement).value, $event)">
              <option value="">（移出病房）</option>
              <option v-for="w in wards" :key="w.uid" :value="w.uid">{{ w.name || w.uid }}</option>
            </select>
          </td>
        </tr>
        <tr v-if="loaded && !elders.length"><td colspan="4">还没有老人档案（去「老人注册」页建）</td></tr>
      </tbody>
    </table>

    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="warn">{{ err }}</p>
  </section>
</template>

<style scoped>
.page { max-width: 1100px; }
h3 { margin: 0 0 6px; font-size: 18px; }
h4 { margin: 22px 0 8px; font-size: 15px; color: #cbd5e1; }
.hint { color: #64748b; font-size: 12px; margin: 6px 0; line-height: 1.7; }
.warn { color: #fbbf24; font-size: 13px; }
.ok { color: #4ade80; font-size: 13px; }
.sub { color: #64748b; font-size: 12px; }
a { color: #60a5fa; }
code { background: #1e293b; padding: 1px 5px; border-radius: 4px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1f2937;
  font-size: 13px; vertical-align: middle; }
th { color: #94a3b8; font-weight: normal; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.row input, select { padding: 8px 12px; border-radius: 8px; border: 1px solid #334155;
  background: #1e293b; color: #e2e8f0; font-size: 14px; }
.row button, .acts button { padding: 8px 14px; border-radius: 8px; border: none;
  background: #1e3a5f; color: #e2e8f0; cursor: pointer; font-size: 13px; }
.acts { display: flex; gap: 6px; flex-wrap: wrap; }
.acts .danger { background: #7f1d1d; }
button.link { background: none; border: none; color: #7dd3fc; padding: 0; font-size: inherit;
  text-decoration: underline; cursor: pointer; }
button:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
