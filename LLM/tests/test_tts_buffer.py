# -*- coding: utf-8 -*-
"""SentenceBuffer 分句规则测试：标点/换行/连续标点/引号/长度兜底/跨 feed。"""
from LLM.voice.tts_buffer import SentenceBuffer


def _feed_all(buf, text):
    out = []
    for ch in text:          # 逐字喂，覆盖跨 feed 切分路径
        out.extend(buf.feed(ch))
    return out


def test_split_on_fullwidth_punct():
    b = SentenceBuffer()
    assert b.feed("今天天气很好。") == ["今天天气很好。"]


def test_semicolon_and_newline():
    b = SentenceBuffer()
    assert b.feed("第一句；第二句\n第三句") == ["第一句；第二句", "第三句"]
    assert b.flush() == ""


def test_consecutive_punct_not_split():
    b = SentenceBuffer()
    assert b.feed("太棒了！！真的吗？！") == ["太棒了！！", "真的吗？！"]


def test_right_quote_joins_prev_sentence():
    b = SentenceBuffer()
    assert b.feed("他说：“好的。”明白了") == ["他说：“好的。”"]


def test_incomplete_tail_stays_in_buffer():
    b = SentenceBuffer()
    assert b.feed("明天早上八点") == []
    assert b.flush() == "明天早上八点"


def test_long_no_punct_forced_split_at_space():
    b = SentenceBuffer(max_chars=10)
    out = b.feed("甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑")
    assert "".join(out) == "甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑"
    assert len(out) >= 2 and all(s for s in out)


def test_long_no_punct_hard_split():
    b = SentenceBuffer(max_chars=6)
    assert "".join(b.feed("一二三四五六七八九十")) == "一二三四五六七八九十"


def test_boundary_cross_feed():
    b = SentenceBuffer()
    out = b.feed("这是第一句。这")
    assert out == ["这是第一句。"]
    out += b.feed("是第二句！")
    assert out == ["这是第一句。", "这是第二句！"]


def test_long_sentence_with_trailing_punct_capped():
    """W1：超长句（无中间断点的长串）带尾部句号 → 句号虽在缓冲内但距起点过远，
    不得整段一次切出等远端句号，先按长度兜底分段：join 还原原文、末段含句号、
    每段 ≤ max_chars 附近（句长 cap 保证及时分段合成出声）。"""
    b = SentenceBuffer(max_chars=10)
    text = "一二三四五六七八九十" * 3 + "。"
    out = b.feed(text)
    assert "".join(out) == text
    assert len(out) >= 2, "长句应被分段而非整段一次切出"
    assert out[-1].endswith("。"), "终止标点归属末段"
    assert all(len(s) <= b.max_chars + 1 for s in out), "每段不得超过 max_chars 太多"
    assert b.flush() == ""
    assert all(s for s in out)
