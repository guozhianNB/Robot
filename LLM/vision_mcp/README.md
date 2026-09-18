# 视觉 MCP：`see_what`

这是一个独立的 Python stdio MCP（Model Context Protocol）服务器，为 LLM 提供一次性的看图问答能力。MCP 工具名是 `see_what`；用户也可以把它称为 `/see_what`。

## 用途与边界

工具读取摄像头当前帧或白名单目录中的一张本地图片，把图片和调用方提供的 `prompt` 一起发送给阿里云百炼 Qwen 视觉模型，并返回文字回答。

它不是连续视频分析器，不做目标跟踪、本地推理、跌倒检测、告警触发、医疗判断或机器人动作，也不会自动启动摄像头服务。图片和回答不在本模块持久化；视觉回答可能有误，重要结论必须人工核实。

## 工具输入

```json
{
  "image": "camera",
  "prompt": "描述画面中桌面上的物品，并指出是否有明显危险物品",
  "channel": 1
}
```

- `image`：必填。传 `camera` 读取摄像头共享服务的最新帧；其他值按本地图片路径处理。
- `prompt`：必填。应具体描述要确认的问题，不能是空字符串。
- `channel`：摄像头通道，默认 `1`，范围 `1..255`；使用本地文件时忽略。

本地文件示例（路径必须位于白名单目录内）：

```json
{
  "image": "LLM/data/vision_inbox/room-101.png",
  "prompt": "图片中有几个人？请简要描述他们正在做什么。",
  "channel": 1
}
```

支持 JPEG、PNG、WebP。程序按文件内容魔数校验格式，并限制路径越界、符号链接逃逸、文件大小和目录范围。

## 配置

必须设置阿里云百炼 API 密钥。密钥只放在环境变量或仓库根目录 `.env`，不要写入源码、日志或请求内容：

```dotenv
DASHSCOPE_API_KEY=your-api-key
```

视觉参数及默认值如下：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VISION_SEE_MODEL` | `qwen-vl-plus` | Qwen 视觉模型 |
| `VISION_SEE_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `VISION_SEE_TIMEOUT` | `20.0` | 云端请求超时（秒）；应保持低于全局 `MCP_TOOL_TIMEOUT`（默认 30 秒），并为取帧和编码预留时间 |
| `VISION_SEE_MAX_TOKENS` | `512` | 回答长度上限 |
| `VISION_SEE_MAX_WIDTH` | `1024` | 摄像头帧上传前的最大宽度；本地文件按原始尺寸发送 |
| `VISION_SEE_QUALITY` | `85` | 摄像头帧 JPEG 质量 |
| `VISION_SEE_MAX_BYTES` | `3145728`（3 MB） | 单张图片大小上限 |
| `VISION_SEE_IMAGE_DIRS` | `LLM/data/vision_inbox` | 本地图片白名单目录，多个目录用系统路径分隔符连接 |
| `VISION_SEE_GRAB_TIMEOUT` | `5.0` | 摄像头取帧超时（秒） |
| `VISION_SEE_CONNECT_TIMEOUT` | `2.0` | 连接摄像头服务超时（秒） |
| `VISION_SEE_CHANNEL` | `1` | 业务层直接调用时的默认抓帧通道；MCP 的 `channel` 显式参数默认仍为 `1` |

摄像头共享服务地址由以下配置决定，默认连接本机：

| 环境变量 | 默认值 |
| --- | --- |
| `VISION_HOST` | `127.0.0.1` |
| `VISION_PORT` | `9540` |

例如后端在 PC、摄像头服务在板卡时，将 `VISION_HOST` 设为板卡地址，并让板卡服务监听可访问地址。

## 启动

先启动摄像头共享服务（需要摄像头时）：

```bash
python -m vision.camera_server
```

无硬件调试可使用模拟源：

```bash
python -m vision.camera_server --mock
```

再独立启动 MCP stdio 服务：

```bash
python LLM/vision_mcp/see_server.py
```

项目后端已在 `LLM/conf.py` 的 `MCP_SERVERS` 注册该子进程。后端启动时若 `mcp_enabled` 为 `false`，不会建立 MCP 连接；运行中关闭该设置后会拒绝工具调用，后端退出时会清理其子进程。开启后端设置后，后端会按需拉起并管理 MCP 连接；也可以直接启动上面的脚本供其他 MCP 客户端使用。Windows 建议使用项目虚拟环境的解释器：`.venv\Scripts\python.exe`。

## 权限与隐私

当工具经本仓库 LLM 后端的 `tools.py` 调用时，角色策略规定 `elder` 和 `admin` 可使用 `see_what`，`ward` 集体层禁用；MCP 配置中的 `roles` 不是绕过后端策略的授权方式，最终仍由后端当前 principal 和角色白名单决定。独立 stdio MCP 进程本身没有 principal 或角色鉴权，任何能够启动或连接该进程的 MCP 客户端都可以调用它；部署时必须自行限制进程和密钥的访问范围。

选中的图片会以 base64 data URL 上传到阿里云百炼视觉模型。请不要把身份证、病历等不应外传的图片放入白名单目录；调用前应确认组织的云端隐私与数据合规要求。视觉调用审计不记录图片内容、base64、prompt、回答或密钥，可记录来源、模型、耗时、字节数、结果状态，以及可能包含本地文件路径的有限诊断错误；服务日志仅记录有限的生命周期诊断信息，不应写入密钥或图片内容。

## 错误与降级

`mcp`、`openai` 或 `DASHSCOPE_API_KEY` 缺失时，MCP 子进程会把原因写到 stderr 并以非零状态退出；后端记录连接失败后继续启动，不影响其他功能。摄像头服务未启动不会阻止 MCP 服务启动，但调用 `camera` 时会返回 `ok: false` 和可操作原因。

云端超时、空响应、摄像头断连、空帧、路径越界、图片超限或格式错误均返回稳定的 JSON 文本，不把异常穿透到 MCP 协议循环，也不让主后端崩溃。成功结果包含 `ok`、`answer`、`model`、`image`、`ms`；失败结果形如：

```json
{"ok": false, "error": "摄像头服务未运行"}
```

## 测试

当前验收范围收窄为本地文件图片链路与 MCP 握手；摄像头到云端的真实链路尚未完成实机验收。

在仓库根目录运行视觉 MCP 测试：

```bash
python -m pytest tests/test_vision_mcp.py -q
```

完整后端相关测试：

```bash
python -m pytest tests/test_vision_mcp.py LLM/tests/test_policy_tools.py LLM/tests/test_prompt_layers.py tests/test_mcp_startup.py -q
```

真实云端验收需要有效的 `DASHSCOPE_API_KEY`，并应单独确认摄像头服务、后端 `mcp_enabled`、角色权限和白名单图片链路；单元测试不会替代真实云端隐私与可用性验收。
