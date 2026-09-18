<script setup lang="ts">
// kiosk 左侧层级栏（规格 §5/D11）：管理层 / 集体层（病房）/ 老人层 三组抽屉。
// 红线：
//   - 「管理层」那条**不走 uid 切换**（POST /api/session/user 带 role 或 uid="admin" 一律 400），
//     只能经 login()（口令门）拿管理员态；
//   - 所有请求都带 X-Surface: kiosk（shared 的 apiGet/apiPost 的 surface 参数）；
//   - 不展示任何老人隐私信息（只显示昵称/uid）。
import { computed, onMounted, ref } from "vue";
import {
  changePassword, getAdminAuth, listWards, login, logout,
  recordWardZone, setSessionUser, setWard, type SessionUser, type Ward,
} from "shared";

interface Profile { uid: string; name?: string; nickname?: string; bed?: string }

const props = defineProps<{ session: SessionUser }>();
const emit = defineEmits<{ (e: "changed"): void; (e: "close"): void }>();

const profiles = ref<Profile[]>([]);
const wards = ref<Ward[]>([]);
const pw = ref("");
const errMsg = ref("");
const showPwChange = ref(false);
const oldPw = ref("");
const newPw = ref("");
const authRequired = ref(true);
const allWards = ref(false);

const role = computed(() => props.session.role);
const isAdmin = computed(() => role.value === "admin");

// 本病房的老人：以 /api/wards 的 elders 名单为准（`ward_uid` 为空 = 还没定位到病房 → 不给列表，
// 避免把全院老人当成"本病房"）
const wardElderIds = computed(() => {
  const cur = wards.value.find(w => w.uid === props.session.ward_uid);
  return cur ? cur.elders : [];
});
const elders = computed(() => {
  if (allWards.value) return profiles.value;
  const ids = wardElderIds.value;
  return profiles.value.filter(p => ids.includes(p.uid) || p.uid === props.session.uid);
});

function fmt(sec: number | null) {
  if (sec == null) return "--:--";
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** shared 的 client 在非 2xx 时只抛 `API <status>: <url>`，**错误详情（`error`/`detail`）会被丢掉**
 *  （`api/` 只能读 shared，本任务的改动范围不允许改它）。所以失败时按同一 URL 重发一次同样的
 *  请求把响应体拿回来，好让「记录位置」的 403 / `{ok:false, error}` 原样显示给用户。
 *  重发只发生在**已经失败**的调用上（写请求此时必然是 4xx，无副作用）。 */
async function fail(e: unknown, fallback: string) {
  const msg = e instanceof Error ? e.message : String(e);
  const m = /^API (\d{3}):\s*(\S+)$/.exec(msg);
  if (!m) return msg || fallback;
  const url = m[2];
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Surface": "kiosk" },
      body: "{}",
    });
    const j = await res.json() as { error?: string; detail?: string };
    return j.error || j.detail || `${fallback}（HTTP ${res.status}）`;
  } catch {
    return `${fallback}（HTTP ${m[1]}）`;    // 连响应体都拿不到：退回状态码
  }
}

async function refresh() {
  try {
    const [w, a, p] = await Promise.all([
      listWards("kiosk"),
      getAdminAuth("kiosk"),
      fetch("/api/profiles").then(r => r.json()),      // 沿用组件原本的取法（不经 shared）
    ]);
    wards.value = w.wards ?? [];
    authRequired.value = a.required;
    profiles.value = p.profiles ?? [];
  } catch { /* 后端不可达：层级栏仍可用已缓存数据 */ }
}

async function doLogin() {
  errMsg.value = "";
  try {
    // 口令门关着时传 null（后端直接放行）
    const r = await login(authRequired.value ? pw.value : null, "kiosk");
    if (!r.ok) { errMsg.value = r.error ?? "登录失败"; return; }
    pw.value = ""; emit("changed");
  } catch (e) { errMsg.value = await fail(e, "登录失败"); }
}

async function doLogout() {
  errMsg.value = "";
  try { await logout("kiosk"); emit("changed"); } catch (e) { errMsg.value = await fail(e, "退出失败"); }
}

async function pick(uid: string) {
  errMsg.value = "";
  try {
    await setSessionUser(uid, true, "kiosk");        // 手动切换即锁定（规格 D11）
    emit("changed");
    emit("close");                                   // 选完即收栏（与旧弹层行为一致）
  } catch (e) { errMsg.value = await fail(e, "切换失败"); }
}

/** 点病房 = **手动切病房**（D18），不是锁定主体：走 `/api/session/ward` 只切集体层背景变量
 *  并带 10 分钟覆盖窗口；锁定会让 `_holds_session()` 为 False，自动切换被"锁定"挡住而不是被
 *  "覆盖窗口"挡住，语义就错了。老人行仍走 `pick()`（锁定切换）。 */
async function pickWard(uid: string) {
  errMsg.value = "";
  try {
    await setWard(uid, "kiosk");
    emit("changed");                                 // 让 App 重新拉 /api/session/user
    emit("close");
  } catch (e) { errMsg.value = await fail(e, "切换病房失败"); }
}

/** 解锁 = 回当前病房的集体层 + 恢复声纹自动判定（规格 §4.5）。
 *  后端已忽略传入 uid（语义与 uid 无关），故按约定的空串调用即可。 */
