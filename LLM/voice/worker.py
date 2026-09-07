# -*- coding: utf-8 -*-
r"""语音后台线程：编排 采集→VAD→(KWS|流式ASR)→声纹→LLM→TTS，含心跳与降级兜底。

流式 ASR 编排（本地 sherpa / 云端火山同一接口，asr.py / asr_cloud.py）：
  - VAD 继续负责"一句话开始/结束"；LISTENING 期间把音频块喂 VAD，
    VAD 判定开口即启动 ASR 会话（回补 0.5s 前沿防丢句首），边说边喂、文本变化
    即广播 voice_state=asr_partial（前端实时字幕）；
  - VAD 弹出一整句 → ASR finish() 收尾拿最终文本 → 走原声纹/LLM/TTS 链路
    （recognized 事件不变）；起会话失败等异常由整段一次性转写兜底（旧语义）。
  - 引擎选择：启动时读 settings.asr_provider（local|cloud），重启生效。
  - 防自声识别：播报(Speaking)期间 VAD 会把机器人自己的 TTS 回声当语音——结束/打断时
    _flush_vad() 清掉自声段，自然播报结束再叠加 config.SPEAK_TAIL_BLANK_S 回声静音窗，
    避免"自己的话被 ASR 识别 → 自问自答"；打断式插话不套静音窗（要立即收音）。

稳健性核心：run() 顶层 try/except 兜住初始化；主循环任何异常只记审计 + 退避重连，
绝不向调用方（FastAPI 事件循环）抛出 → 主程序不崩。"""
import threading
import time

