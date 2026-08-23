# 记忆系统 v3 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把记忆系统从「半自动 + 人工审核」升级为「全自动记录 + 双重自我纠错 + 关系图谱 + 语义检索」，对齐 MaiBot/MIRIX 效果。

**架构：** 四层记忆——SQLite 只读档案（身份+医疗，护士只写）、SQLite 核心记忆（偏好/关系/性格画像）、ChromaDB 普通 RAG（事件/经历/一般事实）、Kuzu 知识图谱（实体-关系）。阿里 text-embedding-v3 做语义检索，失败回退 n-gram。全自动写回 + 即时/整理双重纠错，医疗与身份字段只读红线不变。

**技术栈：** Python 3.11 · FastAPI · SQLite · ChromaDB · Kuzu · 阿里 DashScope embedding（OpenAI 兼容）· pytest

**规格：** `docs/superpowers/specs/2026-08-23-memory-system-v3-design.md`

---

## 文件结构

**新建：**
- `LLM/embed.py` — 阿里 embedding 封装 + n-gram 回退（统一 1024 维输出）
- `LLM/graph.py` — Kuzu 实体-关系图封装（upsert / 一跳查询 / 降级）
- `LLM/ragstore.py` — ChromaDB 向量存储封装（add / query / SQLite 镜像 / 降级）
- `LLM/migrate.py` — 一次性幂等迁移
- `tests/conftest.py` — 测试隔离 fixture（临时数据目录）
- `tests/test_embed.py` / `test_graph.py` / `test_ragstore.py` / `test_memory_v3.py` / `test_migrate.py`

**修改：**
- `LLM/conf.py` — 新增 embedding / 检索 / 分流 / 图谱 / 身份红线配置
- `LLM/db.py` — `profiles` 加 `gender`/`birthday` 列；新增 `core_memories`、`rag_memories` 表及 CRUD
- `LLM/memory.py` — 全自动写回 + 双重纠错 + 检索改造 + 图谱抽取
- `LLM/chat.py` — `build_system` 注入核心记忆 + RAG + 图谱
- `LLM/server.py` — 新 API + lifespan 降级接入 + 迁移触发
- `UI/index.html` — 记忆页签三块
- `requirement.txt` — 补齐核心依赖 + chromadb + kuzu

**不引入 pytest 到生产导入链**（pytest 仅测试用）。可选依赖 chromadb/kuzu/dashscope 只在各自模块顶层 `try/except` 引入，`server.py` 导入链不得硬 import。

---

## 任务 0：环境准备与测试基础

**文件：**
- 修改：`requirement.txt`
- 创建：`tests/conftest.py`

- [ ] **步骤 1：安装核心依赖与新依赖**

```powershell
.venv\Scripts\python.exe -m pip install fastapi uvicorn openai pydantic python-dotenv
.venv\Scripts\python.exe -m pip install chromadb kuzu pytest
```

- [ ] **步骤 2：验证导入**

运行：`.venv\Scripts\python.exe -c "import fastapi, openai, chromadb, kuzu, pytest; print('ok')"`
预期：输出 `ok`

- [ ] **步骤 3：更新 requirement.txt 补齐核心依赖与新依赖**

在 `requirement.txt` 顶部追加（保留原有语音依赖）：

```
fastapi
uvicorn
openai
pydantic
python-dotenv
chromadb
kuzu
```

- [ ] **步骤 4：写 tests/conftest.py（测试隔离）**

```python
# -*- coding: utf-8 -*-
"""测试隔离：把 db/embed/graph/ragstore 的数据目录指向临时目录，避免污染真实 brain.db。"""
import pytest


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    from LLM import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    # graph / ragstore 用 DATA_DIR 派生，逐个 monkeypatch 到 tmp_path
    monkeypatch.setenv("LLM_DATA_DIR", str(tmp_path))
    yield tmp_path
```

- [ ] **步骤 5：运行 pytest 确认基础可用**

运行：`.venv\Scripts\python.exe -m pytest tests -q`
预期：`no tests ran` 或 `collected 0 items`，且无 import/collect 报错。

- [ ] **步骤 6：Commit**

```bash
git add requirement.txt tests/conftest.py
git commit -m "chore: 补齐核心依赖并建立 pytest 测试基础"
```

---

## 任务 1：embed.py —— embedding 封装 + n-gram 回退

**文件：**
- 创建：`LLM/embed.py`
- 修改：`LLM/conf.py`
- 测试：`tests/test_embed.py`

- [ ] **步骤 1：conf.py 新增 embedding 配置**

在 `LLM/conf.py` 的 `LLM 参数` 段前插入：

```python
# ---- 记忆系统 v3：embedding ----
EMBED_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBED_MODEL = "text-embedding-v3"
EMBED_DIM = 1024          # 阿里 text-embedding-v3 默认维度；n-gram 回退也映射到此维度
EMBED_TIMEOUT = 10
```

- [ ] **步骤 2：写失败测试 test_embed.py**

```python
# -*- coding: utf-8 -*-
"""embed.py 测试：回退维度统一 + 无 key 降级。"""
from LLM import embed


def test_fallback_dim_is_embed_dim():
    from LLM.conf import EMBED_DIM
    vecs = embed.embed_texts(["老人喜欢听京剧", "孙子在上小学"])
    assert len(vecs) == 2
    assert all(len(v) == EMBED_DIM for v in vecs)


def test_embed_unavailable_without_key(monkeypatch):
    monkeypatch.setattr(embed, "_AVAILABLE", False)
    vecs = embed.embed_texts(["测试"])
    assert len(vecs[0]) == 1024
```

- [ ] **步骤 3：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_embed.py -q`
预期：FAIL，`ModuleNotFoundError: No module named 'LLM.embed'`

- [ ] **步骤 4：写 LLM/embed.py**

```python
# -*- coding: utf-8 -*-
r"""
Embedding 封装：阿里 text-embedding-v3（DashScope OpenAI 兼容端点）。
无 key / 调用失败 → 回退 vectors.py n-gram（映射到同一 EMBED_DIM 维，保证下游维度一致）。
"""
import math
import os

from . import vectors
from .conf import (BASE_DIR, EMBED_BASE_URL, EMBED_MODEL, EMBED_DIM, EMBED_TIMEOUT)

