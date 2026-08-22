# -*- coding: utf-8 -*-
r"""音频采集/播放抽象（sounddevice）。唯一接触声卡的模块。

稳健性（规格 D7）：sounddevice / PortAudio 缺失或加载失败时，本模块仍可被导入，
具体采集/播放操作抛 RuntimeError，由 worker 捕获后降级 —— 绝不让导入链崩掉主程序。
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
    """播放：独立线程写数据，可从外部 stop() 打断（barge-in）。"""

    def __init__(self):
        self._stream = None
        self._lock = threading.Lock()
        self._thread = None

    def play(self, samples: np.ndarray, sample_rate: int):
        _require_sd()
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
