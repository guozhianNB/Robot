# vision —— MIPI 摄像头共享服务（RDK X5）

## 为什么需要它

RDK X5 的 MIPI 摄像头（`hobot_vio.libsrcampy` / VIO 通道）同一时刻只能被
**一个进程**独占打开。多个程序（目标检测、拍照、推流、LLM 视觉……）如果
各自直接调用 `libsrcampy.Camera` 会互相抢占、报错冲突。

本目录提供一个**摄像头共享服务**：一个守护进程作为摄像头的唯一持有者，
把每一路通道的"最新一帧"缓存到内存，再通过 TCP 协议向任意多个客户端
分发帧。各业务程序只需用 `CameraClient` 取帧，不再关心摄像头占用。

```
                  ┌──────────────────────────────┐
  MIPI 摄像头 ───►│ 采集子进程（独占摄像头+get_img） │
  1920x1080 NV12  │  └ 经单槽队列推最新帧 ──────────┼──┐
                  └──────────────────────────────┘  │
                  ┌──────────────────────────────┐  │
                  │ 主进程（TCP 分发，GIL 空闲）   │◄─┘
                  │  ├ 缓存各通道最新帧             │
                  │  └ 多客户端并发响应             │
                  └──────┬──────────┬──────────┬───┘
                         ▼          ▼          ▼
                    目标检测程序   拍照程序     LLM 视觉
                  (CameraClient) (CameraClient) (CameraClient)
```

> 为什么采集要放子进程：实测 libsrcampy 的 `get_img()` 阻塞等待帧时会
> 长时间占住进程 GIL，导致同进程内的客户端分发线程被饿死（连 PING 都
> 要 ~130ms）。把采集隔离到子进程后，主进程响应回到亚毫秒级。

## 目录结构

```
vision/
├── protocol.py           # 两端共享的协议定义（命令字/帧头格式）
├── camera_server.py      # 服务端守护进程（python3 -m vision.camera_server）
├── camera_client.py      # 客户端库（CameraClient / Frame）
├── __init__.py           # 包入口，导出 CameraClient、Frame
└── examples/
    └── grab_and_save.py  # 示例：取一帧保存 + 订阅几帧
```

## 快速开始

### 1. 启动服务（在仓库根目录 Robot/ 下）

```bash
# 真实摄像头（默认输出两路：通道1=1920x1080 全分辨率，通道2=512x512 小图）
python3 -m vision.camera_server --fps 30

# 只输出一路，或自定义分辨率（宽高须为偶数）
python3 -m vision.camera_server --channels 1920x1080 --fps 30

# 无摄像头自测 / 开发模式（生成合成帧，协议完全一致）
python3 -m vision.camera_server --mock

# 后台常驻
nohup python3 -m vision.camera_server --fps 30 > /tmp/cam_server.log 2>&1 &
```

启动参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--bind` | `127.0.0.1` | 监听地址；跨机共享设 `0.0.0.0` |
| `--port` | `9540` | 监听端口 |
| `--fps` | `30` | 采集帧率 |
| `--channels` | `1920x1080,512x512` | 输出通道（逗号分隔 `WxH`，宽高须为偶数），通道号从 1 开始 |
| `--enable-jpeg` | 关 | 启用硬件 JPEG 编码（`J` 命令，依赖 JPU 驱动） |
| `--mock` | 关 | 合成帧自测模式 |
| `--status` | 关 | 不启动服务，只查询运行中的服务状态（JSON） |

> 注：板卡 VSE 支持 1920x1080 等非 16 对齐分辨率（官方 cdev 示例
> `/app/cdev_demo/vio2display` 与 YOLO 示例均直接用 1080）；16 对齐
> 仅 JPU 编码要求，开启 `--enable-jpeg` 时服务端会内部对齐。

### 2. 客户端取帧

```python
from vision.camera_client import CameraClient