_AVAILABLE = False
_client = None
_MISSING = []


def _init():
    global _AVAILABLE, _client
    try:
        from openai import OpenAI
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
        key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not key:
            _MISSING.append("缺少 DASHSCOPE_API_KEY（.env）")
            return
        _client = OpenAI(api_key=key, base_url=EMBED_BASE_URL)
        _AVAILABLE = True
    except Exception as e:  # noqa: BLE001
        _MISSING.append(str(e))


def _fallback_embed(texts: list[str]) -> list[list[float]]:
    """n-gram 稀疏向量 → EMBED_DIM 维稠密向量（回退用）。"""
    out = []
    for t in texts:
        sparse = vectors._embed(t)
        vec = [0.0] * EMBED_DIM
        for bucket, w in sparse.items():
            vec[bucket % EMBED_DIM] += w
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        out.append(vec)
    return out


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if _AVAILABLE:
        try:
            resp = _client.embeddings.create(model=EMBED_MODEL, input=texts, timeout=EMBED_TIMEOUT)
            return [d.embedding for d in resp.data]
        except Exception:  # noqa: BLE001
            pass
    return _fallback_embed(texts)


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]


def status() -> dict:
    return {"available": _AVAILABLE, "missing": _MISSING}


_init()
```

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_embed.py -q`
预期：PASS（2 passed）

- [ ] **步骤 6：Commit**

```bash
git add LLM/embed.py LLM/conf.py tests/test_embed.py
git commit -m "feat: 阿里 embedding 封装 + n-gram 回退"
```

---

## 任务 2：db.py —— 数据模型扩展

**文件：**
- 修改：`LLM/db.py`、`LLM/conf.py`
- 测试：`tests/test_migrate.py`（先写 core_memories/rag_memories CRUD 测试）

- [ ] **步骤 1：conf.py 新增记忆 v3 配置**

在 embedding 段后插入：

```python
# ---- 记忆系统 v3：检索与分流 ----
MEMORY_TOP_K = 3                # RAG 检索 Top-K
CORE_MEMORY_CAP = 30            # 核心记忆全量注入条数上限
CORE_MEMORY_CHAR_CAP = 2000     # 核心记忆注入字符上限
CORE_IMPORTANCE_THRESHOLD = 3   # importance >= 3 且核心层 type 才进 core_memories
GRAPH_REL_TYPES = ["likes", "dislikes", "family", "related_to", "happened_at"]

# 身份字段红线：模型永不写、永不改（护士只读档案）
IDENTITY_KEYWORDS = ["姓名", "年龄", "生日", "性别", "床位", "床号"]
```

- [ ] **步骤 2：写失败测试（core_memories / rag_memories CRUD）**

`tests/test_migrate.py`：

```python
# -*- coding: utf-8 -*-
"""db v3 表 CRUD 测试。"""
from LLM import db


def test_core_memories_crud(tmp_path, isolated_paths):
    db.init_db()
    mid = db.add_core_memory("elder_001", "preference", "喜欢听京剧", importance=4)
    rows = db.list_core_memories("elder_001")
    assert any(r["id"] == mid and r["content"] == "喜欢听京剧" for r in rows)


def test_rag_memories_crud(tmp_path, isolated_paths):
    db.init_db()
    rid = db.add_rag_memory("elder_001", "episodic", "上周感冒已好转", chroma_id="c1")
    rows = db.list_rag_memories("elder_001")
    assert any(r["chroma_id"] == "c1" for r in rows)


def test_profile_has_identity_columns(tmp_path, isolated_paths):
    db.init_db()
    db.upsert_profile("elder_001", name="张建国", gender="男", birthday="1948-03-02")
    p = db.get_profile("elder_001")
    assert p["gender"] == "男"
    assert p["birthday"] == "1948-03-02"
```

- [ ] **步骤 3：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_migrate.py -q`
预期：FAIL，`AttributeError: module 'LLM.db' has no attribute 'add_core_memory'`

- [ ] **步骤 4：改 db.py schema**

把 `SCHEMA` 里 `profiles` 建表语句增加两列；`SCHEMA` 末尾追加两张表：

```sql
CREATE TABLE IF NOT EXISTS core_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, type TEXT, content TEXT,
  confidence REAL DEFAULT 0.5, importance INTEGER DEFAULT 0,
  source TEXT DEFAULT '', ts TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS rag_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT, chroma_id TEXT, type TEXT, content TEXT,
  importance INTEGER DEFAULT 0, source TEXT DEFAULT '', ts TEXT
);
```

`profiles` 建表语句在 `age INTEGER DEFAULT 0,` 后加：

```sql
  gender TEXT DEFAULT '', birthday TEXT DEFAULT '',
```

- [ ] **步骤 5：改 db.py 增加 CRUD 函数与 profiles 字段**

在 `profiles` 段 `upsert_profile` 签名与 INSERT 列中补 `gender`、`birthday`（与 `bed`、`age` 同方式，默认 `""`）；`get_profile`/`list_profiles` 无需改（`SELECT *`）。

新增函数（放在 `memories` 段之后）：

```python
# ---------------------------------------------------------------- core_memories
def add_core_memory(uid, mtype, content, confidence=0.5, importance=0, source="", ts=None) -> int:
    ts = ts or now_iso()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT INTO core_memories (uid,type,content,confidence,importance,source,ts,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, mtype, content, confidence, importance, source, ts, ts))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_core_memories(uid, limit=None) -> list[dict]:
    sql = "SELECT * FROM core_memories WHERE uid=? ORDER BY importance DESC, id DESC"
    args = [uid]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def update_core_memory(mid, content=None, importance=None, confidence=None) -> None:
    fields = {"updated_at": now_iso()}
    if content is not None:
        fields["content"] = content
    if importance is not None:
        fields["importance"] = importance
    if confidence is not None:
        fields["confidence"] = confidence
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [mid]
    with _lock:
        conn = _conn()
        try:
            conn.execute(f"UPDATE core_memories SET {keys} WHERE id=?", vals)
            conn.commit()
        finally:
            conn.close()


