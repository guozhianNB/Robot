# -*- coding: utf-8 -*-
r"""示例：从共享服务取一帧保存，并打印服务信息。

用法（先启动服务，再运行本脚本）：

    python3 -m vision.camera_server --mock        # 终端 1
    python3 examples/grab_and_save.py             # 终端 2（仓库根目录）
"""

import os
import sys

# 本文件位于 vision/examples/ 下，仓库根目录往上两级
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from vision.camera_client import CameraClient  # noqa: E402


def main():
    with CameraClient() as cam:
        info = cam.info()
        print("服务信息：mode=%s fps=%s jpeg=%s 通道=%s"
              % (info["mode"], info["fps"], info["jpeg"],
                 list(info["channels"].keys())))

        f = cam.get_frame(channel=1)
        print("取到帧：", f)
        f.save("/tmp/cam_frame_%d.yuv" % f.frame_id)
        print("已保存 /tmp/cam_frame_%d.yuv（NV12 原始帧）" % f.frame_id)

        # 订阅 3 帧连续流
        n = 0
        for frame in cam.frames(channel=1):
            print("  订阅帧：", frame.frame_id)
            n += 1
            if n >= 3:
                break

        if info.get("jpeg"):
            j = cam.get_jpeg(channel=1)
            j.save("/tmp/cam_frame_%d.jpg" % j.frame_id)
            print("已保存 JPEG 帧 /tmp/cam_frame_%d.jpg" % j.frame_id)


if __name__ == "__main__":
    main()
