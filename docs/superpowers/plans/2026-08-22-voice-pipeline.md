# 语音交互子系统实现计划（ASR / TTS / VAD / KWS / 声纹）

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在 LLM/ 后端实现"语音进 → 文字 → 大模型 → 语音播报"完整回路，含声纹验证（1:1）+ 识别（1:N）双能力，且音频模块掉线绝不拖垮主程序。

**架构：** 语音逻辑放 `LLM/voice/` 纯逻辑子包（不依赖 FastAPI），由 `LLM/voice_api.py` + `server.py` 内嵌挂载。后台线程 `worker.py` 编排 采集→VAD→(KWS|ASR)→声纹→LLM→TTS，整循环 try/except + 心跳状态 + 退避重连实现稳健性。

**技术栈：** sherpa-onnx 1.13.x（VAD/KWS/ASR/TTS）、3D-Speaker ERes2NetV2 via modelscope（声纹）、sounddevice（音频）、torch CPU、pytest。

**规格依据：** `docs/superpowers/specs/2026-08-22-voice-pipeline-design.md`

**关键实现约定（来自调研，勿违反）：**
1. `accept_waveform` 签名不一致：VAD 是 `accept_waveform(samples)`（无采样率）；KWS/ASR 是 `accept_waveform(sample_rate, samples)`（采样率在前）。
2. `vad.front.samples` 是 **Python list**，必须 `np.asarray(x, dtype=np.float32)` 转换；`vad.front.start` 单位是样本数。
3. `OnlineRecognizer` 用工厂方法 `from_transducer(...)`，不是 `OnlineRecognizerConfig(...)`。
4. TTS 必须带 `rule_fsts`（phone/date/number.fst），否则日期数字读错。
5. KWS 命中后立即 `reset_stream`；ASR 结尾补 0.66s 尾静音 + `input_finished()`。
6. 声纹 embedding 192 维；官方阈值 0.360，本项目"精准优先"默认 0.55（`spk_threshold` 可调）。

**v1 范围说明（有意简化，规格已覆盖）：**
- ASR 对"VAD 切出的完整语音段"转写（内部仍是流式 zipformer 模型），不做逐字增量推送 UI；增量出字留作 2s 延迟优化项。
- 唤醒词检测用固定 `kws_keywords.txt`（拼音），"改唤醒词"需同步改该文件（自动拼音映射超出本期）。
- 打断（barge-in）机制完整实现，但笔记本麦克风会收到扬声器回声，**开发期请戴耳机测试打断**；AEC 回声消除留到板卡端（麦克风阵列硬件）。

---

## 文件结构总览

**创建（新文件）：**
- `LLM/voice/__init__.py` — 子包标记（空）
- `LLM/voice/config.py` — 语音路径/音频/VAD/KWS 常量
- `LLM/voice/session.py` — 会话状态机（纯逻辑，可注入时钟）
- `LLM/voice/audio.py` — 采集/播放抽象（sounddevice，唯一接触声卡）
- `LLM/voice/vad.py` — silero-vad 封装
- `LLM/voice/kws.py` — 关键词唤醒封装
- `LLM/voice/asr.py` — 流式识别封装
- `LLM/voice/tts.py` — 合成封装
- `LLM/voice/speaker.py` — 声纹（注册/验证/识别，含纯函数 `cosine`/`classify`）
- `LLM/voice/identity.py` — 身份融合层（`IdentityVote`/`VoiceprintSource`/`VoiceprintOnlyFusion`/`effective_uid`）
- `LLM/voice/worker.py` — 编排线程 + 心跳 + 崩溃兜底
- `LLM/voice/kws_keywords.txt` — 唤醒词拼音映射
- `LLM/voice_api.py` — 语音服务挂载逻辑（start_voice/status/enroll/list）
- `LLM/tests/test_session.py`、`LLM/tests/test_speaker_math.py`、`LLM/tests/test_identity.py`
- `scripts/fetch_voice_models.py` — 下载/解压模型
- `scripts/voice_selftest.py` — 离线自检（无麦克风）

**修改（现有文件）：**
- `LLM/conf.py` — `DEFAULT_SETTINGS` 新增 4 个键 + 改 asr/tts 默认值
- `LLM/server.py` — 导入 voice_api、lifespan 启停、新增 3 个语音端点
- `requirement.txt` — 固化依赖
- `.gitignore` — 排除 `.research_sherpa/`、`LLM/models/`

---

## 任务 0：依赖安装 + 模型下载 + 工程准备

**文件：**
- 修改：`requirement.txt`、`.gitignore`
- 创建：`scripts/fetch_voice_models.py`

- [ ] **步骤 1：安装 Python 依赖**

运行（项目根目录）：
```powershell
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install sherpa-onnx sounddevice modelscope pytest numpy
.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```
预期：全部成功。验证：`.venv\Scripts\python.exe -c "import sherpa_onnx, sounddevice, modelscope, torch; print(sherpa_onnx.__version__)"` 打印版本号。

- [ ] **步骤 2：固化依赖到 requirement.txt**

写入 `requirement.txt`（完整替换）：
```
sherpa-onnx
sounddevice
numpy
modelscope
pytest
```
（`torch`/`torchaudio` 走 CPU index 单独装，不写进 requirement.txt；在文件末尾加注释行说明：`# torch/torchaudio: pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu`）

- [ ] **步骤 3：写模型下载脚本**

