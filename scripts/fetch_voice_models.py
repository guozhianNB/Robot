# -*- coding: utf-8 -*-
r"""下载并解压语音模型到 LLM/models/voice/。
用法：.venv\Scripts\python.exe scripts\fetch_voice_models.py
（Linux 下：.venv/bin/python scripts/fetch_voice_models.py）
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
