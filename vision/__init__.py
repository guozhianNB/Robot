# -*- coding: utf-8 -*-
r"""vision —— MIPI 摄像头共享子系统。

摄像头（libsrcampy/VIO 通道）同一时刻只能被一个进程独占，多个程序直接
打开会冲突。本包提供"摄像头共享服务"：

- camera_server.py  守护进程：唯一持有摄像头，向多客户端分发帧
- camera_client.py  客户端库：CameraClient / Frame
- protocol.py       两端共享的协议定义

启动服务（仓库根目录 Robot/ 下）：

    python3 -m vision.camera_server          # 真实摄像头
    python3 -m vision.camera_server --mock   # 无摄像头自测

详见 vision/README.md。
"""

__version__ = "1.0.0"

from .camera_client import CameraClient, CameraServerError, CameraNotRunning, Frame  # noqa: F401

__all__ = ["CameraClient", "CameraServerError", "CameraNotRunning",
           "Frame", "__version__"]