创建 `scripts/fetch_voice_models.py`：
```python
# -*- coding: utf-8 -*-
r"""下载并解压语音模型到 LLM/models/voice/。
用法：.venv\Scripts\python.exe scripts\fetch_voice_models.py
"""
import sys, tarfile, urllib.request, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "LLM" / "models" / "voice"
DEST.mkdir(parents=True, exist_ok=True)

URLS = {
    "silero_vad.onnx": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "kws.tar.bz2": "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2",
    "asr.tar.bz2": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23.tar.bz2",
    "tts.tar.bz2": "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-vits-zh-ll.tar.bz2",
}

def main():
    for key, url in URLS.items():
        out = DEST / key
        if key.endswith(".tar.bz2"):
            # 解压后删除压缩包；解压产物目录名与 config.py 中一致
            marker = DEST / ("." + key.replace(".tar.bz2", "") + ".done")
            if marker.exists():
                print(f"[skip] {key}")
                continue
            print(f"[download] {url}")
            with urllib.request.urlopen(url) as r, open(out, "wb") as f:
                shutil.copyfileobj(r, f)
            print(f"[extract] {key}")
            with tarfile.open(out) as t:
                t.extractall(DEST)
            out.unlink()
            marker.write_text("done")
        else:
            if out.exists():
                print(f"[skip] {key}")
                continue
            print(f"[download] {url}")
            with urllib.request.urlopen(url) as r, open(out, "wb") as f:
                shutil.copyfileobj(r, f)
    print("done")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **步骤 4：下载并解压模型**

运行：`.venv\Scripts\python.exe scripts\fetch_voice_models.py`
预期：下载 silero_vad.onnx（0.63MB）+ 3 个 tar.bz2（约 215MB），解压出目录：
- `LLM/models/voice/silero_vad.onnx`
- `LLM/models/voice/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/`（含 tokens.txt、encoder/decoder/joiner-epoch-12-avg-2-chunk-16-left-64.onnx）
- `LLM/models/voice/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/`（含 tokens.txt、encoder/decoder/joiner-epoch-99-avg-1.onnx）
- `LLM/models/voice/sherpa-onnx-vits-zh-ll/`（含 model.onnx、lexicon.txt、tokens.txt、phone/date/number.fst）

（3D-Speaker 的 ckpt 不在这一步下载，modelscope 首次运行会自动拉到 `~/.cache/modelscope`，见任务 8。）

- [ ] **步骤 5：更新 .gitignore**

在 `.gitignore` 末尾追加：
```
# 语音：运行时模型与调研残留不入库
LLM/models/
.research_sherpa/
```

- [ ] **步骤 6：Commit**

```bash
git add requirement.txt .gitignore scripts/fetch_voice_models.py
git commit -m "chore: 语音依赖固件 + 模型下载脚本 + gitignore"
```

---

## 任务 1：语音配置模块 + 子包骨架

**文件：**
- 创建：`LLM/voice/__init__.py`、`LLM/voice/config.py`

- [ ] **步骤 1：创建子包标记**

创建 `LLM/voice/__init__.py`（空文件，仅注释）：
```python
# -*- coding: utf-8 -*-
r"""语音链路子包：ASR / TTS / VAD / KWS / 声纹，纯逻辑，不依赖 FastAPI。"""
```

- [ ] **步骤 2：写 config.py**

创建 `LLM/voice/config.py`：
```python
# -*- coding: utf-8 -*-
r"""语音链路专属配置：模型路径、音频/VAD/KWS 参数。纯常量。"""
from ..conf import BASE_DIR, DATA_DIR

# 目录
MODEL_DIR = BASE_DIR / "LLM" / "models" / "voice"
SPEAKER_DIR = DATA_DIR / "speakers"
SPEAKER_DIR.mkdir(parents=True, exist_ok=True)

# 音频
SAMPLE_RATE = 16000
BLOCK_MS = 100
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000   # 1600 样本/块

# sherpa-onnx 模型路径（目录名 = 解压产物名，勿改）
VAD_MODEL = MODEL_DIR / "silero_vad.onnx"
KWS_DIR = MODEL_DIR / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
ASR_DIR = MODEL_DIR / "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
TTS_DIR = MODEL_DIR / "sherpa-onnx-vits-zh-ll"

# 声纹（3D-Speaker ERes2NetV2，经 modelscope）
SPK_MODEL_ID = "iic/speech_eres2netv2_sv_zh-cn_16k-common"
SPK_EMBED_DIM = 192
SPK_THRESHOLD = 0.55          # 官方基线 0.360，精准优先上调；运行时可被 settings 覆盖

# VAD（silero）
VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE_S = 0.5
VAD_MIN_SPEECH_S = 0.25
VAD_MAX_SPEECH_S = 20.0

# 唤醒（kws）
KWS_THRESHOLD = 0.25

# 打断：播报开始后忽略 VAD 的宽限时长（防开场误判）
BARGE_IN_GRACE_S = 0.3
# 音频设备掉线：连续失败多少次判定为 degraded（仍持续重试，不退出线程）
MAX_RECONNECT = 5
```

- [ ] **步骤 3：冒烟验证可导入**

运行：`.venv\Scripts\python.exe -c "from LLM.voice import config; print(config.SAMPLE_RATE, config.KWS_DIR)"`
预期：打印 `16000` 和 KWS 目录路径，无异常。

- [ ] **步骤 4：Commit**

```bash
git add LLM/voice/__init__.py LLM/voice/config.py
git commit -m "feat(voice): 语音子包骨架与配置模块"
```

---

## 任务 2：会话状态机（TDD）

**文件：**
- 创建：`LLM/voice/session.py`
- 测试：`LLM/tests/test_session.py`

- [ ] **步骤 1：写失败测试**

创建 `LLM/tests/test_session.py`：
```python
# -*- coding: utf-8 -*-
from LLM.voice.session import Session, State

def test_wake_from_idle():
    s = Session(handsfree_sec=30)
    assert s.state == State.IDLE
    assert s.wake() == State.LISTENING

def test_wake_ignored_outside_idle():
    s = Session()
    s.wake()
    s.start_speaking()
    assert s.wake() == State.SPEAKING

def test_speak_requires_listening():
    s = Session()
    assert s.start_speaking() is False
    s.wake()
    assert s.start_speaking() is True
    assert s.state == State.SPEAKING

def test_finish_speaking_returns_to_listening():
    s = Session(); s.wake(); s.start_speaking()
    assert s.finish_speaking() is True
    assert s.state == State.LISTENING

def test_barge_in_from_speaking():
    s = Session(); s.wake(); s.start_speaking()
    assert s.barge_in() is True
    assert s.state == State.LISTENING

def test_timeout_returns_to_idle():
    clock = [0.0]
    s = Session(handsfree_sec=30, clock=lambda: clock[0])
    s.wake()
    clock[0] = 31.0
    assert s.expire() == State.IDLE

def test_speech_resets_timeout():
    clock = [0.0]
    s = Session(handsfree_sec=30, clock=lambda: clock[0])
    s.wake()
    clock[0] = 20.0
    s.note_speech()
    clock[0] = 40.0          # 距上次 speech 20s
    assert s.expire() == State.LISTENING
    clock[0] = 51.0          # 距上次 speech 31s
    assert s.expire() == State.IDLE
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session.py -v`
预期：FAIL，报 `ModuleNotFoundError: No module named 'LLM.voice.session'`。

- [ ] **步骤 3：实现 session.py**

创建 `LLM/voice/session.py`：
```python
# -*- coding: utf-8 -*-
r"""会话状态机：IDLE / LISTENING / SPEAKING。纯逻辑，可注入时钟便于测试。"""
from enum import Enum
import time


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"


