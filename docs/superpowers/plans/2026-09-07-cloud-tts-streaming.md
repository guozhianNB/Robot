# 云端 TTS + 句级流式播报 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让语音链路 LLM 回复"边说边播"（句级流式），本地 sherpa 与云端豆包语音 TTS 2.0 可切换（重启生效），回复内容逐字实时上屏 kiosk 气泡。

**架构：** worker 应答线程消费 `chat.chat_stream` 事件流 → `SentenceBuffer`（标点切分+长度兜底）按句切分 → 每句经 TTS 引擎 `synthesize_chunks` 合成（本地 sherpa 句级 / 云端豆包 HTTP 单向流式 NDJSON 分片）→ 新队列版 `AudioSink` 无缝连续播放；每个 content delta 同步 publish 新总线事件 `chat_partial`，kiosk 打字机式更新 assistant 气泡，`chat_new` 保留作终稿覆盖兜底。`tts_provider(local|cloud)` 照 `asr_provider` 三件套模式，默认 cloud、不可用自动回退本地。

**技术栈：** Python（FastAPI 后端 + numpy + requests）；Vue3 + TS 前端（pnpm monorepo，shared 包 events.ts 为 SSE 事件唯一事实来源）；pytest 单测。

**规格：** `docs/superpowers/specs/2026-09-07-cloud-tts-streaming-design.md`

**外部依赖事实（已核实，见 specs 第三节与 tts_cloud.py docstring 计划注释）：**
- 云端端点 `POST https://openspeech.bytedance.com/api/v3/tts/unidirectional`（HTTP 单向流式，与双向 WS 共用同一豆包 key/resource 体系，蓝本 = GizClaw/doubao-speech-go `tts_v2.go`）。
- 请求 JSON：`{"user":{"uid":...},"req_params":{"text":..., "speaker":..., "audio_params":{"format":"pcm","sample_rate":16000}}}`。
- 响应 = NDJSON 逐行 `{"code":0,"message":...,"done":bool,"data":"<base64 音频分片>"}`；`code==0` 且 `done` 标记收尾；非零 code = 错误。data base64 → PCM16LE。
- 鉴权 header：`X-Api-Key` + `X-Api-Resource-Id`（= `seed-tts-2.0`）——**需 T0 实测探针确认**（豆包语音 UUID Key 兼容性）。
- 前提（阻塞性）：`.env` 需有 `VOLC_TTS_SPEAKER`（用户提供音色 ID，**2026-09-07 核验时尚未写入 .env，需用户补**）；`VOLC_ASR_API_KEY` 已存在且与 TTS 同一把。

**分支：** 当前在 `remote_tts`。每次 commit 只 add 本任务相关文件。

---

### 任务 0：协议实测探针（云端定案，需 VOLC_TTS_SPEAKER）

**文件：**
- 创建：`docs/2.pre/tts2_probe.py`
- 修改：`D:\_project\Robot\.env`（用户补 `VOLC_TTS_SPEAKER=<音色ID>`）

**背景：** HTTP 单向端点存在性、鉴权 header 名与豆包 UUID Key 的兼容性以真实调用为准（参考云端 ASR 那轮"实测链路"的排障方式，避免文档考古）。本任务跑通即定案 tts_cloud.py 的实现基线；若 401/404 则在任务 4 改走双向 WS 蓝本（GizClaw `examples/tts_v2/websocket` 会话语义 + tts_v2.md 事件表），其余设计不变。

- [ ] **步骤 1：写探针脚本**

```python
# -*- coding: utf-8 -*-
"""火山豆包语音 TTS 2.0 HTTP 单向流式探针：验证端点/鉴权/NDJSON 音频分片。
用法：python docs/2.pre/tts2_probe.py   （读根 .env 的 VOLC_ASR_API_KEY / VOLC_TTS_SPEAKER）
通过 → 打印前 3 个音频分片字节数 + done 标记；失败 → 打印 HTTP 状态与响应体前 300 字。"""
import json, os, sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent.parent          # 项目根
sys.path.insert(0, str(BASE))
for ln in (BASE / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

key = os.getenv("VOLC_ASR_API_KEY") or ""
speaker = os.getenv("VOLC_TTS_SPEAKER") or ""
assert key, "缺少 VOLC_ASR_API_KEY"
assert speaker, "缺少 VOLC_TTS_SPEAKER（音色 ID 未配置）"
import requests
url = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
body = {"user": {"uid": "tts-probe"},
        "req_params": {"text": "你好，这是火山豆包语音合成测试。",
                       "speaker": speaker,
                       "audio_params": {"format": "pcm", "sample_rate": 16000}}}
hdrs = {"Content-Type": "application/json", "X-Api-Key": key, "X-Api-Resource-Id": "seed-tts-2.0"}
try:
    with requests.post(url, json=body, headers=hdrs, stream=True, timeout=30) as r:
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:300]); sys.exit(1)
        n, done = 0, 0
        for line in r.iter_lines():
            if not line:
                continue
            j = json.loads(line)
            if j.get("code") not in (0, 20000000):
                print("code:", j.get("code"), j.get("message")); sys.exit(1)
            if j.get("data"):
                import base64
                raw = base64.b64decode(j["data"])
                n += len(raw)
                if n <= 48000:
                    print("audio chunk bytes:", len(raw))
            if j.get("done"):
                done = 1
                break
        print("total audio bytes:", n, "| done:", bool(done))
        sys.exit(0 if (n > 0 and done) else 2)
except Exception as e:
    print("FAIL:", repr(e)[:300]); sys.exit(2)
```

- [ ] **步骤 2：确认 .env 有 VOLC_TTS_SPEAKER（向用户索取音色 ID 补入后执行）**

```powershell
Select-String -Path D:\_project\Robot\.env -Pattern '^VOLC_TTS_SPEAKER='
```
预期：输出该行（若用户已补写）。未设置则找用户要音色 ID，写入后再继续。

- [ ] **步骤 3：运行探针**

运行：`.venv\Scripts\python.exe docs/2.pre/tts2_probe.py`（项目根目录）
预期：`HTTP 200` → 若干 `audio chunk bytes: <n>` → `total audio bytes: >0 | done: True`，退出码 0。
若 `HTTP 401/403`：检查 header 名（改试 `X-Api-App-Key` 等，以官方响应为准）；若 `HTTP 404`：改走双向 WS 蓝本（见任务 0 背景），并把任务 4 的 `_ENDPOINT`/请求构造按 WS 事件表替换。
失败结论必须写进 tts_cloud.py docstring 与 docs/log.md（任务 7 一并记）。

- [ ] **步骤 4：Commit**

```bash
git add docs/2.pre/tts2_probe.py
git commit -m "chore: 豆包TTS2.0 HTTP单向流式探针脚本"
```

---

### 任务 1：配置项（conf / voice/config）

**文件：**
- 修改：`LLM/conf.py:29-31`（DEFAULT_SETTINGS 加 tts_provider）
- 修改：`LLM/voice/config.py`（文件尾追加云端 TTS 读取函数 + 分句常量）

- [ ] **步骤 1：conf.py 加默认设置**