import numpy as np

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
        self.locked_uid = None       # 手动锁定用户（None=未锁定，规格 D11）
        self.src = self.sink = None
        self.vad = self.kws = self.asr = self.tts = self.spk = self.fusion = None
        self.session = session_mod.Session()
        self._speak_started = None
        self._speak_ended_at = None   # 播报自然结束时刻（回声尾巴静音窗用；打断时置 None）
        # 流式 ASR 会话状态
        self._asr_tail = []          # 开口判定前的音频前沿缓冲（防丢句首）
        self._asr_active = False     # 当前是否处于一句话的 ASR 流式会话中
        self._last_partial = ""      # 最近一次广播的 partial 文本（去重）

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

    def _publish(self, event_type: str, **kw):
        """事件广播兜底：publish 失败不影响语音主循环。"""
        if self.publish_fn:
            try:
                self.publish_fn(event_type, **kw)
            except Exception:
                pass

    # ---- 运行时构建（懒加载；失败即抛给 run 降级）----
    def _build_runtime(self):
        self.src = audio.AudioSource()
        self.sink = audio.AudioSink()
        self.vad = vad_mod.VAD()
        self.kws = kws_mod.WakeWordDetector()
        provider = db.get_settings().get("asr_provider", "local")
        if provider == "cloud":
            from . import asr_cloud as cloud_mod   # 缺 websocket-client / key 时构造抛错 → 上层降级
            self.asr = cloud_mod.CloudStreamASR()
        else:
            provider = "local"
            self.asr = asr_mod.StreamASR()
        self.sub_status["asr"] = provider          # /api/voice/status modules 显示当前引擎
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

        st = self.session.state
        if st == session_mod.State.IDLE:
            hit = self.kws.accept(chunk)
            if hit:
                self.session.wake()
                audit.log("voice_wake", keyword=hit)
                # I-2：唤醒即进入 LISTENING，发 listening（前端 VoiceStatusBar 映射"正在听…"）
                self._publish("voice_state", state="listening")

        elif st == session_mod.State.LISTENING:
            # 播报自然结束后的回声尾巴静音窗：丢弃麦克风块（不喂 VAD/ASR），
            # 防止机器人自己的 TTS 回声被当用户语句（自问自答）；打断式插话不设此窗
            if self._speak_ended_at is not None:
                if (time.monotonic() - self._speak_ended_at) < config.SPEAK_TAIL_BLANK_S:
                    pass
                else:
                    self._speak_ended_at = None
                    self._listen_chunk(chunk, settings)
            else:
                self._listen_chunk(chunk, settings)
            seg = self.vad.pop_speech()
            if seg is not None:
                self._finish_utterance(seg, settings)
            if self.session.expire() == session_mod.State.IDLE:
                # 30s 免唤醒窗口超时 → 回待机，前端同步（否则状态条停在"正在听…"）
                self._speak_ended_at = None
                self._reset_asr()
                self._publish("voice_state", state="idle")

        elif st == session_mod.State.SPEAKING:
            self.vad.accept(chunk)   # 打断检测依赖 VAD 持续喂入
            if (self._speak_started is not None
                    and (time.monotonic() - self._speak_started) > config.BARGE_IN_GRACE_S
                    and self.vad.is_speech_now()):
                self.sink.stop()
                self.session.barge_in()
                audit.log("voice_barge_in")
                self._reset_asr()
                self._flush_vad()          # 清掉播报期间攒的自声段
                self._speak_ended_at = None  # 老人正在说话：立即收音，不套回声静音窗
            if self.sink.is_done() and self.session.finish_speaking():
                # SPEAKING → LISTENING（回到 30s 免唤醒收音窗口）；打断已切走状态则跳过
                self._speak_started = None
                self._flush_vad()              # 丢掉播报期间 VAD 攒下的自声段
                self._speak_ended_at = time.monotonic()   # 回声尾巴静音窗起点
                # 播报完成仍处收音窗口：发 listening（前端"正在听…"），
                # 不是 idle——30s 超时回 IDLE 由 expire() 处理
                self._publish("voice_state", state="listening")

    # ---- 流式 ASR 编排 ----
    def _listen_chunk(self, chunk, settings):
        """LISTENING：喂 VAD；检测到开口 → 起 ASR 会话边喂边出字（识别关闭时只跑 VAD）。"""
        self.vad.accept(chunk)
        if not settings.get("asr_enabled", True):
            return
        if not self._asr_active:
            # 前沿缓冲：VAD 判定需要窗口，缓存最近 ~0.5s，开口瞬间回补防丢句首
            self._asr_tail.append(chunk)
            cap = int(config.ASR_ONSET_TAIL_S * config.SAMPLE_RATE // config.BLOCK_SAMPLES) + 1
            if len(self._asr_tail) > cap:
                self._asr_tail.pop(0)
            if self.vad.is_speech_now():
                tail, self._asr_tail = self._asr_tail, []
                try:
                    self.asr.start_session()
                    if tail:
                        self.asr.accept(np.concatenate(tail))
                    self._asr_active = True
                    self._last_partial = ""
                except Exception as e:
                    audit.log("voice_error", action="asr_start", error=str(e))
                    # 起会话失败：本句放弃流式，等 VAD 整段弹出走一次性转写兜底
        else:
            try:
                partial = self.asr.accept(chunk)
            except Exception as e:
                audit.log("voice_error", action="asr_accept", error=str(e))
                self._reset_asr()
                return
            if partial and partial != self._last_partial:
                self._last_partial = partial
                self._publish("voice_state", state="asr_partial", text=partial)

    def _finish_utterance(self, seg, settings):
        """VAD 弹出一整句：取最终文本 → 声纹/LLM 链路（seg 供声纹）。"""
        if not settings.get("asr_enabled", True):
            return
        if self._asr_active:
            try:
                text = self.asr.finish()
            except Exception as e:
                audit.log("voice_error", action="asr_finish", error=str(e))
                text = ""
            had_partial = bool(self._last_partial)
            self._reset_asr()
            if not text and had_partial:
                # 收尾无结果（如一句空白/云端失败）：清掉前端残留的字幕
                self._publish("voice_state", state="asr_partial", text="")
        else:
            # 无流式会话（开口判定丢失/起会话失败等）→ 旧式整段一次性转写兜底
            try:
                text = self.asr.transcribe(seg)
            except Exception as e:
                audit.log("voice_error", action="asr_transcribe", error=str(e))
                text = ""
            self._last_partial = ""
        if text:
            self._handle_speech(seg, text, settings)

    def _flush_vad(self):
        """丢弃 VAD 缓冲：播报结束/打断时清掉自声段，防止自己的话被当用户语句识别。
        reset 优先（连缓冲中的进行中语音一起清）；个别版本不支持时退化为丢弃已完成段。"""
        try:
            self.vad.reset()
        except Exception:
            try:
                while self.vad.pop_speech() is not None:
                    pass
            except Exception:
                pass

    def _reset_asr(self):
        """放弃当前流式 ASR 会话并清状态（句中断/超时回待机/打断时调用）。"""
        if self._asr_active:
            try:
                self.asr.abort()
            except Exception:
                pass
            self._asr_active = False
        self._asr_tail = []
        self._last_partial = ""

    def _handle_speech(self, seg, text, settings):
        self.session.note_speech()
        audit.log("voice_asr", text=text[:200])

        vote = self.fusion.resolve(seg)
        uid = id_mod.effective_uid(vote, self.current_uid, self.locked_uid)
        # 锁定时识别到锁定外用户：只记审计提示，不切换（规格 §8.2 行为矩阵）
        if self.locked_uid and vote.candidate_uid and vote.candidate_uid != self.locked_uid:
            audit.log("voice_spk", action="locked_ignored", locked=self.locked_uid,
                      detected=vote.candidate_uid, score=round(vote.confidence, 3))
        prev_uid = self.current_uid
        self.current_uid = uid or self.current_uid
        audit.log("voice_spk", identified=(vote.candidate_uid is not None),
                  uid=vote.candidate_uid, score=round(vote.confidence, 3))

        chat_uid = self.current_uid or "elder_001"
        # I-1：声纹识别切换了用户（或首次识别出用户）→ 广播 user_changed，
        # 与 server.py 手动切换的广播格式一致，前端状态条/admin toast 同步
        if self.current_uid and self.current_uid != prev_uid:
            self._publish("user_changed", uid=chat_uid,
                          locked=bool(self.locked_uid), source="voiceprint")
        self._publish("voice_state", state="recognized", uid=chat_uid, text=text)
        reply = self.chat_fn(chat_uid, text)
        if self.post_turn_fn:
            try:
                self.post_turn_fn(chat_uid, text, reply)
            except Exception:
                pass
        self._publish("chat_new", uid=chat_uid, user=text, assistant=reply)
        if reply and settings.get("tts_enabled", True):
            self._speak(reply)

    def _speak(self, text):
        samples, sr = self.tts.synthesize(text)
        self._speak_started = time.monotonic()
        self._speak_ended_at = None
        self.session.start_speaking()
        self.sink.play(samples, sr)
        audit.log("voice_tts", text=text[:100], ms=len(samples) * 1000 // sr)
        self._publish("voice_state", state="speaking", text=text)

    def stop(self):
        self._stop.set()
        self._reset_asr()
        if self.asr is not None:
            try:
                self.asr.close()
            except Exception:
                pass
        if self.src:
            self.src.stop()
        if self.sink:
            self.sink.stop()