class Session:
    def __init__(self, handsfree_sec: float = 30.0, clock=time.monotonic):
        self.state = State.IDLE
        self.handsfree_sec = handsfree_sec
        self._clock = clock
        self._last_activity = 0.0

    def _touch(self):
        self._last_activity = self._clock()

    def wake(self) -> State:
        if self.state == State.IDLE:
            self.state = State.LISTENING
            self._touch()
        return self.state

    def note_speech(self) -> None:
        if self.state == State.LISTENING:
            self._touch()

    def start_speaking(self) -> bool:
        if self.state == State.LISTENING:
            self.state = State.SPEAKING
            return True
        return False

    def finish_speaking(self) -> bool:
        if self.state == State.SPEAKING:
            self.state = State.LISTENING
            self._touch()
            return True
        return False

    def barge_in(self) -> bool:
        if self.state == State.SPEAKING:
            self.state = State.LISTENING
            self._touch()
            return True
        return False

    def expire(self) -> State:
        if self.state == State.LISTENING and (self._clock() - self._last_activity) > self.handsfree_sec:
            self.state = State.IDLE
        return self.state
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_session.py -v`
预期：7 个用例全部 PASS。

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/session.py LLM/tests/test_session.py
git commit -m "feat(voice): 会话状态机（唤醒/免唤醒窗口/打断/超时）"
```

---

## 任务 3：音频采集/播放抽象

**文件：**
- 创建：`LLM/voice/audio.py`

- [ ] **步骤 1：先确认音频设备可用**

运行：`.venv\Scripts\python.exe -c "import sounddevice as sd; print(sd.query_devices())"`
预期：列出至少一个输入设备和一个输出设备（笔记本自带）。记住默认设备名，供后续排障。

- [ ] **步骤 2：实现 audio.py**

创建 `LLM/voice/audio.py`：
```python
# -*- coding: utf-8 -*-
r"""音频采集/播放抽象（sounddevice）。唯一接触声卡的模块。"""
import threading
import numpy as np
import sounddevice as sd

from . import config


class AudioSource:
    """16k 单声道采集。read() 阻塞取一块；设备掉线时抛 PortAudioError。"""

    def __init__(self, sample_rate=config.SAMPLE_RATE, block_samples=config.BLOCK_SAMPLES):
        self.sample_rate = sample_rate
        self.block_samples = block_samples
        self._stream = None

    def start(self):
        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            blocksize=self.block_samples)
        self._stream.start()

    def read(self) -> np.ndarray:
        data, _ = self._stream.read(self.block_samples)
        return data[:, 0].astype(np.float32, copy=False)

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class AudioSink:
    """播放：独立线程写数据，可从外部 stop() 打断（barge-in）。"""

    def __init__(self):
        self._stream = None
        self._lock = threading.Lock()
        self._thread = None

    def play(self, samples: np.ndarray, sample_rate: int):
        self.stop()
        with self._lock:
            self._stream = sd.OutputStream(
                samplerate=sample_rate, channels=1, dtype="float32")
            self._stream.start()
        self._thread = threading.Thread(target=self._run, args=(samples,), daemon=True)
        self._thread.start()

    def _run(self, samples):
        try:
            self._stream.write(samples)
        except Exception:
            pass  # 被打断（abort）或设备丢失，静默结束
        finally:
            with self._lock:
                if self._stream is not None:
                    try:
                        self._stream.stop()
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None

    def stop(self):
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.abort()
                except Exception:
                    pass
                self._stream = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def is_done(self) -> bool:
        with self._lock:
            return self._stream is None
```

- [ ] **步骤 3：冒烟：录 1 秒 + 播放静音**

运行（项目根目录）：
```powershell
.venv\Scripts\python.exe -c "import numpy as np, time; from LLM.voice.audio import AudioSource, AudioSink; s=AudioSource(); s.start(); t=time.time(); chunks=[]; [chunks.append(s.read()) for _ in range(10)]; s.stop(); print('录到', sum(c.shape[0] for c in chunks), '样本，耗时', round(time.time()-t,2), 's'); AudioSink().play(np.zeros(16000, dtype=np.float32), 16000)"
```
预期：打印约 16000 样本（1 秒），无异常。

- [ ] **步骤 4：Commit**

```bash
git add LLM/voice/audio.py
git commit -m "feat(voice): 音频采集/播放抽象（sounddevice，支持打断）"
```

---

## 任务 4：VAD 封装

**文件：**
- 创建：`LLM/voice/vad.py`

- [ ] **步骤 1：实现 vad.py**

创建 `LLM/voice/vad.py`：
```python
# -*- coding: utf-8 -*-
r"""VAD 封装（sherpa-onnx silero-vad）：喂 PCM，弹完整语音段。"""
import numpy as np
import sherpa_onnx

from . import config


class VAD:
    def __init__(self, model_path=config.VAD_MODEL,
                 threshold=config.VAD_THRESHOLD,
                 min_silence=config.VAD_MIN_SILENCE_S,
                 min_speech=config.VAD_MIN_SPEECH_S,
                 max_speech=config.VAD_MAX_SPEECH_S):
        cfg = sherpa_onnx.VadModelConfig()
        cfg.sample_rate = config.SAMPLE_RATE
        cfg.silero_vad.model = str(model_path)
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = min_speech
        cfg.silero_vad.max_speech_duration = max_speech
        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        self._ws = cfg.silero_vad.window_size

    def accept(self, samples: np.ndarray):
        for i in range(0, len(samples), self._ws):
            self._vad.accept_waveform(samples[i:i + self._ws])

    def pop_speech(self) -> np.ndarray | None:
        """有完整语音段则返回其 PCM（float32 1-D），否则 None。"""
        if self._vad.empty():
            return None
        seg = self._vad.front
        pcm = np.asarray(seg.samples, dtype=np.float32)
        self._vad.pop()
        return pcm

    def is_speech_now(self) -> bool:
        return self._vad.is_speech_detected()

    def flush(self):
        self._vad.flush()

    def reset(self):
        self._vad.reset()
```

- [ ] **步骤 2：冒烟：喂 1 秒静音不崩、无语音段**

