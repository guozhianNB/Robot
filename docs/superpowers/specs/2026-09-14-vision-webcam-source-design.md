# 摄像头来源可选（板卡 MIPI / Windows 摄像头）设计

- 日期：2026-09-14
- 状态：**设计已批准，待实现**
- 涉及：`vision/`（新增 `webcam.py`、改 `camera_server.py`）、`LLM/conf.py`、`vision/webbridge.py`
- 前置阅读：`vision/README.md`、`docs/log.md` 2026-09-14（续）条目

## 一、背景与问题

`vision/` 是"摄像头共享服务"：一个守护进程独占摄像头，按通道把最新帧分发给多个客户端，
避免多个业务程序（目标检测/拍照/LLM 视觉）互相抢占。现有两个后端：

| 后端 | 用途 |
| --- | --- |
| `CameraBackend` | 板卡 MIPI，经 `hobot_vio.libsrcampy`，采集在**独立子进程** |
| `MockBackend` | 合成帧，无摄像头自测 |

**问题**：`camera_server.py` 的逻辑是"`--mock` 之外一律走 MIPI"。而自 2026-09-14 起
**LLM 后端默认跑在 PC 上**，PC 上没有 `hobot_vio`，于是 PC 上想接真实摄像头只能 `--mock`
（假画面），**无法用真实画面调试**。

用户诉求原话：

> 「我希望摄像头不仅可以用板卡的，还能用Windows电脑的，方便调试。」
> 「我的意思就是说优先用板卡的摄像头，如果不行，说明在windows系统中，用windows的摄像头」

即：**不是二选一，也不是两路同时接入**，而是"自动判断当前在哪、能用哪个就用哪个，优先板卡"。

## 二、目标与非目标

### 目标

1. 在 PC 上一条命令即可让 `camera_server` 读到 Windows 摄像头（OpenCV），
   且 `camera_client` / `webbridge` / `/api/vision/*` **零改动**即可用。
2. `--source auto`（默认）：板卡上优先 MIPI，非板卡环境自动用 webcam；失败时
   **把每个候选的失败原因都报出来**，不只报第一个。
3. 提供一个用户可自查"到底有没有摄像头"的入口（`--list-cameras`）。
4. 补上"在 PC 后端上看**板卡**摄像头"所必需的远端地址配置（见 §五 D4）。

### 非目标（本轮明确不做）

- **两路摄像头同时接入 / 时间对齐 / 双路合流**——用户明确只需"能用哪个用哪个"。
- admin/kiosk 的摄像头页面（直接开 `/api/vision/snapshot` 已可看图）。
- `webbridge` 的 cv2 JPEG 快路径（现有纯 stdlib 编码器已可用）。
- 把 `WebcamBackend` 挪进子进程——先做成进程内后端，理由与验证见 §五 D3。
- **板卡真机验收**——本机 ssh 不通、且无 MIPI 硬件，见 §九。

## 三、架构

三个后端并列，共用同一鸭子接口（与 `CameraServer` 的耦合点只有这三个方法）：

```
                    ┌─────────────────────────────────────┐
   --source auto ──►│ 选择顺序：mock →(平台偏好) mipi→webcam │
                    └──────────────┬──────────────────────┘
                                   ▼
        ┌──────────────────┬──────────────────┬──────────────────┐
        │  CameraBackend   │  WebcamBackend   │   MockBackend    │
        │  (MIPI, 子进程)   │  (新增, cv2)      │   (合成帧)        │
        └────────┬─────────┴────────┬─────────┴────────┬─────────┘
                 └──────────────────┼──────────────────┘
                                    ▼  统一产出 NV12
                        {通道: (frame_id, ts_us, nv12_bytes)}
                                    ▼
                     CameraServer._capture_loop（缓存最新帧 + 分发）
                                    ▼
                 protocol(裸 TCP) ─► camera_client ─► webbridge ─► /api/vision/*
```

**后端接口契约**（新后端必须实现，缺一不可）：

