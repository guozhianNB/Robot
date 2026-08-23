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
                self._publish("voice_state", state="wake")

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
                self._publish("voice_state", state="idle")

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
        self.session.start_speaking()
        self.sink.play(samples, sr)
        audit.log("voice_tts", text=text[:100], ms=len(samples) * 1000 // sr)
        self._publish("voice_state", state="speaking", text=text)

    def stop(self):
        self._stop.set()
        if self.src:
            self.src.stop()
        if self.sink:
            self.sink.stop()