在 `LLM/conf.py` 的 `"tts_enabled": True,` 之后加一行：

```python
    "tts_provider": "cloud",        # 合成引擎：cloud=豆包语音(默认；不可用自动回退本地)/ local=sherpa（重启生效，worker 启动时读取）
```

- [ ] **步骤 2：voice/config.py 追加云端 TTS 配置与分句常量**

在 `LLM/voice/config.py` 文件末尾（cloud_asr_resource_id 之后）追加：

```python
# ---- 云端 TTS（火山豆包语音 2.0，与 ASR 同控制台 API Key；延迟读取，见模块 docstring）----
def cloud_tts_api_key():
    """VOLC_TTS_API_KEY 优先，未配置时回落 ASR 同一把 key（同控制台 API Key）。"""
    return os.getenv("VOLC_TTS_API_KEY") or os.getenv("VOLC_ASR_API_KEY") or ""


def cloud_tts_speaker():
    """音色 ID：豆包语音控制台 → 音色库（必填，缺失时云端引擎构造抛错）。"""
    return os.getenv("VOLC_TTS_SPEAKER") or ""


def cloud_tts_resource_id():
    return os.getenv("VOLC_TTS_RESOURCE_ID") or "seed-tts-2.0"


def cloud_tts_model():
    return os.getenv("VOLC_TTS_MODEL") or "seed-tts-2.0-standard"


def cloud_tts_endpoint():
    return os.getenv("VOLC_TTS_ENDPOINT") or "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
```

- [ ] **步骤 3：回归冒烟**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q 2>&1 | Select-Object -Last 3`
预期：`64 passed`（或与改动前相同数量），确认 DEFAULT_SETTINGS 加键不破坏 settings 读写。

- [ ] **步骤 4：Commit**

```bash
git add LLM/conf.py LLM/voice/config.py
git commit -m "feat(tts): 新增 tts_provider 默认设置与云端TTS配置读取(豆包2.0)"
```

---

### 任务 2：SentenceBuffer 分句缓冲器（TDD）

**文件：**
- 创建：`LLM/voice/tts_buffer.py`
- 创建：`LLM/tests/test_tts_buffer.py`

**规则（specs 4.1）：** 终止标点 `。！？…`（全角）+ 换行 `\n` 断句，标点归属前句；连续终止标点（`！！`/`？!`/`……`）整体归属不碎切；`。"`/`。」` 右引号归属前句再断；超 `max_chars` 无断点强制切（次级分隔取空格/逗号/顿号，无则硬切）；跨 feed 边界正确。

- [ ] **步骤 1：编写失败测试**

```python
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
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_tts_buffer.py -q`
预期：FAIL（ImportError: cannot import name 'SentenceBuffer'）

- [ ] **步骤 3：实现 SentenceBuffer**

```python
# -*- coding: utf-8 -*-
r"""LLM 回复流式分句缓冲器（句级 TTS 流式的前置组件，纯逻辑无 IO）。

输入：LLM chat_stream 的 content 增量（token 级碎片，句子常在中间断开）。
输出：完整句子列表——按中文终止标点切分，未完成尾部留在缓冲等下一增量；
     无标点超长句由长度兜底强制切分，避免"迟迟不出声"。
规则（详见 specs 4.1）：终止标点 。！？… 与 \n 为断点且归属前句；连续终止标点
整体归属不碎切；句号后的右引号并入前句；超 max_chars 无断点 → 找最后空格/逗号/
顿号强切，再找不到则硬切。不需要语义/句法解析，不需要超时强切。"""
import re

_SENT_END = re.compile(r"[。！？…]|(?:\n)|(?:[。！？…]+[”’」』])")
_QUOTE_TAIL = re.compile(r"[”’」』]$")
_MAX_HARD = 60

_SEP = re.compile(r"[ ，、]")      # 次级分隔（长度兜底优先断点）


class SentenceBuffer:
    def __init__(self, max_chars: int = 60):
        self.max_chars = max_chars
        self._buf = ""

    def feed(self, delta: str) -> list:
        """入缓冲，返回本次切出的完整句列表（空串/空白 delta 返回空）。"""
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
        out = []
        while True:
            if not self._buf:
                break
            if self._buf[-1] == "\n":          # 换行即断点（去尾）
                self._buf = self._buf[:-1]
                if self._buf:
                    out.append(self._buf)
                    self._buf = ""
                continue
            m = _SENT_END.search(self._buf)
            if m and (len(self._buf) <= self.max_chars + 8 or m.start() < self.max_chars):
                # 断点存在且在长度约束内 → 含断点及其后引号整体切走
                end = m.end()
                if end < len(self._buf) and _QUOTE_TAIL.match(self._buf[end]):
                    end += 1
                sent, self._buf = self._buf[:end], self._buf[end:]
                out.append(sent)
                continue
            if len(self._buf) >= self.max_chars:
                # 无可用断点且超长：优先次级分隔，否则硬切
                seg = _SEP.search(self._buf)
                cut = seg.start() if seg else self.max_chars
                if seg and seg.start() == 0:
                    cut = 1
                out.append(self._buf[:cut])
                self._buf = self._buf[cut:].lstrip(" ")
                continue
            break
        return out
```

> 说明：`_drain` 的 `_SENT_END.search` 每次从缓冲头找断点，找不到且未超长 → 保留下次 feed；断点位置超过 max_chars 余量 → 走长度兜底。实现若有边界瑕疵（如引号跨 feed），以测试为准微调，规则以测试断言为准。

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_tts_buffer.py -q`
预期：`8 passed`

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/tts_buffer.py LLM/tests/test_tts_buffer.py
git commit -m "feat(tts): SentenceBuffer 分句缓冲器(标点切分+长度兜底, 纯逻辑可单测)"
```

---

### 任务 3：AudioSink 队列播放器（TDD）

**文件：**
- 修改：`LLM/voice/audio.py:64-132`（AudioSink 重写为队列写线程）
- 创建：`LLM/tests/test_audio_sink.py`

**语义（specs 4.4）：** `enqueue(samples)` 追加 16k 样本段，同一输出流连续分块写（句间无缝）；`end_of_stream()` 标记"无更多段"，队列清空后写线程自行收流；`stop()` 打断：置停止事件 + 清队列 + abort 唤醒，join 写线程；`is_done()` = 输出流已关闭；`play(samples, sample_rate)` 兼容旧调用（= stop + 复位结束标记 + enqueue）。写线程仍是输出流的唯一创建/关闭者。

- [ ] **步骤 1：编写失败测试**

