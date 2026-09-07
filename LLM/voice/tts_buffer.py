# -*- coding: utf-8 -*-
r"""LLM 回复流式分句缓冲器（句级 TTS 流式的前置组件，纯逻辑无 IO）。

输入：LLM chat_stream 的 content 增量（token 级碎片，句子常在中间断开）。
输出：完整句子列表——按中文终止标点切分，未完成尾部留在缓冲等下一增量；
     无标点超长句由长度兜底强制切分，避免"迟迟不出声"。
规则（详见 specs 4.1，行为以 test_tts_buffer.py 断言为准）：
- 终止标点 。！？…（连续多个整体归属，如 ！！/？!/…… 不碎切）为断点且归属前句，
  断点后紧跟的右引号 ”’」』 并入前句再断（。" / 。” 场景）；
- 换行 \n 亦为断点：\n 前内容整段成句出；本批内容含 \n 时，末行残余随换行语义一并
  切出（测试约定 feed("A；B\nC") → ["A；B", "C"] 且 flush 为空）；
- 超 max_chars 且缓冲内无任何断点 → 长度兜底：不足一段直接全出，否则在
  buf[:max_chars] 窗口内取最后一个空格/逗号/顿号切（含分隔符），窗口内无次级分隔则
  硬切 max_chars，直至缓冲切空；
- 不需要语义/句法解析，不需要超时强切。"""
import re

_SENT_END = re.compile(r"[。！？…]+[”’」』]?")   # 连续终止标点整体归属 + 可选后随右引号
_SEP = re.compile(r"[ ，、]")                   # 次级分隔（长度兜底优先断点）


class SentenceBuffer:
    def __init__(self, max_chars: int = 60):
        self.max_chars = max_chars
        self._buf = ""

    def feed(self, delta: str) -> list:
        """入缓冲，返回本次切出的完整句列表（空串 delta 返回空）。"""
        if not delta:
            return []
        self._buf += delta
        return self._drain()

    def flush(self) -> str:
        """取走剩余半句并清空（LLM 流结束收尾调用）。"""
        tail, self._buf = self._buf, ""
        return tail

    def empty(self) -> bool:
        return not self._buf

    def _drain(self) -> list:
        """从左到右增量切句：每次取最早断点（终止标点串 / 换行），切走含断点前缀；
        flush_rest 表示换行或长度兜底已触发，此后残余（含不足 max_chars 的尾段）
        也要切出，保证含 \n/超长文本一次 feed 内切空。"""
        out = []
        flush_rest = False
        while self._buf:
            buf = self._buf
            nl = buf.find("\n")
            m = _SENT_END.search(buf)
            if m is not None and (nl == -1 or m.start() < nl):
                # 终止标点串（连续整体 + 可选后随引号）归属前句 → 整段切走
                out.append(buf[: m.end()])
                self._buf = buf[m.end():]
                flush_rest = False
                continue
            if nl != -1:
                # 换行断句：\n 前内容（无断点的纯前缀）整段成句；残余随换行语义切出
                if nl:
                    out.append(buf[:nl])
                self._buf = buf[nl + 1:]
                flush_rest = True
                continue
            # 缓冲内无任何断点：超长或换行残余 → 长度兜底强制切
            if len(buf) >= self.max_chars or flush_rest:
                cut = self._force_cut(buf)
                out.append(buf[:cut])
                self._buf = buf[cut:]
                flush_rest = True
                continue
            break
        return out

    def _force_cut(self, buf: str) -> int:
        """长度兜底断点下标：整段不足 max_chars 全出；否则取 buf[:max_chars] 内最后
        一个空格/逗号/顿号（含之，保序不丢字符）；窗口内无次级分隔则硬切 max_chars。"""
        if len(buf) <= self.max_chars:
            return len(buf)
        window = buf[: self.max_chars]
        hits = list(_SEP.finditer(window))
        if hits:
            return hits[-1].start() + 1
        return self.max_chars
