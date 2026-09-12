# -*- coding: utf-8 -*-
r"""语音后台线程：编排 采集→VAD→(KWS|流式ASR)→声纹→LLM→TTS，含心跳与降级兜底。

流式 ASR 编排（本地 sherpa / 云端火山同一接口，asr.py / asr_cloud.py）：
  - VAD 继续负责"一句话开始/结束"；LISTENING 期间把音频块喂 VAD，
    VAD 判定开口即启动 ASR 会话（回补 0.5s 前沿防丢句首），边说边喂、文本变化
    即广播 voice_state=asr_partial（前端实时字幕）；
  - VAD 弹出一整句 → ASR finish() 收尾拿最终文本 → 走原声纹/LLM/TTS 链路
    （recognized 事件不变）；起会话失败等异常由整段一次性转写兜底（旧语义）。
  - 引擎选择：启动时读 settings.asr_provider（local|cloud），重启生效。
  - 应答编排：recognized 后启动应答线程消费 chat_stream——content 增量逐字广播
    chat_partial（前端上屏），完整句切出后按句合成入队播报（句级 TTS，tts_provider=cloud
    时云端句失败自动回退本地引擎，不中断播报）；老人插话打断 = sink.stop() + _abort 置位
    （仅播放期 barge-in 语义），应答线程停止后续合成/入队；chat_new 与 post_turn 在
    整段流收尾后发（旧 _speak 整段合成已删，改由 _consume_reply/_answer 编排）。
  - 轮次代数防双线程重叠：_start_answer 每轮 self._turn += 1（int 原子读写）抢占"最新轮"
    身份，新轮无条件置位 _answering 接管；think 期老人追问新整句 → 旧轮即便阻塞在
    chat_stream 网络读/合成中，也会在其下一检查点（循环顶/入队闸，self._turn != turn）
    观察到被取代而静默退出——绝无第二条线程并发消费同一流、end_of_stream 不会被调两次；
    消费异常（如设备掉线 enqueue 抛错）由 _consume_reply 吞掉记审计，_answer 仍以最新轮
    身份把已收文本落 chat_new 历史（不丢该轮）。
  - 防自声识别：播报(Speaking)期间 VAD 会把机器人自己的 TTS 回声当语音——结束/打断时
    _flush_vad() 清掉自声段，自然播报结束再叠加 config.SPEAK_TAIL_BLANK_S 回声静音窗，
    避免"自己的话被 ASR 识别 → 自问自答"；打断式插话不套静音窗（要立即收音）。

稳健性核心：run() 顶层 try/except 兜住初始化；主循环任何异常只记审计 + 退避重连，
绝不向调用方（FastAPI 事件循环）抛出 → 主程序不崩。"""
import queue
import threading
import time

import numpy as np

from .. import db, log as audit
from . import config
from . import audio, vad as vad_mod, kws as kws_mod, asr as asr_mod, tts as tts_mod
from . import speaker as spk_mod, identity as id_mod, session as session_mod
from . import tts_buffer                 # LLM 回复分句缓冲（句级 TTS 前置）