```python
# -*- coding: utf-8 -*-
"""AudioSink 队列播放测试（fake sounddevice，无真实声卡依赖）。"""
import threading
import time

import numpy as np

from LLM.voice import audio as audio_mod


class FakeStream:
    def __init__(self):
        self.writes = []          # list[np.ndarray]（实际送入 write 的块）
        self.closed = False

    def start(self):
        pass

    def write(self, x):
        self.writes.append(np.asarray(x).copy())
        time.sleep(0.001)         # 让写线程有调度窗口

    def abort(self):
        self.closed = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


class FakeSd:
    def __init__(self):
        self.streams = []

    def OutputStream(self, samplerate, channels, dtype):
        s = FakeStream()
        self.streams.append(s)
        return s


def _patch_sd(monkeypatch):
    fake = FakeSd()
    monkeypatch.setattr(audio_mod, "sd", fake)
    monkeypatch.setattr(audio_mod, "_SD_OK", True)
    return fake


def _wait_done(sink, timeout=2.0):
    t0 = time.time()
    while not sink.is_done() and time.time() - t0 < timeout:
        time.sleep(0.01)
    return sink.is_done()


def test_enqueue_plays_segments_in_order(monkeypatch):
    fake = _patch_sd(monkeypatch)
    sink = audio_mod.AudioSink()
    a = np.zeros(3200, dtype=np.float32)
    b = np.ones(3200, dtype=np.float32)
    sink.enqueue(a)
    sink.enqueue(b)
    sink.end_of_stream()
    assert _wait_done(sink)
    assert len(fake.streams) == 1
    got = np.concatenate([w for w in fake.streams[0].writes]) if fake.streams[0].writes else np.zeros(0)
    assert len(got) == 6400
    assert got[3200] == 1.0 and got[0] == 0.0     # 先 a 后 b，顺序播放


def test_stop_clears_queue_and_closes(monkeypatch):
    fake = _patch_sd(monkeypatch)
    sink = audio_mod.AudioSink()
    sink.enqueue(np.zeros(320000, dtype=np.float32))   # 超长段确保没播完
    time.sleep(0.05)
    sink.stop()
    assert _wait_done(sink)
    assert all(s.closed for s in fake.streams)


def test_play_compat(monkeypatch):
    fake = _patch_sd(monkeypatch)
    sink = audio_mod.AudioSink()
    sink.play(np.zeros(1600, dtype=np.float32), 16000)
    sink.end_of_stream()
    assert _wait_done(sink)
    assert fake.streams and fake.streams[0].writes
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_audio_sink.py -q`
预期：FAIL（当前 AudioSink 无 enqueue/end_of_stream）

- [ ] **步骤 3：重写 AudioSink**

把 `LLM/voice/audio.py` 中 `class AudioSink` 整体替换为：

```python
class AudioSink:
    """队列播放器：独立线程从内部队列取"样本段"，段内分块连续写（句间无缝）。
    语义：enqueue 追加一段（首个段启动输出流）；end_of_stream() 标记无更多段，
    队列清空后写线程自行收流（自然播完）；stop() 打断=置停止事件+清队列+abort
    唤醒并 join（barge-in）；is_done()=输出流已关闭。写线程是输出流唯一创建/关闭者
    （沿用分块写+单次关闭模型，防 PortAudio 双关闭竞态）。
    本类只处理 16k 样本（云端 TTS 固定请求 16k，与本地一致），不做重采样。"""

    _WRITE_BLOCK_S = 0.2

    def __init__(self):
        self._lock = threading.Lock()
        self._stream = None
        self._thread = None
        self._stop_write = threading.Event()
        self._queue = []          # 待播样本段（float32 16k）
        self._ended = False       # end_of_stream 已调用：无更多段
        self._write_frames = int(config.SAMPLE_RATE * self._WRITE_BLOCK_S)

    def enqueue(self, samples: np.ndarray):
        """追加一段 16k float32 音频到播放队列；首个段时启动输出流。"""
        _require_sd()
        arr = np.asarray(samples, dtype=np.float32)
        if arr.size == 0:
            return
        with self._lock:
            self._queue.append(arr)
            if self._thread is None and self._stream is None:
                self._stop_write = threading.Event()
                self._ended = False
                try:
                    stream = sd.OutputStream(
                        samplerate=config.SAMPLE_RATE, channels=1, dtype="float32")
                    stream.start()
                except Exception:
                    self._queue.clear()
                    raise
                self._stream = stream
                self._thread = threading.Thread(
                    target=self._run, args=(stream,), daemon=True)
                self._thread.start()

    def end_of_stream(self):
        """标记无更多段：队列清空后写线程自行收流（自然播完语义）。"""
        with self._lock:
            self._ended = True

    def _run(self, stream):
        """取段 → 段内分块阻塞写；stop/收流条件满足后退出；本线程唯一关闭流。"""
        n = self._write_frames
        try:
            while not self._stop_write.is_set():
                with self._lock:
                    if self._queue:
                        samples = self._queue.pop(0)
                    elif self._ended:
                        break
                    else:
                        samples = None
                if samples is None:
                    time.sleep(0.02)
                    continue
                for i in range(0, len(samples), n):
                    if self._stop_write.is_set():
                        break
                    stream.write(samples[i:i + n])    # 阻塞 ~0.2s/块；打断时抛错退出
        except Exception:
            pass
        finally:
            with self._lock:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
                if self._stream is stream:
                    self._stream = None
                self._thread = None

    def stop(self):
        """打断播放：清队列并关闭输出流（同步等待写线程退出）。"""
        with self._lock:
            self._stop_write.set()
            self._queue.clear()
            stream = self._stream
            if stream is not None:
                try:
                    stream.abort()
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def is_done(self) -> bool:
        with self._lock:
            return self._stream is None and self._thread is None

    def play(self, samples: np.ndarray, sample_rate: int):
        """旧语义兼容：打断当前播放后播这一段（内部复刻 stop+enqueue）。"""
        self.stop()
        with self._lock:
            self._stop_write = threading.Event()
            self._ended = False
        self.enqueue(samples)
```

注意：删除旧 `_run(self, stream, samples)` 与 `play` 的旧实现（整段一次性入参 → 段入队）；文件头部 docstring 补充队列模型说明。

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_audio_sink.py -q`
预期：`3 passed`

- [ ] **步骤 5：既有音频相关回归**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_worker_events.py -q 2>&1 | Select-Object -Last 3`
预期：暂允许因后续任务未完成而失败（该文件在任务 5 改造）；本轮只确认无 import 崩溃即可。

- [ ] **步骤 6：Commit**

```bash
git add LLM/voice/audio.py LLM/tests/test_audio_sink.py
git commit -m "feat(voice): AudioSink 改队列播放器(句间无缝拼接+打断清队)"
```

---

### 任务 4：TTS 引擎统一接口 + 云端引擎（TDD）

**文件：**
- 修改：`LLM/voice/tts.py`（本地引擎加 synthesize_chunks / sample_rate）
- 创建：`LLM/voice/tts_cloud.py`
- 创建：`LLM/tests/test_tts_cloud.py`

**统一引擎接口（specs 4.2）：** 本地与云端引擎都提供：
- `provider: str`（"local"/"cloud"）
- `sample_rate: int`（= 16000）
- `synthesize_chunks(text) -> iterable[np.ndarray]`：流式——本地一次性生成 yield 整段；云端逐 NDJSON 分片 yield。
- `synthesize(text) -> (np.ndarray, int)`：整段 = `np.concatenate(list(synthesize_chunks(text)))`（云端实现），本地沿用原逻辑。

