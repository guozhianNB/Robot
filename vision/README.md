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
├── webcam.py             # Windows/USB 摄像头后端（OpenCV，可选依赖）
├── webbridge.py          # HTTP 桥：给上位机浏览器看画面（/api/vision/*）
├── face.py               # 人脸检测（YOLOv8n-face ONNX）
├── faceid.py             # 人脸识别（ArcFace ONNX，512 维指纹）
├── vision_test_start.py  # ★ 图形界面入口：人脸录入 / 人脸检测 / 删除数据
├── vtest/                # 上面那个界面用到的内部零件（不要单独运行）
│   ├── service.py        #   摄像头共享服务子进程的起停
│   ├── pipeline.py       #   后台取帧线程（抓帧 → 检测/识别 → 发布最新帧）
│   ├── people.py         #   档案：算下一个 uid、按姓名反查、调 update_elder.py
│   └── ui.py             #   三个 Tkinter 窗口（一级/二级/三级）
├── __init__.py           # 包入口，导出 CameraClient、Frame
└── examples/
    └── grab_and_save.py  # 示例：取一帧保存 + 订阅几帧
```

## 摄像头从哪来（三种来源）

| 来源 | 说明 | 依赖 |
| --- | --- | --- |
| `mipi` | 板卡 MIPI（RDK X5），采集跑在**独立子进程** | `hobot_vio`（板卡自带） |
| `webcam` | Windows/USB 摄像头，进程内采集 | `opencv-python`（**可选**） |
| `mock` | 合成帧，协议完全一致 | 无 |

`--source auto`（**默认**）会按平台挑：**板卡优先 `mipi`，PC 优先 `webcam`**，
失败则自动试下一个，全失败再把每个原因逐条报出来。所以：

```bash
python3 -m vision.camera_server                       # 板卡上=MIPI；PC 上=USB 摄像头
python3 -m vision.camera_server --source webcam        # 明确要 Windows 摄像头
python3 -m vision.camera_server --list-cameras         # 先查哪些设备号真能出画面
python3 -m vision.camera_server --source webcam --device 1 --channels 1280x720
```

> **PC 上装 opencv**：`pip install opencv-python`。没装时 `webcam` 来源会以
> 清晰提示失败（`--source auto` 会继续试其它来源），**后端照常启动**——
> `cv2` 是惰性导入的，不参与后端导入链。

### Windows 上注意

- 摄像头可能是 **640x480 而非你请求的 1920x1080**。此时服务按**设备实际尺寸**
  产帧，并把真实尺寸写进帧头与 `info()`（否则 NV12 长度与帧头不符，下游错位）。
- **无法同时被两个程序独占**：先关掉「相机」应用 / 其它占用摄像头的程序。
- 一个物理摄像头**不可能同时以两种分辨率出图**：多通道是从同一次读帧缩放来的。
- 设备号不确定就用 `--list-cameras` —— 它**逐个试读一帧**，这是"到底有没有
  摄像头"的可靠判据（设备管理器/注册表会把**曾经装过的**设备也列出来，会误判）。

## 在 PC 浏览器里看画面（上位机可达）

裸 TCP 协议浏览器说不了，所以后端（`LLM/server.py`）把本服务桥成了 HTTP：

| 端点 | 说明 |
| --- | --- |
| `GET /api/vision/status` | 服务状态（通道/帧计数/模式/连的是哪台），含 `status: running\|unavailable` |
| `GET /api/vision/snapshot?channel=1&quality=80` | 单帧 JPEG，`<img src="...">` 可直接显示 |
| `GET /api/vision/stream?channel=1&fps=10` | MJPEG 连续流，`<img src="...">` 即动态画面 |

最快验证：

```
http://127.0.0.1:8000/api/vision/snapshot?channel=1
http://127.0.0.1:8000/api/vision/stream?channel=1&fps=10
```

**摄像头服务在哪台机器？** 由 `LLM/conf.py` 的 `VISION_HOST`/`VISION_PORT`
决定（可用同名环境变量覆盖），默认 `127.0.0.1:9540`：

- 后端与摄像头服务**同机**（PC 上用 webcam，或板卡上用 MIPI）→ 默认值就对。
- **后端在 PC、摄像头在板卡** → 设 `VISION_HOST=<板卡地址>`（同 `MAPS_SSH_HOST`），
  并在板卡上以 `--bind 0.0.0.0` 启动服务。
- `/api/vision/status` 的 `target` 字段会回显实际连的地址，排查时不用猜配置。

设计要点：
- **自动选择编码路径**：服务端开了 `--enable-jpeg` 就走板卡硬件编码（省 CPU）；
  否则退回 `webbridge.py` 里的**纯 stdlib 基线 JPEG 编码器**，任何环境都有画面。
  响应头 `X-Vision-Source: hardware|software` 会告诉你走了哪条。
- **降级不崩**：摄像头服务没起时，`/status` 返回 `ok: True, status: "unavailable"`
  （服务健康 ≠ 功能可用），`/snapshot` 返回 503 JSON，`/stream` **直接结束流**
  而不是无限空转挂住请求。
- **HTTP 桥零新增第三方依赖**：软件编码器只用 stdlib（不依赖 opencv/Pillow）。
  opencv 只被 `webcam` 来源用到，且惰性导入。

> 实测（mock 后端）：快照 320x240 约 4KB、软件编码单帧约 20ms；
> MJPEG 15fps 稳定输出。软件编码器会按 `max_width=640` 自动整数倍下采样
> 控制体积，需要全分辨率时用 `--enable-jpeg` 走硬件。

## 快速开始

### 1. 启动服务（在仓库根目录 Robot/ 下）

```bash
# 板卡：走 MIPI（默认两路：通道1=1920x1080 全分辨率，通道2=512x512 小图）
python3 -m vision.camera_server --fps 30

