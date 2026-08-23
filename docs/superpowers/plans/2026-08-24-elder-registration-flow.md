# 老人注册流程 + 身份样本管理 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 前端新增"老人注册"向导（基本信息 → 声纹录制[试听/重录/保存] → 人脸占位 → 成功切换），记忆页支持追加/清除声纹样本（合并平均）；后端声纹改两步式 API（录制暂存 + 入档分离），新增人脸占位路由。

**架构：** 方案 A（前端向导编排，复用/扩展现有 API）。声纹：`POST /api/voice/record` 录制并暂存（特征+音频内存暂存，TTL 10 分钟）→ `POST /api/voice/enroll` 提交入档（`append` 合并平均，档案存 `emb+count`，旧 npz 兼容）；`DELETE /api/voice/record/{id}` 丢弃暂存；`DELETE /api/voice/speakers/{uid}` 清除声纹档案。人脸仅 `GET /api/face/status` 占位返回 unavailable。

**技术栈：** FastAPI + SQLite（无新表）+ 原生 JS 单文件 SPA；测试 pytest + httpx TestClient。

**规格：** `docs/superpowers/specs/2026-08-24-elder-registration-flow-design.md`
**人脸备忘：** `docs/temp/face-recognition-notes.md`

**测试运行方式**（项目根目录）：`.venv\Scripts\python.exe -m pytest tests LLM/tests -q`
（`tests/conftest.py` 已把 db 指向临时目录；`httpx` 0.28.1 可用，TestClient 可测。）

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `LLM/conf.py` | 修改 | 新增 `VOICE_PENDING_TTL_S`、`VOICE_ENROLL_SECONDS` |
| `LLM/voice/speaker.py` | 修改 | `merge_profile` 纯函数、`enroll(append)`、`enroll_embedding`、`delete`、`sample_count`、npz `count` 兼容 |
| `LLM/voice_api.py` | 修改 | `_pending` 暂存、`record_speaker` / `commit_speaker` / `discard_recording` / `get_recording_audio` / `delete_speaker` / `list_speaker_details`，旧 `enroll_speaker` 保留兼容 |
| `LLM/server.py` | 修改 | 新增 voice 两步式路由 + face 占位路由 |
| `LLM/tests/test_speaker_math.py` | 修改 | `merge_profile` 纯函数测试 |
| `LLM/tests/test_speaker_enroll.py` | 创建 | `SpeakerRecognizer` append/delete/sample_count/兼容测试 |
| `LLM/tests/test_voice_api_enroll.py` | 创建 | `voice_api` 两步式/降级路径测试 |
| `LLM/tests/test_server_voice_routes.py` | 创建 | 路由 TestClient 测试 |
| `UI/index.html` | 修改 | 顶栏按钮、注册向导、记忆页身份样本区块 |
| `docs/log.md` | 修改 | 开发日志追加 |

---

### 任务 1：conf.py 新增声纹录制常量

**文件：** `LLM/conf.py`

- [ ] **步骤 1：添加常量**

在 `DEFAULT_SETTINGS` 之后、`THINKING_KEYWORDS` 之前插入：

```python
# 声纹录制
VOICE_ENROLL_SECONDS = 15        # 注册/追加默认录制秒数
VOICE_PENDING_TTL_S = 600        # 录制暂存（特征+音频）内存保留时长
```

- [ ] **步骤 2：Commit**

```bash
git add LLM/conf.py
git commit -m "feat(voice): 新增声纹录制常量（默认秒数/暂存 TTL）"
```

---

### 任务 2：speaker.py 新增 merge_profile 纯函数（TDD）

**文件：** 修改 `LLM/voice/speaker.py`；测试 `LLM/tests/test_speaker_math.py`

- [ ] **步骤 1：编写失败的测试**

在 `LLM/tests/test_speaker_math.py` 末尾追加：

```python
# -*- coding: utf-8 -*-
import numpy as np
from LLM.voice.speaker import merge_profile


def _vec(first=1.0):
    v = np.zeros(8, dtype=np.float32)
    v[0] = first
    return v / (np.linalg.norm(v) + 1e-6)


def test_merge_profile_new_when_no_old():
    merged, count = merge_profile(None, 0, _vec(1.0))
    assert count == 1
    assert np.allclose(merged, _vec(1.0))


def test_merge_profile_weighted_average():
    # 旧档案 2 次样本（vec A），新样本 vec B → 均值 (2A+B)/3
    a, b = _vec(1.0), _vec(2.0)
    merged, count = merge_profile(a, 2, b)
    assert count == 3
    expect = (a * 2 + b) / 3
    expect = expect / (np.linalg.norm(expect) + 1e-6)
    assert np.allclose(merged, expect, atol=1e-5)
    assert abs(np.linalg.norm(merged) - 1.0) < 1e-5   # 归一化


def test_merge_profile_zero_old_count_treated_as_new():
    merged, count = merge_profile(_vec(1.0), 0, _vec(2.0))
    assert count == 1
    assert np.allclose(merged, _vec(2.0))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_speaker_math.py -q`
预期：FAIL，`ImportError: cannot import name 'merge_profile'`

- [ ] **步骤 3：实现 merge_profile**

在 `LLM/voice/speaker.py` 的 `cosine` 函数后插入：

```python
def merge_profile(old: np.ndarray | None, old_count: int, new: np.ndarray):
    """声纹合并平均：new 并入旧档案。返回 (merged_emb, new_count)。
    old 为 None 或 old_count<=0 时视为新建（count=1）。"""
    if old is None or old_count <= 0:
        emb = np.asarray(new, dtype=np.float32)
        return emb / (np.linalg.norm(emb) + 1e-6), 1
    merged = (old.astype(np.float32) * old_count + np.asarray(new, dtype=np.float32)) / (old_count + 1)
    merged = merged / (np.linalg.norm(merged) + 1e-6)
    return merged.astype(np.float32), old_count + 1
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_speaker_math.py -q`
预期：PASS（含原有 cosine/classify 用例）

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/speaker.py LLM/tests/test_speaker_math.py
git commit -m "feat(voice): 声纹合并平均 merge_profile 纯函数"
```

---

### 任务 3：SpeakerRecognizer 支持追加/删除/样本计数（TDD）

**文件：** 修改 `LLM/voice/speaker.py`；创建 `LLM/tests/test_speaker_enroll.py`

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_speaker_enroll.py`：