- [ ] **步骤 1：本地引擎加流式接口**

在 `LLM/voice/tts.py` 的 `class TTS` 内 `synthesize` 之后追加：

```python
    @property
    def sample_rate(self):
        return config.SAMPLE_RATE

    def synthesize_chunks(self, text):
        """流式合成接口：本地离线模型一次生成，yield 整段（句级即粒度）。"""
        samples, _ = self.synthesize(text)
        if len(samples) > 0:
            yield samples
```

（无需新测试文件；既有行为不变，任务 5 的 worker 流式测试覆盖调用路径。）

- [ ] **步骤 2：编写云端引擎纯函数测试（失败）**

```python
# -*- coding: utf-8 -*-
"""CloudStreamTTS 纯逻辑测试：请求构造 / NDJSON 行解析（不触网）。"""
import base64

from LLM.voice import tts_cloud as tc


def test_build_request_body():
    body = tc.build_request_body("你好。", "zh_xx_speaker_1", 16000)
    rp = body["req_params"]
    assert rp["text"] == "你好。"
    assert rp["speaker"] == "zh_xx_speaker_1"
    assert rp["audio_params"] == {"format": "pcm", "sample_rate": 16000}
    assert body["user"]["uid"]


def test_parse_stream_line_audio():
    pcm = b"\x00\x01\xff\xfe"
    line = '{"code":0,"data":"' + base64.b64encode(pcm).decode() + '","done":false}'
    out = tc.parse_stream_line(line)
    assert out["audio"] == pcm
    assert out["done"] is False
    assert out["error"] is None


def test_parse_stream_line_done_and_error():
    ok = tc.parse_stream_line('{"code":0,"done":true}')
    assert ok["audio"] is None and ok["done"] is True
    err = tc.parse_stream_line('{"code":12345,"message":"boom"}')
    assert err["error"] == "boom"
    none = tc.parse_stream_line("not json")
    assert none is None


def test_parse_stream_line_ok_code_alias():
    # 服务端成功码别名 20000000（对齐 SAUC 的 _OK_CODES 惯例）
    out = tc.parse_stream_line('{"code":20000000,"data":"","done":true}')
    assert out["done"] is True and out["error"] is None
```

- [ ] **步骤 3：运行确认失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_tts_cloud.py -q`
预期：FAIL（ImportError）

- [ ] **步骤 4：实现 tts_cloud.py**

```python
# -*- coding: utf-8 -*-
r"""云端中文 TTS：火山豆包语音 2.0 HTTP 单向流式（音频分片 NDJSON 返回）。

协议（蓝本 = GizClaw/doubao-speech-go tts_v2.go，2026-09-07 已核对；端点/鉴权以
docs/2.pre/tts2_probe.py 实测为准）：
  - POST {endpoint}/api/v3/tts/unidirectional，JSON body：
      {"user":{"uid":...}, "req_params": {"text":..., "speaker":..., "audio_params":
      {"format":"pcm","sample_rate":16000}}}
  - header：X-Api-Key（豆包语音控制台 UUID Key，同云端 ASR）+ X-Api-Resource-Id=seed-tts-2.0
  - 响应 = NDJSON：每行 {"code":0,"message":...,"done":bool,"data":"<base64 PCM16 分片>"}
    成功码 0 / 20000000；done=true 收尾；非零 code 报错。
  - 本实现按句请求（每句一个短请求，首音最快；不用双向 WS 文本增量流，YAGNI）。

稳健性：requests 缺失时仍可导入（_REQ_OK=False），实例化时抛 RuntimeError 说明，
由 worker 启动降级处理 —— 不污染后端导入链。输出恒为 16k 单声道 float32，与本地/声卡一致。"""
import json

import numpy as np

from . import config

try:
    import requests
    _REQ_OK = True
except Exception:      # 缺依赖：仅云端 TTS 不可用，实例化时给出明确提示
    requests = None
    _REQ_OK = False

_OK_CODES = (0, 20000000)
_FALLBACK_SAMPLE_RATE = 16000


def build_request_body(text: str, speaker: str, sample_rate: int = 16000,
                       uid: str = "robot-voice-tts") -> dict:
    """构造 POST body（纯函数，便于单测）。"""
    return {
        "user": {"uid": uid},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {"format": "pcm", "sample_rate": sample_rate},
        },
    }


def parse_stream_line(raw) -> dict:
    """解析一行 NDJSON → dict(audio=bytes|None, done=bool, error=str|None)；坏行返回 None。

    data 为 base64(PCM16) 分片；成功码 0/20000000，其余按错误返回 message。"""
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    line = raw.decode("utf-8", "ignore").strip() if isinstance(raw, (bytes, bytearray)) else raw.strip()
    if not line:
        return None
    try:
        j = json.loads(line)
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    code = j.get("code", 0)
    if code not in _OK_CODES:
        return {"audio": None, "done": False,
                "error": j.get("message") or j.get("msg") or ("错误码 " + str(code))}
    audio = None
    data = j.get("data")
    if data:
        try:
            audio = __import__("base64").b64decode(data)
        except Exception:
            audio = None
    return {"audio": audio, "done": bool(j.get("done")), "error": None}


class CloudStreamTTS:
    """火山豆包 TTS 2.0 引擎：句级请求，NDJSON 分片流式合成。"""

    provider = "cloud"
    sample_rate = _FALLBACK_SAMPLE_RATE

    def __init__(self):
        if not _REQ_OK:
            raise RuntimeError("云端 TTS 不可用：缺少 requests 依赖")
        if not config.cloud_tts_api_key():
            raise RuntimeError("云端 TTS 不可用：未配置 API Key（VOLC_TTS_API_KEY，"
                               "缺省复用 VOLC_ASR_API_KEY，豆包语音控制台 → API 调用 → 创建 API Key）")
        if not config.cloud_tts_speaker():
            raise RuntimeError("云端 TTS 不可用：未配置音色 VOLC_TTS_SPEAKER"
                               "（豆包语音控制台 → 音色库，形如 zh_female_xxx_bigtts）")
        self._endpoint = config.cloud_tts_endpoint()
        self._key = config.cloud_tts_api_key()
        self._speaker = config.cloud_tts_speaker()
        self._resource = config.cloud_tts_resource_id()

    def synthesize_chunks(self, text):
        """句级请求；逐 NDJSON 分片 yield float32 16k 样本。空文本 yield 空。"""
        clean = text.strip()
        if not clean:
            return
        import base64
        body = build_request_body(clean, self._speaker, self.sample_rate)
        hdrs = {"Content-Type": "application/json",
                "X-Api-Key": self._key,
                "X-Api-Resource-Id": self._resource}
        try:
            resp = requests.post(self._endpoint, json=body, headers=hdrs,
                                 stream=True, timeout=(10, 60))
        except Exception as e:
            raise RuntimeError("云端 TTS 请求失败: {}".format(str(e)[:200]))
        try:
            if resp.status_code != 200:
                raise RuntimeError("云端 TTS HTTP {}: {}".format(
                    resp.status_code, resp.text[:200]))
            for line in resp.iter_lines():
                out = parse_stream_line(line)
                if out is None:
                    continue
                if out["error"]:
                    raise RuntimeError("云端 TTS 错误: " + out["error"])
                if out["audio"]:
                    pcm16 = np.frombuffer(out["audio"], dtype="<i2")
                    yield pcm16.astype(np.float32) / 32768.0
                if out["done"]:
                    break
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def synthesize(self, text):
        """整段合成（测试/兜底用）：拼装全部分片。"""
        chunks = list(self.synthesize_chunks(text))
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        return np.concatenate(chunks), self.sample_rate
```

> 说明：`sample_rate` 若服务端返回非 16k 的 pcm（正常不会，请求已限定），worker 端不做重采样——以请求限定为准。若任务 0 探针发现端点/鉴权不符（如需 `X-Api-App-Key`），只改本文件 `_endpoint`/headers 常量与 docstring，接口不变。

- [ ] **步骤 5：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_tts_cloud.py LLM/tests/test_tts_buffer.py -q`
预期：`12 passed`