| 方法 | 语义 |
| --- | --- |
| `open()` | 打开设备；失败抛 `RuntimeError`（消息面向人），并保证已分配的资源被释放 |
| `next_frame(timeout=0.5)` | 返回 `{通道号: (fid, ts_us, NV12 bytes)}`；超时返回 `None`；后端错误抛 `RuntimeError` |
| `close()` | 幂等，可重复调用 |

**关键不变式**：所有后端一律产出 **NV12**。这是"下游零改动"的全部原因（见 §五 D1）。

## 四、组件

### 4.1 新增 `vision/webcam.py`

| 符号 | 说明 |
| --- | --- |
| `bgr_to_nv12(frame, width, height) -> bytes` | 纯函数，**只依赖 numpy**，可脱离摄像头独立测试 |
| `WebcamBackend` | 实现 §三 接口契约；`cv2` 惰性导入 |
| `list_cameras(max_index=6) -> list[int]` | 逐个试打开，返回**真能读出帧**的设备号 |

**`bgr_to_nv12` 数学规格**（必须与 `cv2.COLOR_YUV2BGR_NV12` 可逆，否则画面偏色）：

采用 BT.601 **limited range**（与 `Frame.bgr()` 用的 `COLOR_YUV2BGR_NV12` 一致）：

```
Y =  0.257R + 0.504G + 0.098B + 16
U = -0.148R - 0.291G + 0.439B + 128
V =  0.439R - 0.368G - 0.071B + 128
```

- 结果 `clip(0,255)` 后取 `uint8`
- 色度按 **2x2 平均**下采样（不是只取左上像素，否则画面有块状色噪）
- **输出布局**：`Y` 平面 `W*H` 字节，随后交织的 `UV` 平面 `W*H/2` 字节，
  每 2x2 块依次是 `U, V`（即 NV12；注意与 I420 的"先是整块 U 再整块 V"不同）

**`WebcamBackend` 要点**：

- `cv2.VideoCapture(index)`，Windows 上优先 `cv2.CAP_DSHOW`（MSMF 打开慢且偶发失败），
  失败再退回默认后端
- 按 `channels` 里的最大分辨率设置 `CAP_PROP_FRAME_WIDTH/HEIGHT`，**并读回实际尺寸**——
  设备可能只支持 640x480，此时必须**按实际尺寸产帧**（否则 NV12 长度与帧头不符，
  这正是 2026-09-14 修掉的同类 bug）
- 多通道：从同一次 `read()` 的 BGR 帧**缩放出各路尺寸**（一个物理摄像头不可能同时
  以两种分辨率出图），用 `cv2.resize`
- 读帧失败（`ret=False`）连续超过阈值 → 抛 `RuntimeError`，交由 `CameraServer`
  现有的 `_capture_loop` 报错退出（保持既有语义）

### 4.2 改 `vision/camera_server.py`

- 新增 `--source {auto,mipi,webcam,mock}`（默认 `auto`）、`--device N`（默认 0）、
  `--list-cameras`
- `--mock` **保留**为 `--source mock` 的别名（不破坏现有文档/脚本/测试）
- 抽出后端工厂 `make_backend(args, channels)`，`auto` 的选择逻辑集中在此
- `info()`：`mode` 由 `real|mock` 变为 **`mipi|webcam|mock`**，并新增 `source`、`device` 字段

### 4.3 改 `LLM/conf.py` + `vision/webbridge.py`

- `conf.py` 新增 `VISION_HOST`（默认 `127.0.0.1`）、`VISION_PORT`（默认 `9540`），
  允许 env 覆盖（与项目其它配置一致的 `os.getenv` 写法）
- `webbridge._client()` / `status()` / `get_jpeg()` 的 host/port 默认值改为读该配置

## 五、关键决策

