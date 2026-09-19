# -*- coding: utf-8 -*-
r"""vtest —— `vision/vision_test_start.py` 的 GUI 实现包。

**入口只有一个**：`python vision/vision_test_start.py`（或 `python -m vision.vision_test_start`）。
本包里的模块都是它的内部零件，**不要单独运行**（它们没有 `__main__`）：

    service.py    摄像头共享服务子进程的起停（camera_server 的看护者）
    pipeline.py   后台取帧线程：抓帧 → 检测（+可选识别）→ 发布"最新一帧+人脸"
    people.py     档案侧的手脚：读档案、算下一个 uid、按姓名反查 uid、调 update_elder.py
    ui.py         三个 Tkinter 窗口（一级/二级/三级）

分层理由：把"碰摄像头的""碰档案的""画窗口的"分开，前两者能脱离 Tk 单测
（见 `tests/test_vtest.py`），窗口那层只剩摆放控件与刷新。
"""