- [ ] **步骤 6：Commit**

```bash
git add LLM/voice/tts.py LLM/voice/tts_cloud.py LLM/tests/test_tts_cloud.py
git commit -m "feat(tts): 云端豆包TTS2.0引擎(HTTP单向流式NDJSON分片)+本地流式接口"
```

---

### 任务 5：worker 流式编排 + voice_api 接线（核心改造）

**文件：**
- 修改：`LLM/voice/worker.py`（构造签名、_build_runtime、_handle_speech、新增 _consume_reply/_start_answer/_answer/_enqueue_sentence/_abort、_step SPEAKING 判定）
- 修改：`LLM/voice_api.py:103-124`（删 _chat_fn，改传 stream_fn；get_status 加 tts_provider，136-149 行）
- 修改：`LLM/tests/test_worker_events.py`（全文件适配新构造与流式语义）

**关键设计（specs 4.5/4.6）：**
- 构造签名改为 `VoiceWorker(stream_fn, post_turn_fn=None, publish_fn=None)`；`stream_fn(uid, text)` 返回 chat_stream 事件迭代器。self.stream_fn。
- `_build_runtime`：tts_provider=cloud → try `CloudStreamTTS`，抛错回退本地；本地引擎总是构造为 `self._local_tts`（cloud 模式下的兜底引擎）。`self.tts` = 实际引擎；`sub_status["tts"]` = provider（含 fallback 时标注）。
- `_handle_speech` 保留声纹/用户切换/publish recognized，LLM 段改为 `_start_answer`。
- `_consume_reply(uid, user_text, settings)` **同步**：消费事件流 → chat_partial 广播 → 分句 → 合成入队（speak 时）；返回完整 assistant 文本（单测直接同步调它）。
- `_answer` 线程包装：调 _consume_reply → publish chat_new → post_turn_fn；finally 复位 `_answering`/abort。
- 打断：主循环 SPEAKING 分支插话 → sink.stop()（新实现清队收流）→ barge_in + `_abort.set()`。`_enqueue_sentence` 入队前检查 `session.state == SPEAKING`（被打断后不再发声）。
- 主循环播报结束判定：`sink.is_done() and not self._answering and session.finish_speaking()`。
- 回声静音窗/_flush_vad/免唤醒逻辑**不动**。

- [ ] **步骤 1：改写 worker.py（完整 diff 描述）**

**1a. 顶部导入**追加 `from . import tts_buffer`；`import threading` 已有。

**1b. `__init__` 签名与新增字段**（`chat_fn` 参数改名 `stream_fn`，字段 `self.stream_fn`；去掉 `self.chat_fn`）：

```python
    def __init__(self, stream_fn, post_turn_fn=None, publish_fn=None):
        super().__init__(daemon=True, name="voice-worker")
        self.stream_fn = stream_fn          # (uid, text) -> iterable[chat_stream 事件 dict]
        self.post_turn_fn = post_turn_fn    # (uid, user_text, assistant) -> None
        self.publish_fn = publish_fn        # 事件广播，可空
        ...原有字段不动...
        self.tts = self._local_tts = None   # 实际引擎 + 本地兜底引擎
        self._answering = False             # 应答线程活跃标志（防队列瞬时空误判播报结束）
        self._abort = threading.Event()     # 打断应答（停止后续句子合成/入队）
```

**1c. `_build_runtime` TTS 段**（原第 91 行 `self.tts = tts_mod.TTS()` 替换）：

```python
        self._local_tts = tts_mod.TTS()               # 本地兜底引擎始终构造（离线可用）
        provider = db.get_settings().get("tts_provider", "cloud")
        self.tts = self._local_tts
        if provider == "cloud":
            try:
                from . import tts_cloud as cloud_tts_mod
                self.tts = cloud_tts_mod.CloudStreamTTS()
            except Exception as e:
                audit.log("voice_error", action="tts_provider_fallback",
                          provider="cloud", error=str(e))
                self.tts = self._local_tts
                self.sub_status["tts_fallback"] = "云端不可用，已回退本地：{}".format(str(e)[:120])
        self.sub_status["tts"] = self.tts.provider    # /api/voice/status modules 显示实际引擎
```

**1d. `_handle_speech` 尾部替换**（原 288-303 行 chat_fn/chat_new/_speak 段）：

```python
        chat_uid = self.current_uid or "elder_001"
        # I-1：声纹识别切换了用户（或首次识别出用户）→ 广播 user_changed（保留原逻辑）
        if self.current_uid and self.current_uid != prev_uid:
            self._publish("user_changed", uid=chat_uid,
                          locked=bool(self.locked_uid), source="voiceprint")
        self._publish("voice_state", state="recognized", uid=chat_uid, text=text)
        # 流式问答：应答线程消费 chat_stream → 逐字上屏(chat_partial) + 句级 TTS 播放
        self._start_answer(chat_uid, text, dict(settings))
```

（注意原代码中 `vote/uid` 切换与 `prev_uid` 段保留在 `_start_answer` 调用之前。）

**1e. 新增应答编排方法**（替换原 `_speak`；`_speak` 删除）：

