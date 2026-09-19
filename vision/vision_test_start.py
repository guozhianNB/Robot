# -*- coding: utf-8 -*-
r"""人脸测试台（图形界面）—— 本项目人脸能力的**唯一 GUI 入口**。

用法
----
    .\.venv\Scripts\python.exe vision\vision_test_start.py

一级小窗三个按钮：

    ① 人脸录入   打开二级小窗（实时画面）→ 镜头前有脸才让点「开始录入」
                 → 弹三级小窗填姓名/称呼/床位/年龄（uid 自动 = 前一个 uid + 1）
                 → 显示「已录入」，3 秒后回到一级小窗
                 **镜头前没检测到人脸 → 直接退出，档案与样本库都不动**
    ② 人脸检测   打开二级小窗，实时把 YOLO 检出的人脸画出来，并拿后台库比对，
                 把老人的**姓名显示在对应的人头上**（不在库里显示"未知"）
    ③ 删除数据   打开二级小窗，按姓名反查 uid，确认后连根删除（档案+人脸样本+声纹+
                 记忆+提醒+历史），删完回到一级小窗

退出：**按 Ctrl+C**（或在窗口里点关闭）。

它包装的命令（与手工敲完全是同一条路径）
----------------------------------------
    人脸录入  python scripts\update_elder.py <uid> --name 姓名 --nickname 称呼 --bed 床位 --age 年龄
    人脸检测  python scripts\face_check.py --live
    删除数据  python scripts\update_elder.py <uid> --delete --yes

有一处**故意的实现差异**，说明白免得你以为漏了：
"人脸检测"不是去 `subprocess` 起 `face_check.py --live`，而是**在同一个进程里调它内部
那套判定**（`LLM.face_api.analyze/probe` + 同一个 `camera_server`）。原因是硬约束：
`face_check.py --live` 会自己再起一个 `camera_server`，而**摄像头同一时刻只能被一个进程
独占**，两个服务必然抢设备、后起的那个直接打不开。同进程复用还有一个好处 ——
画面与框来自**同一帧**，不会"框追不上脸"。判定门槛、轨迹稳定逻辑与
`face_check.py --live` 用的是同一份代码，所以结果完全可比。

自测选项（给你或自动化用）
--------------------------
    --source mock                没有摄像头也能把界面/通路跑起来（合成帧，画面里没人脸）
    --open enroll|detect|delete  启动后自动打开某个二级小窗
    --click start_enroll|find|delete   + --click-arg 文本：自动点一下
    --selftest 8                 启动 8 秒后自动关闭退出（配合 --auto 做冒烟测试）
    --auto                       非交互：弹窗改成打印；--auto-yes 让确认类弹窗答"是"
"""

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def build_parser():
    p = argparse.ArgumentParser(
        prog="python vision/vision_test_start.py",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="人脸测试台：人脸录入 / 人脸检测（名字画在头上）/ 删除数据")
    p.add_argument("--source", default="auto",
                   choices=("auto", "webcam", "mipi", "mock"),
                   help="摄像头来源：auto（板卡优先 mipi、PC 优先 webcam）/ mock（无摄像头自测）")
    p.add_argument("--device", type=int, default=None, help="webcam 设备号（默认 0）")
    p.add_argument("--camera-port", type=int, default=None,
                   help="摄像头共享服务端口（默认自动挑一个空闲端口）")
    p.add_argument("--camera-fps", type=int, default=30, help="采集帧率上限")
    p.add_argument("--channels", default="640x480",
                   help="摄像头通道（逗号分隔，如 640x480 或 1280x720,640x480）。"
                        "只有第 --channel 路会被用上，多要一路只是白费 CPU 与带宽；"
                        "卡顿就先降到这里的分辨率")
    p.add_argument("--camera-wait", type=float, default=25.0,
                   help="等摄像头服务就绪的上限（秒）")
    p.add_argument("--channel", type=int, default=1, help="取第几路画面（camera_server 的通道号）")
    p.add_argument("--enroll-frames", type=int, default=5, help="录入时采样多少张人脸")
    p.add_argument("--preview-width", type=int, default=640, help="预览宽度（像素）")
    p.add_argument("--preview-height", type=int, default=360, help="预览高度（像素）")
    p.add_argument("--open", default="none",
                   choices=("none", "enroll", "detect", "delete"),
                   help="启动后自动打开哪个二级小窗（自测用）")
    p.add_argument("--click", default=None,
                   help="自动点按钮（自测用）；可用逗号串起来，例：find,delete")
    p.add_argument("--click-arg", default=None, help="--click find 时要填进姓名框的文本")
    p.add_argument("--selftest", type=float, default=0.0,
                   help="启动 N 秒后自动关闭（自测用）")
    p.add_argument("--auto", action="store_true",
                   help="非交互：所有弹窗改成打印（自测用）")
    p.add_argument("--auto-yes", action="store_true",
                   help="非交互模式下确认类弹窗一律答「是」（会真删数据，慎用）")
    return p


def banner(args):
    print("=" * 70, flush=True)
    print(" vision_test_start —— 人脸测试台", flush=True)
    print(" 摄像头来源：%s   共享服务端口：%s   采样帧数：%s"
          % (args.source, args.camera_port, args.enroll_frames), flush=True)
    print(" 摄像头**按需打开**：进入录入/检测窗口才开，返回一级小窗就关。", flush=True)
    print(" 退出：按 Ctrl+C（或关掉窗口）。", flush=True)
    print("=" * 70, flush=True)


def main(argv=None):
    args = build_parser().parse_args(argv)

    # 顺序要紧：LLM/conf.py 在 **import 时**就把 VISION_HOST/VISION_PORT 固化下来了，
    # 所以必须先定端口、写进环境变量，再去 import 任何 LLM.* / vision.vtest.* 的东西。
    from vision.vtest.service import free_port
    port = int(args.camera_port or free_port())
    args.camera_port = port
    os.environ["VISION_HOST"] = "127.0.0.1"
    os.environ["VISION_PORT"] = str(port)
    os.environ["PYTHONUTF8"] = "1"

    try:
        import tkinter as tk
    except ImportError as e:                             # pragma: no cover
        print("[vision_test_start] 缺少 tkinter（Python 自带的 GUI 模块）：%s\n"
              "  Windows/macOS 官方 Python 自带；Linux 需要 apt install python3-tk。"
              % e, flush=True)
        return 3
    try:
        import cv2                                     # noqa: F401
    except ImportError:
        print("[vision_test_start] 缺少 opencv-python（预览缩放与编码要用）：\n"
              "  .venv\\Scripts\\pip.exe install opencv-python", flush=True)
        return 4

    from vision.vtest import ui

    banner(args)
    root = tk.Tk()
    app = ui.App(root, args)
    root.protocol("WM_DELETE_WINDOW", app.shutdown)
    if args.open != "none":
        app.open_window(args.open)
    if args.click:
        app.schedule_click(args.click)
    if args.selftest:
        root.after(int(float(args.selftest) * 1000), app.shutdown)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
