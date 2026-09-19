# -*- coding: utf-8 -*-
r"""
轻量向量检索（纯 Python 实现，零第三方依赖）。
  - 不做本地 embedding 模型（Python 3.14 下装 bge/ChromaDB 有兼容风险，且演示规模很小），
    改用"字符 n-gram 哈希 + TF 加权"的稀疏向量做中文短文本相似度，效果够用于检索 Top-K。
  - 用法：build_index(docs) -> index；recall(index, query, top_k)。
"""
import math
import re
from collections import Counter

_DIM = 4096  # 哈希桶数


def _grams(text: str):
    """提取字符 2-gram + 3-gram（中文短句检索效果好于整词分词）。"""
    text = re.sub(r"\s+", "", text.lower())
    grams = []
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            grams.append(text[i:i + n])
    return grams


def _embed(text: str) -> dict:
    """TF 加权稀疏向量（dict: bucket -> weight）。"""
    c = Counter(_grams(text))
    vec = {}
    for g, cnt in c.items():
        bucket = hash(g) % _DIM
        vec[bucket] = vec.get(bucket, 0.0) + (1.0 + math.log(cnt))
    # L2 归一化
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm > 0:
        vec = {k: v / norm for k, v in vec.items()}
    return vec


def _cos(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) < len(b) else (b, a)
    return sum(v * large.get(k, 0.0) for k, v in small.items())


def build_index(docs: list[dict]):
    """docs: [{"id":..., "text":..., "meta":{...}}, ...] -> index dict"""
    return [{"doc": d, "vec": _embed(d["text"])} for d in docs]


def recall(index, query: str, top_k: int = 3) -> list[dict]:
    """返回 [{doc, score}] 按相似度降序。"""
    qv = _embed(query)
    scored = []
    for item in index:
        s = _cos(qv, item["vec"])
        if s > 0:
            scored.append({"doc": item["doc"], "score": round(s, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