运行：`.venv\Scripts\python.exe -c "import numpy as np; from LLM.voice.vad import VAD; v=VAD(); v.accept(np.zeros(16000, dtype=np.float32)); print('pop:', v.pop_speech()); print('is_speech:', v.is_speech_now())"`
预期：`pop: None`，`is_speech: False`（静音无语音段）。

- [ ] **步骤 3：Commit**

```bash
git add LLM/voice/vad.py
git commit -m "feat(voice): VAD 封装（silero-vad 切句）"
```

---

## 任务 5：关键词唤醒封装

**文件：**
- 创建：`LLM/voice/kws.py`、`LLM/voice/kws_keywords.txt`

- [ ] **步骤 1：写唤醒词拼音文件**

创建 `LLM/voice/kws_keywords.txt`（内容如下，`@` 后是命中的关键词）：
```
x iǎo j ī q ì r én @小机器人
```
> 注意：该文件按模型 tokens.txt 的"声母+韵母"切分。实现者务必用 `LLM/models/voice/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/tokens.txt` 核对 `x`/`iǎo`/`j`/`ī`/`q`/`ì`/`r`/`én` 这些 token 是否都在（若韵母拼写不一致以 tokens.txt 为准微调）。

- [ ] **步骤 2：实现 kws.py**

创建 `LLM/voice/kws.py`：
```python
# -*- coding: utf-8 -*-
r"""关键词唤醒（sherpa-onnx kws-zipformer 中文）。"""
from pathlib import Path
import sherpa_onnx

from . import config


class WakeWordDetector:
    def __init__(self, kws_dir=config.KWS_DIR, keywords_file=None):
        if keywords_file is None:
            keywords_file = Path(__file__).with_name("kws_keywords.txt")
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(kws_dir / "tokens.txt"),
            encoder=str(kws_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            decoder=str(kws_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
            joiner=str(kws_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
            keywords_file=str(keywords_file),
            num_threads=2, provider="cpu",
            keywords_threshold=config.KWS_THRESHOLD,
        )
        self._stream = self._kws.create_stream()

    def accept(self, samples: np.ndarray) -> str | None:
        """喂 PCM；命中返回关键词字符串，未命中返回 None。"""
        self._stream.accept_waveform(config.SAMPLE_RATE, samples)
        hit = None
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            r = self._kws.get_result(self._stream)
            if r != "":
                hit = r
                self._kws.reset_stream(self._stream)
        return hit
```

- [ ] **步骤 3：冒烟：喂静音不崩、无命中**

运行：`.venv\Scripts\python.exe -c "import numpy as np; from LLM.voice.kws import WakeWordDetector; k=WakeWordDetector(); print('hit:', k.accept(np.zeros(1600, dtype=np.float32)))"`
预期：`hit: None`（静音不命中，模型加载成功）。

- [ ] **步骤 4：真机冒烟（可选，需麦克风）**

运行：`.venv\Scripts\python.exe -c "from LLM.voice.audio import AudioSource; from LLM.voice.kws import WakeWordDetector; s=AudioSource(); s.start(); k=WakeWordDetector(); print('请喊「小机器人」...'); import time; t=time.time(); hit=None; 
while time.time()-t<8:
    h=k.accept(s.read()); 
    if h: hit=h; break
s.stop(); print('命中:', hit)"`
预期：喊"小机器人"时打印 `命中: 小机器人`（若 8 秒内未命中，检查步骤 1 的拼音切分）。

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/kws.py LLM/voice/kws_keywords.txt
git commit -m "feat(voice): 关键词唤醒封装（中文 zipformer）"
```

---

## 任务 6：流式识别封装

**文件：**
- 创建：`LLM/voice/asr.py`

- [ ] **步骤 1：实现 asr.py**

创建 `LLM/voice/asr.py`：
```python
# -*- coding: utf-8 -*-
r"""中文识别（sherpa-onnx streaming-zipformer-zh-14M）。v1 对完整语音段转写。"""
import numpy as np
import sherpa_onnx

from . import config


class StreamASR:
    def __init__(self, asr_dir=config.ASR_DIR):
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(asr_dir / "tokens.txt"),
            encoder=str(asr_dir / "encoder-epoch-99-avg-1.onnx"),
            decoder=str(asr_dir / "decoder-epoch-99-avg-1.onnx"),
            joiner=str(asr_dir / "joiner-epoch-99-avg-1.onnx"),
            num_threads=2, sample_rate=config.SAMPLE_RATE, feature_dim=80,
            decoding_method="greedy_search", provider="cpu",
        )

    def transcribe(self, samples: np.ndarray) -> str:
        s = self._recognizer.create_stream()
        s.accept_waveform(config.SAMPLE_RATE, samples)
        tail = int(0.66 * config.SAMPLE_RATE)   # 尾静音，促使 decoder 收尾
        s.accept_waveform(config.SAMPLE_RATE, np.zeros(tail, dtype=np.float32))
        s.input_finished()
        while self._recognizer.is_ready(s):
            self._recognizer.decode_stream(s)
        return self._recognizer.get_result(s).strip()
```

- [ ] **步骤 2：冒烟：用 TTS 产物做往返（顺带验证 TTS，见任务 7 后统一跑）**

本任务先只验证模型能加载并转写一段空/静音：
运行：`.venv\Scripts\python.exe -c "import numpy as np; from LLM.voice.asr import StreamASR; a=StreamASR(); print(repr(a.transcribe(np.zeros(16000, dtype=np.float32))))"`
预期：返回空串 `''` 或短文本，无异常（模型加载成功）。

- [ ] **步骤 3：Commit**

```bash
git add LLM/voice/asr.py
git commit -m "feat(voice): 中文流式识别封装（zipformer-zh-14M）"
```

---

## 任务 7：TTS 封装

**文件：**
- 创建：`LLM/voice/tts.py`

- [ ] **步骤 1：实现 tts.py**

创建 `LLM/voice/tts.py`：
```python
# -*- coding: utf-8 -*-
r"""中文 TTS（sherpa-onnx vits-zh-ll，16k 输出）。"""
import numpy as np
import sherpa_onnx

from . import config


