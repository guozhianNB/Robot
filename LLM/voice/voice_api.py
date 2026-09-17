# -*- coding: utf-8 -*-
r"""语音服务的挂载逻辑：启动 worker、状态查询、声纹建档。路由按 AGENTS.md 惯例放 server.py。

稳健性（系统要求，见 AGENTS.md「系统稳健性」）：
  语音链路依赖 numpy / sherpa_onnx / sounddevice / modelscope 等外部包，
  目标环境（requirement.txt 未装齐 / 无模型）缺失时，本模块必须降级：
  置 _VOICE_AVAILABLE=False 并记录 _MISSING_DEPS，所有语音能力返回"不可用"，
  绝不让导入链崩掉主程序 —— 后端必须在无语音依赖时也能正常启动。
"""
import io
import sqlite3
import time
import uuid
import wave
from pathlib import Path

from ..store import db
from ..core import bus, log as audit
from ..agent import chat
from ..conf import VOICE_PENDING_TTL_S

# ---------------------------------------------------------------------------
# 可选能力降级：外部依赖逐个尝试引入，收集所有缺失项。
# _VOICE_AVAILABLE = False 时，以下模块级名字（np / *_mod / voice_config）不可用，
# 所有语音函数必须先行判断 _VOICE_AVAILABLE 再使用它们。
# ---------------------------------------------------------------------------
_VOICE_AVAILABLE = True
_MISSING_DEPS = []

try:
    import numpy as np
except ImportError as _exc:
    _VOICE_AVAILABLE = False
    _MISSING_DEPS.append(str(_exc))

try:
    from . import worker as worker_mod, speaker as spk_mod, vad as vad_mod
    from . import audio as audio_mod, config as voice_config
except ImportError as _exc:
    _VOICE_AVAILABLE = False
    _MISSING_DEPS.append(str(_exc))


def _degraded_msg():
    """降级原因文案（缺失依赖列表；无缺失时给通用文案）。"""
    if _MISSING_DEPS:
        return "语音链路不可用（缺少依赖：{}）".format("; ".join(_MISSING_DEPS))
    return "语音链路不可用（模块加载失败）"

_worker = None
_recognizer = None   # 声纹实例（建档端点复用，避免重复加载）
_schema_path = None  # 已确认建过表的 DB_PATH（见 _ensure_schema）


def _ensure_schema() -> None:
    """兜底建表：会话层要按 `profiles.kind` 推导角色（R1），没有表就推导不了。

    正常启动由 `server.lifespan` 的 `db.init_db()` 建好；这里再兜一次是为了"库文件在、
    表不在"的场合（首启半途中断、测试隔离出空库）——`GET /api/session/user` 是车前屏
    轮询的热路径，绝不能因为缺表就 500。`init_db()` 幂等但含迁移检查，故按 DB_PATH
    记忆化：生产上 lifespan 已建过表，这里只是一次字符串比较。

    记忆化的洞：库文件**被删/被换**（路径没变）时，短路会让这个端点一直 500。
    故命中缓存时也要做一次**廉价探活**（`db.get_profile_kind()` 读一下 `profiles` 表）：
    表在就返回，读失败说明库文件被删/被换 → 清掉记忆化重试一次；仍失败就抛出去
    （不吞真实错误——比如目录不可写，重试也没用，必须让调用方看到）。
    """
    global _schema_path
    if _schema_path == db.DB_PATH:
        try:
            db.get_profile_kind("__schema_probe__")     # 廉价探活：表在就返回 ""
            return
        except Exception:                              # noqa: BLE001  库文件被删/换 → 失效重试
            _schema_path = None
    try:
        db.init_db()
    except sqlite3.OperationalError:
        _schema_path = None
        db.init_db()
    _schema_path = db.DB_PATH