# PC：走 Windows/USB 摄像头（需先 pip install opencv-python）
python3 -m vision.camera_server --source webcam --fps 15

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
| `--source` | `auto` | 摄像头来源：`auto`（按平台自动选）/ `mipi` / `webcam` / `mock` |
| `--device` | `0` | `--source webcam` 的设备号（0 起；用 `--list-cameras` 查） |
| `--list-cameras` | 关 | 列出真能读出画面的设备号后退出 |
| `--bind` | `127.0.0.1` | 监听地址；跨机共享设 `0.0.0.0` |
| `--host` | 取 `--bind` | `--status` 查询的目标地址（`0.0.0.0` 会自动换成回环） |
| `--port` | `9540` | 监听端口 |
| `--fps` | `30` | 采集帧率 |
| `--channels` | `1920x1080,512x512` | 输出通道（逗号分隔 `WxH`，宽高须为偶数），通道号从 1 开始 |
| `--enable-jpeg` | 关 | 启用硬件 JPEG 编码（`J` 命令，依赖 JPU 驱动） |
| `--mock` | 关 | 等价于 `--source mock`（保留兼容） |
| `--status` | 关 | 不启动服务，只查询运行中的服务状态（JSON） |

> 注：板卡 VSE 支持 1920x1080 等非 16 对齐分辨率（官方 cdev 示例
> `/app/cdev_demo/vio2display` 与 YOLO 示例均直接用 1080）；16 对齐
> 仅 JPU 编码要求，开启 `--enable-jpeg` 时服务端会内部对齐。

> `info()` 的 `mode` 取值：`mock` / `mipi` / `webcam`（2026-09-14 起；
> 旧的 `real` 已细化为 `mipi` 与 `webcam`，因为现在有两种真实来源）。

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
| `info()` | 服务状态 dict：mode/fps/jpeg/jpeg_size/每通道分辨率与帧计数 |
| `ping()` | 服务是否存活 |
| `get_frame(channel=1)` | 取该通道最新一帧（可能重复，适合"最新画面"） |
| `get_next_frame(channel, last_id=None, timeout="default")` | 阻塞等待下一新帧（frame_id 递增；断线续传传 last_id） |
| `frames(channel=1)` | 订阅连续帧流生成器 |
| `get_jpeg(channel=1)` | 最新一帧的 JPEG（服务端需 `--enable-jpeg`） |

`Frame` 字段：`frame_id / ts_us / channel / width / height / fmt("NV12"|"JPEG") / data`。
方法：`nv12_array()`（numpy）、`bgr()` / `rgb()`（cv2）、`save(path)`。

**超时与取消（重要）**：`get_next_frame` 在服务端有新帧前不会返回任何字节，
所以"对端已死"和"还在等"从字节层面无法区分。构造时用 `wait_timeout` 设上限，
或单次调用传 `timeout="<秒>"`：

```python
cam = CameraClient(wait_timeout=5.0)      # 所有 get_next_frame 最多等 5s
try:
    f = cam.get_next_frame(channel=1)
except CameraTimeout:
    ...                                   # 超时；客户端会自动重连，直接重试即可
```

- 超时抛 `CameraTimeout`（**不是** `ConnectionError` 子类，别当成断线）。
- 超时后该连接被丢弃（服务端那笔迟到应答无法撤回），下次调用自动重连；
  **游标只在真正取到帧后推进，因此不丢帧**。
- `wait_timeout=None` 表示不限制（旧行为，可能永久阻塞）——只在确定服务端
  一定会持续出帧时使用。

