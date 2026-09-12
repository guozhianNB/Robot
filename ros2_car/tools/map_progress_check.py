#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建图进度校验：同一位置连存两次图，比对 MD5；再走一段，再存一次，判断地图是否在更新。

用法（板卡，已 source 环境）:
    python3 tools/map_progress_check.py --tag before
    python3 tools/map_progress_check.py --tag after

输出：每次存图的 MD5 与尺寸，便于人工比对（相同 = 地图未更新）。
"""
import argparse
import hashlib
import os
import subprocess
import sys
import time


def save_map(prefix):
    """调 map_saver_cli；async slam 的 /map 按需发布，可能失败 → 重试。"""
    env = os.environ.copy()
    for attempt in range(1, 5):
        r = subprocess.run(
            ['ros2', 'run', 'nav2_map_server', 'map_saver_cli', '-f', prefix],
            capture_output=True, text=True, timeout=90, env=env)
        pgm = prefix + '.pgm'
        if os.path.exists(pgm) and os.path.getsize(pgm) > 100:
            return True, attempt
        print(f'  第 {attempt} 次存图失败（{r.returncode}），重试…')
        time.sleep(3)
    return False, 0


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', required=True)
    ap.add_argument('--dir', default=os.path.expanduser('~/map_checks'))
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    prefix = os.path.join(args.dir, f'map_{args.tag}')
    ok, tries = save_map(prefix)
    if not ok:
        print(f'[FAIL] 存图失败（{args.tag}）')
        return 1
    pgm, yaml = prefix + '.pgm', prefix + '.yaml'
    size = os.path.getsize(pgm)
    print(f'[{args.tag}] 存图成功（第 {tries} 次尝试）')
    print(f'   {pgm}  {size} B  md5={md5(pgm)}')
    if os.path.exists(yaml):
        with open(yaml) as f:
            for line in f:
                if line.strip():
                    print('   yaml:', line.strip())
    return 0


if __name__ == '__main__':
    sys.exit(main())
