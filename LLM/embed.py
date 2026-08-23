# -*- coding: utf-8 -*-
r"""
Embedding 封装：阿里 text-embedding-v3（DashScope OpenAI 兼容端点）。
无 key / 调用失败 → 回退 vectors.py n-gram（映射到同一 EMBED_DIM 维，保证下游维度一致）。
"""
import math
import os

from . import vectors
from .conf import BASE_DIR, EMBED_BASE_URL, EMBED_MODEL, EMBED_DIM, EMBED_TIMEOUT

_AVAILABLE = True
_MISSING = []
_client = None

try:
    from openai import OpenAI
except ImportError as _exc:
    OpenAI = None
    _AVAILABLE = False
    _MISSING.append("openai: " + str(_exc))

try:
    from dotenv import load_dotenv
except ImportError as _exc:
    load_dotenv = None
    _AVAILABLE = False
    _MISSING.append("python-dotenv: " + str(_exc))


def _ensure_client():
    """懒加载 OpenAI 客户端；key 缺失或构造失败 → _AVAILABLE=False 并返回 None。"""
    global _client, _AVAILABLE
    if _client is not None:
        return _client
    if OpenAI is None or load_dotenv is None:
        return None
    load_dotenv(BASE_DIR / ".env")
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not key:
        _AVAILABLE = False
        if "缺少 DASHSCOPE_API_KEY（.env）" not in _MISSING:
            _MISSING.append("缺少 DASHSCOPE_API_KEY（.env）")
        return None
    try:
        _client = OpenAI(api_key=key, base_url=EMBED_BASE_URL)
    except Exception as _exc:
        _AVAILABLE = False
        _MISSING.append(str(_exc))
    return _client


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


def embed_texts(texts):
    if not texts:
        return []
    if _AVAILABLE:
        client = _ensure_client()
        if client is not None:
            try:
                resp = client.embeddings.create(model=EMBED_MODEL, input=texts, timeout=EMBED_TIMEOUT)
                vecs = [d.embedding for d in resp.data]
                if all(len(v) == EMBED_DIM for v in vecs):
                    return vecs
                # 维度不符 → 视为异常，回退
            except Exception:
                pass
    return _fallback_embed(texts)


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]


def status() -> dict:
    # 懒加载：无 key 时把 _AVAILABLE 置 False 并收集缺失提示，保证状态查询反映真实可用性。
    _ensure_client()
    return {"available": _AVAILABLE, "missing": _MISSING}
