# -*- coding: utf-8 -*-
r"""
把地图编辑器需要的 5 个外部资源（上游 editor.html 里的 CDN）落成本地自托管文件。

背景（规格 §B6.1 改动 1~5、§B9 坑 1~2）：
  * 上游 `ROS-SLAM-Map-Editor/editor.html` 用 5 条 CDN 引 jQuery 3.4.1 / js-yaml 4.1.0 /
    Bootstrap 4.4.1（CSS+JS）/ Font Awesome 4.7.0；jQuery 与 js-yaml 是**硬依赖**，
    不通就整页白屏，所以「完全离线可用」必须靠本地 vendor。
  * Font Awesome 的 CSS 用**相对路径**引 `../fonts/*`，目录结构必须照搬。

用法（在项目根目录，任意 Python 3 均可，只用 stdlib）：

    .venv\Scripts\python.exe scripts/vendor_mapeditor_assets.py

产物落点（规格 §B3.3）::

    frontend/packages/mapeditor/public/vendor/
      jquery-3.4.1.min.js
      js-yaml-4.1.0.min.js
      bootstrap-4.4.1.min.css
      bootstrap-4.4.1.bundle.min.js
      font-awesome-4.7.0/css/font-awesome.min.css
      font-awesome-4.7.0/fonts/*
      ROS-SLAM-Map-Editor.LICENSE      ← 上游 MIT 原文（衍生作品必须保留）

幂等：文件已存在且大小一致则跳过（`--force` 强制重下）。
本脚本**不进后端导入链**，只在需要补资源时手工跑一次。
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENDOR = BASE_DIR / "frontend" / "packages" / "mapeditor" / "public" / "vendor"
UPSTREAM = BASE_DIR / "ROS-SLAM-Map-Editor"

# 镜像优先：国内环境直连官方 CDN 常失败（本项目实测 schannel 拿不到凭证）
REGISTRIES = [
    "https://registry.npmmirror.com",
    "https://registry.npmjs.org",
]

# (包名, 版本, 包内路径, 落地相对路径)
FILES = [
    ("jquery", "3.4.1", "package/dist/jquery.min.js", "jquery-3.4.1.min.js"),
    ("js-yaml", "4.1.0", "package/dist/js-yaml.min.js", "js-yaml-4.1.0.min.js"),
    ("bootstrap", "4.4.1", "package/dist/css/bootstrap.min.css", "bootstrap-4.4.1.min.css"),
    ("bootstrap", "4.4.1", "package/dist/js/bootstrap.bundle.min.js", "bootstrap-4.4.1.bundle.min.js"),
    ("font-awesome", "4.7.0", "package/css/font-awesome.min.css",
     "font-awesome-4.7.0/css/font-awesome.min.css"),
]
FONTS_PREFIX = "package/fonts/"          # Font Awesome 的 webfonts，全量搬
FONTS_DST = "font-awesome-4.7.0/fonts"


def _download(pkg: str, ver: str) -> bytes:
    last = None
    for reg in REGISTRIES:
        url = f"{reg}/{pkg}/-/{pkg}-{ver}.tgz"
        try:
            print(f"  [get] {url}")
            with urllib.request.urlopen(url, timeout=120) as r:   # noqa: S310 (固定 https)
                return r.read()
        except Exception as e:                                     # noqa: BLE001
            print(f"  [warn] {reg} 失败：{type(e).__name__}: {e}")
            last = e
    raise RuntimeError(f"{pkg}@{ver} 全部镜像下载失败：{last}")


def _extract(tgz: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            f = tf.extractfile(m)
            if f is not None:
                out[m.name] = f.read()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="下载并自托管地图编辑器的 5 个前端资源")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()

    VENDOR.mkdir(parents=True, exist_ok=True)
    (VENDOR / FONTS_DST).mkdir(parents=True, exist_ok=True)

    # 按包缓存，避免 bootstrap 下两次
    cache: dict[tuple[str, str], dict[str, bytes]] = {}
    written = skipped = 0

    for pkg, ver, src, rel in FILES:
        dst = VENDOR / rel
        if dst.exists() and not args.force:
            print(f"  [skip] {rel}（已存在 {dst.stat().st_size} B）")
            skipped += 1
            continue
        key = (pkg, ver)
        if key not in cache:
            cache[key] = _extract(_download(pkg, ver))
        blob = cache[key].get(src)
        if blob is None:
            print(f"  [ERROR] {pkg}@{ver} 里找不到 {src}")
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
        print(f"  [ok]   {rel}  {len(blob)} B")
        written += 1

    # Font Awesome webfonts（CSS 用相对路径 ../fonts/ 引，必须同结构）
    key = ("font-awesome", "4.7.0")
    if key not in cache:
        cache[key] = _extract(_download(*key))
    fonts = {k: v for k, v in cache[key].items() if k.startswith(FONTS_PREFIX)}
    if not fonts:
        print("  [ERROR] font-awesome 包里没有 fonts/")
        return 1
    for name, blob in sorted(fonts.items()):
        dst = VENDOR / FONTS_DST / name[len(FONTS_PREFIX):]
        if dst.exists() and not args.force and dst.stat().st_size == len(blob):
            skipped += 1
            continue
        dst.write_bytes(blob)
        print(f"  [ok]   {FONTS_DST}/{dst.name}  {len(blob)} B")
        written += 1

    # 上游 MIT 原文（衍生作品必须随附）
    lic_dst = VENDOR / "ROS-SLAM-Map-Editor.LICENSE"
    src_lic = UPSTREAM / "LICENSE"
    if src_lic.exists():
        if not lic_dst.exists() or args.force:
            shutil.copyfile(src_lic, lic_dst)
            print(f"  [ok]   ROS-SLAM-Map-Editor.LICENSE  {lic_dst.stat().st_size} B")
            written += 1
        else:
            skipped += 1
    else:
        print(f"  [warn] 找不到上游 LICENSE（{src_lic}），跳过")

    # 核对 5 条 CDN 是否都已落地（缺一条就是白屏/图标空框）
    missing = [rel for *_, rel in FILES if not (VENDOR / rel).exists()]
    if missing:
        print("[FAIL] 缺失：" + ", ".join(missing))
        return 1
    print(f"\n完成：写入 {written} 个、跳过 {skipped} 个 → {VENDOR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