class TTS:
    def __init__(self, tts_dir=config.TTS_DIR, sid=2):
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(tts_dir / "model.onnx"),
                    lexicon=str(tts_dir / "lexicon.txt"),
                    tokens=str(tts_dir / "tokens.txt"),
                ),
                provider="cpu", num_threads=2,
            ),
            rule_fsts=",".join(
                str(tts_dir / f) for f in ("phone.fst", "date.fst", "number.fst")),
            max_num_sentences=1,
        )
        assert tts_config.validate()
        self._tts = sherpa_onnx.OfflineTts(tts_config)
        self._sid = sid

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """返回 (float32 1-D samples, sample_rate)。"""
        gen = sherpa_onnx.GenerationConfig()
        gen.sid = self._sid
        gen.speed = 1.0
        audio = self._tts.generate(text, gen)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)
```

- [ ] **步骤 2：冒烟：合成 + 播放 + ASR 往返**

运行：`.venv\Scripts\python.exe -c "from LLM.voice.tts import TTS; from LLM.voice.asr import StreamASR; t=TTS(); s, sr=t.synthesize('你好，欢迎使用养老机器人'); print('samples', s.size, 'sr', sr); a=StreamASR(); print('ASR:', repr(a.transcribe(s)))"`
预期：打印 `samples > 0`、`sr 16000`，ASR 返回近似"你好欢迎使用养老机器人"的非空文本（往返闭环验证 ASR+TTS 均正常）。

- [ ] **步骤 3：Commit**

```bash
git add LLM/voice/tts.py
git commit -m "feat(voice): 中文 TTS 封装（vits-zh-ll，16k）"
```

---

## 任务 8：声纹模块（TDD 纯函数 + 模型冒烟）

**文件：**
- 创建：`LLM/voice/speaker.py`
- 测试：`LLM/tests/test_speaker_math.py`

- [ ] **步骤 1：写失败测试（纯函数，不依赖 torch）**

创建 `LLM/tests/test_speaker_math.py`：
```python
# -*- coding: utf-8 -*-
import numpy as np
from LLM.voice.speaker import cosine, classify

def test_cosine_same_is_one():
    e = np.random.default_rng(0).random(192).astype(np.float32)
    assert abs(cosine(e, e) - 1.0) < 1e-4

def test_cosine_orthogonal_is_zero():
    assert abs(cosine(np.array([1, 0], dtype=np.float32),
                      np.array([0, 1], dtype=np.float32))) < 1e-6

def test_classify_picks_best_above_threshold():
    profiles = {"a": np.array([1, 0, 0], dtype=np.float32),
                "b": np.array([0, 1, 0], dtype=np.float32)}
    q = np.array([0.9, 0.2, 0.1], dtype=np.float32)
    uid, score = classify(q, profiles, threshold=0.5)
    assert uid == "a" and score > 0.5

def test_classify_rejects_below_threshold():
    profiles = {"a": np.array([1, 0], dtype=np.float32)}
    uid, score = classify(np.array([0.0, 1.0], dtype=np.float32), profiles, 0.5)
    assert uid is None

def test_classify_empty_profiles():
    uid, score = classify(np.array([1, 0], dtype=np.float32), {}, 0.5)
    assert uid is None and score == 0.0
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_speaker_math.py -v`
预期：FAIL，`ModuleNotFoundError`。

- [ ] **步骤 3：实现 speaker.py**

创建 `LLM/voice/speaker.py`：
```python
# -*- coding: utf-8 -*-
r"""声纹：3D-Speaker ERes2NetV2（modelscope）。注册 / 验证(1:1) / 识别(1:N)。"""
from pathlib import Path
import numpy as np

from . import config


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))


def classify(emb: np.ndarray, profiles: dict, threshold: float):
    """profiles: {uid: emb}。返回 (uid|None, 最高分)。纯函数，便于测试。"""
    if not profiles:
        return None, 0.0
    best_uid, best = None, -1.0
    for uid, prof in profiles.items():
        s = cosine(prof, emb)
        if s > best:
            best, best_uid = s, uid
    if best >= threshold:
        return best_uid, best
    return None, best


class SpeakerRecognizer:
    def __init__(self, threshold=config.SPK_THRESHOLD, model_id=config.SPK_MODEL_ID,
                 profile_dir=config.SPEAKER_DIR):
        self.threshold = threshold
        self.model_id = model_id
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pipeline = None
        self._profiles = {}
        self._load()

    def _ensure_model(self):
        if self._pipeline is None:
            from modelscope.pipelines import pipeline  # 懒加载，避免拖慢启动
            self._pipeline = pipeline(
                task="speaker-verification", model=self.model_id, device="cpu")

    def embed(self, wav_16k: np.ndarray) -> np.ndarray:
        self._ensure_model()
        ret = self._pipeline([wav_16k], output_emb=True)
        return np.asarray(ret["embs"][0], dtype=np.float32)

    def enroll(self, uid: str, segments: list[np.ndarray]) -> np.ndarray:
        """segments: 若干段 16k 语音；逐段提特征取平均，落盘 npz。"""
        embs = [self.embed(s) for s in segments if len(s) >= config.SAMPLE_RATE]
        if not embs:
            raise ValueError("没有足够长（≥1s）的语音段用于注册")
        profile = np.mean(embs, axis=0).astype(np.float32)
        profile = profile / (np.linalg.norm(profile) + 1e-6)
        self._profiles[uid] = profile
        np.savez(self.profile_dir / f"{uid}.npz", emb=profile)
        return profile

    def verify(self, uid: str, wav_16k: np.ndarray) -> tuple[bool, float]:
        if uid not in self._profiles:
            return False, 0.0
        score = cosine(self._profiles[uid], self.embed(wav_16k))
        return score >= self.threshold, score

    def identify(self, wav_16k: np.ndarray) -> tuple[str | None, float]:
        emb = self.embed(wav_16k)
        return classify(emb, self._profiles, self.threshold)

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())

    def _load(self):
        for f in self.profile_dir.glob("*.npz"):
            try:
                self._profiles[f.stem] = np.load(f)["emb"].astype(np.float32)
            except Exception:
                continue
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_speaker_math.py -v`
预期：5 个用例全部 PASS。

- [ ] **步骤 5：模型冒烟（首次会自动下载 ~72MB ckpt）**

运行：`.venv\Scripts\python.exe -c "import numpy as np; from LLM.voice.tts import TTS; from LLM.voice.speaker import SpeakerRecognizer; s,sr=TTS().synthesize('今天天气不错'); spk=SpeakerRecognizer(); e=spk.embed(s); print('embedding shape:', e.shape, 'norm:', float(np.linalg.norm(e)))"`
预期：`embedding shape: (192,)`（首次运行会先下载 ckpt 到 `~/.cache/modelscope`，耗时取决于网络）。

- [ ] **步骤 6：Commit**

```bash
git add LLM/voice/speaker.py LLM/tests/test_speaker_math.py
git commit -m "feat(voice): 声纹模块（ERes2NetV2 注册/验证/识别）"
```

---

## 任务 9：身份融合层（TDD）

**文件：**
- 创建：`LLM/voice/identity.py`
- 测试：`LLM/tests/test_identity.py`

- [ ] **步骤 1：写失败测试**

创建 `LLM/tests/test_identity.py`：
```python
# -*- coding: utf-8 -*-
from LLM.voice.identity import IdentityVote, effective_uid