def delete_core_memory(mid) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM core_memories WHERE id=?", (mid,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- rag_memories（镜像表）
def add_rag_memory(uid, mtype, content, chroma_id, importance=0, source="", ts=None) -> int:
    ts = ts or now_iso()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT INTO rag_memories (uid,chroma_id,type,content,importance,source,ts) VALUES (?,?,?,?,?,?,?)",
                (uid, chroma_id, mtype, content, importance, source, ts))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_rag_memories(uid, limit=None) -> list[dict]:
    sql = "SELECT * FROM rag_memories WHERE uid=? ORDER BY id DESC"
    args = [uid]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
```

- [ ] **步骤 6：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_migrate.py -q`
预期：PASS（3 passed）

- [ ] **步骤 7：Commit**

```bash
git add LLM/db.py LLM/conf.py tests/test_migrate.py
git commit -m "feat: 核心记忆/镜像表数据模型 + 身份字段"
```

---

## 任务 3：graph.py —— Kuzu 知识图谱

**文件：**
- 创建：`LLM/graph.py`
- 测试：`tests/test_graph.py`

- [ ] **步骤 1：写失败测试 test_graph.py**

```python
# -*- coding: utf-8 -*-
"""Kuzu 图谱封装测试：实体/边去重 + 一跳查询 + 降级。"""
import pytest
from LLM import graph


@pytest.fixture
def g(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "_DB_PATH", str(tmp_path / "graph"))
    graph._init()
    if not graph._AVAILABLE:
        pytest.skip("kuzu 未安装")
    return graph


def test_upsert_entity_and_relation(g):
    g.upsert_entity("elder_001", "elder_001:张建国", "张建国", "person")
    g.upsert_entity("elder_001", "elder_001:京剧", "京剧", "topic")
    g.upsert_relation("elder_001", "elder_001:张建国", "elder_001:京剧", "likes")
    rels = g.one_hop("elder_001:张建国")
    assert any(r["target"] == "京剧" and r["type"] == "likes" for r in rels)


def test_relation_dedup(g):
    g.upsert_relation("elder_001", "elder_001:张建国", "elder_001:京剧", "likes")
    g.upsert_relation("elder_001", "elder_001:张建国", "elder_001:京剧", "likes")
    rels = g.one_hop("elder_001:张建国")
    likes = [r for r in rels if r["type"] == "likes" and r["target"] == "京剧"]
    assert len(likes) == 1


def test_status_degraded_when_unavailable(monkeypatch):
    monkeypatch.setattr(graph, "_AVAILABLE", False)
    assert graph.status()["available"] is False
    assert graph.one_hop("x") == []
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_graph.py -q`
预期：FAIL，`ModuleNotFoundError: No module named 'LLM.graph'`

- [ ] **步骤 3：写 LLM/graph.py**

```python
# -*- coding: utf-8 -*-
r"""
Kuzu 知识图谱封装：实体/关系 upsert（去重）+ 一跳关系查询。
可选依赖缺失 → _AVAILABLE=False，全部接口降级为空操作，不阻断对话。
"""
from .conf import DATA_DIR
from . import log as audit

_AVAILABLE = False
_DB_PATH = str(DATA_DIR / "graph")
_db = None
_conn = None
_MISSING = []


def _init():
    global _AVAILABLE, _db, _conn
    try:
        import kuzu
        _db = kuzu.Database(_DB_PATH)
        _conn = kuzu.Connection(_db)
        _conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS Entity(id STRING PRIMARY KEY, uid STRING, name STRING, type STRING)")
        _conn.execute(
            "CREATE REL TABLE IF NOT EXISTS Relation(FROM Entity TO Entity, type STRING, uid STRING, ts STRING)")
        _AVAILABLE = True
    except Exception as e:  # noqa: BLE001
        _MISSING.append(str(e))


def upsert_entity(uid: str, eid: str, name: str, etype: str) -> None:
    if not _AVAILABLE:
        return
    try:
        _conn.execute(
            "MERGE (e:Entity {id: $id}) ON CREATE SET e.uid=$uid, e.name=$name, e.type=$type",
            {"id": eid, "uid": uid, "name": name, "type": etype})
    except Exception as e:  # noqa: BLE001
        audit.log("graph", action="upsert_entity_error", uid=uid, eid=eid, error=str(e))


def upsert_relation(uid: str, src_id: str, dst_id: str, rtype: str) -> None:
    if not _AVAILABLE:
        return
    try:
        _conn.execute(
            "MATCH (a:Entity {id: $src}), (b:Entity {id: $dst}) "
            "MERGE (a)-[r:Relation {type: $type}]->(b) "
            "ON CREATE SET r.uid=$uid, r.ts=$ts",
            {"src": src_id, "dst": dst_id, "type": rtype, "uid": uid, "ts": ""})
    except Exception as e:  # noqa: BLE001
        audit.log("graph", action="upsert_relation_error", uid=uid, error=str(e))


def one_hop(eid: str) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        rows = _conn.execute(
            "MATCH (a:Entity {id: $id})-[r:Relation]->(b:Entity) RETURN b.name AS target, r.type AS type",
            {"id": eid})
        out = []
        while rows.has_next():
            rec = rows.get_next()
            out.append({"target": rec[0], "type": rec[1]})
        return out
    except Exception:  # noqa: BLE001
        return []


def entities_by_name(uid: str, name: str) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        rows = _conn.execute(
            "MATCH (e:Entity) WHERE e.uid=$uid AND e.name=$name RETURN e.id, e.type",
            {"uid": uid, "name": name})
        out = []
        while rows.has_next():
            rec = rows.get_next()
            out.append({"id": rec[0], "type": rec[1]})
        return out
    except Exception:  # noqa: BLE001
        return []


def list_entities(uid: str) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        rows = _conn.execute(
            "MATCH (e:Entity) WHERE e.uid=$uid RETURN e.id, e.name, e.type",
            {"uid": uid})
        out = []
        while rows.has_next():
            rec = rows.get_next()
            out.append({"id": rec[0], "name": rec[1], "type": rec[2]})
        return out
    except Exception:  # noqa: BLE001
        return []


def status() -> dict:
    return {"available": _AVAILABLE, "missing": _MISSING}


_init()
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_graph.py -q`
预期：PASS（3 passed；kuzu 缺失时 3 skipped，也视为通过但需确认已装）

- [ ] **步骤 5：Commit**

```bash
git add LLM/graph.py tests/test_graph.py
git commit -m "feat: Kuzu 知识图谱封装（实体/关系/一跳查询/降级）"
```

