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

_AVAILABLE = True
_PATH = str(DATA_DIR / "chroma")
_client = None
_MISSING = []

try:
    import chromadb
except ImportError as _exc:
    chromadb = None
    _AVAILABLE = False
    _MISSING.append("chromadb: " + str(_exc))


def _init():
    global _client
    if chromadb is None:
        return
    try:
        _client = chromadb.PersistentClient(path=_PATH)
        _AVAILABLE = True
    except Exception as _exc:
        _AVAILABLE = False
        _MISSING.append(str(_exc))


def _coll(uid: str):
    return _client.get_or_create_collection(
        name=f"memories_{uid}",
        metadata={"hnsw:space": "cosine"},
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