def test_effective_uid_uses_candidate():
    v = IdentityVote("elder_002", 0.7, "voiceprint")
    assert effective_uid(v, "elder_001") == "elder_002"

def test_effective_uid_falls_back_on_low_confidence():
    v = IdentityVote(None, 0.2, "voiceprint")
    assert effective_uid(v, "elder_001") == "elder_001"

def test_effective_uid_none_when_no_fallback():
    assert effective_uid(IdentityVote(None, 0.2, "voiceprint"), None) is None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_identity.py -v`
预期：FAIL，`ModuleNotFoundError`。

- [ ] **步骤 3：实现 identity.py**

创建 `LLM/voice/identity.py`：
```python
# -*- coding: utf-8 -*-
r"""身份融合层：本期只有声纹一路，人脸路接口预留（规格 D8）。"""
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class IdentityVote:
    candidate_uid: Optional[str]
    confidence: float
    source: str


class IdentitySource(Protocol):
    name: str
    def probe(self, wav) -> IdentityVote: ...


class VoiceprintSource:
    name = "voiceprint"

    def __init__(self, recognizer):
        self.recognizer = recognizer

    def probe(self, wav) -> IdentityVote:
        uid, score = self.recognizer.identify(wav)
        return IdentityVote(uid, score, self.name)


class VoiceprintOnlyFusion:
    """本期单源裁决。未来加 FaceSource 后在此加权/择优（接口不变）。"""

    def __init__(self, recognizer):
        self._source = VoiceprintSource(recognizer)

    def resolve(self, wav) -> IdentityVote:
        return self._source.probe(wav)


def effective_uid(vote: IdentityVote, current_uid: Optional[str]) -> Optional[str]:
    """宁问勿猜：高置信度用识别结果，低置信度沿用当前 uid。"""
    if vote.candidate_uid is not None:
        return vote.candidate_uid
    return current_uid
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_identity.py -v`
预期：3 个用例全部 PASS。

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/identity.py LLM/tests/test_identity.py
git commit -m "feat(voice): 身份融合层（声纹单路，人脸路预留）"
```

---

## 任务 10：编排线程 + 稳健性兜底

**文件：**
- 创建：`LLM/voice/worker.py`

- [ ] **步骤 1：实现 worker.py**

创建 `LLM/voice/worker.py`：
```python
# -*- coding: utf-8 -*-
r"""语音后台线程：编排 采集→VAD→(KWS|ASR)→声纹→LLM→TTS，含心跳与降级兜底。

稳健性核心：run() 顶层 try/except 兜住初始化；主循环任何异常只记审计 + 退避重连，
绝不向调用方（FastAPI 事件循环）抛出 → 主程序不崩。"""
import threading
import time

from .. import db, log as audit
from . import config
from . import audio, vad as vad_mod, kws as kws_mod, asr as asr_mod, tts as tts_mod
from . import speaker as spk_mod, identity as id_mod, session as session_mod


class VoiceWorker(threading.Thread):
    def __init__(self, chat_fn, post_turn_fn, publish_fn=None):
        super().__init__(daemon=True, name="voice-worker")
        self.chat_fn = chat_fn            # (uid, text) -> str
        self.post_turn_fn = post_turn_fn  # (uid, user_text, assistant) -> None
        self.publish_fn = publish_fn      # 事件广播，可空
        self._stop = threading.Event()
        self.status = "stopped"           # running / degraded / disabled / stopped
        self.sub_status = {}
        self.current_uid = None
        self.src = self.sink = None
        self.vad = self.kws = self.asr = self.tts = self.spk = self.fusion = None
        self.session = session_mod.Session()
        self._speak_started = None

    # ---- 状态上报 ----
    def _report(self, status: str, **kw):
        self.status = status
        self.sub_status.update(kw)
        audit.log("voice_error" if status == "degraded" else "voice_state",
                  status=status, **kw)
        if self.publish_fn:
            try:
                self.publish_fn("voice_status", status=status, **kw)
            except Exception:
                pass

    # ---- 运行时构建（懒加载；失败即抛给 run 降级）----
    def _build_runtime(self):
        self.src = audio.AudioSource()
        self.sink = audio.AudioSink()
        self.vad = vad_mod.VAD()
        self.kws = kws_mod.WakeWordDetector()
        self.asr = asr_mod.StreamASR()
        self.tts = tts_mod.TTS()
        self.spk = spk_mod.SpeakerRecognizer()
        self.fusion = id_mod.VoiceprintOnlyFusion(self.spk)

    def _reconnect(self) -> bool:
        try:
            if self.src:
                self.src.stop()
            self.src = audio.AudioSource()
            self.src.start()
            return True
        except Exception:
            return False

    def run(self):
        try:
            self._build_runtime()
            self.src.start()
        except Exception as e:
            self._report("degraded", error=f"初始化失败: {e}")
            return
        self._report("running")
        fails = 0
        while not self._stop.is_set():
            try:
                settings = db.get_settings()
                if not settings.get("voice_enabled", True):
                    self._report("disabled")
                    time.sleep(1.0)
                    continue
                if self.status in ("disabled", "degraded"):
                    self._report("running")
                self._step(settings)
                fails = 0
            except Exception as e:
                fails += 1
                audit.log("voice_error", error=str(e), retry=fails)
                if self._reconnect():
                    fails = 0
                if fails >= config.MAX_RECONNECT:
                    self._report("degraded", error=str(e), retries=fails)
                time.sleep(2.0)

    def _step(self, settings):
        # 运行时可调参数
        self.session.handsfree_sec = float(settings.get("handsfree_seconds", 30.0))
        if self.spk is not None:
            self.spk.threshold = float(settings.get("spk_threshold", config.SPK_THRESHOLD))
        chunk = self.src.read()
        self.vad.accept(chunk)

        if self.session.state == session_mod.State.IDLE:
            hit = self.kws.accept(chunk)
            if hit:
                self.session.wake()
                audit.log("voice_wake", keyword=hit)

        elif self.session.state == session_mod.State.LISTENING:
            seg = self.vad.pop_speech()
            if seg is not None:
                self._handle_speech(seg, settings)
            self.session.expire()

        elif self.session.state == session_mod.State.SPEAKING:
            if (self._speak_started is not None
                    and (time.monotonic() - self._speak_started) > config.BARGE_IN_GRACE_S
                    and self.vad.is_speech_now()):
                self.sink.stop()
                self.session.barge_in()
                audit.log("voice_barge_in")
            if self.sink.is_done():
                self.session.finish_speaking()
                self._speak_started = None

    def _handle_speech(self, seg, settings):
        self.session.note_speech()
        if not settings.get("asr_enabled", True):
            return
        text = self.asr.transcribe(seg)
        if not text.strip():
            return
        audit.log("voice_asr", text=text[:200])

        vote = self.fusion.resolve(seg)
        uid = id_mod.effective_uid(vote, self.current_uid)
        self.current_uid = uid or self.current_uid
        audit.log("voice_spk", identified=(vote.candidate_uid is not None),
                  uid=vote.candidate_uid, score=round(vote.confidence, 3))

        chat_uid = self.current_uid or "elder_001"
        reply = self.chat_fn(chat_uid, text)
        if self.post_turn_fn:
            try:
                self.post_turn_fn(chat_uid, text, reply)
            except Exception:
                pass
        if reply and settings.get("tts_enabled", True):
            self._speak(reply)

    def _speak(self, text):
        samples, sr = self.tts.synthesize(text)
        self._speak_started = time.monotonic()
        self.session.start_speaking()
        self.sink.play(samples, sr)
        audit.log("voice_tts", text=text[:100], ms=len(samples) * 1000 // sr)

    def stop(self):
        self._stop.set()
        if self.src:
            self.src.stop()
        if self.sink:
            self.sink.stop()
```

