# Qwen 图像识别 MCP 设计

日期：2026-09-17

## 1. 目标与边界

新增一个独立的 Python stdio MCP 服务器，为 LLM 提供通用看图问答能力。用户可把该能力称为 `/see_what`，MCP 协议内的合法工具名为 `see_what`。

工具把一张本地图片或摄像头当前帧与调用方 LLM 提供的文字 `prompt` 一起发送给阿里云百炼 Qwen 视觉模型，并返回模型的文字回答。它不内置跌倒检测、告警、医疗判断或其他业务提示词，也不触发任何机器人动作。

## 2. 接口

工具输入：

```json
{
  "image": "camera",
  "prompt": "描述画面中桌面上的物品",
  "channel": 1
}
```

- `image: str`：必填。值为 `camera` 时读取摄像头共享服务的最新帧；其他值视为本地图片路径。
- `prompt: str`：必填，由上层 LLM 提供，去除首尾空白后不得为空。
- `channel: int = 1`：摄像头通道，范围 1 到 255；文件来源时忽略。

成功返回 JSON 文本，字段为 `ok`、`answer`、`model`、`image` 和 `ms`。失败返回 `{"ok": false, "error": "可操作的错误原因"}`。MCP handler 始终返回文本块可消费的字符串，不把异常抛到协议循环。

## 3. 组件与数据流

- `LLM/vision_mcp/vision_client.py`：与 MCP 解耦的业务核心，负责输入校验、文件读取、摄像头取帧、图片编码和 Qwen API 调用。
- `LLM/vision_mcp/see_server.py`：只负责 MCP 工具注册、JSON 文本封装、stdio 生命周期和缺依赖降级。
- `LLM/conf.py`：在 `MCP_SERVERS` 注册子进程，复用后端当前 Python 解释器，并继承 `DASHSCOPE_API_KEY` 等环境变量。
- `LLM/policy.py`：`elder` 显式允许 `see_what`；`admin` 保持全部能力；`ward` 不开放。

摄像头数据流为 `see_what -> vision.CameraClient -> camera_server 最新 NV12 帧 -> JPEG -> Qwen`。MCP 子进程不直接打开摄像头硬件，也不经主后端 HTTP 回调。

文件数据流为 `see_what -> 白名单与文件校验 -> base64 data URL -> Qwen`。支持 JPEG、PNG、WebP，以文件魔数而非扩展名判断。

## 4. 云端调用与配置

使用项目已有 `openai` SDK调用阿里云百炼 OpenAI 兼容端点：

- `DASHSCOPE_API_KEY`：必需，来自环境变量或仓库根 `.env`，不得写入源码、日志或返回值。
- `VISION_SEE_BASE_URL`：默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`。
- `VISION_SEE_MODEL`：覆盖默认 Qwen 视觉模型。
- `VISION_SEE_TIMEOUT`、`VISION_SEE_MAX_TOKENS`、`VISION_SEE_MAX_WIDTH`、`VISION_SEE_QUALITY`：控制超时、回答长度与图片成本。

请求使用单条 user message，内容包含调用方 `prompt` 文本块和 base64 data URL 图片块。服务器不擅自改写 prompt，只在工具描述中提示上层 LLM 提问应具体。

## 5. 安全与隐私

- 本地图片只能位于 `VISION_SEE_IMAGE_DIRS` 配置的目录内；默认目录为 `LLM/data/vision_inbox`。
- 路径在 `resolve()` 后检查归属，阻止 `..`、符号链接逃逸和字符串前缀混淆。
- 限制图片字节数，只接受 JPEG、PNG、WebP。
- 审计事件只记录来源、模型、耗时、字节数和结果状态，不记录图片、base64、密钥或完整回答。
- 工具描述明确说明图片会上传阿里云，视觉回答可能有误，重要结论需人工核实。
- `ward` 集体层不开放工具；`elder` 和 `admin` 可用。

## 6. 降级与错误处理

`mcp`、`openai` 或密钥缺失时，MCP 子进程向 stderr 输出简短原因并以非零状态退出；`LLM.mcp_client` 记录连接失败后继续启动主后端。摄像头服务不可用不阻止 MCP 服务器启动，因为摄像头可能稍后启动；调用时返回明确错误。

所有外部调用均设置超时。云端错误、空响应、摄像头断连、帧为空、图片越界或格式错误均转换成稳定的失败 JSON，不污染 stdout，也不导致主后端崩溃。

## 7. 测试与验收

单元测试覆盖：

- `camera` 来源及通道传递，摄像头帧编码成功与异常。
- 本地文件白名单、路径逃逸、不存在、空文件、超限及格式校验。
- Qwen 请求中的模型、prompt、图片 data URL、超时和 token 参数。
- 云端异常、异常响应和空回答。
- `see_what` 成功/失败结构与 MCP JSON 文本返回。
- 配置注册以及 `elder/admin` 可见、`ward` 不可见。
- 缺少可选依赖或密钥时主后端导入和启动路径不受影响。

真实验收需要配置 `DASHSCOPE_API_KEY`，启动 `vision.camera_server`，开启后端设置 `mcp_enabled`，确认 `/api/tools` 可见 `see_what`，并分别用摄像头和白名单目录图片完成一次调用。

## 8. 非目标

- 不做连续视频分析、多图会话、目标跟踪或本地视觉推理。
- 不自动拉起摄像头服务。
- 不保存上传图片或云端回答。
- 不把识图结果直接连接到告警、车控或医疗业务。