with CameraClient() as cam:          # 默认连 127.0.0.1:9540
    info = cam.info()                # 服务状态（通道/帧计数/模式）
    f = cam.get_frame(channel=1)     # 最新一帧（NV12 bytes）
    bgr = f.bgr()                    # 转 BGR ndarray（需 opencv-python）
    f.save("/tmp/frame.yuv")         # 保存原始 NV12

    # 逐帧消费：每帧只出现一次、严格递增（适合推理/录像）
    while True:
        fr = cam.get_next_frame(channel=1)
        do_inference(fr.bgr())

    # 或订阅连续帧流（生成器，独立连接）
    for fr in cam.frames(channel=1):
        print(fr.frame_id)
```

也可直接跑示例：

```bash
python3 vision/examples/grab_and_save.py
```

### 客户端 API 一览（vision/camera_client.py）

| 方法 | 说明 |
| --- | --- |
| `info()` | 服务状态 dict：mode/fps/jpeg/每通道分辨率与帧计数 |
| `ping()` | 服务是否存活 |
| `get_frame(channel=1)` | 取该通道最新一帧（可能重复，适合"最新画面"） |
| `get_next_frame(channel, last_id=None)` | 阻塞等待下一新帧（frame_id 递增；断线续传传 last_id） |
| `frames(channel=1)` | 订阅连续帧流生成器 |
| `get_jpeg(channel=1)` | 最新一帧的 JPEG（服务端需 `--enable-jpeg`） |

`Frame` 字段：`frame_id / ts_us / channel / width / height / fmt("NV12"|"JPEG") / data`。
方法：`nv12_array()`（numpy）、`bgr()` / `rgb()`（cv2）、`save(path)`。

线程说明：同一 `CameraClient` 实例不保证线程安全，多线程各建一个实例即可。

## 协议简述

客户端 → 服务端：`1 字节命令`（`I` 信息 / `G` 最新帧 / `N` 下一新帧 /
`S` 订阅 / `J` JPEG / `P` 心跳），`G/N/S/J` 后跟 1 字节通道号，`N` 再跟
8 字节大端 `last_id`。

帧响应：40 字节定长二进制帧头 + 原始载荷。帧头格式见 `protocol.py`
（magic `VCAM`、version、cmd、channel、format、width、height、
frame_id、ts_us、size）。错误响应为一行文本，前缀 `ERR `。

## 常见问题

- **启动报 "Address already in use"**：已有一个实例在跑，先查
  `python3 -m vision.camera_server --status`，或换 `--port`。
- **启动报 "No camera sensor found / open_cam 失败"**：摄像头没被检测到。
  检查接线与供电；确认没有其他进程占用摄像头；VIO 传感器探测依赖
  i2c 总线与 GPIO 复位（部分环境 /sys 只读或权限受限时探测会失败，
  需在板卡正常环境/root 下运行，见下方说明）。
- **只能接一个 MIPI 摄像头**：官方 VIO 自动检测模式不支持同时接多个，
  多接会报错（详见 `/app/pydev_demo/08_mipi_camera_sample/README.md`）。
- **分辨率报错**：通道宽高须为偶数（NV12 要求）；JPEG 编码若报错，确认
  宽高为 16 对齐（服务端会自动对齐编码尺寸，但极小分辨率可能超出 JPU 支持）。
- **JPEG 命令报 "jpeg disabled"**：启动时加 `--enable-jpeg`；若仍报
  编码器初始化失败，说明 JPU 驱动不可用（服务会自动降级为仅 NV12）。

## systemd 开机自启（可选）

```ini
# /etc/systemd/system/vision-camera.service
[Unit]
Description=MIPI camera sharing server (RDK X5)
After=network.target

[Service]
User=sunrise
WorkingDirectory=/home/sunrise/Robot
ExecStart=/usr/bin/python3 -m vision.camera_server --fps 30
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vision-camera
sudo systemctl status vision-camera
```

## 参考

- 板卡官方示例：`/app/pydev_demo/08_mipi_camera_sample/`
  （`open_cam` 多通道写法见 `09_web_display_camera_sample/`）
- 依赖：服务端需要 `hobot_vio`（板卡系统 Python 自带）；客户端可选
  `numpy` / `opencv-python`（转数组/转彩色时才用，惰性导入）。
