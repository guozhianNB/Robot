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