```python
    # ---- 流式问答编排（句级 TTS）----
    def _start_answer(self, uid, user_text, settings):
        """启动应答线程（单活跃）。回复期间若老人插话 → 主循环打断路径置 _abort。"""
        if self._answering:
            self._abort.set()               # 上一轮异常滞留：请求中止，本轮接手
        self._abort.clear()
        self._answering = True
        t = threading.Thread(target=self._answer, args=(uid, user_text, settings),
                             daemon=True, name="voice-answer")
        t.start()

    def _answer(self, uid, user_text, settings):
        try:
            assistant = self._consume_reply(uid, user_text, settings)
            self._publish("chat_new", uid=uid, user=user_text, assistant=assistant)
            if self.post_turn_fn:
                try:
                    self.post_turn_fn(uid, user_text, assistant)
                except Exception:
                    pass
        except Exception as e:
            audit.log("voice_error", action="tts_answer", error=str(e)[:200])
        finally:
            self._answering = False
            self._abort.clear()
            if self.sink is not None:
                self.sink.end_of_stream()   # 自然播完：队列清空后收流

    def _consume_reply(self, uid, user_text, settings):
        """同步消费 chat_stream 事件流（单测直接调用）：
        content → 广播 chat_partial(逐字上屏) → 分句缓冲 → 完整句合成入队（仅 speak 模式）；
        done → flush 尾句。返回完整 assistant 文本。"""
        speak = bool(settings.get("tts_enabled", True)) and self.tts is not None
        buf = tts_buffer.SentenceBuffer()
        full = []
        started = threading.Event()        # 首句已入队（确保仅触发一次 speaking/start_speaking）

        def enqueue_sentence(sent):
            clean = tts_mod.sanitize_tts_text(sent)
            if not clean:
                return
            if speak and self.session.state != session_mod.State.SPEAKING \
                    and self._abort.is_set():
                return                    # 被打断：不再发声
            chunks = []
            try:
                chunks = list(self.tts.synthesize_chunks(clean))
            except Exception as e:
                audit.log("voice_error", action="tts_cloud_sentence", error=str(e)[:160])
                if self.tts is not self._local_tts and self._local_tts is not None:
                    try:                  # 云端句失败 → 本地兜底，不中断播报
                        chunks = list(self._local_tts.synthesize_chunks(clean))
                    except Exception:
                        chunks = []
                else:
                    chunks = []
            if not chunks:
                return
            if speak:
                if not started.is_set():
                    started.set()
                    self._speak_started = time.monotonic()
                    self._speak_ended_at = None
                    if not self.session.start_speaking():
                        return            # 状态机未在 LISTENING（已被打断）：放弃首句
                    self._publish("voice_state", state="speaking")
                if self.session.state != session_mod.State.SPEAKING:
                    return                # 已被打断（后续句）：不再入队
                self.sink.enqueue(np.concatenate(chunks))
                audit.log("voice_tts", text=clean[:80], provider=self.tts.provider,
                          ms=len(clean) * 250)   # 播报时长粗估（中文 ~4字/秒，仅日志参考）

        for ev in self.stream_fn(uid, user_text):
            if self._abort.is_set():
                break
            t = ev.get("type")
            if t == "content":
                delta = ev.get("content") or ""
                if delta:
                    self._publish("chat_partial", uid=uid, delta=delta)
                    full.append(delta)
                    for sent in buf.feed(delta):
                        enqueue_sentence(sent)
            elif t == "done":
                break
        tail = buf.flush()
        if tail:
            enqueue_sentence(tail)
        return "".join(full)
```

**1f. `_step` SPEAKING 分支播报结束判定**（第 181 行原式 `if self.sink.is_done() and self.session.finish_speaking():` 改为）：

```python
            if self.sink.is_done() and not self._answering \
                    and self.session.finish_speaking():
```

并在该分支内（finish 成功后、原回声静音窗起点设置处）追加一行（若打断路径已置 `_speak_ended_at=None`，此处自然结束才设置）：

```python
                # 流式应答线程可能仍在收尾（无句子可播时 _answering 很快复位）；
                # 结束判定已含 not _answering —— 见上
```

（改动仅为第 181 行条件 + 保持原有块内逻辑；若实现后发现"无句子可播但已 publish speaking"的竞态（首句入队与 _answering 复位间隔），在 finish 前补 `self.sink.end_of_stream()` 兜底调用——_answer finally 已调用，无需重复。）

**1g. 打断路径**（原 170-180 行 SPEAKING 插话分支）在 `self.sink.stop()` 后追加：

```python
                self._abort.set()          # 通知应答线程停止后续合成/入队
```

- [ ] **步骤 2：voice_api.py 接线**

**2a. 删除 `_chat_fn`（103-111 行）与 `_fn`，改为：**

```python
def _stream_fn(client, model):
    def _fn(uid, text):
        settings = db.get_settings()
        return chat.chat_stream(client, model, uid, text, "auto", settings)
    return _fn
```

**2b. `start_voice` 中构造改为：**

```python
    _worker = worker_mod.VoiceWorker(_stream_fn(client, model), post_turn_fn,
                                     publish_fn=bus.publish)
```

**2c. `get_status`（136-149 行）三个返回分支都加 `"tts_provider": settings.get("tts_provider", "cloud")`**（照 asr_provider 行加）。

- [ ] **步骤 3：改写 test_worker_events.py**

完整替换为（保留原语义用例 + 新增流式用例）：