| # | 决策 | 理由 |
| --- | --- | --- |
| **D1** | WebcamBackend 把 BGR **归一化成 NV12**，而不是新增一种帧格式 | 下游（`camera_client`、`webbridge`、软件 JPEG 编码器、`/api/vision/*`）全部按 NV12 处理。归一化让这些**一行都不用改**；反之则要在协议格式、客户端转换、编码器分支同时开刀 |
| **D2** | `cv2` **只在 `open()` 内惰性导入** | 项目红线（AGENTS.md「系统稳健性」）：可选依赖不得出现在后端导入链顶层。且 `webbridge` 被 `LLM/server.py` 的路由函数导入，必须保证无 cv2 也能起后端 |
| **D3** | `WebcamBackend` 先做成**进程内**后端（同 MockBackend），不做子进程 | 现有 MIPI 后端用子进程是因为 `libsrcampy.get_img()` 长时间占 GIL 把分发线程饿死；`cv2.VideoCapture.read()` 在 C 层通常释放 GIL，无此问题。子进程会显著增加复杂度（队列/就绪握手/退出清理），先不引入 |
| **D3 验证** | 实现后必须实测：webcam 采集运行中，`GET /api/vision/status` 的 PING 延迟 | 若劣化明显（经验阈值 >100ms，即复现 MIPI 那种饿死），**再**把 WebcamBackend 挪进子进程；因接口一致（§三），该改动是局部的 |
| **D4** | 新增 `VISION_HOST`/`VISION_PORT` 配置 | 用户"优先板卡"的部署形态是「后端在 PC、摄像头在板卡」，而 `webbridge` 现在**硬编码 127.0.0.1:9540**，不改就永远只能看 PC 自己的摄像头。这是该诉求能落地的前提 |
| **D5** | `mode` 取值 `real`→`mipi` | `real` 在只有 MIPI 一种真实来源时够用；现在有两种真实来源，必须区分。同步更新 `README` 与测试。属**小口径变更**，需在文档中显式标注 |

## 六、来源选择（`auto`）与错误处理

`auto` 的选择顺序与失败处理：

1. `--source mock` 显式给出 → 直接用 `MockBackend`（最高优先级，覆盖一切）。
2. 否则按平台偏好排序候选：**板卡（Linux 且 `hobot_vio` 可导入）→ `[mipi, webcam]`；
   其它平台 → `[webcam, mipi]`**。
3. 依次尝试 `open()`；**成功即用**。
4. 全部失败 → 抛出**汇总错误**，把每个候选的失败原因逐条列出，并提示
   `--list-cameras` 与 `--mock`。**不允许只报第一个**（对齐项目"缺多个依赖时别只报第一个"的口径）。

各场景行为：

| 场景 | 行为 |
| --- | --- |
| PC + 有摄像头 + 已装 cv2 | `auto` 选中 webcam，正常出帧 |
| PC + **未装 cv2** | 该候选失败原因为"未安装 opencv-python（pip install opencv-python）"，汇总后退出码 2；**后端本身照常启动**（D2） |
| PC + 已装 cv2 但无摄像头 | 汇总报"设备 0 打不开"，提示 `--list-cameras` |
| 板卡 + MIPI 正常 | 选中 mipi（与现状行为一致） |
| 板卡 + MIPI 失败 | 继续尝试 webcam，最终汇总报告两者的失败原因 |
| `--list-cameras` | 打印可读设备号后退出（**不需要**摄像头可用即能跑，用于排查） |

## 七、测试计划（不需要摄像头 / 不需要 cv2 即可跑）

沿用 `tests/test_vision.py` 的现有做法：全程 `--mock` 或**假 cv2**，`numpy`/`Pillow` 用
`importorskip`。

