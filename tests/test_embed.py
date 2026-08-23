# -*- coding: utf-8 -*-
"""embed.py 测试：回退维度统一 + 无 key 降级。"""
from LLM import embed


def test_fallback_dim_is_embed_dim(monkeypatch):
    monkeypatch.setattr(embed, "_AVAILABLE", False)
    from LLM.conf import EMBED_DIM
    vecs = embed.embed_texts(["老人喜欢听京剧", "孙子在上小学"])
    assert len(vecs) == 2
    assert all(len(v) == EMBED_DIM for v in vecs)


def test_embed_unavailable_without_key(monkeypatch):
    monkeypatch.setattr(embed, "_AVAILABLE", False)
    vecs = embed.embed_texts(["测试"])
    assert len(vecs[0]) == 1024