- [ ] **步骤 2：静态检查**

运行：`.venv\Scripts\python.exe -m py_compile LLM/voice/worker.py`
预期：无输出（编译通过）。

- [ ] **步骤 3：Commit**

```bash
git add LLM/voice/worker.py
git commit -m "feat(voice): 语音编排线程（心跳状态 + 掉线重连 + 逐模块降级）"
```

---

## 任务 11：FastAPI 挂载 + 设置 + 审计 + 端点

**文件：**
- 创建：`LLM/voice_api.py`
- 修改：`LLM/conf.py`、`LLM/server.py`

- [ ] **步骤 1：conf.py 新增设置项**

在 `LLM/conf.py` 的 `DEFAULT_SETTINGS` 中，将 `asr_enabled`/`tts_enabled` 默认值改为 `True`，并在其后新增：
```python
    "asr_enabled": True,            # 语音识别（真实开关）
    "tts_enabled": True,            # 语音合成（真实开关）
    "voice_enabled": True,          # 语音链路总开关（启动时是否拉起 worker）
    "wakeword": "小机器人",          # 唤醒词（显示用；实际检测用 kws_keywords.txt）
    "handsfree_seconds": 30,        # 免唤醒连续对话窗口
    "spk_threshold": 0.55,          # 声纹余弦阈值（官方基线 0.360，精准优先上调）
```
（注意：`set_settings` 只接受 `DEFAULT_SETTINGS` 中存在的键，新增键自动纳入持久化。）

- [ ] **步骤 2：写 voice_api.py**

创建 `LLM/voice_api.py`：
```python
# -*- coding: utf-8 -*-
r"""语音服务的挂载逻辑：启动 worker、状态查询、声纹建档。路由按 AGENTS.md 惯例放 server.py。"""
import time
from pathlib import Path

import numpy as np

from . import db, bus, chat, log as audit
from .voice import worker as worker_mod, speaker as spk_mod, vad as vad_mod
from .voice import audio as audio_mod, config as voice_config

_worker = None
_recognizer = None   # 声纹实例（建档端点复用，避免重复加载）


def _chat_fn(client, model):
    def _fn(uid, text):
        settings = db.get_settings()
        parts = []
        for ev in chat.chat_stream(client, model, uid, text, "auto", settings):
            if ev["type"] == "content":
                parts.append(ev["content"])
        return "".join(parts)
    return _fn


def start_voice(client, model, post_turn_fn):
    global _worker
    if not db.get_settings().get("voice_enabled", True):
        return None
    _worker = worker_mod.VoiceWorker(_chat_fn(client, model), post_turn_fn, publish_fn=bus.publish)
    _worker.start()
    return _worker


def stop_voice():
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None


def get_status():
    settings = db.get_settings()
    if _worker is None:
        return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
                "status": "stopped", "modules": {}, "speakers": list_speakers()}
    return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
            "status": _worker.status, "modules": dict(_worker.sub_status),
            "speakers": list_speakers()}


def list_speakers():
    return sorted(p.stem for p in voice_config.SPEAKER_DIR.glob("*.npz"))


def enroll_speaker(uid: str, seconds: int = 15) -> dict:
    """录 seconds 秒 → VAD 切段 → 提特征平均入档。"""
    global _recognizer
    if _worker is not None:
        _worker.src.stop()   # 暂停常驻采集，避免设备争用
    try:
        src = audio_mod.AudioSource()
        src.start()
        buf = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            chunk = src.read()
            if chunk is not None:
                buf.append(chunk)
        src.stop()
        samples = np.concatenate(buf) if buf else np.zeros(0, dtype=np.float32)
        if _recognizer is None:
            _recognizer = spk_mod.SpeakerRecognizer()
        v = vad_mod.VAD()
        v.accept(samples)
        v.flush()
        segs = []
        while True:
            seg = v.pop_speech()
            if seg is None:
                break
            segs.append(seg)
        _recognizer.enroll(uid, segs)
        audit.log("voice_spk", action="enroll", uid=uid, segments=len(segs))
        return {"ok": True, "uid": uid, "segments": len(segs)}
    finally:
        if _worker is not None:
            try:
                _worker.src.start()
            except Exception:
                pass
```

- [ ] **步骤 3：server.py 接入**

3a. 在 `server.py` 顶部导入处，把 `from . import db, bus, chat, memory as rag, reminder, tools as tool_mod` 改为：
```python
from . import db, bus, chat, memory as rag, reminder, tools as tool_mod, voice_api
```