异常体系：`CameraServerError`（基类）/ `CameraNotRunning`（连不上，
是 `OSError` 子类）/ `CameraTimeout`（等待超时）。

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
- **PC 上报"未安装 opencv-python"**：`--source webcam`（或 `auto` 落到 webcam）
  需要它：`pip install opencv-python`。没装时其它来源仍可用，**后端也照常启动**
  （`cv2` 惰性导入，不在导入链上）。
- **PC 上报"设备 N 打不开"**：摄像头没插好、被别的程序占用（先关掉「相机」应用），
  或设备号不对 —— 用 `--list-cameras` 逐个试读确认。
- **PC 上的画面分辨率比请求的小**：设备只支持到那个尺寸（常见 640x480）。服务按
  **设备实际尺寸**产帧并如实写进帧头/`info()`，这是刻意的 —— 强行按请求尺寸声明
  会让 NV12 长度与帧头不符、下游错位。
- **想确认"到底有没有摄像头"**：别信设备管理器/注册表（会把**曾经装过的**设备也
  列出来），用 `--list-cameras`，它逐个试读一帧。
- **`--status` 查不到服务**：`--bind` 是**监听**地址、不能当连接地址用。
  服务以 `--bind 0.0.0.0` 启动时，查询请显式给目标：
  `python3 -m vision.camera_server --status --host 127.0.0.1`
  （传 `--bind 0.0.0.0` 时也会自动换成回环，但显式写 `--host` 最清楚）。
- **`get_next_frame` 一直不返回**：它要等到"比 last_id 更新的一帧"才应答。
  若传了过大的 `last_id`（例如断线后拿了过期游标），旧版本会**永久挂死**。
  现在构造 `CameraClient(wait_timeout=5.0)` 即可超时；超时抛 `CameraTimeout`，
  客户端自动重连，直接重试。
- **浏览器看不了画面**：本服务是裸 TCP，浏览器要用后端的 HTTP 桥
  （见上文「在 PC 浏览器里看画面」），端口是 **8000**（后端），不是 9540。
- **`X-Vision-Source: software` 且画面比预期小**：说明没开板卡硬件 JPEG，
  走了 stdlib 软件编码并按 `max_width=640` 下采样。要全分辨率就加
  `--enable-jpeg` 重启服务端。
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

## 人脸检测 / 识别（`vision/face.py`、`vision/faceid.py`）

- 检测：`vision/face.py`（YOLOv8n-face ONNX，输出人脸框）；CLI
  `python -m vision.face 图片.jpg --out 目录`，查设备/状态 `--status --download`。
- 识别：`vision/faceid.py`（ArcFace ONNX，输出 512 维指纹）；CLI
  `python -m vision.faceid --status | --download [--variant r50] | --compare A.jpg B.jpg`。
- 权重在 `vision/models/`（不入库）：`yolov8n-face.onnx`(12MB)、`arcface_mbf.onnx`(13.6MB，
  默认，本机 27ms/张)、`arcface_r50.onnx`(174MB，精度最高但 324ms/张)。
- 样本库 `LLM/data/faces/<uid>/`、接口层与稳定判定 `LLM/face_api.py`、HTTP 接口
  `/api/face/*`。
- **⚠️ 上生产前必读：[人脸识别注意事项.md](人脸识别注意事项.md)** —— 关键点缺失导致的对齐打折、
  红外夜视掉识别率、阈值必须自标定、活体检测缺失、隐私红线、性能预算与自查清单。
- **🔒 照片绝不进 GitHub**：`python scripts\check_privacy.py` 一条命令查"索引 / 未忽略的未跟踪
  文件 / git 历史"三条通道（`.gitignore` 管不了已跟踪文件与历史，2026-09-19 已有声纹事故）；
  `tests/test_privacy.py` 会在每次 `pytest` 时自动把关，`.githooks/` 提供 commit/push 钩子
  （`git config core.hooksPath .githooks` 启用）。详见[注意事项 §8.1](人脸识别注意事项.md)。
- **⌨️ 只想敲命令：[人脸识别操作手册.md](人脸识别操作手册.md)** —— 添加新成员 / 检测人脸是否在库 /
  删除成员 三条流程 + 全部参数说明 + 常见问题（`scripts/face_check.py`、`scripts/update_elder.py`、
  `python -m LLM.face_api`）。

## 图形界面：人脸测试台（`vision_test_start.py`）

一个窗口把人脸链路的三件事做完 —— **这是本项目人脸能力的唯一 GUI 入口**
（界面代码在 `vision/vtest/`，但只能通过本脚本启动）：

```powershell
.\.venv\Scripts\python.exe vision\vision_test_start.py
```

