<script setup lang="ts">
// 记忆页：老人下拉 + 核心/RAG/待审核三区 + 定稿/保护/删除/导入/回收站（MaiBot 对标 P0-P3 管理面）
import { onMounted, ref } from "vue";

interface Profile { uid: string; name?: string; bed?: string; age?: number }
interface Mem { id: number; uid: string; type: string; content: string;
  importance?: number; ts?: string; status?: string; authority?: string; pinned?: number }
interface RecycleOp { id: number; uid: string; target_table: string; target_id: number;
  reason?: string; created_at?: string }

const profiles = ref<Profile[]>([]);
const uid = ref("elder_001");
const core = ref<Mem[]>([]);
const rag = ref<Mem[]>([]);
const pending = ref<Mem[]>([]);
const recycle = ref<RecycleOp[]>([]);
const importText = ref("");
const msg = ref("");

async function api(path: string, method = "GET") {
  const res = await fetch(path, { method });
  return res.json();
}

async function loadProfiles() {
  const body = await api("/api/profiles");
  profiles.value = (body.profiles ?? []) as Profile[];
  if (profiles.value.length && !profiles.value.some((p) => p.uid === uid.value)) {
    uid.value = profiles.value[0].uid;
  }
}

async function load() {
  const [bCore, bRag, bMem] = await Promise.all([
    api(`/api/memories/core?uid=${uid.value}`),
    api(`/api/memories/rag?uid=${uid.value}`),
    api(`/api/memories?uid=${uid.value}`),
  ]);
  core.value = bCore.memories ?? [];
  rag.value = bRag.memories ?? [];
  pending.value = (bMem.memories ?? []).filter((m: Mem) => m.status === "pending");
}

async function loadRecycle() {
  const b = await api("/api/memories/recycle");
  recycle.value = (b.operations ?? []).slice(0, 30);
}

async function act(fn: () => Promise<void>) {
  msg.value = "";
  try { await fn(); await load(); } catch (e) { msg.value = `操作失败：${e}`; }
}

function onChange() { load(); loadRecycle(); }

// 核心记忆操作
const coreConfirm = (mid: number) => act(async () => { await api(`/api/memories/core/${mid}/confirm`, "POST"); });
const coreUnconfirm = (mid: number) => act(async () => { await api(`/api/memories/core/${mid}/unconfirm`, "POST"); });
const corePin = (mid: number) => act(async () => { await api(`/api/memories/core/${mid}/pin`, "POST"); });
const coreUnpin = (mid: number) => act(async () => { await api(`/api/memories/core/${mid}/unpin`, "POST"); });
const coreDel = (mid: number) => act(async () => { await api(`/api/memories/core/${mid}`, "DELETE"); });
const ragDel = (rid: number) => act(async () => { await api(`/api/memories/rag/${rid}`, "DELETE"); });
const memConfirm = (mid: number) => act(async () => { await api(`/api/memories/${mid}/confirm`, "POST"); });
const memReject = (mid: number) => act(async () => { await api(`/api/memories/${mid}/reject`, "POST"); });
const restoreOp = async (op: RecycleOp) => {
  await act(async () => { await api(`/api/memories/recycle/${op.id}/restore`, "POST"); });
  await loadRecycle();
};
const purgeAll = async () => {
  await act(async () => { await api("/api/memories/recycle/purge", "POST"); });
  await loadRecycle();
};
const doImport = async () => {
  if (!importText.value.trim()) return;
  await act(async () => {
    await fetch("/api/memories/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: uid.value, text: importText.value, split: "paragraph" }),
    });
    importText.value = "";
  });
  await load();
};

onMounted(async () => { await loadProfiles(); await load(); await loadRecycle(); });
</script>