```python
# -*- coding: utf-8 -*-
import numpy as np
from LLM.voice import speaker as spk_mod
from LLM.voice import config


def _wav():
    return np.ones(config.SAMPLE_RATE, dtype=np.float32) * 0.1   # 1 秒 16k 语音


def _vec(first=1.0):
    v = np.zeros(8, dtype=np.float32)
    v[0] = first
    return v / (np.linalg.norm(v) + 1e-6)


def _recognizer(tmp_path, monkeypatch):
    r = spk_mod.SpeakerRecognizer(profile_dir=str(tmp_path))
    monkeypatch.setattr(r, "embed", lambda wav: _vec(1.0))
    return r


def test_enroll_new_writes_count1(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])
    d = np.load(tmp_path / "elder_x.npz")
    assert int(d["count"]) == 1
    assert "elder_x" in r._profiles


def test_enroll_append_merges(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])            # append=False，count=1
    r.enroll("elder_x", [_wav()], append=True)
    d = np.load(tmp_path / "elder_x.npz")
    assert int(d["count"]) == 2
    assert r.sample_count("elder_x") == 2


def test_enroll_append_without_existing_is_new(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()], append=True)   # 无旧档案，等效新建
    assert r.sample_count("elder_x") == 1


def test_enroll_override_resets_count(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])
    r.enroll("elder_x", [_wav()], append=True)
    r.enroll("elder_x", [_wav()])                # 再次覆盖 → count 归 1
    assert r.sample_count("elder_x") == 1


def test_enroll_embedding_merges(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll_embedding("elder_x", _vec(1.0))
    r.enroll_embedding("elder_x", _vec(2.0), append=True)
    assert r.sample_count("elder_x") == 2
    d = np.load(tmp_path / "elder_x.npz")
    expect = (_vec(1.0) * 1 + _vec(2.0)) / 2
    expect = expect / (np.linalg.norm(expect) + 1e-6)
    assert np.allclose(d["emb"], expect, atol=1e-5)


def test_delete_removes_file_and_profile(tmp_path, monkeypatch):
    r = _recognizer(tmp_path, monkeypatch)
    r.enroll("elder_x", [_wav()])
    r.delete("elder_x")
    assert not (tmp_path / "elder_x.npz").exists()
    assert "elder_x" not in r._profiles
    assert r.sample_count("elder_x") == 0


def test_legacy_npz_without_count_counts_as_1(tmp_path):
    v = _vec(1.0)
    np.savez(tmp_path / "elder_old.npz", emb=v)   # 旧格式：无 count
    r = spk_mod.SpeakerRecognizer(profile_dir=str(tmp_path))
    assert r.sample_count("elder_old") == 1
    assert "elder_old" in r._profiles
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_speaker_enroll.py -q`
预期：FAIL（`enroll` 缺 `append` 参数 / 无 `enroll_embedding` / `delete` / `sample_count`）

- [ ] **步骤 3：实现**

修改 `LLM/voice/speaker.py`：

1. `enroll` 改为内部算均值后调 `enroll_embedding`（DRY），签名加 `append=False`：

```python
    def enroll(self, uid: str, segments: list[np.ndarray], append: bool = False) -> np.ndarray:
        """segments: 若干段 16k 语音；逐段提特征取平均，落盘 npz。
        append=True 时与已有档案合并平均（档案样本计数 +1）。"""
        embs = [self.embed(s) for s in segments if len(s) >= config.SAMPLE_RATE]
        if not embs:
            raise ValueError("没有足够长（≥1s）的语音段用于注册")
        new = np.mean(embs, axis=0).astype(np.float32)
        return self.enroll_embedding(uid, new, append=append)

    def enroll_embedding(self, uid: str, emb: np.ndarray, append: bool = False) -> np.ndarray:
        """直接用特征向量入档（录制暂存路径用）；append=True 合并平均。"""
        old = self._profiles.get(uid)
        old_count = self.sample_count(uid)
        merged, count = merge_profile(old if append else None, old_count if append else 0, emb)
        self._profiles[uid] = merged
        np.savez(self.profile_dir / f"{uid}.npz", emb=merged, count=count)
        return merged
```

2. 新增 `delete` 与 `sample_count`（放在 `list_profiles` 附近）：

```python
    def delete(self, uid: str) -> None:
        """清除该 uid 的声纹档案（文件 + 内存）。"""
        self._profiles.pop(uid, None)
        f = self.profile_dir / f"{uid}.npz"
        if f.exists():
            f.unlink()

    def sample_count(self, uid: str) -> int:
        """档案样本计数：新 npz 读 count；旧 npz（无 count）视为 1；无档案 0。"""
        f = self.profile_dir / f"{uid}.npz"
        if not f.exists():
            return 0
        try:
            d = np.load(f)
            return int(d["count"]) if "count" in d else 1
        except Exception:
            return 1
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_speaker_enroll.py LLM/tests/test_speaker_math.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice/speaker.py LLM/tests/test_speaker_enroll.py
git commit -m "feat(voice): 声纹档案支持追加合并/删除/样本计数（npz count 兼容旧格式）"
```

---

### 任务 4：voice_api.py 两步式录制/入档 API（TDD）

**文件：** 修改 `LLM/voice_api.py`；创建 `LLM/tests/test_voice_api_enroll.py`

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_voice_api_enroll.py`：

```python
# -*- coding: utf-8 -*-
import numpy as np
from LLM import voice_api
from LLM.voice import config


class FakeRecognizer:
    def __init__(self):
        self.calls = []
        self.deleted = None
    def embed(self, wav):
        return np.ones(8, dtype=np.float32) * 0.1
    def enroll_embedding(self, uid, emb, append=False):
        self.calls.append((uid, append))
    def sample_count(self, uid):
        return 7
    def delete(self, uid):
        self.deleted = uid


class FakeAudioSource:
    def __init__(self):
        self.n = 0
    def start(self):
        pass
    def read(self):
        self.n += 1
        return None if self.n > 6 else np.zeros(1600, dtype=np.float32)
    def stop(self):
        pass


class FakeVAD:
    def __init__(self):
        self._done = False
    def accept(self, samples):
        pass
    def flush(self):
        pass
    def pop_speech(self):
        if not self._done:
            self._done = True
            return np.ones(config.SAMPLE_RATE, dtype=np.float32) * 0.1
        return None