| 一级小窗的按钮 | 二级小窗 | 干什么 |
| --- | --- | --- |
| ① 人脸录入 | 实时画面 + 「开始录入」 | 镜头前**有脸**才让点；点了弹三级小窗填姓名/称呼/床位/年龄（uid 自动 = 前一个 uid + 1）→ 显示「已录入」，3 秒后回一级小窗。**镜头前没脸就直接退出，档案与样本库都不动** |
| ② 人脸检测 | 实时画面（名字画在头上） | YOLO 检出人脸 → 比样本库 → 把**老人的姓名**画在对应人头上（不在库里显示"未知" + 相似度），底部给出稳定/可切换判定 |
| ③ 删除数据 | 输入姓名 + 候选列表 | 按姓名/称呼反查 uid → 确认后 `update_elder.py <uid> --delete --yes`（先自动备份数据库）→ 删完回一级小窗 |

退出：**Ctrl+C**（或关窗口）。

它包装的命令（与手工敲完全同一条路径）：

```
人脸录入  python scripts\update_elder.py <uid> --name 姓名 --nickname 称呼 --bed 床位 --age 年龄
人脸检测  python scripts\face_check.py --live
删除数据  python scripts\update_elder.py <uid> --delete --yes
```

**一处故意的实现差异**（不是漏做）："人脸检测"不是 `subprocess` 起
`face_check.py --live`，而是在同一进程里调它内部那套判定
（`LLM.face_api.analyze/probe` + 同一个 `camera_server`）。硬约束是：
`face_check.py --live` 会**自己再起一个** `camera_server`，而摄像头同一时刻只能被一个
进程独占，两个服务必然抢设备、后起的那个直接打不开。同进程复用还有个好处 ——
框与画面来自**同一帧**，不会"框追不上脸"。门槛与轨迹稳定逻辑和 `--live` 是同一份代码。

细节与设计取舍：

- **摄像头按需打开**：进二级小窗才起 `camera_server`，返回一级小窗立刻关（不常开）；
- **中文姓名画在 Tk Canvas 上**，不用 cv2 画字 —— cv2 的 Hershey 字体画不了中文（"张桂芳"会变成 `???`）；
- **抓帧/推理在工作线程**，主线程只做缩放+编码+画图（约 30fps 上限），所以窗口不卡；
- 录入时先采人脸样本、再写档案；写档案失败且该 uid **原本没有样本**时会回滚刚采的样本（原来就有样本则不动，避免误删旧样本）。

自测选项（无摄像头也能验通路）：

```powershell
# 冒烟：合成帧源 + 12 秒后自动退出（画面里没有人脸，只看界面与通路是否正常）
.\.venv\Scripts\python.exe vision\vision_test_start.py --source mock --selftest 12

# 自动打开某个二级小窗并"点"按钮（非交互，弹窗改成打印）
.\.venv\Scripts\python.exe vision\vision_test_start.py --source mock --open enroll --click start_enroll --auto --selftest 12
.\.venv\Scripts\python.exe vision\vision_test_start.py --source mock --open delete --click find,delete --click-arg 姓名 --auto --auto-yes --selftest 18
```

`--source mock` 是合成帧（协议完全一致、画面里没人脸）；`--auto` 把所有弹窗改成打印；
`--auto-yes` 让确认类弹窗自动答"是"（会**真删数据**，只在拿废弃 uid 做验证时用）。
单测见 `tests/test_vtest.py`（uid 生成/校验、姓名反查、命令拼装、取帧线程的开关与容错）。

### 画面卡顿怎么查（先分清取帧慢 / 推理慢）

窗口底部把两个速率**分开**显示：`预览 x fps   推理 y 次/秒（检测 z ms）`。

| 现象 | 含义 | 怎么办 |
| --- | --- | --- |
| 预览高、推理低 | 正常。推理就是慢（一轮 0.25~0.7s），框会滞后，画面不该卡 | 不用管；想更快只能换模型/上 BPU |
| 预览也低（个位数） | 取帧侧的问题：摄像头协商到 YUY2、被别的程序占用、USB 带宽不够、或服务端 CPU 被吃光 | 看服务日志那行 `[webcam] 设备 0 实际格式：…@…fps FOURCC=…`；降 `--channels 640x480`、`--camera-fps 15`；关掉占摄像头的程序（残留的 `camera_server`/后端/相机应用） |
| 画面延迟越来越大（人不动了画面还在动） | 驱动缓冲里积压了旧帧 | 已内置 `CAP_PROP_BUFFERSIZE=1`；仍不行就换 `--device`/换 USB 口 |

排查用的开关：`--channels`（默认 `640x480`，**只请求一路**；多要一路只是白费 CPU 与带宽，
还会把服务端每帧的 NV12 转换成本翻倍）、`--camera-fps`（默认 30）、`--source webcam --device N`
（换设备号）、`--list-cameras`（服务端参数，查哪些设备号真能出画面）。