<template>
  <div>
    <div class="toolbar">
      <label class="pick">老人
        <select v-model="uid" @change="onChange">
          <option v-for="p in profiles" :key="p.uid" :value="p.uid">
            {{ p.name ? `${p.name}（${p.uid}）` : p.uid }}
          </option>
        </select>
      </label>
      <button @click="load">刷新</button>
      <span v-if="msg" class="err">{{ msg }}</span>
    </div>

    <section>
      <h3>🧠 核心记忆
        <small>（AI 归纳标注「仅供参考」，护士「定稿」后视为可信；「保护」防被自动清理/画像覆盖）</small>
      </h3>
      <p v-if="!core.length" class="empty">暂无</p>
      <div v-for="m in core" :key="'c' + m.id" class="row core">
        <div class="row-main">
          <span class="badge">[{{ m.type }}]</span>
          <span v-if="m.pinned" class="pin" title="已保护">📌</span>
          <span v-if="(m.authority ?? 'llm') === 'nurse'" class="ok" title="护士已定稿">✓</span>
          <span v-else class="guess" title="AI 归纳，仅供参考">AI</span>
          <span v-if="(m.importance ?? 0) >= 3" class="star">★</span>
          {{ m.content }}
          <small class="meta">（imp {{ m.importance ?? 0 }}<template v-if="m.ts"> · {{ m.ts }}</template>）</small>
        </div>
        <div class="actions">
          <button v-if="m.authority !== 'nurse'" @click="coreConfirm(m.id)">定稿</button>
          <button v-else @click="coreUnconfirm(m.id)">退AI</button>
          <button v-if="!m.pinned" @click="corePin(m.id)">保护</button>
          <button v-else class="danger" @click="coreUnpin(m.id)">解除</button>
          <button class="danger" @click="coreDel(m.id)">删除</button>
        </div>
      </div>
    </section>

    <section>
      <h3>📚 RAG 记忆 <small>（普通事件/经历，语义检索 Top-K；删除进回收站可恢复）</small></h3>
      <p v-if="!rag.length" class="empty">暂无</p>
      <div v-for="m in rag" :key="'r' + m.id" class="row rag">
        <div class="row-main">
          <span class="badge">[{{ m.type }}]</span>
          {{ m.content }}
          <small class="meta"><template v-if="m.importance"> · imp {{ m.importance }}</template><template v-if="m.ts"> · {{ m.ts }}</template></small>
        </div>
        <div class="actions"><button class="danger" @click="ragDel(m.id)">删除</button></div>
      </div>
    </section>

    <section>
      <h3>⏳ 待人工确认 <small>（沉淀/导入待审核，确认后进入检索）</small></h3>
      <p v-if="!pending.length" class="empty">暂无待处理</p>
      <div v-for="m in pending" :key="'p' + m.id" class="row pending">
        <div class="row-main">
          <span class="badge">[{{ m.type }}]</span>
          {{ m.content }}
          <small class="meta"><template v-if="m.ts"> · {{ m.ts }}</template></small>
        </div>
        <div class="actions">
          <button @click="memConfirm(m.id)">确认</button>
          <button class="danger" @click="memReject(m.id)">拒绝</button>
        </div>
      </div>
    </section>

    <section>
      <h3>📥 批量导入 <small>（粘贴背景资料/喜好清单，按段落切分入待审核）</small></h3>
      <textarea v-model="importText" rows="3" placeholder="粘贴文本，空行分段…"></textarea>
      <button @click="doImport">导入为待审核记忆</button>
    </section>

    <section>
      <h3>🗑 回收站 <small>（软删记忆可恢复，超过 {{ 30 }} 天自动深清）</small></h3>
      <p v-if="!recycle.length" class="empty">暂无</p>
      <div v-for="op in recycle" :key="'o' + op.id" class="row recycle">
        <div class="row-main">
          <small>#{{ op.id }} · {{ op.target_table }}#{{ op.target_id }} · {{ op.reason ?? "手动删除" }}<template v-if="op.created_at"> · {{ op.created_at }}</template></small>
        </div>
        <div class="actions"><button @click="restoreOp(op)">恢复</button></div>
      </div>
      <button v-if="recycle.length" class="danger" @click="purgeAll">立即深清（不可恢复）</button>
    </section>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.pick { display: inline-flex; align-items: center; gap: 8px; color: #94a3b8; }
.pick select { padding: 8px 10px; border-radius: 8px; border: none;
  background: #1e293b; color: #e2e8f0; min-width: 180px; }
.err { color: #f87171; font-size: 13px; }
section { margin-bottom: 24px; }
h3 { margin: 0 0 10px; font-size: 16px; }
h3 small { color: #64748b; font-weight: normal; font-size: 12px; }
.empty { color: #64748b; font-size: 13px; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 10px 14px; margin-bottom: 8px; border-radius: 10px; }
.row.core { background: #1e3a5f; }
.row.rag { background: #1e293b; }
.row.pending { background: #3b2f16; }
.row.recycle { background: #111827; }
.row-main { flex: 1; }
.badge { color: #7dd3fc; margin-right: 6px; }
.star { color: #fbbf24; margin-right: 4px; }
.ok { color: #4ade80; margin-right: 4px; font-weight: bold; }
.guess { color: #fbbf24; margin-right: 4px; font-size: 11px; border: 1px solid #fbbf24;
  border-radius: 4px; padding: 0 4px; vertical-align: 1px; }
.pin { margin-right: 4px; }
.meta { color: #64748b; margin-left: 6px; }
.actions { display: flex; gap: 6px; flex-shrink: 0; }
.actions button { padding: 6px 10px; border-radius: 6px; border: none;
  background: #334155; color: #e2e8f0; cursor: pointer; font-size: 12px; }
.actions .danger, button.danger { background: #7f1d1d; }
button.danger { padding: 6px 10px; border-radius: 6px; border: none; color: #fecaca; cursor: pointer; }
textarea { width: 100%; background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
  border-radius: 8px; padding: 8px; margin-bottom: 8px; box-sizing: border-box; }
</style>
