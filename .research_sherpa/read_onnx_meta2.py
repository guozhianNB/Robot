# -*- coding: utf-8 -*-
import onnxruntime as ort
import os
import time

for p in [
    r"D:\_project\Robot\.research_sherpa\vits_zh_ll_model.onnx",
]:
    print("== ", os.path.basename(p), round(os.path.getsize(p) / 1e6, 1), "MB", flush=True)
    t0 = time.time()
    sess = ort.InferenceSession(p, providers=["CPUExecutionProvider"])
    print("loaded in", round(time.time() - t0, 1), "s", flush=True)
    meta = sess.get_modelmeta()
    for k in sorted(meta.custom_metadata_map.keys()):
        print("  ", k, "=", meta.custom_metadata_map[k], flush=True)
print("DONE", flush=True)
