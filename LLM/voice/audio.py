# -*- coding: utf-8 -*-
r"""音频采集/播放抽象（sounddevice）。唯一接触声卡的模块。

稳健性（规格 D7）：sounddevice / PortAudio 缺失或加载失败时，本模块仍可被导入，
具体采集/播放操作抛 RuntimeError，由 worker 捕获后降级 —— 绝不让导入链崩掉主程序。

播放线程模型（防原生崩溃，2026-09-07）：
  - 写入线程**分块阻塞写**（每块 ~0.2s），不再把整段音频一次性 write —— 最长播报
    可达数十秒（一次数十万帧的阻塞写），打断时与另一线程 abort()/close() 并发是
    PortAudio 访问违规的典型诱因；
  - 流对象**只由写入线程创建并唯一一次关闭**；stop() 只置停止事件 + abort() 唤醒
    阻塞中的 write，然后 join 写入线程 —— 消除"stop() close 与写线程 finally 再
    close 同一流"的双关闭竞态。
  - 播放队列（AudioSink，2026-09-07）：enqueue(samples) 把 16k 样本段追加进内部
    队列（首个段启动输出流与写线程）；end_of_stream() 标记"无更多段"，写线程在
    队列清空后自行收流（自然播完）；stop() 打断=置停止事件 + 清队列 + abort 唤醒
    + join（barge-in）；写线程从队列取段、段内分块连续写（句间无缝），仍是输出流
    唯一创建/关闭者。play(samples, sample_rate) 兼容旧调用（= stop + 复位 + enqueue）。
"""
import threading
import time

import numpy as np

try:
    import sounddevice as sd
    _SD_OK = True
except Exception:   # PortAudio 缺失 / 无音频库：仅音频能力降级，不影响主程序
    sd = None
    _SD_OK = False

from . import config


def _require_sd():
    if not _SD_OK:
        raise RuntimeError("sounddevice/PortAudio 不可用：音频采集与播放已降级")


class AudioSource:
    """16k 单声道采集。read() 阻塞取一块；设备掉线时抛 PortAudioError。"""

    def __init__(self, sample_rate=config.SAMPLE_RATE, block_samples=config.BLOCK_SAMPLES):
        self.sample_rate = sample_rate
        self.block_samples = block_samples
        self._stream = None

    def start(self):
        _require_sd()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            blocksize=self.block_samples)
        self._stream.start()

    def read(self) -> np.ndarray:
        _require_sd()
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