```python
# -*- coding: utf-8 -*-
"""VoiceWorker 事件广播测试：wake / recognized / chat_partial / chat_new / speaking / idle。"""
import threading

import numpy as np

from LLM.voice import worker as worker_mod
from LLM.voice import session as session_mod
from LLM.voice import tts_buffer


def _make_worker(stream_fn=None, post_turn_fn=None):
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    w = worker_mod.VoiceWorker(
        stream_fn=stream_fn or (lambda uid, text: iter([])),
        post_turn_fn=post_turn_fn or (lambda uid, user, assistant: None),
        publish_fn=pub,
    )
    return w, events


def _silence_audit(monkeypatch):
    monkeypatch.setattr(worker_mod.audit, "log", lambda event, **kw: None)


def _install_stubs(w):
    """注入可测假件：会话/引擎/声卡/分句器全部 stub。"""
    w.session = session_mod.Session()
    w.session.wake()
    w._local_tts = w.tts = type("Tts", (), {
        "provider": "local",
        "synthesize_chunks": lambda self, t: (np.zeros(160, dtype=np.float32) for _ in (1,)),
    })()
    w.sink = type("Sink", (), {"enqueue": lambda self, s: None,
                               "stop": lambda self: None,
                               "is_done": lambda self: True,
                               "end_of_stream": lambda self: None})()


def test_wake_publish(monkeypatch):
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None})()
    w.kws = type("Kws", (), {"accept": lambda self, c: "小机器人"})()
    w._step({})
    assert ("voice_state", {"state": "listening"}) in events


def test_consume_reply_streams_partial_and_chat_new(monkeypatch):
    """content 逐段广播 chat_partial；整句切出后触发 speaking；chat_new 收尾。"""
    _silence_audit(monkeypatch)
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    def stream_fn(uid, text):
        return iter([
            {"type": "content", "content": "好的，"},
            {"type": "content", "content": "我记住了。"},
            {"type": "done", "assistant": "好的，我记住了。"},
        ])

    w = worker_mod.VoiceWorker(stream_fn=stream_fn,
                               post_turn_fn=lambda uid, u, a: None,
                               publish_fn=pub)
    _install_stubs(w)
    w.current_uid = "elder_002"
    assistant = w._consume_reply("elder_002", "今天吃药了吗", {"tts_enabled": True})
    assert assistant == "好的，我记住了。"
    partials = [p for ev, p in events if ev == "chat_partial"]
    assert "".join(p["delta"] for p in partials) == "好的，我记住了。"
    # 分句后整句已入队：本 stub 里首句 enqueue 即置 speaking（enqueue 无副作用，
    # 故 speaking 触发依赖真 sink；此处断言 chat_new 与 recognized 由外层负责）
    assert ("chat_new", {"uid": "elder_002", "user": "今天吃药了吗",
                         "assistant": "好的，我记住了。"}) not in events  # chat_new 由 _answer 层发
    # speaking 由 _consume_reply 内部触发（started 事件置位后才 publish）
    speaking = [p for ev, p in events if ev == "voice_state" and p["state"] == "speaking"]
    assert speaking


def test_speech_publishes_recognized(monkeypatch):
    """语音识别整句 → recognized + user_changed（不做 LLM 段同步断言）。"""
    _silence_audit(monkeypatch)
    events = []

    def pub(ev, **payload):
        events.append((ev, payload))

    w = worker_mod.VoiceWorker(stream_fn=lambda uid, t: iter([]),
                               publish_fn=pub)
    w.session = session_mod.Session()
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "我今天有点头晕"})()
    w.fusion = type(
        "Fusion", (),
        {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": "elder_002", "confidence": 0.9})()},
    )()
    _install_stubs(w)
    w._handle_speech("seg", {"asr_enabled": True, "tts_enabled": True})
    # recognized 同步广播；chat_new 由应答线程异步发（此处不等待线程）
    assert ("voice_state",
            {"state": "recognized", "uid": "elder_002", "text": "我今天有点头晕"}) in events
    assert ("user_changed",
            {"uid": "elder_002", "locked": False, "source": "voiceprint"}) in events


def test_speaking_done_publishes_listening(monkeypatch):
    """播报完成（is_done 且应答线程已结束）→ 回 LISTENING（前端"正在听…"）。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.session.start_speaking()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "is_speech_now": lambda self: False})()
    w.sink = type("Sink", (), {"is_done": lambda self: True,
                               "end_of_stream": lambda self: None})()
    w._answering = False
    w._step({})
    assert ("voice_state", {"state": "listening"}) in events
    assert w.session.state == session_mod.State.LISTENING


def test_answering_true_keeps_speaking(monkeypatch):
    """队列瞬时空但应答线程仍活跃 → 不得误判播报结束。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.session.start_speaking()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "is_speech_now": lambda self: False})()
    w.sink = type("Sink", (), {"is_done": lambda self: True,
                               "end_of_stream": lambda self: None})()
    w._answering = True
    w._step({})
    assert w.session.state == session_mod.State.SPEAKING


def test_listen_timeout_publishes_idle(monkeypatch):
    """30s 免唤醒窗口超时 → 回待机（IDLE）。"""
    _silence_audit(monkeypatch)
    w, events = _make_worker()
    w.session = session_mod.Session()
    w.session.wake()
    w.src = type("Src", (), {"read": lambda self: b"\x00" * 320})()
    w.vad = type("Vad", (), {"accept": lambda self, c: None,
                             "pop_speech": lambda self: None})()
    w.sink = type("Sink", (), {"is_done": lambda self: False,
                               "end_of_stream": lambda self: None})()
    fake_clock = [100.0]

    class FakeClock:
        @staticmethod
        def __call__():
            return fake_clock[0]

    w.session._clock = FakeClock()
    w.session._last_activity = 50.0
    w._step({})
    assert w.session.state == session_mod.State.IDLE
    assert ("voice_state", {"state": "idle"}) in events


def test_publish_failure_is_silent(monkeypatch):
    """publish_fn 抛异常必须被吞掉，不影响语音主循环。"""
    _silence_audit(monkeypatch)
    w = worker_mod.VoiceWorker(
        stream_fn=lambda uid, t: iter([]),
        post_turn_fn=lambda uid, u, a: None,
        publish_fn=lambda ev, **kw: (_ for _ in ()).throw(RuntimeError("bus down")),
    )
    _install_stubs(w)
    w.asr = type("Asr", (), {"transcribe": lambda self, seg: "测试"})()
    w.fusion = type("Fusion", (),
                    {"resolve": lambda self, seg: type("Vote", (), {"candidate_uid": None, "confidence": 0.1})()})()
    w._consume_reply("elder_001", "测试", {"tts_enabled": True})   # 不应抛异常
```

> 删除旧测试中的 `chat_fn` 引用与 `test_speak_publishes_speaking`（_speak 已删除；speaking 触发路径由 `test_consume_reply_streams_partial_and_chat_new` 覆盖）。`_install_stubs` 的 sink stub 会让 speaking 触发（enqueue 后置位）——若实现里 speaking 在 enqueue 前发布则断言相应调整，以"首句入队成功才 speaking"为准。

- [ ] **步骤 4：跑 worker 测试**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_worker_events.py -q`
预期：`7 passed`（新用例全过；若有断言与实现细节出入，以规格语义为准修正测试或实现——speaking 时机与 chat_partial 顺序不可回退）

- [ ] **步骤 5：全量回归**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q 2>&1 | Select-Object -Last 3`
预期：全绿，数量 ≥ 原 64 + 新增（buffer 8 + audio 3 + cloud 4 已在前置任务计入）。

- [ ] **步骤 6：Commit**

```bash
git add LLM/voice/worker.py LLM/voice_api.py LLM/tests/test_worker_events.py
git commit -m "feat(voice): worker流式问答编排(chat_partial逐字上屏+句级TTS+打断中止)"
```

---

### 任务 6：前端（events.ts / kiosk / admin）

**文件：**
- 修改：`frontend/packages/shared/src/events.ts`（ChatPartialEvent + BusEvent + KNOWN_TYPES）
- 修改：`frontend/packages/kiosk/src/App.vue`（recognized/chat_partial/chat_new 处理 + TTS 切换按钮）
- 修改：`frontend/packages/kiosk/src/components/SettingsSheet.vue`（合成引擎单选组）
- 修改：`frontend/packages/admin/src/pages/VoiceStatusPage.vue`（tts_provider/modules.tts 显示）

- [ ] **步骤 1：events.ts 加事件类型**

在 `ChatNewEvent` 定义后加：

```ts
export interface ChatPartialEvent {
  type: "chat_partial";
  uid: string;
  delta: string;   // 助手回复实时增量（LLM content delta），kiosk 打字机式追加
}
```

`BusEvent` 联合加 `| ChatPartialEvent`；`KNOWN_TYPES` 数组加 `"chat_partial"`。

- [ ] **步骤 2：kiosk App.vue 改事件处理**

`onEvent` 中替换 `voice_state recognized` 分支与 `chat_new` 分支、新增 `chat_partial` 分支：