---

## 任务 4：ragstore.py —— ChromaDB 向量存储

**文件：**
- 创建：`LLM/ragstore.py`
- 测试：`tests/test_ragstore.py`

- [ ] **步骤 1：写失败测试 test_ragstore.py**

```python
# -*- coding: utf-8 -*-
"""ChromaDB 封装测试：add/query + SQLite 镜像 + 降级。"""
import pytest
from LLM import ragstore, db


@pytest.fixture
def rs(tmp_path, monkeypatch):
    monkeypatch.setattr(ragstore, "_PATH", str(tmp_path / "chroma"))
    ragstore._init()
    if not ragstore._AVAILABLE:
        pytest.skip("chromadb 未安装")
    return ragstore


def test_add_and_query(rs, isolated_paths):
    db.init_db()
    rs.add("elder_001", "episodic", "上周感冒已好转", source="llm")
    hits = rs.query("elder_001", "感冒好了吗", top_k=3)
    assert any("感冒" in h["content"] for h in hits)


def test_mirror_row_created(rs, isolated_paths):
    db.init_db()
    rs.add("elder_001", "semantic", "喜欢京剧", source="llm")
    rows = db.list_rag_memories("elder_001")
    assert any(r["content"] == "喜欢京剧" for r in rows)


def test_degraded_query_empty(monkeypatch):
    monkeypatch.setattr(ragstore, "_AVAILABLE", False)
    assert ragstore.query("elder_001", "任意", top_k=3) == []
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ragstore.py -q`
预期：FAIL，`ModuleNotFoundError: No module named 'LLM.ragstore'`

- [ ] **步骤 3：写 LLM/ragstore.py**

```python
# -*- coding: utf-8 -*-
r"""
ChromaDB 向量存储封装：按 uid 分 collection，add/query 语义检索。
写入同步落 SQLite rag_memories 镜像表（降级兜底 + 前端回读 + 审计回链）。
可选依赖缺失 → _AVAILABLE=False，写操作只落镜像表、查询返回空，不阻断对话。
"""
import uuid

from . import db
from . import embed
from . import log as audit
from .conf import DATA_DIR, EMBED_DIM

_AVAILABLE = False
_PATH = str(DATA_DIR / "chroma")
_client = None
_MISSING = []


def _init():
    global _AVAILABLE, _client
    try:
        import chromadb
        _client = chromadb.PersistentClient(path=_PATH)
        _AVAILABLE = True
    except Exception as e:  # noqa: BLE001
        _MISSING.append(str(e))


def _coll(uid: str):
    return _client.get_or_create_collection(
        name=f"memories_{uid}",
        metadata={"hnsw:space": "cosine", "dimension": EMBED_DIM},
    )


def add(uid: str, mtype: str, content: str, importance: int = 0, source: str = "") -> str:
    chroma_id = uuid.uuid4().hex
    if _AVAILABLE:
        try:
            vec = embed.embed_texts([content])[0]
            _coll(uid).add(
                ids=[chroma_id], embeddings=[vec], documents=[content],
                metadatas=[{"uid": uid, "type": mtype, "importance": importance, "source": source}])
        except Exception as e:  # noqa: BLE001
            audit.log("memory_change", action="ragstore_add_error", uid=uid, error=str(e))
    db.add_rag_memory(uid, mtype, content, chroma_id, importance=importance, source=source)
    return chroma_id


def query(uid: str, q: str, top_k: int = 3) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        qv = embed.embed_texts([q])[0]
        res = _coll(uid).query(query_embeddings=[qv], n_results=top_k)
        hits = []
        docs = res.get("documents") or [[]]
        metas = res.get("metadatas") or [[]]
        for doc, meta in zip(docs[0], metas[0]):
            hits.append({"content": doc, "meta": meta})
        return hits
    except Exception as e:  # noqa: BLE001
        audit.log("memory_change", action="ragstore_query_error", uid=uid, error=str(e))
        return []


def status() -> dict:
    return {"available": _AVAILABLE, "missing": _MISSING}


_init()
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ragstore.py -q`
预期：PASS（3 passed；chromadb 缺失时 2 skipped + 1 passed）

- [ ] **步骤 5：Commit**

```bash
git add LLM/ragstore.py tests/test_ragstore.py
git commit -m "feat: ChromaDB 向量存储封装（add/query/镜像/降级）"
```

---

## 任务 5：memory.py —— 全自动写回 + 分流 + 红线

**文件：**
- 修改：`LLM/memory.py`
- 测试：`tests/test_memory_v3.py`

- [ ] **步骤 1：写失败测试（分流 + 红线拦截）**

`tests/test_memory_v3.py`：

```python
# -*- coding: utf-8 -*-
"""memory v3 写回分流 + 红线测试（不依赖真实 LLM，直接调 _apply_v3）。"""
from LLM import db, memory


def test_semantic_goes_to_rag(monkeypatch, isolated_paths):
    db.init_db()
    added = {}
    monkeypatch.setattr(memory.ragstore, "add",
                        lambda uid, t, c, **kw: added.setdefault("rag", []).append(c))
    r = memory._apply_v3("elder_001", {"type": "semantic", "content": "喜欢京剧", "importance": 2})
    assert r["route"] == "rag"
    assert added["rag"] == ["喜欢京剧"]


def test_high_importance_core_goes_to_core(monkeypatch, isolated_paths):
    db.init_db()
    r = memory._apply_v3("elder_001", {"type": "preference", "content": "喜欢听京剧", "importance": 4})
    assert r["route"] == "core"
    assert any(m["content"] == "喜欢听京剧" for m in db.list_core_memories("elder_001"))


def test_low_importance_core_downgrades_to_rag(monkeypatch, isolated_paths):
    db.init_db()
    added = []
    monkeypatch.setattr(memory.ragstore, "add", lambda uid, t, c, **kw: added.append(c))
    r = memory._apply_v3("elder_001", {"type": "preference", "content": "爱吃甜的", "importance": 1})
    assert r["route"] == "rag"
    assert added == ["爱吃甜的"]


def test_medical_rejected(monkeypatch, isolated_paths):
    db.init_db()
    r = memory._apply_v3("elder_001", {"type": "fact", "content": "每天吃两片降压药", "importance": 5})
    assert r["route"] == "reject"


def test_identity_rejected(monkeypatch, isolated_paths):
    db.init_db()
    r = memory._apply_v3("elder_001", {"type": "fact", "content": "他的姓名是王五", "importance": 5})
    assert r["route"] == "reject"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py -q`