class _TextReplyFeed:
    """线程安全地把文本增量转成 VoiceWorker 可消费的事件流。"""

    def __init__(self):
        self._events = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False

    def feed(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            if self._closed:
                return
            self._events.put({"type": "content", "content": delta})

    def finish(self, flush_tail: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._events.put({"type": "done", "flush_tail": bool(flush_tail)})

    def __iter__(self):
        while True:
            event = self._events.get()
            yield event
            if event.get("type") == "done":
                return


class VoiceWorker(threading.Thread):
    def __init__(self, stream_fn, post_turn_fn=None, publish_fn=None):
        super().__init__(daemon=True, name="voice-worker")
        self.stream_fn = stream_fn      # (uid, text) -> iterable[chat_stream 事件 dict]
        self.post_turn_fn = post_turn_fn  # (uid, user_text, assistant) -> None
        self.publish_fn = publish_fn    # 事件广播，可空
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
        # 流式问答编排状态（引擎在 _build_runtime 填充）
        self.tts = self._local_tts = None   # 实际引擎 + 本地兜底引擎
        self._answering = False             # 应答线程活跃标志（防队列瞬时空误判播报结束）
        self._turn = 0                      # 应答轮次代数：_start_answer 每轮自增抢占
        self._abort = threading.Event()     # barge-in 打断应答（仅播放期语义；新轮由 _turn 接管）
        self._text_reply_feed = None        # 当前文本回复事件流（新轮开始时关闭旧流）

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
        provider = db.get_settings().get("asr_provider", "cloud")
        if provider == "cloud":
            try:
                from . import asr_cloud as cloud_mod   # 缺 websocket-client / key 时构造抛错
                self.asr = cloud_mod.CloudStreamASR()
            except Exception as e:
                # 云端不可用（未配 key / 缺依赖）→ 回退本地，语音链路照常，不降级整机
                audit.log("voice_error", action="asr_provider_fallback",
                          provider="cloud", error=str(e))
                provider = "local"
                self.asr = asr_mod.StreamASR()
                self.sub_status["asr_fallback"] = "云端不可用，已回退本地：{}".format(str(e)[:120])
        else:
            provider = "local"
            self.asr = asr_mod.StreamASR()
        self.sub_status["asr"] = provider          # /api/voice/status modules 显示实际引擎
        # TTS：本地引擎始终构造（离线兜底）；tts_provider=cloud 时优先云端，构造失败回退本地
        self._local_tts = tts_mod.TTS()
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
        self.sub_status["tts"] = self.tts.provider   # 本地/云端引擎均声明 provider 类属性
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
                self._abort.set()          # 打断先行：先通知应答线程停手，再停输出流
                self.sink.stop()           # stop() 内 join(≤2s)：期间应答线程已见 abort，不会 enqueue 续播
                self.session.barge_in()
                audit.log("voice_barge_in")
                self._reset_asr()
                self._flush_vad()          # 清掉播报期间攒的自声段
                self._speak_ended_at = None  # 老人正在说话：立即收音，不套回声静音窗
            if self.sink.is_done() and not self._answering \
                    and self.session.finish_speaking():
                # 流式应答线程可能仍在收尾（无句子可播时 _answering 很快复位）；
                # 结束判定已含 not _answering —— 见上
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
        # 流式问答：应答线程消费 chat_stream → 逐字上屏(chat_partial) + 句级 TTS 播放
        self._start_answer(chat_uid, text, dict(settings))

    # ---- 流式问答编排（句级 TTS）----
    def _start_answer(self, uid, user_text, settings):
        """启动应答线程（轮次代数防双线程重叠）。

        每轮 self._turn += 1（int 原子读写）抢占"最新轮"身份，新轮无条件置位
        _answering 接管；_abort 事件只保留播放期 barge-in 语义（SPEAKING 分支置位、
        不带新轮）——打断后旧轮"收尾"（partial 文本仍落 chat_new 历史）。think 期
        老人追问新整句 → 旧轮即便阻塞在 chat_stream 网络读 / 合成中，也会在其下一
        检查点（循环顶 / 入队闸，self._turn != turn）观察到被取代而静默退出，
        绝无第二条线程并发消费同一流。"""
        self._turn += 1
        turn = self._turn
        self._abort.clear()          # 新轮接管：清 barge-in 残留，避免掐死新轮
        self._answering = True       # 无条件置位（覆盖旧值，新轮接管）
        t = threading.Thread(target=self._answer, args=(uid, user_text, settings, turn),
                             daemon=True, name="voice-answer")
        t.start()

    def _answer(self, uid, user_text, settings, turn):
        """应答线程体：消费 → 收尾。收尾权 = 仍是最新轮（turn == self._turn）：
        复位 _answering / 清 abort / 收流 / 落 chat_new + post_turn（W2：消费异常
        也带已收文本落历史）；被新轮取代的轮静默退出——不发 chat_new/post_turn/
        end_of_stream，不复位 _answering（新轮管），仅记审计。"""
        try:
            assistant = self._consume_reply(uid, user_text, settings, turn)
        except Exception as e:
            # 消费内异常已在 _consume_reply 记过（tts_consume）；此处兜底防收尾路径异常
            audit.log("voice_error", action="tts_answer", error=str(e)[:200])
            assistant = ""
        finally:
            if turn == self._turn:
                # 仍是最新轮：正常收尾 —— partial/全文落历史 + post_turn + 收流
                self._answering = False
                self._abort.clear()
                if self.sink is not None:
                    self.sink.end_of_stream()   # 自然播完：队列清空后收流
                self._publish("chat_new", uid=uid, user=user_text, assistant=assistant)
                if self.post_turn_fn:
                    try:
                        self.post_turn_fn(uid, user_text, assistant)
                    except Exception:
                        pass
            else:
                # 已被新轮取代：静默退出（收尾权归新轮），仅记审计
                audit.log("voice_error", action="answer_superseded", uid=uid)

    def _consume_reply(self, uid, user_text, settings, turn):
        """同步消费 chat_stream 事件流（_answer 应答线程内调用；单测直接调用）：
        content → 广播 chat_partial(逐字上屏) → 分句缓冲 → 完整句合成入队（仅 speak 模式）；
        done → flush 尾句。返回完整 assistant 文本。
        turn = 本轮次代数：被新轮取代（self._turn != turn）或 _abort 置位时，检查点
        （循环顶 / 入队闸）立即停止后续消费/合成/入队。
        主循环异常（如设备掉线 enqueue 抛错）→ 记审计后返回已收文本，不抛穿。"""
        consume_settings = dict(settings)
        consume_settings["_voice_uid"] = uid
        events = self.stream_fn(uid, user_text)
        return self._consume_events(events, consume_settings, turn)

    def _consume_events(self, events, settings, turn,
                        publish_text=True, wake_if_idle=False):
        """消费回复事件：按句合成入队，可关闭文字广播或从待机态唤醒播报。"""
        speak = bool(settings.get("tts_enabled", True)) and self.tts is not None
        buf = tts_buffer.SentenceBuffer()
        full = []
        started = threading.Event()        # 首句已入队（确保仅触发一次 speaking/start_speaking）
        flush_tail = True

        def enqueue_sentence(sent):
            clean = tts_mod.sanitize_tts_text(sent)
            if not clean:
                return
            if speak and self.session.state != session_mod.State.SPEAKING \
                    and (self._abort.is_set() or self._turn != turn):
                return                    # 被打断 / 已被新轮取代：不再发声
            chunks = []
            try:
                chunks = list(self.tts.synthesize_chunks(clean))
            except Exception as e:
                audit.log("voice_error",
                          action="tts_cloud_sentence" if self.tts is not self._local_tts
                          else "tts_local_sentence",
                          error=str(e)[:160])
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
                if self._abort.is_set() or self._turn != turn:
                    return                # abort 已置位 / 已被新轮取代：绝不入队/发声（合成期间被打断）
                if not started.is_set():
                    started.set()
                    self._speak_started = time.monotonic()
                    self._speak_ended_at = None
                    if wake_if_idle:
                        self.session.wake()
                    if not self.session.start_speaking():
                        return            # 状态机未在 LISTENING（已被打断）：放弃首句
                    self._publish("voice_state", state="speaking")
                if self.session.state != session_mod.State.SPEAKING \
                        or self._turn != turn:
                    return                # 已被打断 / 被新轮取代（后续句）：不再入队
                self.sink.enqueue(np.concatenate(chunks))
                audit.log("voice_tts", text=clean[:80],
                          provider=self.tts.provider,
                          ms=len(clean) * 250)   # 播报时长粗估（中文 ~4字/秒，仅日志参考）

        if self._turn != turn:
            return ""                     # 尚未进流即已被新轮取代：直接退出
        try:
            for ev in events:
                if self._abort.is_set() or self._turn != turn:
                    break                 # 打断 / 被新轮取代：停止后续消费与分句
                t = ev.get("type")
                if t == "content":
                    delta = ev.get("content") or ""
                    if delta:
                        if publish_text:
                            self._publish("chat_partial",
                                          uid=settings.get("_voice_uid") or self.current_uid or "elder_001",
                                          delta=delta)
                        full.append(delta)
                        if speak:         # 仅 speak 模式分句+合成（非 speak 不逐句调 TTS）
                            for sent in buf.feed(delta):
                                enqueue_sentence(sent)
                elif t == "done":
                    flush_tail = bool(ev.get("flush_tail", True))
                    break
        except Exception as e:
            # 消费异常（如设备掉线 enqueue 抛错）：记审计后带已收文本退出，不抛穿
            audit.log("voice_error", action="tts_consume", error=str(e)[:200])
            return "".join(full)
        if speak and flush_tail:
            tail = buf.flush()
            if tail:
                enqueue_sentence(tail)
        return "".join(full)

    def begin_text_reply(self, settings):
        """启动一轮仅负责播报的增量文本回复，返回其线程安全 feed。"""
        if self.tts is None or self.sink is None:
            return None

        self._abort.set()
        self.sink.stop()
        self.session.barge_in()
        self._turn += 1
        turn = self._turn
        if self._text_reply_feed is not None:
            self._text_reply_feed.finish(flush_tail=False)
        self._abort.clear()
        self._answering = True
        feed = _TextReplyFeed()
        self._text_reply_feed = feed
        t = threading.Thread(target=self._answer_text_reply,
                             args=(feed, dict(settings), turn),
                             daemon=True, name="voice-answer")
        t.start()
        return feed

    def _answer_text_reply(self, feed, settings, turn):
        """文本播报线程体：消费事件，并把收流权留给最新轮。"""
        try:
            self._consume_events(feed, settings, turn,
                                 publish_text=False, wake_if_idle=True)
        except Exception as e:
            audit.log("voice_error", action="tts_answer", error=str(e)[:200])
        finally:
            if turn == self._turn:
                self._answering = False
                self._abort.clear()
                self._text_reply_feed = None
                self.sink.end_of_stream()
            else:
                audit.log("voice_error", action="answer_superseded")

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