3b. 在 `lifespan` 中 `drain_task = bus.start_drain()` 之后加一行，`yield` 之后 `drain_task.cancel()` 之前加停用：
```python
    drain_task = bus.start_drain()   # 广播扇出任务
    voice_api.start_voice(client, MODEL, _post_chat_jobs)
    yield
    voice_api.stop_voice()
    drain_task.cancel()
```

3c. 在设置路由之后、`/api/events` 之前，新增三个端点：
```python
# ---------------------------------------------------------------- 语音
@app.post("/api/voice/enroll")
async def voice_enroll(body: dict):
    uid = (body or {}).get("uid", "elder_001")
    seconds = int((body or {}).get("seconds", 15))
    return await asyncio.to_thread(voice_api.enroll_speaker, uid, seconds)


@app.get("/api/voice/status")
async def voice_status():
    return voice_api.get_status()


@app.get("/api/voice/speakers")
async def voice_speakers():
    return {"ok": True, "speakers": voice_api.list_speakers()}
```

- [ ] **步骤 4：启动验证不崩**

运行（项目根目录）：
```powershell
.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000
```
预期：服务正常启动；日志里出现 `voice_state`（running）或 `degraded`（若未装模型/无麦克风则 degraded，但**主进程不崩、/api/health 仍可用**）。

- [ ] **步骤 5：验证端点**

另开终端：
```powershell
curl http://127.0.0.1:8000/api/voice/status
curl http://127.0.0.1:8000/api/voice/speakers
curl http://127.0.0.1:8000/api/health
```
预期：status 返回 JSON（status 字段为 running/degraded/disabled/stopped 之一）；speakers 返回 `{"ok":true,"speakers":[]}`；health 返回 `{"ok":true,...}`。

- [ ] **步骤 6：Commit**

```bash
git add LLM/voice_api.py LLM/conf.py LLM/server.py
git commit -m "feat(voice): FastAPI 挂载 + 设置开关 + 建档/状态/列表端点"
```

---

## 任务 12：端到端集成实测 + 掉线不崩验证（验收）

**文件：**
- 创建：`scripts/voice_selftest.py`

- [ ] **步骤 1：写离线自检脚本（无麦克风，覆盖 5 个模型）**

创建 `scripts/voice_selftest.py`：
```python
# -*- coding: utf-8 -*-
r"""离线自检：不依赖麦克风，验证 5 类能力可加载 + TTS→ASR 往返 + 声纹 embedding 形状。
用法：.venv\Scripts\python.exe scripts\voice_selftest.py
"""
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from LLM.voice import config
from LLM.voice import tts as tts_mod, asr as asr_mod, vad as vad_mod, kws as kws_mod, speaker as spk_mod


def main():
    print("1/5 VAD 加载 ...")
    v = vad_mod.VAD()
    v.accept(np.zeros(config.SAMPLE_RATE, dtype=np.float32))
    print("   VAD ok")

    print("2/5 KWS 加载 ...")
    k = kws_mod.WakeWordDetector()
    assert k.accept(np.zeros(config.BLOCK_SAMPLES, dtype=np.float32)) is None
    print("   KWS ok")

    print("3/5 TTS 合成 ...")
    t = tts_mod.TTS()
    samples, sr = t.synthesize("你好，世界")
    assert samples.size > 0 and sr == config.SAMPLE_RATE
    print(f"   TTS ok: {samples.size} samples @ {sr}Hz")

    print("4/5 ASR 转写（TTS 往返）...")
    a = asr_mod.StreamASR()
    print(f"   ASR ok: {a.transcribe(samples)!r}")

    print("5/5 声纹 embedding ...")
    s = spk_mod.SpeakerRecognizer()
    emb = s.embed(samples)
    assert emb.shape == (config.SPK_EMBED_DIM,)
    print(f"   Speaker ok: embedding shape {emb.shape}")

    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：跑离线自检**

运行：`.venv\Scripts\python.exe scripts\voice_selftest.py`
预期：打印 5 步全部 ok，最后 `ALL OK`（首次会下载声纹 ckpt）。

- [ ] **步骤 3：真实链路验收（需麦克风）**

按顺序验证，逐项勾选：
- [ ] 启动 server 后，`/api/voice/status` 显示 `status: running`
- [ ] 喊"小机器人"→ 审计日志 `LLM/data/audit.jsonl` 出现 `voice_wake`
- [ ] 接着说一句话 → 出现 `voice_asr`（文本对）、`voice_spk`（identified true/false + score）
- [ ] 机器人 TTS 播报回复 → 出现 `voice_tts`
- [ ] 播报中说话（戴耳机）→ 出现 `voice_barge_in`，播报中断
- [ ] 30 秒不说话 → 回到待机（日志无异常，需再喊唤醒词才能说话）

- [ ] **步骤 4：掉线不崩验收（硬要求 D7）**

- [ ] 运行中拔掉/禁用麦克风（或 `sd.default.device` 指向一个无效设备）→ `/api/voice/status` 变为 `degraded`，但 **`/api/health` 仍返回 ok、网页聊天/提醒照常**
- [ ] 恢复麦克风 → status 自动回 `running`（退避重连生效）
- [ ] 检查 `LLM/data/audit.jsonl` 有连续 `voice_error` 记录，无 `Traceback` 打到 uvicorn 主进程日志

- [ ] **步骤 5：声纹双档案验收**

- [ ] 用 `/api/voice/enroll`（body `{"uid":"elder_001","seconds":15}`）录自己 15 秒 → 返回 `segments ≥ 1`
- [ ] 再录 `elder_002`（换一个人或变声）→ 成功
- [ ] 说话后看 `voice_spk` 日志的 `identified` 与 `uid` 是否正确区分两位

- [ ] **步骤 6：Commit 自检脚本**

```bash
git add scripts/voice_selftest.py
git commit -m "test(voice): 离线自检脚本（模型加载 + TTS/ASR 往返 + 声纹形状）"
```

---

## 完成标准

- [ ] `python -m pytest LLM/tests -v` 全绿（session / speaker_math / identity 共 15 用例）
- [ ] `scripts/voice_selftest.py` 输出 `ALL OK`
- [ ] 真实链路验收 6 项 + 掉线不崩 3 项 + 声纹双档案 3 项全部通过
- [ ] 规格 §1–§11 全部有对应实现（§9 移植要点属 M5，不在本期）