预期：FAIL，`AttributeError: module 'LLM.memory' has no attribute '_apply_v3'`

- [ ] **步骤 3：改 memory.py 新增 import 与 _apply_v3 分流函数**

在 `memory.py` 顶部，把现有的 `from .conf import MEMORY_RULES, EVENT_TTL_DAYS, EPISODE_TTL_DAYS` **替换**为下面这段（新增 `graph`/`ragstore` 导入与 v3 常量）：

```python
from . import graph
from . import ragstore
from .conf import (MEMORY_RULES, EVENT_TTL_DAYS, EPISODE_TTL_DAYS,
                   CORE_IMPORTANCE_THRESHOLD, IDENTITY_KEYWORDS)
```

新增函数（放在 `_apply_entry` 之前）：

```python
def _apply_v3(uid: str, e: dict) -> dict:
    """记忆 v3 分流：episodic/semantic → RAG；核心层 type 按 importance 分流；
    医疗/身份红线一律 reject。"""
    mtype = (e.get("type") or "semantic").lower()
    content = (e.get("content") or "").strip()
    importance = int(e.get("importance") or 0)
    if not content:
        return {"route": "skip"}
    if mtype == "medical" or any(k in content for k in MEDICAL_KEYWORDS):
        audit.log("memory_change", action="reject", uid=uid, type=mtype,
                  content=content, reason="医疗只读红线")
        return {"route": "reject", "reason": "医疗信息只允许人工录入"}
    if any(k in content for k in IDENTITY_KEYWORDS):
        audit.log("memory_change", action="reject", uid=uid, type=mtype,
                  content=content, reason="身份只读红线")
        return {"route": "reject", "reason": "身份信息只允许护士录入"}

    core_types = {"preference", "relation", "persona", "style", "fact"}
    if mtype in core_types and importance >= CORE_IMPORTANCE_THRESHOLD:
        db.add_core_memory(uid, mtype, content, importance=importance, source="llm:consolidate")
        audit.log("memory_change", action="core_add", uid=uid, type=mtype, content=content)
        return {"route": "core"}
    # 其余（episodic/semantic 或低 importance 核心层）→ RAG
    ragstore.add(uid, mtype, content, importance=importance, source="llm:consolidate")
    audit.log("memory_change", action="rag_add", uid=uid, type=mtype, content=content)
    return {"route": "rag"}
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py -q`
预期：PASS（5 passed）

- [ ] **步骤 5：Commit**

```bash
git add LLM/memory.py tests/test_memory_v3.py
git commit -m "feat: 记忆 v3 写回分流 + 医疗/身份红线"
```

---

## 任务 6：memory.py —— 双重自我纠错

**文件：**
- 修改：`LLM/memory.py`
- 测试：`tests/test_memory_v3.py`（追加）

- [ ] **步骤 1：写失败测试（纠错逻辑，不依赖真实 LLM）**

在 `tests/test_memory_v3.py` 追加：

```python
def _mk_client(judge_reply):
    class C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class R:
                        choices = [type("Ch", (), {"message": type("M", (), {"content": judge_reply})()})()]
                    return R()
    return C()


def test_instant_correct_updates_core(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人姓张", importance=4)
    monkeypatch.setattr("LLM.chat.llm_json",
                        lambda c, m, p: {"correct": True, "mid": mid, "new_content": "老人姓王"})
    r = memory.correct_instant("elder_001", "我其实不姓张，我姓王", _mk_client("{}"), "test-model")
    assert r["corrected"] is True
    assert db.get_core_memory(mid)["content"] == "老人姓王"


def test_instant_correct_blocks_identity(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人喜欢戏曲", importance=4)
    monkeypatch.setattr("LLM.chat.llm_json",
                        lambda c, m, p: {"correct": True, "mid": mid, "new_content": "姓名是李四"})
    r = memory.correct_instant("elder_001", "我其实姓李", _mk_client("{}"), "test-model")
    assert r["corrected"] is False
    assert db.get_core_memory(mid)["content"] == "老人喜欢戏曲"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py::test_instant_correct_updates_core -q`
预期：FAIL，`AttributeError: ... 'correct_instant'`

- [ ] **步骤 3：改 db.py 增加 get_core_memory**

在 `core_memories` 段加：

```python
def get_core_memory(mid: int) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM core_memories WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()
```

- [ ] **步骤 4：改 memory.py 增加即时纠错函数**