def _patch_available(monkeypatch):
    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", True)
    monkeypatch.setattr(voice_api, "_worker", None)
    monkeypatch.setattr(voice_api, "_recognizer", FakeRecognizer())
    monkeypatch.setattr(voice_api.audio_mod, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(voice_api.vad_mod, "VAD", FakeVAD)


def test_record_ok(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    res = voice_api.record_speaker(seconds=2)
    assert res["ok"] is True and res["recording_id"] and res["segments"] == 1
    assert res["recording_id"] in voice_api._pending
    assert "wav" in voice_api._pending[res["recording_id"]]


def test_commit_ok(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    res = voice_api.record_speaker(seconds=2)
    rid = res["recording_id"]
    out = voice_api.commit_speaker(rid, "elder_x", append=True)
    assert out["ok"] is True and out["uid"] == "elder_x" and out["samples"] == 7
    assert voice_api._recognizer.calls == [("elder_x", True)]
    assert rid not in voice_api._pending            # 入档后暂存清除


def test_commit_unknown_recording(monkeypatch):
    _patch_available(monkeypatch)
    out = voice_api.commit_speaker("nope", "elder_x", append=True)
    assert out["ok"] is False and "过期" in out["error"]


def test_discard_idempotent(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    res = voice_api.record_speaker(seconds=2)
    rid = res["recording_id"]
    assert voice_api.discard_recording(rid)["ok"] is True
    assert voice_api.discard_recording(rid)["ok"] is True   # 幂等
    assert rid not in voice_api._pending


def test_get_recording_audio(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    rid = voice_api.record_speaker(seconds=2)["recording_id"]
    data, ctype = voice_api.get_recording_audio(rid)
    assert data and ctype == "audio/wav" and data[:4] == b"RIFF"
    assert voice_api.get_recording_audio("nope") is None


def test_delete_speaker(monkeypatch):
    _patch_available(monkeypatch)
    out = voice_api.delete_speaker("elder_x")
    assert out["ok"] is True and voice_api._recognizer.deleted == "elder_x"


def test_degraded_paths_return_ok_false(monkeypatch):
    monkeypatch.setattr(voice_api, "_VOICE_AVAILABLE", False)
    assert voice_api.record_speaker(seconds=2)["ok"] is False
    assert voice_api.commit_speaker("x", "u")["ok"] is False
    assert voice_api.delete_speaker("u")["ok"] is False


def test_pending_ttl_cleanup(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api, "_pending", {})
    monkeypatch.setattr(voice_api, "VOICE_PENDING_TTL_S", 0)
    rid1 = voice_api.record_speaker(seconds=2)["recording_id"]
    rid2 = voice_api.record_speaker(seconds=2)["recording_id"]
    assert rid1 not in voice_api._pending   # 第二次录制时旧暂存被清
    assert rid2 in voice_api._pending


def test_list_speaker_details(monkeypatch):
    _patch_available(monkeypatch)
    monkeypatch.setattr(voice_api._recognizer, "list_profiles", lambda: ["elder_a"])
    monkeypatch.setattr(voice_api._recognizer, "sample_count", lambda uid: 3)
    assert voice_api.list_speaker_details() == {"elder_a": {"samples": 3}}
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_voice_api_enroll.py -q`
预期：FAIL（`record_speaker` 等不存在 / `_pending` 不存在）

- [ ] **步骤 3：实现**

修改 `LLM/voice_api.py`：

1. 顶部 import 增补：

```python
import io
import time
import uuid
import wave
from pathlib import Path

from . import db, bus, chat, log as audit
from .conf import VOICE_PENDING_TTL_S
```

2. 在模块级（`_recognizer` 定义附近）新增暂存与工具函数：

```python
_pending = {}   # recording_id -> {"emb", "segments", "wav", "ts"}（录制暂存，TTL 后清理）


def _wav_bytes(samples: np.ndarray) -> bytes:
    """float32 16k 单声道 → wav bytes（16bit PCM），供试听。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(voice_config.SAMPLE_RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def _cleanup_pending():
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["ts"] > VOICE_PENDING_TTL_S]:
        _pending.pop(k, None)
```

3. 新增两步式函数（放在 `enroll_speaker` 之前）：

```python
def record_speaker(seconds: int = 15) -> dict:
    """录 seconds 秒 → VAD 切段 → 提特征，暂存内存（特征+音频），返回 recording_id。
    不落档；由 commit_speaker 提交入档，discard_recording 丢弃。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "error": _degraded_msg()}
    global _recognizer
    if _worker is not None:
        _worker.src.stop()
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
        embs = [_recognizer.embed(s) for s in segs if len(s) >= voice_config.SAMPLE_RATE]
        if not embs:
            return {"ok": False, "error": "没有检测到有效语音段，请靠近麦克风再说一遍"}
        emb = np.mean(embs, axis=0).astype(np.float32)
        rid = uuid.uuid4().hex
        _cleanup_pending()
        _pending[rid] = {"emb": emb, "segments": len(segs),
                         "wav": _wav_bytes(samples), "ts": time.time()}
        audit.log("voice_spk", action="record", rid=rid, segments=len(segs))
        return {"ok": True, "recording_id": rid, "segments": len(segs)}
    except Exception as e:
        audit.log("voice_error", action="record", error=str(e))
        return {"ok": False, "error": str(e)}
    finally:
        if _worker is not None:
            try:
                _worker.src.start()
            except Exception:
                pass


def commit_speaker(recording_id: str, uid: str, append: bool = True) -> dict:
    """把暂存特征入档（append=True 与已有档案合并平均），入档后清除暂存。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "uid": uid, "error": _degraded_msg()}
    item = _pending.pop(recording_id, None)
    if item is None:
        return {"ok": False, "uid": uid, "error": "录音已过期或不存在，请重新录制"}
    try:
        global _recognizer
        if _recognizer is None:
            _recognizer = spk_mod.SpeakerRecognizer()
        _recognizer.enroll_embedding(uid, item["emb"], append=append)
        audit.log("voice_spk", action="commit", uid=uid, append=append,
                  segments=item["segments"])
        return {"ok": True, "uid": uid, "samples": _recognizer.sample_count(uid)}
    except Exception as e:
        audit.log("voice_error", action="commit", uid=uid, error=str(e))
        return {"ok": False, "uid": uid, "error": str(e)}


def discard_recording(recording_id: str) -> dict:
    """丢弃暂存（幂等）。"""
    _pending.pop(recording_id, None)
    return {"ok": True}


def get_recording_audio(recording_id: str):
    """返回 (wav_bytes, "audio/wav")；不存在返回 None（试听用）。"""
    item = _pending.get(recording_id)
    if item is None:
        return None
    return item["wav"], "audio/wav"


def delete_speaker(uid: str) -> dict:
    """清除该 uid 的声纹档案（不影响老人基本信息档案）。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "uid": uid, "error": _degraded_msg()}
    try:
        global _recognizer
        if _recognizer is None:
            _recognizer = spk_mod.SpeakerRecognizer()
        _recognizer.delete(uid)
        audit.log("voice_spk", action="delete", uid=uid, by="nurse")
        return {"ok": True, "uid": uid}
    except Exception as e:
        audit.log("voice_error", action="delete", uid=uid, error=str(e))
        return {"ok": False, "uid": uid, "error": str(e)}


def list_speaker_details() -> dict:
    """{uid: {"samples": n}}，前端渲染样本数用。语音不可用时返回空。"""
    if not _VOICE_AVAILABLE:
        return {}
    global _recognizer
    if _recognizer is None:
        try:
            _recognizer = spk_mod.SpeakerRecognizer()
        except Exception:
            return {}
    return {uid: {"samples": _recognizer.sample_count(uid)}
            for uid in _recognizer.list_profiles()}
```

4. 旧 `enroll_speaker` 改为复用两步式（保持向后兼容：无 recording_id 的调用走这里，语义=覆盖建档）：

```python
def enroll_speaker(uid: str, seconds: int = 15) -> dict:
    """旧行为兼容：录 seconds 秒并覆盖建档（等效 record + commit append=False）。"""
    res = record_speaker(seconds)
    if not res.get("ok"):
        return {"ok": False, "uid": uid, "error": res.get("error", "录制失败")}
    return commit_speaker(res["recording_id"], uid, append=False)
```

注意：`record_speaker`/`commit_speaker`/`delete_speaker` 内部用到 `global _recognizer`，需与现有 `enroll_speaker` 一样在函数内声明。

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_voice_api_enroll.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add LLM/voice_api.py LLM/tests/test_voice_api_enroll.py
git commit -m "feat(voice): 声纹两步式 API——录制暂存/入档/丢弃/试听/清除（旧 enroll 兼容）"
```

---

### 任务 5：server.py 新增路由（TDD，TestClient）

**文件：** 修改 `LLM/server.py`；创建 `LLM/tests/test_server_voice_routes.py`

- [ ] **步骤 1：编写失败的测试**

创建 `LLM/tests/test_server_voice_routes.py`：

```python
# -*- coding: utf-8 -*-
from fastapi.testclient import TestClient

from LLM import server, voice_api


def _patch(monkeypatch):
    monkeypatch.setattr(voice_api, "record_speaker",
                        lambda seconds=15: {"ok": True, "recording_id": "rec123", "segments": 2})
    monkeypatch.setattr(voice_api, "commit_speaker",
                        lambda rid, uid, append=True: {"ok": True, "uid": uid, "samples": 3})
    monkeypatch.setattr(voice_api, "discard_recording", lambda rid: {"ok": True})
    monkeypatch.setattr(voice_api, "delete_speaker",
                        lambda uid: {"ok": True, "uid": uid})
    monkeypatch.setattr(voice_api, "get_recording_audio",
                        lambda rid: (b"RIFF\x00\x00\x00\x00WAVE", "audio/wav") if rid == "rec123" else None)
    monkeypatch.setattr(voice_api, "list_speakers", lambda: ["elder_a"])
    monkeypatch.setattr(voice_api, "list_speaker_details",
                        lambda: {"elder_a": {"samples": 2}})


def test_voice_record_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)   # 不进 with：不触发 lifespan，避免副作用
    r = c.post("/api/voice/record", json={"seconds": 15})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["recording_id"] == "rec123"


def test_voice_enroll_route_new(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.post("/api/voice/enroll", json={"uid": "elder_x", "recording_id": "rec123", "append": True})
    assert r.status_code == 200 and r.json()["samples"] == 3


def test_voice_enroll_route_legacy(monkeypatch):
    # 无 recording_id → 回退旧 enroll_speaker
    monkeypatch.setattr(voice_api, "enroll_speaker",
                        lambda uid, seconds=15: {"ok": True, "uid": uid, "segments": 1})
    c = TestClient(server.app)
    r = c.post("/api/voice/enroll", json={"uid": "elder_x", "seconds": 15})
    assert r.status_code == 200 and r.json()["ok"]


def test_voice_discard_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.delete("/api/voice/record/rec123")
    assert r.status_code == 200 and r.json()["ok"]


def test_voice_delete_speaker_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.delete("/api/voice/speakers/elder_x")
    assert r.status_code == 200 and r.json()["ok"]


def test_voice_audio_route(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.get("/api/voice/record/rec123/audio")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    r2 = c.get("/api/voice/record/missing/audio")
    assert r2.status_code == 404


def test_voice_speakers_route_details(monkeypatch):
    _patch(monkeypatch)
    c = TestClient(server.app)
    r = c.get("/api/voice/speakers")
    j = r.json()
    assert j["speakers"] == ["elder_a"] and j["details"] == {"elder_a": {"samples": 2}}


def test_face_status_route():
    c = TestClient(server.app)
    r = c.get("/api/face/status")
    j = r.json()
    assert r.status_code == 200 and j["ok"] is True and j["status"] == "unavailable"
```

- [ ] **步骤 2：运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_server_voice_routes.py -q`
预期：FAIL（404：路由不存在）

- [ ] **步骤 3：实现**

修改 `LLM/server.py`，在 `voice_speakers` 路由之后、广播区块之前替换/新增：

```python
@app.post("/api/voice/record")
async def voice_record(body: dict = None):
    """两步式声纹第 1 步：录制并暂存（不落档），返回 recording_id。"""
    seconds = int((body or {}).get("seconds", 15))
    return await asyncio.to_thread(voice_api.record_speaker, seconds)


@app.get("/api/voice/record/{recording_id}/audio")
async def voice_record_audio(recording_id: str):
    """试听：返回暂存录音的 wav。"""
    got = await asyncio.to_thread(voice_api.get_recording_audio, recording_id)
    if got is None:
        return {"ok": False, "error": "录音已过期或不存在"}, 404
    data, ctype = got
    from fastapi.responses import Response
    return Response(content=data, media_type=ctype)


@app.delete("/api/voice/record/{recording_id}")
async def voice_record_discard(recording_id: str):
    """丢弃暂存录音（重录/放弃时用）。"""
    return await asyncio.to_thread(voice_api.discard_recording, recording_id)
```

修改现有 `/api/voice/enroll`（支持 recording_id+append，兼容旧调用）：

```python
@app.post("/api/voice/enroll")
async def voice_enroll(body: dict = None):
    body = body or {}
    uid = body.get("uid", "elder_001")
    if body.get("recording_id"):
        # 两步式第 2 步：提交暂存入档（append 默认 True = 合并平均）
        append = bool(body.get("append", True))
        return await asyncio.to_thread(voice_api.commit_speaker,
                                       body["recording_id"], uid, append)
    # 旧行为兼容：无 recording_id 直接录 seconds 秒覆盖建档
    seconds = int(body.get("seconds", 15))
    return await asyncio.to_thread(voice_api.enroll_speaker, uid, seconds)
```

修改 `/api/voice/speakers`（加 details）：

```python
@app.get("/api/voice/speakers")
async def voice_speakers():
    return {"ok": True, "speakers": voice_api.list_speakers(),
            "details": voice_api.list_speaker_details()}
```

新增人脸占位路由（放在语音区块末尾）：

```python
@app.get("/api/face/status")
async def face_status():
    """人脸录入占位：本期未实现，返回 unavailable（前端据此置灰按钮）。"""
    return {"ok": True, "status": "unavailable",
            "reason": "人脸录入尚未接入（占位接口，见 docs/temp/face-recognition-notes.md）"}
```

- [ ] **步骤 4：运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_server_voice_routes.py -q`
预期：PASS

再跑全量后端测试确认无回归：
运行：`.venv\Scripts\python.exe -m pytest tests LLM/tests -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add LLM/server.py LLM/tests/test_server_voice_routes.py
git commit -m "feat(voice): 两步式声纹路由 + 试听/清除 + 人脸占位路由"
```

---

### 任务 6：前端注册向导 + 身份样本管理（`UI/index.html`，手工验证）

**文件：** 修改 `UI/index.html`。无自动化测试，每子步骤后按"手工验证"跑一次。

- [ ] **步骤 1：新增 CSS（<style> 区块，`#exit-overlay` 规则之后）**

```css
/* ---------- 注册向导 ---------- */
.wizard-steps { display: flex; gap: 6px; align-items: center; font-size: 12px; color: #9a9ab4; margin-bottom: 14px; }
.wizard-steps .step { display: inline-flex; align-items: center; gap: 4px; }
.wizard-steps .num { background: #2a2a3e; border-radius: 50%; width: 18px; height: 18px; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; }
.wizard-steps .step.done .num { background: #1f9d55; }
.wizard-steps .step.active .num { background: #3b82f6; color: #fff; }
.wizard-steps .sep { color: #444; }
.option-card { background: #101018; border: 1px solid #333; border-radius: 10px; padding: 12px 14px; cursor: pointer; margin-bottom: 10px; }
.option-card:hover { border-color: #3b82f6; }
.option-card .oc-title { font-weight: 600; }
.option-card .oc-sub { color: #9a9ab4; font-size: 11.5px; margin-top: 2px; }
.rec-panel { background: #101018; border: 1px solid #333; border-radius: 10px; padding: 16px; text-align: center; }
.rec-panel .rec-countdown { font-size: 22px; font-weight: 700; color: #f87171; margin: 6px 0; }
.identity-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.identity-chips .chip { background: #2a2a3e; border-radius: 6px; padding: 3px 8px; font-size: 12px; }
.identity-chips .chip.off { color: #888; }
```

- [ ] **步骤 2：顶栏加"注册老人"按钮**

在 `index.html` 第 231 行 `<button id="mod-btn" ...>` 之前插入：

```html
<button id="reg-btn" class="btn gray sm" style="background:#1f9d55" onclick="openRegisterWizard()">➕ 注册老人</button>
```

手工验证：刷新页面，顶栏出现绿色"➕ 注册老人"按钮（点击暂无反应，下一步实现）。

- [ ] **步骤 3：注册向导 JS（状态机骨架 + 步骤 1 表单 + 保存档案）**

在 `saveProfile()` 函数之后插入（新函数）：

```javascript
    // ================================================================
    // 老人注册向导（4 步：基本信息 → 声纹 → 人脸占位 → 完成）
    // ================================================================
    let reg = { step: 0, uid: "", recordingId: null };

    async function nextElderUid() {
      const { profiles } = await api("/api/profiles");
      let max = 0;
      for (const p of profiles) {
        const m = /^elder_(\d+)$/.exec(p.uid || "");
        if (m) max = Math.max(max, parseInt(m[1], 10));
      }
      return "elder_" + String(max + 1).padStart(3, "0");
    }

    async function openRegisterWizard() {
      reg = { step: 0, uid: "", recordingId: null };
      reg.uid = await nextElderUid();
      regStep(1);
    }

    function regStep(n) {
      reg.step = n;
      const steps = ["基本信息", "声纹", "人脸", "完成"];
      const html = steps.map((s, i) => {
        const idx = i + 1;
        const cls = idx < n ? "step done" : idx === n ? "step active" : "step";
        return `<span class="${cls}"><span class="num">${idx < n ? "✓" : idx}</span>${s}</span>`;
      }).join('<span class="sep">──</span>');
      document.getElementById("modal-title").textContent = "➕ 注册老人";
      document.getElementById("modal-body").innerHTML = `<div class="wizard-steps">${html}</div>` +
        (n === 1 ? regStep1Html() : n === 2 ? regStep2Html() : n === 3 ? regStep3Html() : regStep4Html());
      document.getElementById("modal-foot").innerHTML = "";
      document.getElementById("modal-mask").style.display = "flex";
    }

    function regStep1Html() {
      return `<div class="form-grid">
        <div class="field"><label>UID（自动生成，可改）</label><input id="reg-uid" value="${escapeHtml(reg.uid)}"></div>
        <div class="field"><label>姓名 *</label><input id="reg-name" placeholder="张桂芳"></div>
        <div class="field"><label>称呼</label><input id="reg-nickname" placeholder="张奶奶"></div>
        <div class="field"><label>床位</label><input id="reg-bed" placeholder="3-15"></div>
        <div class="field"><label>年龄</label><input id="reg-age" type="number"></div>
        <div class="field"><label>性别</label><input id="reg-gender" placeholder="女"></div>
        <div class="field"><label>生日</label><input id="reg-birthday" type="date"></div>
        <div class="field"><label>偏好称呼（如：闺女）</label><input id="reg-call" placeholder="闺女"></div>
        <div class="field" style="grid-column:1/3"><label>喜欢话题（逗号分隔）</label><input id="reg-topics" placeholder="京剧、孙子、养生"></div>
        <div class="field" style="grid-column:1/3"><label>说话风格画像</label><input id="reg-style" placeholder="轻声细语，爱用'囡囡'称呼"></div>
        <div class="field" style="grid-column:1/3"><label>备注</label><input id="reg-notes" placeholder="喜欢听评书，晚饭后散步"></div>
      </div>
      <div class="hint" style="margin-top:8px">病史 / 用药可在注册完成后于记忆页档案表单补录</div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
        <button class="btn gray" onclick="closeModal()">取消</button>
        <button class="btn" onclick="regSaveProfile()">保存档案 → 下一步</button>
      </div>`;
    }

    async function regSaveProfile() {
      const name = document.getElementById("reg-name").value.trim();
      if (!name) { toast("请填写姓名", "", "err"); return; }
      const preferences = {
        称呼: document.getElementById("reg-call").value.trim(),
        话题: document.getElementById("reg-topics").value.split(/[，,]/).map(s => s.trim()).filter(Boolean),
      };
      await api("/api/profiles", {
        method: "POST",
        body: JSON.stringify({
          uid: document.getElementById("reg-uid").value.trim() || reg.uid,
          name,
          nickname: document.getElementById("reg-nickname").value.trim(),
          bed: document.getElementById("reg-bed").value.trim(),
          age: Number(document.getElementById("reg-age").value) || 0,
          gender: document.getElementById("reg-gender").value.trim(),
          birthday: document.getElementById("reg-birthday").value.trim(),
          profile: { 病史: [], 用药: [] }, preferences,
          style: document.getElementById("reg-style").value.trim(),
          notes: document.getElementById("reg-notes").value.trim(),
        }),
      });
      reg.uid = document.getElementById("reg-uid").value.trim() || reg.uid;
      toast("✅ 档案已保存");
      regStep(2);
    }

    // regStep 最终版：n===3（人脸占位）需异步拉取 /api/face/status，其余步骤同步渲染
    function regStep(n) {
      reg.step = n;
      const steps = ["基本信息", "声纹", "人脸", "完成"];
      const html = steps.map((s, i) => {
        const idx = i + 1;
        const cls = idx < n ? "step done" : idx === n ? "step active" : "step";
        return `<span class="${cls}"><span class="num">${idx < n ? "✓" : idx}</span>${s}</span>`;
      }).join('<span class="sep">──</span>');
      document.getElementById("modal-title").textContent = "➕ 注册老人";
      const body = document.getElementById("modal-body");
      body.innerHTML = `<div class="wizard-steps">${html}</div>`;
      if (n === 3) {
        regStep3Html().then(h => { body.innerHTML = `<div class="wizard-steps">${html}</div>` + h; });
      } else {
        body.innerHTML += n === 1 ? regStep1Html() : n === 2 ? regStep2Html() : regStep4Html();
      }
      document.getElementById("modal-foot").innerHTML = "";
      document.getElementById("modal-mask").style.display = "flex";
    }
```

手工验证：点"➕ 注册老人"→ 弹出向导，步骤 1 表单 UID 自动为 elder_00N；填姓名保存后进入步骤 2。

- [ ] **步骤 4：步骤 2 声纹录制（录制/试听/重录/保存/跳过）**

在 `regStep1Html` 之后插入：

```javascript
    function regStep2Html() {
      return `<div class="rec-panel" id="reg-rec-panel">
        <div style="font-size:26px">🎙️</div>
        <div>请老人对着机器人说一段话（15 秒）</div>
        <div class="hint" style="margin:6px 0">录制完成后可试听、重录或保存</div>
        <button class="btn" onclick="regStartRecord()">▶ 开始录制</button>
        <div class="hint" style="margin-top:8px">语音模块不可用时可跳过，稍后在记忆页追加</div>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
        <button class="btn gray" onclick="regStep(3)">跳过（稍后追加）</button>
      </div>`;
    }

    let regTimer = null;
    async function regStartRecord() {
      const panel = document.getElementById("reg-rec-panel");
      panel.innerHTML = '<div style="font-size:26px">🔴 录音中…</div><div class="rec-countdown" id="reg-count">15</div><div class="hint">请老人对着机器人说话</div>';
      regTimer = setInterval(() => {
        const el = document.getElementById("reg-count");
        if (!el) return;
        const n = parseInt(el.textContent, 10) - 1;
        el.textContent = Math.max(n, 0);
      }, 1000);
      try {
        const res = await api("/api/voice/record", {
          method: "POST", body: JSON.stringify({ seconds: 15 }),
        });
        if (!res.ok) throw new Error(res.error || "录制失败");
        reg.recordingId = res.recording_id;
        regShowRecordResult(res.segments);
      } catch (e) {
        if (regTimer) clearInterval(regTimer);
        panel.innerHTML = `<div class="hint" style="color:#f87171">⚠️ ${escapeHtml(e.message)}</div>
          <div style="display:flex;gap:8px;justify-content:center;margin-top:10px">
            <button class="btn gray" onclick="regStep(3)">跳过（稍后追加）</button>
            <button class="btn" onclick="regStartRecord()">重试</button>
          </div>`;
      }
    }

    function regShowRecordResult(segments) {
      if (regTimer) { clearInterval(regTimer); regTimer = null; }
      const panel = document.getElementById("reg-rec-panel");
      panel.innerHTML = `<div style="font-size:26px">✅</div>
        <div>录音完成，检测到 <b>${segments}</b> 段有效语音</div>
        <div style="display:flex;gap:8px;justify-content:center;margin-top:12px">
          <button class="btn gray" onclick="regListen()">🔊 试听</button>
          <button class="btn gray" onclick="regStartRecord()">🔁 重录</button>
          <button class="btn" onclick="regCommitRecord(false)">💾 保存</button>
        </div>
        <div class="hint" style="margin-top:8px">「重录」丢弃本次录音重新录；「保存」建档（首次）</div>`;
    }

    async function regListen() {
      try {
        const r = await fetch(API + "/api/voice/record/" + reg.recordingId + "/audio");
        if (!r.ok) { toast("录音已过期，请重新录制", "", "err"); return; }
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = new Audio(url);
        a.play();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
      } catch (e) { toast("试听失败：" + e.message, "", "err"); }
    }

    async function regCommitRecord(append) {
      const res = await api("/api/voice/enroll", {
        method: "POST",
        body: JSON.stringify({ uid: reg.uid, recording_id: reg.recordingId, append }),
      });
      if (!res.ok) { toast("保存失败：" + res.error, "", "err"); return; }
      reg.recordingId = null;
      reg.voiceDone = true;            // 声纹建档完成（完成页据此显示"已建档"）
      toast("✅ 声纹已建档", "样本数 " + res.samples);
      regStep(3);
    }
```

手工验证：步骤 2 点"开始录制"→ 倒计时 15 秒 → 结果态出现试听/重录/保存；点保存进入步骤 3。语音模块不可用时显示错误并可跳过。

- [ ] **步骤 5：步骤 3 人脸占位 + 步骤 4 完成页（切换老人）**

在 `regCommitRecord` 之后插入：

```javascript
    async function regStep3Html() {
      let face = "";
      try {
        const st = await api("/api/face/status");
        face = st.status === "unavailable"
          ? `<div class="hint">${escapeHtml(st.reason || "人脸录入尚未接入")}</div>`
          : '<div class="hint">人脸模块可用</div>';
      } catch (e) { face = '<div class="hint">人脸模块状态未知</div>'; }
      return `<div style="background:#101018;border:1px dashed #444;border-radius:10px;padding:16px;text-align:center">
        <div style="font-size:26px">📷</div>
        <div>摄像头录入人脸 — 功能尚未接入</div>
        ${face}
        <button class="btn" disabled style="background:#333;color:#888;cursor:not-allowed;margin-top:10px">📷 拍照录入（未开放）</button>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
        <button class="btn" onclick="regFinish()">完成注册</button>
      </div>`;
    }

    function regStep4Html() {
      return `<div style="background:#101018;border:1px solid #1f9d55;border-radius:10px;padding:16px;text-align:center">
        <div style="font-size:28px">🎉</div>
        <div style="font-size:15px;margin-top:4px">老人注册成功！</div>
        <div class="hint" style="margin-top:6px">${escapeHtml(reg.name || "")}（${escapeHtml(reg.uid)}）· 声纹${reg.voiceDone ? "已建档" : "未建档（可在记忆页追加）"} · 人脸待接入</div>
        <button class="btn" style="margin-top:12px" onclick="regDone()">完成，切换到该老人</button>
      </div>`;
    }

    function regFinish() {
      regStep(4);   // reg.voiceDone 由 regCommitRecord 成功时置位；跳过则保持 false
    }

    async function regDone() {
      closeModal();
      currentUid = reg.uid;
      localStorage.setItem("uid", currentUid);
      document.getElementById("uid-select").value = currentUid;
      toast("✅ 已切换到 " + reg.uid);
      loadProfiles(); loadMemory(); loadChatHistory();
    }

手工验证：步骤 3 显示人脸占位（按钮禁用）→ "完成注册" → 步骤 4 成功页 → "完成，切换到该老人" → 顶栏当前老人变为新 uid，记忆页/对话历史刷新。

- [ ] **步骤 6：记忆页身份样本区块 + 追加面板（追加/清除/试听）**

在 `index.html` 记忆页档案卡片（`📋 老人档案` 的 `<div class="card">` 内，`saveProfile` 按钮行之后）插入身份样本区块：

```html
        <div style="margin-top:12px">
          <div class="hint">身份样本（当前老人）</div>
          <div class="identity-chips" id="identity-chips"><span class="chip">加载中…</span></div>
          <div style="display:flex;gap:8px;margin-top:8px">
            <button class="btn" onclick="openVoiceAppendPanel()">🎙️ 追加声纹样本</button>
            <button class="btn" disabled style="background:#333;color:#888;cursor:not-allowed" onclick="toast('人脸录入尚未接入','','err')">📷 追加人脸照片（未开放）</button>
          </div>
        </div>
```

在 `loadMemory()` 里（档案表单回填之后、`mem-confirmed` 渲染之前）插入：

```javascript
      // 身份样本状态（声纹档案数 / 人脸占位）
      try {
        const vres = await api("/api/voice/speakers");
        const det = (vres.details || {})[uid];
        const spk = det ? `🎙️ 声纹已建档（${det.samples} 次样本）` : "🎙️ 声纹未建档";
        document.getElementById("identity-chips").innerHTML =
          `<span class="chip">${spk}</span><span class="chip off">📷 人脸未接入</span>`;
      } catch (e) {
        document.getElementById("identity-chips").innerHTML = '<span class="chip off">身份样本状态未知</span>';
      }
```

在 `regDone` 之后插入追加面板逻辑：

```javascript
    // ================================================================
    // 追加声纹面板（追加样本 / 清除档案；录制后试听/重录/保存）
    // ================================================================
    let appendRec = null;
    let appendTimer = null;

    async function openVoiceAppendPanel() {
      appendRec = null;
      const det = ((await api("/api/voice/speakers")).details || {})[currentUid];
      const status = det ? `已建档 · ${det.samples} 次样本（追加会合并平均，越录越准）` : "未建档（追加即首次建档）";
      document.getElementById("modal-title").textContent = "🎙️ 声纹样本管理（" + currentUid + "）";
      document.getElementById("modal-body").innerHTML = `
        <div class="hint" style="margin-bottom:10px">当前：${escapeHtml(status)}</div>
        <div class="option-card" onclick="appendStart()">
          <div class="oc-title">➕ 追加样本</div>
          <div class="oc-sub">重新录一段（15 秒），与已有档案合并平均</div>
        </div>
        <div class="option-card" style="border-color:#dc2626" onclick="appendClear()">
          <div class="oc-title">🗑️ 清除档案</div>
          <div class="oc-sub">删除该老人的全部声纹样本（需确认；不影响老人基本信息档案）</div>
        </div>`;
      document.getElementById("modal-foot").innerHTML = '<button class="btn gray" onclick="closeModal()">关闭</button>';
      document.getElementById("modal-mask").style.display = "flex";
    }

    async function appendStart() {
      const body = document.getElementById("modal-body");
      body.innerHTML = '<div class="rec-panel"><div style="font-size:26px">🔴 录音中…</div><div class="rec-countdown" id="append-count">15</div><div class="hint">请老人对着机器人说话</div></div>';
      appendTimer = setInterval(() => {
        const el = document.getElementById("append-count");
        if (el) el.textContent = Math.max(parseInt(el.textContent, 10) - 1, 0);
      }, 1000);
      try {
        const res = await api("/api/voice/record", {
          method: "POST", body: JSON.stringify({ seconds: 15 }),
        });
        if (!res.ok) throw new Error(res.error || "录制失败");
        appendRec = res.recording_id;
        appendResult(res.segments);
      } catch (e) {
        if (appendTimer) { clearInterval(appendTimer); appendTimer = null; }
        body.innerHTML = `<div class="hint" style="color:#f87171">⚠️ ${escapeHtml(e.message)}</div>
          <div style="display:flex;gap:8px;justify-content:center;margin-top:10px">
            <button class="btn" onclick="appendStart()">重试</button>
            <button class="btn gray" onclick="openVoiceAppendPanel()">返回</button>
          </div>`;
      }
    }

    function appendResult(segments) {
      if (appendTimer) { clearInterval(appendTimer); appendTimer = null; }
      const body = document.getElementById("modal-body");
      body.innerHTML = `<div class="rec-panel">
        <div style="font-size:26px">✅</div>
        <div>录音完成，检测到 <b>${segments}</b> 段有效语音</div>
        <div style="display:flex;gap:8px;justify-content:center;margin-top:12px">
          <button class="btn gray" onclick="appendListen()">🔊 试听</button>
          <button class="btn gray" onclick="appendStart()">🔁 重录</button>
          <button class="btn" onclick="appendCommit()">💾 保存</button>
        </div>
        <div class="hint" style="margin-top:8px">「重录」丢弃本次录音重新录；「保存」合并入档</div>
      </div>`;
    }

    async function appendListen() {
      try {
        const r = await fetch(API + "/api/voice/record/" + appendRec + "/audio");
        if (!r.ok) { toast("录音已过期，请重新录制", "", "err"); return; }
        const url = URL.createObjectURL(await r.blob());
        new Audio(url).play();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
      } catch (e) { toast("试听失败：" + e.message, "", "err"); }
    }

    async function appendCommit() {
      const res = await api("/api/voice/enroll", {
        method: "POST",
        body: JSON.stringify({ uid: currentUid, recording_id: appendRec, append: true }),
      });
      if (!res.ok) { toast("保存失败：" + res.error, "", "err"); return; }
      appendRec = null;
      toast("✅ 已合并入档", "样本数 " + res.samples);
      closeModal();
      loadMemory();
    }

    async function appendClear() {
      if (!confirm("确认清除 " + currentUid + " 的全部声纹样本？老人基本信息档案不受影响。")) return;
      const res = await api("/api/voice/speakers/" + currentUid, { method: "DELETE" });
      if (!res.ok) { toast("清除失败：" + res.error, "", "err"); return; }
      toast("🗑️ 声纹档案已清除", "可重新录制建档");
      closeModal();
      loadMemory();
    }
```

手工验证：记忆页档案卡片显示声纹状态徽标；"追加声纹样本"→ 追加/清除两选项 → 录制 → 试听/重录/保存；"清除档案"确认后样本数归零；"追加人脸照片"置灰。

- [ ] **步骤 7：Commit**

```bash
git add UI/index.html
git commit -m "feat(ui): 老人注册向导 + 记忆页声纹追加/清除/试听（人脸占位）"
```

---

### 任务 7：端到端手工验证 + 开发日志

**文件：** 修改 `docs/log.md`

- [ ] **步骤 1：启动后端**

运行（项目根目录，后台）：`.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000`
预期：日志显示启动完成，无 traceback。

- [ ] **步骤 2：按验证清单逐项确认（浏览器打开 `UI/index.html`）**

1. 顶栏出现"➕ 注册老人"绿色按钮；
2. 点开 → 向导 4 步指示器，步骤 1 表单 UID 自动 `elder_00N`（已有 elder_001 → elder_002）；
3. 填姓名保存 → 步骤 2；点"开始录制"→ 15 秒倒计时 → 结果态"试听/重录/保存"；
4. 试听能播放录音；重录重新计时；保存后提示"样本数 1"进入步骤 3；
5. 步骤 3 人脸占位（按钮禁用，显示"尚未接入"）→ "完成注册" → 步骤 4 成功页 → "完成，切换到该老人"；
6. 顶栏当前老人变为新 uid；对话页/记忆页数据为该老人；
7. 记忆页档案卡片出现"身份样本"区块：`🎙️ 声纹已建档（1 次样本）` + `📷 人脸未接入`；
8. "追加声纹样本"→ 面板两选项 → 录制 → 保存 → 徽标变"2 次样本"；
9. "清除档案"→ confirm → 徽标变"声纹未建档"；
10. "追加人脸照片"按钮置灰不可点；
11. 语音依赖缺失环境（如无 sounddevice）：步骤 2 显示错误并可"跳过"；追加面板录制报错可"返回"（降级不卡死）。

- [ ] **步骤 3：追加开发日志**

在 `docs/log.md` 末尾按现有格式追加一条（记录：注册向导、两步式声纹 API、试听/清除、人脸占位、merge 平均、commit 号）。

- [ ] **步骤 4：Commit**

```bash
git add docs/log.md
git commit -m "docs: 老人注册流程实现日志"
```

---

## 自检记录

- **规格覆盖度：** 设计文档 3.1（speaker 升级/两步式 API/路由）→ 任务 2/3/4/5；3.2 人脸占位 → 任务 5；4.1 顶栏按钮 → 任务 6 步骤 2；4.2 向导 4 步 → 任务 6 步骤 3/4/5；4.3 记忆页区块/追加面板 → 任务 6 步骤 6；§6 错误处理降级 → 任务 4 测试 + 任务 7 清单第 11 项；§7 测试 → 任务 2/3/4/5 + 任务 7。无遗漏。
- **占位符扫描：** 无 TODO/待定；所有步骤含完整代码或精确指令。
- **类型一致性：** `merge_profile(old, old_count, new) -> (emb, count)` 在任务 2/3 中签名一致；`enroll_embedding(uid, emb, append)` 在任务 3/4 一致；`record_speaker(seconds)` / `commit_speaker(rid, uid, append)` / `delete_speaker(uid)` / `get_recording_audio(rid)` / `list_speaker_details()` 在任务 4/5 一致；前端 `reg.recordingId` / `appendRec` 用法一致。`regStep3Html` 为 async（返回 Promise），`regStep` 中 n===3 分支用 `.then` 渲染，其余同步 —— 已注明。