def set_session_uid(uid: str, locked: bool) -> dict:
    """手动切换当前会话主体（规格 D11）。

    会话主体与角色的持有权已移交 `session.py`；本函数保留同名接口做转发，
    现有调用点（server 路由 / worker）不变；同时同步 worker 的锁定用户。
    语音不可用时仍返回 ok —— 会话状态独立于语音能力。

    **解锁语义（规格 §4.5）**：`locked=False` = 回集体层、恢复声纹自动判定，
    故**忽略传进来的 uid**（老前端发的是 `setSessionUser(uid ?? "elder_001", false)`；
    若把那个兜底 uid 钉成主体，`_holds_session()` 恒 False，位置自动切换直接停摆）。
    这里做后端 fail-safe，不依赖前端先改。

    返回体在 principal 之上补 `ok`：老接口形状（`{"ok": True, "uid", "locked"}`）
    有既有调用点与用例依赖，不能少。"""
    from ..agent import session as role_session
    _ensure_schema()
    if locked:
        res = dict(role_session.set_subject(uid, True, slot="kiosk", source="manual"))
    else:
        # 解锁 = 回到集体层、恢复声纹自动判定（规格 §4.5）：不许把传进来的 uid 钉成主体
        res = dict(role_session.set_subject(role_session.current_ward() or "", False,
                                            slot="kiosk", source="manual"))
    res["ok"] = True
    if _worker is not None:
        try:
            # **兼容镜像，不参与判定**：worker 的锁定语义一律读会话层（`principal["locked"]`）。
            # 这里的同步只在语音可用时发生，语音降级/管理台登出后镜像会陈旧 —— 所以它
            # 绝不能再当"是否锁定"的依据（规格 §4.5/D11）。
            _worker.locked_uid = uid if locked else None
        except Exception:
            pass
    audit.log("session", action="set_uid", uid=uid, locked=bool(locked), by="nurse")
    return res


def get_session_uid() -> dict:
    """当前会话主体（含角色/槽位/当前病房/TTL/自动切换状态）。

    `uid` 的老语义保留：**没有主体时仍是 None**（老前端靠 null 判断"没选人"），
    此时兼容旧行为看一眼声纹判定结果。"""
    from ..agent import session as session_mod
    _ensure_schema()
    p = session_mod.get_principal("kiosk")
    uid = p["uid"] or None
    if uid is None and _worker is not None:      # 兼容旧行为：没主体时看一眼声纹判定结果
        uid = getattr(_worker, "current_uid", None)
    return {"ok": True, "uid": uid, "locked": p["locked"], "source": p["source"],
            "role": p["role"], "slot": p["slot"], "ward_uid": p["ward_uid"],
            "ttl_remain": session_mod.ttl_remain("kiosk"),
            "auth_required": db.get_admin_auth()["required"],
            "autoswitch": session_mod.autoswitch_state()}

_pending = {}   # recording_id -> {"emb", "segments", "wav", "ts"}（录制暂存，TTL 后清理）


def _wav_bytes(samples) -> bytes:
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


def _stream_fn(client, model):
    """(uid, text) -> chat_stream 事件迭代器（worker 流式问答消费，任务5）。

    principal 由顶层角色会话层提供（权限与数据注入口径的唯一来源，R1）——
    这里的 `session` 是**角色会话层**，与 `LLM.voice.session`（语音状态机）无关，
    故用 role_session 别名避免误读。"""
    def _fn(uid, text):
        settings = db.get_settings()
        from ..agent import session as role_session
        # 第 5 个参数传空串 = "不在请求里指定档位"：语音轮次不过前端，档位只能来自
        # settings.thinking_mode（前端按钮落库）。传 "auto" 会把用户的手动档位顶掉。
        # 见规格 docs/superpowers/specs/2026-09-17-thinking-mode-switch-design.md D2。
        return chat.chat_stream(client, model, uid, text, "", settings,
                                principal=role_session.get_principal("kiosk"))
    return _fn


def _best_effort_text_tts_error(action, error):
    """记录文本播报错误；审计故障不能反向影响聊天请求。"""
    try:
        audit.log("voice_error", action=action, error=str(error)[:200])
    except Exception:
        pass


def begin_text_reply():
    """开始一轮文本增量播报；语音不可用时静默降级。"""
    if not _VOICE_AVAILABLE or _worker is None:
        return None
    try:
        settings = db.get_settings()
        if not settings.get("voice_enabled", True) or not settings.get("tts_enabled", True):
            return None
        return _worker.begin_text_reply(settings)
    except Exception as e:
        _best_effort_text_tts_error("text_tts_begin", e)
        return None


