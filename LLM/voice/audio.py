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
"""
import threading

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
    """播放：独立线程分块写数据，可从外部 stop() 打断（barge-in）。

    状态约定：流对象由 _run 写入线程创建/唯一关闭；stop() 置事件并 abort() 唤醒；
    is_done() 在流关闭后返回 True。"""

    _WRITE_BLOCK_S = 0.2          # 每次 write 的音频时长（秒），避免整段巨帧阻塞写

    def __init__(self):
        self._lock = threading.Lock()
        self._stream = None
        self._thread = None
        self._stop_write = threading.Event()
        self._write_frames = int(config.SAMPLE_RATE * self._WRITE_BLOCK_S)

    def play(self, samples: np.ndarray, sample_rate: int):
        _require_sd()
        self.stop()
        self._write_frames = max(1, int(sample_rate * self._WRITE_BLOCK_S))
        with self._lock:
            self._stop_write = threading.Event()
            try:
                stream = sd.OutputStream(
                    samplerate=sample_rate, channels=1, dtype="float32")
                stream.start()
            except Exception:
                self._stream = None
                raise
            self._stream = stream
        self._thread = threading.Thread(
            target=self._run, args=(stream, samples), daemon=True)
        self._thread.start()

    def _run(self, stream, samples):
        """分块阻塞写；stop() 触发事件/abort 后退出；本线程是流的唯一关闭者。"""
        n = self._write_frames
        try:
            for i in range(0, len(samples), n):
                if self._stop_write.is_set():
                    break
                stream.write(samples[i:i + n])   # 阻塞 ~0.2s/块；被打断时抛错退出
        except Exception:
            pass                                 # 被打断（abort）或设备丢失，静默结束
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
        with self._lock:
            self._stop_write.set()
            stream = self._stream
            if stream is not None:
                try:
                    stream.abort()               # 唤醒阻塞中的 write（抛错 → 写线程收尾）
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def is_done(self) -> bool:
        with self._lock:
            return self._stream is None
