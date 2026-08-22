# -*- coding: utf-8 -*-
r"""下载并解压语音模型到 LLM/models/voice/。

用法：.venv\Scripts\python.exe scripts\fetch_voice_models.py
（Linux 下：.venv/bin/python scripts/fetch_voice_models.py）

优先 GitHub release（官方源）；不可达时自动回退镜像：
  - KWS / silero_vad：modelscope（pkufool 镜像仓库）
  - ASR / TTS：hf-mirror.com（HuggingFace 镜像，csukuangfj 仓库）
"""
import sys, tarfile, urllib.request, shutil, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "LLM" / "models" / "voice"
DEST.mkdir(parents=True, exist_ok=True)

# 官方源（GitHub release）
GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
# 镜像源
MS_KWS = "https://modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/resolve/master"
HF_ASR = "https://hf-mirror.com/csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/resolve/main"
HF_TTS = "https://hf-mirror.com/csukuangfj/sherpa-onnx-vits-zh-ll/resolve/main"

KWS_DIR = DEST / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
ASR_DIR = DEST / "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"
TTS_DIR = DEST / "sherpa-onnx-vits-zh-ll"


def _download(url: str, out: Path, timeout: int = 600, total_cap_s: float | None = None) -> None:
    """下载 url 到 out。timeout 为单次 socket 操作超时；total_cap_s 为总时长硬上限
    （urllib 的 timeout 只限单次 read，数据缓慢滴答时总时长可能失控，需手动截止）。"""
    print(f"[download] {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r, open(out, "wb") as f:
        while True:
            b = r.read(65536)
            if not b:
                break
            f.write(b)
            if total_cap_s is not None and (time.time() - t0) > total_cap_s:
                raise TimeoutError(f"下载超过 {total_cap_s:.0f}s 总时长上限，放弃（源过慢）")


def _try_tarball(key: str, url: str, dest_dir: Path) -> bool:
    """尝试从 GitHub 拉 tar.bz2 并解压；慢/失败（网络/超时）返回 False 走镜像。
    GitHub 海外源在本机可能极慢（~24KB/s），总时长限 40s，超时即回退。"""
    marker = DEST / f".{key}.done"
    if marker.exists():
        print(f"[skip] {key}")
        return True
    out = DEST / f"{key}.tar.bz2"
    try:
        _download(url, out, timeout=45, total_cap_s=40)
    except Exception as e:
        print(f"[warn] {key} GitHub 源失败（{e}），改用镜像逐文件下载")
        out.unlink(missing_ok=True)
        return False
    print(f"[extract] {key}")
    with tarfile.open(out) as t:
        t.extractall(DEST)
    out.unlink()
    marker.write_text("done")
    return True


def _fetch_files(key: str, dest_dir: Path, base_url: str, files: list[str]) -> None:
    marker = DEST / f".{key}.done"
    if marker.exists():
        print(f"[skip] {key}")
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        out = dest_dir / f
        if not out.exists():
            _download(f"{base_url}/{f}", out)
    marker.write_text("done")


def main():
    # 1) silero_vad.onnx：优先本地调研残留（同源文件），否则 GitHub，再回退 hf-mirror
    out = DEST / "silero_vad.onnx"
    if not out.exists():
        local = ROOT / ".research_sherpa" / "silero_vad.onnx"
        if local.exists():
            print(f"[copy] 复用调研残留 {local}")
            shutil.copyfile(local, out)
        else:
            for label, url in (
                ("github", f"{GH}/asr-models/silero_vad.onnx"),
                ("hf-mirror", "https://hf-mirror.com/csukuangfj/silero-vad/resolve/main/silero_vad.onnx"),
            ):
                try:
                    _download(url, out)
                    break
                except Exception as e:
                    print(f"[warn] silero_vad {label} 失败: {e}")
                    out.unlink(missing_ok=True)

    # 2) KWS（zipformer-wenetspeech-3.3M）
    kws_files = ["tokens.txt",
                 "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                 "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
                 "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"]
    if not _try_tarball("kws", f"{GH}/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2", KWS_DIR):
        _fetch_files("kws", KWS_DIR, MS_KWS, kws_files)

    # 3) ASR（streaming-zipformer-zh-14M）
    asr_files = ["tokens.txt", "encoder-epoch-99-avg-1.onnx",
                 "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.onnx"]
    if not _try_tarball("asr", f"{GH}/asr-models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23.tar.bz2", ASR_DIR):
        _fetch_files("asr", ASR_DIR, HF_ASR, asr_files)

    # 4) TTS（vits-zh-ll）
    tts_files = ["model.onnx", "lexicon.txt", "tokens.txt",
                 "phone.fst", "date.fst", "number.fst"]
    if not _try_tarball("tts", f"{GH}/tts-models/sherpa-onnx-vits-zh-ll.tar.bz2", TTS_DIR):
        _fetch_files("tts", TTS_DIR, HF_TTS, tts_files)

    print("done")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