async function unlock() {
  errMsg.value = "";
  try {
    await setSessionUser("", false, "kiosk");
    emit("changed");
  } catch (e) { errMsg.value = await fail(e, "解锁失败"); }
}

async function doChangePw() {
  errMsg.value = "";
  try {
    const r = await changePassword(oldPw.value, newPw.value, "kiosk");
    if (!r.ok) { errMsg.value = r.error ?? "改口令失败"; return; }
    showPwChange.value = false; oldPw.value = ""; newPw.value = "";
  } catch (e) { errMsg.value = await fail(e, "改口令失败"); }
}

async function toggleAuth() {
  errMsg.value = "";
  try {
    const r = await setAdminAuth(!authRequired.value, "kiosk");
    authRequired.value = r.required;
    emit("changed");
  } catch (e) { errMsg.value = await fail(e, "开关口令门失败"); }
}

async function markZone(w: Ward) {
  errMsg.value = "";
  try {
    const r = await recordWardZone(w.uid, "kiosk");
    if (!r.ok) { errMsg.value = r.error ?? "记录失败"; return; }   // 后端业务失败：原样显示 error
    await refresh();
  } catch (e) { errMsg.value = await fail(e, "记录位置失败"); }
}

onMounted(refresh);
</script>

<template>
  <div class="layer-drawer">
    <section>
      <h4>🛡 管理层</h4>
      <template v-if="!isAdmin">
        <div class="row">
          <input v-if="authRequired" v-model="pw" type="password" placeholder="管理员口令"
                 @keyup.enter="doLogin" />
          <button @click="doLogin">{{ authRequired ? "进入" : "直接进入（无口令保护⚠️）" }}</button>
        </div>
      </template>
      <template v-else>
        <p class="cur">当前（剩 {{ fmt(session.ttl_remain) }}）</p>
        <div class="row">
          <button @click="doLogout">退出管理层</button>
          <button @click="showPwChange = !showPwChange">口令设置</button>
          <button @click="toggleAuth">{{ authRequired ? "关闭口令门" : "开启口令门" }}</button>
        </div>
        <div v-if="showPwChange" class="row">
          <input v-model="oldPw" type="password" placeholder="旧口令" />
          <input v-model="newPw" type="password" placeholder="新口令" />
          <button @click="doChangePw">保存</button>
        </div>
      </template>
      <p v-if="!authRequired" class="warn">⚠️ 当前无口令保护，任何人都能进管理层</p>
    </section>

    <section>
      <h4>🏠 集体层（病房）</h4>
      <p v-if="!session.autoswitch.enabled" class="hint">
        位置未知 · 手动切病房（{{ session.autoswitch.reason }}）
      </p>
      <ul>
        <li v-for="w in wards" :key="w.uid" :class="{ active: w.uid === session.ward_uid }">
          <span class="name" @click="pickWard(w.uid)">{{ w.name || w.uid }}</span>
          <button v-if="isAdmin" class="mini" title="把当前房间记为这个病房的区域"
                  @click="markZone(w)">记录位置</button>
        </li>
        <li v-if="!wards.length" class="hint">还没有病房用户（在管理台「病房管理」里新建）</li>
      </ul>
    </section>

    <section>
      <h4>
        👴 老人层
        <button class="mini" @click="allWards = !allWards">
          {{ allWards ? "只看本病房" : "全部病房" }}
        </button>
      </h4>
      <ul>
        <li v-for="p in elders" :key="p.uid" :class="{ active: p.uid === session.uid }"
            @click="pick(p.uid)">
          {{ p.nickname || p.name || p.uid }}
        </li>
        <li v-if="!elders.length" class="hint">
          {{ allWards ? "还没有登记老人" : "本病房还没有登记老人" }}
        </li>
      </ul>
      <button v-if="session.locked" class="mini" @click="unlock">
        解除锁定（回当前病房的集体层）
      </button>
    </section>

    <p v-if="errMsg" class="warn">{{ errMsg }}</p>
    <button class="close" @click="emit('close')">关闭</button>
  </div>
</template>

<style scoped>
.layer-drawer { position: absolute; left: 0; top: 0; bottom: 0; width: 320px; overflow-y: auto;
  background: #1b2430; color: #eef3f8; padding: 16px; z-index: 40;
  box-shadow: 4px 0 18px rgba(0, 0, 0, .5); box-sizing: border-box; }
section { border-bottom: 1px solid #33445a; padding-bottom: 12px; margin-bottom: 12px; }
h4 { margin: 0 0 8px; font-size: 15px; display: flex; align-items: center; gap: 8px; }
ul { list-style: none; margin: 0; padding: 0; }
li { display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 8px; border-radius: 8px; cursor: pointer; }
li.active { background: #2b6cb0; }
li .name { flex: 1; }
.row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
input { flex: 1; min-width: 90px; padding: 6px; border-radius: 6px; border: 1px solid #46586f;
  background: #0f1620; color: inherit; }
button { padding: 6px 10px; border-radius: 6px; border: 1px solid #46586f; background: #243244;
  color: inherit; cursor: pointer; }
button.mini { font-size: 12px; padding: 2px 6px; }
.hint { color: #9fb2c8; font-size: 12px; }
.warn { color: #ffd166; font-size: 12px; }
.cur { margin: 4px 0; }
.close { width: 100%; padding: 8px; }
</style>