```ts
function onEvent(ev: BusEvent) {
  if (ev.type === "voice_state") {
    if (ev.state === "asr_partial") { liveText.value = ev.text ?? ""; return; }
    state.value = ev.state;
    if (ev.uid) uid.value = ev.uid;
    if (ev.state === "recognized" && ev.text) {
      // 语音问答开始：老人语句入气泡 + assistant 占位（等待 chat_partial 渐进）
      liveText.value = "";
      messages.value.push({ role: "user", content: ev.text, uid: uid.value ?? undefined });
      messages.value.push({ role: "assistant", content: "" });
    } else if (ev.state === "idle") {
      liveText.value = "";
    }
    return;
  }
  if (ev.type === "chat_partial") {
    const last = messages.value[messages.value.length - 1];
    if (last && last.role === "assistant") last.content += ev.delta;
    else messages.value.push({ role: "assistant", content: ev.delta });
    return;
  }
  if (ev.type === "chat_new") {
    liveText.value = "";
    const msgs = messages.value;
    const prev = msgs[msgs.length - 2];
    const last = msgs[msgs.length - 1];
    if (last?.role === "assistant" && prev?.role === "user" && prev.content === ev.user) {
      last.content = ev.assistant;      // 渐进气泡覆盖为终稿（防 SSE 丢帧）
    } else {
      msgs.push({ role: "user", content: ev.user, uid: ev.uid });
      msgs.push({ role: "assistant", content: ev.assistant });
    }
    return;
  }
  if (ev.type === "voice_status" && ev.status === "degraded") {
    state.value = "unavailable";
    liveText.value = "";
  }
  if (ev.type === "reminder") reminder.value = ev;
  if (ev.type === "user_changed") {
    uid.value = ev.uid;
    locked.value = ev.locked;
  }
}
```

注意：原 `recognized` 分支的"定格识别文本"行为删除（进气泡）；`chat_new` 的去重键 = 倒数第二条 user 气泡内容与 `ev.user` 一致。

**步骤 2b. 加 TTS 快捷切换**（模板底部按钮区，`asr-toggle` 按钮旁）：

```html
<button class="settings-btn tts-toggle" @click="toggleTtsProvider">
  合成：{{ ttsProvider === "cloud" ? "云端" : "本地" }}
</button>
```

script 内：

```ts
const ttsProvider = ref("cloud");   // local | cloud（合成引擎，重启服务后生效）

async function loadTtsProvider() {
  try {
    const res = await fetch("/api/settings");
    const body = await res.json();
    ttsProvider.value = body.settings?.tts_provider ?? "cloud";
  } catch { /* 忽略，保留默认 */ }
}

async function toggleTtsProvider() {
  const next = ttsProvider.value === "cloud" ? "local" : "cloud";
  ttsProvider.value = next;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { tts_provider: next } }),
    });
  } catch { /* 保存失败也提示 */ }
  alert(`合成引擎已切换为${next === "cloud" ? "云端（火山）" : "本地"}，重启服务后生效`);
}
```

`onMounted` 里加 `loadTtsProvider();`；样式 `.tts-toggle { background: #1d4ed8; }`（或复用 asr-toggle 类）。

- [ ] **步骤 3：SettingsSheet.vue 加合成引擎单选组**

在"识别引擎"组后追加同构组：

```html
<div class="group">
  <div class="group-title">合成引擎（重启服务后生效）</div>
  <label>
    <input type="radio" name="tts_provider" value="local"
           :checked="settings.tts_provider !== 'cloud'"
           @change="save('tts_provider', 'local')" />
    本地合成（离线）
  </label>
  <label>
    <input type="radio" name="tts_provider" value="cloud"
           :checked="settings.tts_provider === 'cloud'"
           @change="save('tts_provider', 'cloud')" />
    云端合成（火山）
  </label>
</div>
```

- [ ] **步骤 4：admin VoiceStatusPage.vue 加状态行**

在 `<p v-if="status.reason">` 后加：

```html
<p>识别引擎：{{ status.asr_provider ?? "-" }} ｜ 合成引擎：{{ status.tts_provider ?? "-" }}
   ｜ 实际 TTS：{{ status.modules?.tts ?? "-" }}<span v-if="status.modules?.tts_fallback">（已回退本地）</span></p>
```

- [ ] **步骤 5：类型检查与构建**

运行：`cd frontend && pnpm -w exec vue-tsc --noEmit -p packages/kiosk/tsconfig.json`（如无 vue-tsc 则以 `pnpm dev:admin`/`dev:kiosk` 编译日志为准）
预期：无类型错误（`pnpm install` 已在先；shared 改动后 admin/kiosk 引用的 `BusEvent` 联合自动包含新事件）

- [ ] **步骤 6：Commit**

```bash
git add frontend/packages/shared/src/events.ts frontend/packages/kiosk/src/App.vue frontend/packages/kiosk/src/components/SettingsSheet.vue frontend/packages/admin/src/pages/VoiceStatusPage.vue
git commit -m "feat(web): chat_partial事件+语音气泡渐进+合成引擎切换(kiosk/admin)"
```

---

### 任务 7：收尾（回归 / log / 构建 / 记忆）

**文件：**
- 修改：`docs/log.md`（2026-09-07 追加条目）
- 修改：`.env`（若任务 0 未补 VOLC_TTS_SPEAKER，此处提醒用户）
- 运行验证（不做代码改动）

- [ ] **步骤 1：后端全量回归**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests -q 2>&1 | Select-Object -Last 3`
预期：全绿。

- [ ] **步骤 2：后端冒烟（本机，可无音频环境）**

运行（项目根，后台）：
```powershell
.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
```
然后：`Invoke-RestMethod http://127.0.0.1:8000/api/voice/status | ConvertTo-Json`
预期：`status` 为 running 或 degraded（无音频环境）+ `modules.tts` 显示实际引擎（local，因本机未配 key 或已回退）；`tts_provider` 字段存在。
再验证 `POST /api/settings {"settings":{"tts_provider":"local"}}` 可持久化、`GET /api/settings` 返回新值。冒烟后 kill 进程。

- [ ] **步骤 3：构建前端产物（供生产挂载）**

运行：`powershell -ExecutionPolicy Bypass -File scripts/build_frontend.ps1`（项目根）
预期：admin/kiosk dist 生成；无 TS/构建错误。

- [ ] **步骤 4：docs/log.md 追加条目**

在 `docs/log.md` 2026-09-07 相关小节后追加（摘要：云端豆包 TTS 2.0 + 句级流式播报落地；SentenceBuffer/AudioSink 队列/worker 应答线程/chat_partial 事件/tts_provider 切换；任务 0 实测结论；真机复测清单：边说边播首句延迟、气泡渐进、打断、引擎切换重启生效、云端故障回退）。

- [ ] **步骤 5：Commit**

```bash
git add docs/log.md
git commit -m "docs: 云端TTS+句级流式播报落地记录(2026-09-07)"
```

- [ ] **步骤 6：真机复测（用户侧，板卡）**

复测清单（写入 log 条目）：①语音问答边说边播、句间连续无爆音；②kiosk 气泡逐字增长且播完后与历史一致；③播放期与合成期插话打断都生效、无自问自答复发；④kiosk/admin 切本地/云端 → 重启后端 → /api/voice/status modules.tts 变化；⑤断网/错 key 时云端回退本地不崩。
板卡部署还需：.env 补 `VOLC_TTS_SPEAKER`，重建 kiosk dist（同 ASR 那轮）。

- [ ] **步骤 7：更新记忆**

更新 Robot 项目记忆：云端 TTS 功能落地状态（todo→stale 的判定以真机复测通过为准）；若 .env 仍缺音色 ID，记入待办提醒用户。
