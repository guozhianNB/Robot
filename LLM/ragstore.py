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
from .conf import DATA_DIR

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
    global _client, _AVAILABLE
    if chromadb is None:
        return
    try:
        _client = chromadb.PersistentClient(path=_PATH)
        _AVAILABLE = True
    except Exception as _exc:
        _AVAILABLE = False
        _MISSING.append("chromadb: " + str(_exc))


def _coll(uid: str):
    return _client.get_or_create_collection(
        name=f"memories_{uid}",
        metadata={"hnsw:space": "cosine"},
    )


def add(uid: str, mtype: str, content: str, importance: int = 0, source: str = "",
        external_id: str = "") -> str:
    """写入一条 RAG 记忆。external_id 非空时幂等：同一 uid+external_id 已写入则跳过返回原 chroma_id。

    （P0a 对标 MaiBot external_id 幂等：重试/并发触发同业务只落一次）
    """
    if external_id:
        exist = db.get_rag_by_external(uid, external_id)
        if exist:
            return exist.get("chroma_id") or ""
        if not db.claim_external(uid, external_id, kind=f"rag:{mtype}"):
            # 并发窗口内被他人抢注 → 再查一次，仍未落则继续（写入后 claim 幂等兜底）
            exist = db.get_rag_by_external(uid, external_id)
            if exist:
                return exist.get("chroma_id") or ""
    chroma_id = uuid.uuid4().hex
    if _AVAILABLE:
        try:
            vec = embed.embed_texts([content])[0]
            _coll(uid).add(
                ids=[chroma_id], embeddings=[vec], documents=[content],
                metadatas=[{"uid": uid, "type": mtype, "importance": importance, "source": source}])
        except Exception as e:  # noqa: BLE001
            audit.log("memory_change", action="ragstore_add_error", uid=uid, error=str(e))
    db.add_rag_memory(uid, mtype, content, chroma_id, importance=importance,
                      source=source, external_id=external_id or "")
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


def delete_by_chroma_id(uid: str, chroma_id: str) -> bool:
    """按 chroma_id 删除向量（软删联动）：镜像表行已由 db.soft_delete 处理，此处只清向量。"""
    if not _AVAILABLE or not chroma_id:
        return True
    try:
        _coll(uid).delete(ids=[chroma_id])
        return True
    except Exception as e:  # noqa: BLE001
        audit.log("memory_change", action="ragstore_delete_error", uid=uid,
                  chroma_id=chroma_id, error=str(e))
        return False


def reindex_row(uid: str, mtype: str, content: str, importance: int = 0,
                source: str = "", old_chroma_id: str = "") -> str:
    """恢复软删的 RAG 行：若 chroma 里已无该 id（被清过）则重建向量并回写新 chroma_id。

    返回新 chroma_id（未变则返回 old_chroma_id 原值）。
    """
    if not _AVAILABLE or not old_chroma_id:
        return old_chroma_id
    try:
        existing = _coll(uid).get(ids=[old_chroma_id])
        docs = (existing or {}).get("documents") or []
        if docs:
            return old_chroma_id  # 向量还在，无需重建
    except Exception:  # noqa: BLE001
        pass
    try:
        new_id = uuid.uuid4().hex
        vec = embed.embed_texts([content])[0]
        _coll(uid).add(ids=[new_id], embeddings=[vec], documents=[content],
                       metadatas=[{"uid": uid, "type": mtype, "importance": importance,
                                   "source": source}])
        return new_id
    except Exception as e:  # noqa: BLE001
        audit.log("memory_change", action="ragstore_reindex_error", uid=uid, error=str(e))
        return old_chroma_id


def status() -> dict:
    return {"available": _AVAILABLE, "missing": _MISSING}


_init()