| 测试 | 覆盖 |
| --- | --- |
| `bgr_to_nv12` 数学 | 已知纯色块（黑/白/纯红/纯绿/纯蓝）→ 手算期望 Y/U/V；输出长度 = `W*H*3//2`；UV 交织顺序（U 在 V 前） |
| `bgr_to_nv12` 2x2 色度平均 | 构造 2x2 四色块，断言色度是平均值而非左上角采样 |
| `bgr_to_nv12` → cv2 往返 | `pytest.importorskip("cv2")`：转 NV12 再 `COLOR_YUV2BGR_NV12` 转回，与原图差值在容差内（**这条是本机唯一能验"不偏色"的手段**） |
| 假 cv2 驱动 `WebcamBackend` | 帧类型/长度/尺寸符合协议；`frame_id` 严格递增；`open()/close()` 幂等 |
| 设备不支持请求分辨率 | 假 cv2 报告实际 640x480，断言产出的 NV12 是 **640x480**（帧头一致性回归） |
| `read()` 持续失败 | 抛 `RuntimeError` 而非静默空转 |
| `--source` 解析 | `auto/mipi/webcam/mock` 合法；非法值报 `ArgumentTypeError`；`--mock` 等价 `--source mock` |
| `auto` 选择逻辑 | monkeypatch 平台与 `hobot_vio` 可用性，断言候选顺序与最终选中项 |
| 汇总错误 | 全候选失败时，**每个**候选的原因都出现在消息里 |
| `list_cameras` | 假 cv2 下：只有 0/2 可读时返回 `[0, 2]` |
| **降级红线** | 无 cv2 时 `import vision.camera_server` 仍成功；`--source webcam` 报清晰错误 |
| 端到端 | `--source webcam` + 假 cv2 → `CameraClient` 取到帧 → `/api/vision/snapshot` 出**可被 Pillow 解码**的 JPEG |
| 现有 51 项 | 必须继续全过（`mode` 口径变更处同步修正） |

## 八、文档计划

- `vision/README.md`：新增「Windows 摄像头」一节（安装、`--source`、`--device`、
  `--list-cameras`、常见排查）；后端表格补 `WebcamBackend`；`mode` 口径变更说明
- `requirement.txt`：`opencv-python` 归入**可选**节并注明"缺失时摄像头来源降级为
  mipi/mock，后端照常启动"
- `AGENTS.md`：更新 vision 那一条（补 `--source` 与 webcam 支持）
- `docs/log.md`：追加本轮条目

## 九、限制与风险（必须如实记录）

1. **本机无法验证真实取帧**：本沙箱到 `pypi.org` / 清华 / 阿里云 PyPI 的 SSL 全部失败、
   `pip download` 超时，故**无法安装 opencv-python**；全盘也未找到已装 cv2 的 Python 环境。
   → 需要用户执行 `pip install opencv-python` 后**共同验收**（§十 步骤 3~4）。
2. **本机是否有可用摄像头未知**：注册表有 `Camera`/`USB\Class_0e` 多条记录，但这正是
   已知会"把历史设备也列出来"的场景，`Get-PnpDevice` 在本机报 CIM 不可用。
   → 故设计里加了 `--list-cameras` 让用户一条命令自查。
3. **板卡真机未验收**：MIPI 路径与 `--enable-jpeg` 硬件编码路径仍未在板卡实测
   （ssh 不通，见 2026-09-14 日志）。本轮改动**不应**影响 MIPI 路径，但需回归确认。
4. **GIL 风险**：见 D3/D3 验证。若实测 PING 劣化，需转子进程方案。

## 十、验收标准（可执行）

```bash
# 1. 纯代码层（本机可跑，不需要摄像头/cv2）
.venv\Scripts\python.exe -m pytest tests/test_vision.py -q      # 全过（含新增用例）

# 2. 无 cv2 时的降级（本机可跑）
.venv\Scripts\python.exe -m vision.camera_server --source webcam
#   → 退出码 2 + 明确提示"未安装 opencv-python"
.venv\Scripts\python.exe -c "from LLM.server import app"        # 仍能 import

# 3. 【需用户配合】装依赖
.venv\Scripts\python.exe -m pip install opencv-python

# 4. 【需用户配合】真实取帧
.venv\Scripts\python.exe -m vision.camera_server --list-cameras   # 看到可读设备号
.venv\Scripts\python.exe -m vision.camera_server --source webcam --fps 15
#   浏览器打开 http://127.0.0.1:8000/api/vision/snapshot?channel=1 应看到真实画面

# 5. auto 在 PC 上自动选 webcam；在板卡上仍选 mipi（板卡侧需另行验收）

# 6. GIL 回归（D3 验证）
#    采集运行中反复请求 /api/vision/status，观察响应延迟是否劣化（阈值 >100ms 需处置）
```