def feed_text_reply(handle, delta):
    """向文本增量播报句柄投递一段文本；失败时静默降级。"""
    if handle is None or not delta:
        return None
    try:
        handle.feed(delta)
    except Exception as e:
        _best_effort_text_tts_error("text_tts_feed", e)
    return None


def end_text_reply(handle, flush_tail=True):
    """结束文本增量播报；失败时静默降级。"""
    if handle is None:
        return None
    try:
        handle.finish(flush_tail=flush_tail)
    except Exception as e:
        _best_effort_text_tts_error("text_tts_end", e)
    return None


def start_voice(client, model, post_turn_fn):
    global _worker
    if not _VOICE_AVAILABLE:
        audit.log("voice_degraded", action="start_voice", error=_degraded_msg())
        print("[WARN] " + _degraded_msg() + "，后端继续以无语音模式运行")
        return None
    if not db.get_settings().get("voice_enabled", True):
        return None
    _worker = worker_mod.VoiceWorker(_stream_fn(client, model), post_turn_fn,
                                     publish_fn=bus.publish)
    _worker.start()
    return _worker


def stop_voice():
    global _worker
    if not _VOICE_AVAILABLE:
        return
    if _worker is not None:
        _worker.stop()
        _worker = None


def get_status():
    settings = db.get_settings()
    prov = settings.get("asr_provider", "local")
    tts_prov = settings.get("tts_provider", "cloud")
    if not _VOICE_AVAILABLE:
        return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
                "status": "unavailable", "modules": {}, "speakers": [],
                "asr_provider": prov, "tts_provider": tts_prov,
                "reason": _degraded_msg()}
    if _worker is None:
        return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
                "status": "stopped", "modules": {}, "speakers": list_speakers(),
                "asr_provider": prov, "tts_provider": tts_prov}
    return {"ok": True, "voice_enabled": settings.get("voice_enabled", True),
            "status": _worker.status, "modules": dict(_worker.sub_status),
            "speakers": list_speakers(), "asr_provider": prov,
            "tts_provider": tts_prov}


def list_speakers():
    if not _VOICE_AVAILABLE:
        return []
    return sorted(p.stem for p in voice_config.SPEAKER_DIR.glob("*.npz"))


def record_speaker(seconds: int = 15, uid: str = None) -> dict:
    """录 seconds 秒 → VAD 切段 → 提特征，暂存内存（特征+音频），返回 recording_id。
    不落档；由 commit_speaker 提交入档，discard_recording 丢弃。"""
    if not _VOICE_AVAILABLE:
        return {"ok": False, "error": _degraded_msg()}
    global _recognizer
    if _worker is not None:
        try:
            _worker.src.stop()
        except Exception:
            pass  # worker 存在但音频源异常时不应拖垮录制
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
        audit.log("voice_spk", action="record", rid=rid, segments=len(segs), uid=uid)
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
        _sync_worker_profiles()
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


def _sync_worker_profiles():
    """档案变更后同步语音 worker 的内存档案（worker 存在时），否则静默跳过。"""
    try:
        if _worker is not None:
            spk = getattr(_worker, "spk", None)
            if spk is not None and hasattr(spk, "reload"):
                spk.reload()
    except Exception:
        pass


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
        _sync_worker_profiles()
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
    out = {}
    for uid in _recognizer.list_profiles():
        try:
            out[uid] = {"samples": _recognizer.sample_count(uid)}
        except ValueError:
            continue   # 跳过非白名单遗留档案（如修复前的中文 uid）
    return out


def enroll_speaker(uid: str, seconds: int = 15) -> dict:
    """旧行为兼容：录 seconds 秒并覆盖建档（等效 record + commit append=False）。"""
    res = record_speaker(seconds, uid=uid)
    if not res.get("ok"):
        return {"ok": False, "uid": uid, "error": res.get("error", "录制失败")}
    return commit_speaker(res["recording_id"], uid, append=False)