```python
CORRECT_PROMPT = """下面是一句老人新说的话。判断它是否在纠正/更新机器人之前的某条记忆。
若是，输出要纠正的记忆目标与新内容；否则 correct=false。
只输出 JSON：{{"correct": true或false, "mid": <记忆id，无则 null>, "new_content": "纠正后的内容"}}

【新说的话】
{text}
"""


def correct_instant(uid: str, user_text: str, client, model: str) -> dict:
    """即时纠错：对话返回后异步调用。识别"纠正/更新旧记忆"，直接更新（医疗/身份红线除外）。"""
    from .chat import llm_json
    try:
        data = llm_json(client, model, CORRECT_PROMPT.format(text=user_text))
    except Exception as e:  # noqa: BLE001
        audit.log("memory_correct", action="instant_error", uid=uid, error=str(e))
        return {"corrected": False, "reason": "llm_error"}
    if not isinstance(data, dict) or not data.get("correct") or not data.get("mid"):
        return {"corrected": False, "reason": "no_correction"}
    mid = int(data["mid"])
    old = db.get_core_memory(mid)
    new_content = (data.get("new_content") or "").strip()
    if not old or not new_content:
        return {"corrected": False, "reason": "no_target"}
    if any(k in new_content for k in MEDICAL_KEYWORDS) or any(k in new_content for k in IDENTITY_KEYWORDS):
        audit.log("memory_correct", action="blocked", uid=uid, mid=mid,
                  old=old["content"], new=new_content, reason="医疗/身份红线")
        return {"corrected": False, "reason": "redline"}
    db.update_core_memory(mid, content=new_content)
    audit.log("memory_correct", action="instant", uid=uid, mid=mid,
              old=old["content"], new=new_content)
    return {"corrected": True, "mid": mid}
```

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py -q`
预期：PASS（7 passed）

- [ ] **步骤 6：Commit**

```bash
git add LLM/memory.py LLM/db.py tests/test_memory_v3.py
git commit -m "feat: 即时自我纠错（红线拦截）"
```

> 注：整理纠错（consolidate 内矛盾自动更新）在任务 8 改造 consolidate 时一并落地，本任务先打通即时纠错。

---

## 任务 7：检索注入 —— recall 改造 + build_system

**文件：**
- 修改：`LLM/memory.py`、`LLM/chat.py`
- 测试：`tests/test_memory_v3.py`（追加）

- [ ] **步骤 1：写失败测试（recall v3 组装）**

`tests/test_memory_v3.py` 追加：

```python
def test_recall_v3_returns_core_and_rag(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    db.add_core_memory("elder_001", "preference", "喜欢听京剧", importance=5)
    monkeypatch.setattr(memory.ragstore, "query",
                        lambda uid, q, top_k: [{"content": "上周感冒已好转", "meta": {}}])
    monkeypatch.setattr(memory.graph, "one_hop", lambda eid: [])
    r = memory.recall_v3("elder_001", "想听戏")
    assert "喜欢听京剧" in r["context"]
    assert "感冒" in r["context"]
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py::test_recall_v3_returns_core_and_rag -q`
预期：FAIL，`AttributeError: ... 'recall_v3'`

- [ ] **步骤 3：改 memory.py 增加 recall_v3**

```python
def recall_v3(uid: str, query: str) -> dict:
    """v3 检索组装：只读档案 + 核心记忆（cap）+ RAG Top-K + 图谱一跳关系。"""
    from .conf import MEMORY_TOP_K, CORE_MEMORY_CAP, CORE_MEMORY_CHAR_CAP
    parts = []
    sources = []
    profile = db.get_profile(uid)
    parts += _profile_memory(profile, uid)
    sources += [{"type": "profile"} for _ in parts]

    cores = db.list_core_memories(uid, limit=CORE_MEMORY_CAP)
    for m in cores:
        parts.append(f"[核心] {m['content']}（{m['type']}）")
    sources += [{"type": "core", "id": m["id"]} for m in cores]

    for h in ragstore.query(uid, query, top_k=MEMORY_TOP_K):
        parts.append(f"[记忆] {h['content']}")
        sources.append({"type": "rag"})

    for eid in _query_entities(uid, query):
        for rel in graph.one_hop(eid):
            parts.append(f"[关系] {eid.split(':')[-1]} {rel['type']} {rel['target']}")
            sources.append({"type": "graph"})

    context = "\n".join(parts)
    if len(context) > CORE_MEMORY_CHAR_CAP + 3000:
        context = context[:CORE_MEMORY_CHAR_CAP + 3000]
    return {"context": context, "sources": sources}


def _query_entities(uid: str, query: str) -> list[str]:
    """从 query 匹配该 uid 已有实体名（名称出现在 query 中），返回命中实体 id。"""
    ids = []
    for ent in graph.list_entities(uid):
        name = ent.get("name") or ""
        if name and name in query:
            ids.append(ent["id"])
    return ids
```

- [ ] **步骤 4：改 chat.py build_system 用 recall_v3**

把 `build_system` 内 `recall = rag.recall(uid, query)` 改为 `recall = rag.recall_v3(uid, query)`。

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py::test_recall_v3_returns_core_and_rag -q`
预期：PASS

- [ ] **步骤 6：手动验证老 recall 仍可用**

运行：`.venv\Scripts\python.exe -c "from LLM import memory; print('ok')"`
预期：输出 `ok`（无 import 报错）

- [ ] **步骤 7：Commit**

```bash
git add LLM/memory.py LLM/chat.py tests/test_memory_v3.py
git commit -m "feat: v3 检索注入（核心记忆+RAG+图谱骨架）"
```

---

## 任务 8：consolidate 改造 + 整理纠错 + 图谱抽取

**文件：**
- 修改：`LLM/memory.py`
- 测试：`tests/test_memory_v3.py`（追加）

- [ ] **步骤 1：写失败测试（consolidate 产出分发，mock LLM）**

`tests/test_memory_v3.py` 追加：

```python
CONSOLIDATE_JSON = '''
{"entries": [
  {"type":"episodic","content":"上周感冒已好转","importance":2},
  {"type":"preference","content":"喜欢听京剧","importance":4}
 ],
 "relations": [{"src":"张建国","stype":"person","rel":"likes","dst":"京剧","dtype":"topic"}],
 "digest":"聊了身体恢复和京剧爱好",
 "portrait":"喜欢京剧，身体在恢复"}
'''


def test_consolidate_v3_routes_and_graph(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    monkeypatch.setattr("LLM.chat.llm_json", lambda c, m, p: __import__("json").loads(CONSOLIDATE_JSON))
    monkeypatch.setattr(memory, "_take_pending", lambda uid: [{"role": "user", "content": "我好了，喜欢京剧"}])
    monkeypatch.setattr(memory, "_dedup_check", lambda uid, c, **kw: None)
    monkeypatch.setattr(memory.graph, "upsert_entity", lambda *a: None)
    monkeypatch.setattr(memory.graph, "upsert_relation", lambda *a: None)
    monkeypatch.setattr(memory.ragstore, "add", lambda uid, t, c, **kw: None)
    r = memory.consolidate("elder_001", None, "test-model")
    assert r["ok"] is True
    assert any(m["content"] == "喜欢听京剧" for m in db.list_core_memories("elder_001"))


def test_apply_v3_correct_updates_old(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人姓张", importance=4)
    r = memory._apply_v3("elder_001", {"action": "correct", "correct_id": mid, "content": "老人姓王"})
    assert r["route"] == "correct"
    assert db.get_core_memory(mid)["content"] == "老人姓王"


def test_apply_v3_correct_blocks_identity(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, memory
    db.init_db()
    mid = db.add_core_memory("elder_001", "fact", "老人喜欢戏曲", importance=4)
    r = memory._apply_v3("elder_001", {"action": "correct", "correct_id": mid, "content": "姓名是李四"})
    assert r["route"] == "reject"
    assert db.get_core_memory(mid)["content"] == "老人喜欢戏曲"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py::test_consolidate_v3_routes_and_graph -q`
预期：FAIL（`consolidate` 当前签名/逻辑未走 v3 分流）

- [ ] **步骤 3：改 memory.py 的 consolidate 走 v3 分流 + 图谱抽取**

将 `consolidate()` 内 `_apply_entry` 调用改为 `_apply_v3`；`data` 解析后新增关系与画像处理：

```python
    for e in data.get("entries", []) or []:
        r = _apply_v3(uid, e)
        key = r["route"]
        stats[key] = stats.get(key, 0) + 1

    for rel in data.get("relations", []) or []:
        _upsert_relation(uid, rel)

    digest = (data.get("digest") or "").strip()
    if digest:
        _append_summary_and_episode(uid, digest)

    portrait = (data.get("portrait") or "").strip()
    if portrait:
        _upsert_portrait(uid, portrait)
```

新增辅助函数：

```python
def _upsert_relation(uid: str, rel: dict) -> None:
    src, dst = (rel.get("src") or "").strip(), (rel.get("dst") or "").strip()
    if not src or not dst:
        return
    sid = f"{uid}:{src}"
    did = f"{uid}:{dst}"
    graph.upsert_entity(uid, sid, src, rel.get("stype", "entity"))
    graph.upsert_entity(uid, did, dst, rel.get("dtype", "entity"))
    graph.upsert_relation(uid, sid, did, rel.get("rel", "related_to"))


def _append_summary_and_episode(uid: str, digest: str) -> None:
    prev = db.get_summary(uid)
    new_sum = (prev + "\n" + f"[{db.now_iso()[:10]}] {digest}").strip()
    db.set_summary(uid, new_sum[-900:])
    if not _dedup_check(uid, digest, mtype="episode"):
        ragstore.add(uid, "episodic", digest, source="llm:consolidate")
        audit.log("memory_change", action="episode_add", uid=uid, content=digest)


def _upsert_portrait(uid: str, portrait: str) -> None:
    # 画像写入核心记忆（type=persona, importance=5），旧 persona 条目软覆盖（删旧写新）
    for m in db.list_core_memories(uid):
        if m["type"] == "persona":
            db.delete_core_memory(m["id"])
    db.add_core_memory(uid, "persona", portrait, importance=5, source="llm:consolidate")
    audit.log("memory_change", action="portrait_update", uid=uid, portrait=portrait)
```

**整理纠错扩展（规格 6.2）：** 让 LLM 在整理时能输出"修正旧记忆"，`_apply_v3` 处理 correct 动作（红线拦截后 `update_core_memory`）。

在 `_apply_v3` 中，`mtype/content/importance` 解析之后、分流之前插入：

```python
    # 整理纠错：本条是在修正旧核心记忆
    if e.get("action") == "correct" and e.get("correct_id"):
        mid = int(e["correct_id"])
        old = db.get_core_memory(mid)
        if old and content:
            if any(k in content for k in MEDICAL_KEYWORDS) or any(k in content for k in IDENTITY_KEYWORDS):
                audit.log("memory_correct", action="blocked", uid=uid, mid=mid, reason="红线")
                return {"route": "reject"}
            db.update_core_memory(mid, content=content)
            audit.log("memory_correct", action="consolidate", uid=uid, mid=mid,
                      old=old["content"], new=content)
            return {"route": "correct"}
```

`CONSOLIDATE_PROMPT` 的 `entries 规则` 末尾追加第 6 条：

```text
6. 若某条新信息是在【修正】已有记忆（已有记忆编号见上文《已有记忆》列表），
   用 {"action":"correct", "correct_id":<已有记忆编号>, "content":"修正后的完整内容"} 表示。
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_memory_v3.py -q`
预期：PASS（全 11 passed）

- [ ] **步骤 5：Commit**

```bash
git add LLM/memory.py tests/test_memory_v3.py
git commit -m "feat: consolidate 走 v3 分流 + 图谱抽取 + episode/画像"
```

---

## 任务 9：server.py —— API + 降级接入 + 即时纠错接线

**文件：**
- 修改：`LLM/server.py`

- [ ] **步骤 1：lifespan 与降级接入**

在 `lifespan` 启动序列（`db.init_db()` 之后）追加迁移与自检；`_post_chat_jobs` 里追加即时纠错：

```python
    # 记忆 v3 迁移（幂等）+ 依赖自检
    try:
        from . import migrate
        migrate.run()
    except Exception as e:
        audit.log("memory_change", action="migrate_error", error=str(e))

    from . import embed as embed_mod, ragstore, graph
    audit.log("memory_degraded", embed=embed_mod.status(),
              ragstore=ragstore.status(), graph=graph.status())
```

`_post_chat_jobs` 末尾追加（在 `chat.summarize_old` 之后）：

```python
    try:
        rag.correct_instant(uid, user_text, client, MODEL)
    except Exception:
        pass
```

- [ ] **步骤 2：新增 API 端点**

在 `/api/context` 之后追加：

```python
@app.get("/api/memories/core")
async def core_memories_list(uid: str = Query("elder_001")):
    return {"ok": True, "memories": db.list_core_memories(uid)}


@app.delete("/api/memories/core/{mid}")
async def core_memories_delete(mid: int):
    db.delete_core_memory(mid)
    return {"ok": True}


@app.get("/api/memories/rag")
async def rag_memories_list(uid: str = Query("elder_001")):
    return {"ok": True, "memories": db.list_rag_memories(uid)}


@app.get("/api/memories/graph")
async def graph_view(uid: str = Query("elder_001")):
    from . import graph as g
    return {"ok": True, "status": g.status()}


@app.get("/api/memories/health")
async def memories_health():
    from . import embed as e, ragstore, graph as g
    return {"ok": True, "embed": e.status(), "ragstore": ragstore.status(), "graph": g.status()}
```

- [ ] **步骤 3：启动验证**

运行：`.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 127.0.0.1 --port 8000`
预期：无 import 崩溃；访问 `http://127.0.0.1:8000/api/memories/health` 返回三个 status 的 JSON（降级时 `available:false` + `missing` 原因）。

- [ ] **步骤 4：Commit**

```bash
git add LLM/server.py
git commit -m "feat: 记忆 v3 API + 降级自检 + 即时纠错接线"
```

---

## 任务 10：migrate.py —— 一次性幂等迁移

**文件：**
- 创建：`LLM/migrate.py`
- 测试：`tests/test_migrate.py`（追加）

- [ ] **步骤 1：写失败测试（幂等迁移）**

`tests/test_migrate.py` 追加：

```python
def test_migrate_idempotent(tmp_path, isolated_paths, monkeypatch):
    from LLM import db, migrate
    db.init_db()
    # 造旧数据
    db.add_memory("elder_001", "event", "上周感冒", status="confirmed", source="llm")
    db.add_memory("elder_001", "preference", "喜欢京剧", status="confirmed", source="llm")
    monkeypatch.setattr(migrate.ragstore, "add", lambda uid, t, c, **kw: None)
    r1 = migrate.run()
    n_core_after_first = len(db.list_core_memories("elder_001"))
    r2 = migrate.run()
    assert r1["ok"] is True
    assert n_core_after_first == len(db.list_core_memories("elder_001"))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_migrate.py::test_migrate_idempotent -q`
预期：FAIL，`ModuleNotFoundError: No module named 'LLM.migrate'`

- [ ] **步骤 3：写 LLM/migrate.py**

```python
# -*- coding: utf-8 -*-
r"""一次性幂等迁移：旧 memories/portraits/summaries/style/preferences → v3 分层。
用 settings 里的 migrate_done 标记保证只跑一次。"""
from . import db
from . import ragstore
from . import log as audit


def run() -> dict:
    settings = db.get_settings()
    if settings.get("migrate_done"):
        return {"ok": True, "skipped": True}
    migrated = {"core": 0, "rag": 0}
    for m in db.list_memories():
        if m.get("status") != "confirmed":
            continue
        if m.get("expires_at") and m["expires_at"] < db.now_iso():
            continue
        mtype = m["type"]
        content = m["content"]
        if mtype in ("preference", "fact", "relation", "persona", "style"):
            db.add_core_memory(m["uid"], mtype, content, importance=3, source="migrate")
            migrated["core"] += 1
        else:  # event → episodic
            ragstore.add(m["uid"], "episodic", content, source="migrate")
            migrated["rag"] += 1
    for uid in _all_uids():
        portrait = db.get_portrait(uid)
        if portrait:
            db.add_core_memory(uid, "persona", portrait, importance=5, source="migrate")
            migrated["core"] += 1
        summary = db.get_summary(uid)
        if summary:
            ragstore.add(uid, "episodic", summary, source="migrate")
            migrated["rag"] += 1
    db.set_settings({"migrate_done": True})
    audit.log("memory_change", action="migrate", **migrated)
    return {"ok": True, **migrated}


def _all_uids() -> list[str]:
    return [p["uid"] for p in db.list_profiles()]
```

> 注：`db.set_settings` 目前只接受 `DEFAULT_SETTINGS` 里已声明的 key；需在 `conf.py` 的 `DEFAULT_SETTINGS` 追加 `"migrate_done": False`。

- [ ] **步骤 4：conf.py 追加 migrate_done 默认项**

`DEFAULT_SETTINGS` 追加：

```python
    "migrate_done": False,        # 记忆 v3 一次性迁移是否已完成
```

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_migrate.py -q`
预期：PASS（4 passed）

- [ ] **步骤 6：Commit**

```bash
git add LLM/migrate.py LLM/conf.py tests/test_migrate.py
git commit -m "feat: 一次性幂等迁移旧记忆到 v3 分层"
```

---

## 任务 11：UI 记忆页签三块 + 收尾验证

**文件：**
- 修改：`UI/index.html`

- [ ] **步骤 1：改记忆页签为三块**

在 `page-memory` 区块（现有 `portrait-box` 与记忆列表之间）新增「核心记忆」「图谱」两区，复用现有 `loadMemory()` 里已加载的 confirmed/pending 渲染逻辑，改为并行请求 `/api/memories/core`、`/api/memories/rag`、`/api/memories/graph` 三接口渲染：

```html
<h3>🧠 核心记忆（性格/偏好/关系，模型自动沉淀）</h3>
<div id="core-box" class="hint">加载中…</div>
<h3>🗂️ RAG 记忆（事件/经历/一般事实，语义检索用）</h3>
<div id="rag-box" class="hint">加载中…</div>
<h3>🕸️ 知识图谱（实体-关系，可查看）</h3>
<div id="graph-box" class="hint">加载中…</div>
```

`loadMemory()` 内用 `fetch` 三个端点填充 `core-box`/`rag-box`/`graph-box`（`escapeHtml` 渲染，与现有 `portrait-box` 一致）。

- [ ] **步骤 2：手动验证**

浏览器打开 `UI/index.html` → 记忆页签：三块正常渲染；`/api/memories/health` 返回降级状态不报错。

- [ ] **步骤 3：Commit**

```bash
git add UI/index.html
git commit -m "feat: 记忆页签三块（核心/RAG/图谱）"
```

---

## 收尾验证清单（全部任务完成后执行）

- [ ] `.venv\Scripts\python.exe -m pytest tests -q` 全绿
- [ ] `.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 127.0.0.1 --port 8000` 启动无崩溃
- [ ] 无 `DASHSCOPE_API_KEY` 时 `/api/memories/health` 返回 `embed.available=false` 且对话不崩（降级回退）
- [ ] 配置 `DASHSCOPE_API_KEY` 后 embedding 生效（`embed.available=true`）
- [ ] 医疗/身份关键词写入被拒绝（审计 `memory_change reject`）
- [ ] 对话后记忆自动沉淀（核心记忆 + RAG 分别可见），无需人工审核

---

## 依赖顺序与阻塞说明

- 任务 1-4（embed/graph/ragstore/db）是纯基础设施，无相互顺序依赖，但都是任务 5+ 的前置。
- 任务 5-6 依赖任务 1-4；任务 7 依赖 5；任务 8 依赖 5-6；任务 9-10 依赖 8；任务 11 依赖 9。
- **阿里 embedding 真实调用需 `DASHSCOPE_API_KEY`（.env）**：未配置时走 n-gram 回退，所有测试用 mock/回退，不阻塞。
