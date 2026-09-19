# Qwen 图像识别 MCP 实现计划

> **面向 AI 代理的工作者：** 使用 executing-plans 在当前会话逐任务实现；每项遵循红灯、绿灯、重构顺序。

**目标：** 提供可由现有 LLM 后端及其他 MCP 客户端调用的 `see_what(image, prompt, channel)` 工具，支持摄像头当前帧和受限本地图片，并调用阿里云百炼 Qwen 视觉模型返回文字回答。

**架构：** 保留 `vision_client.py` 的纯业务层与 `see_server.py` 的 MCP 适配层。业务层统一解析图像来源、校验文件、抓取共享摄像头帧并构造 OpenAI 兼容的多模态请求；MCP 层只注册 schema 和把 dict 序列化为文本。后端通过 `conf.MCP_SERVERS` 启动子进程，通过 `policy.py` 约束角色。

**技术栈：** Python 3、OpenAI Python SDK、阿里云百炼兼容 API、MCP Python SDK、pytest、现有 `vision.CameraClient`。

---

## 文件职责

- 修改 `LLM/vision_mcp/vision_client.py`：实现最终 `image/prompt/channel` 业务接口、图片来源解析、Qwen 调用与稳定错误结构。
- 修改 `LLM/vision_mcp/see_server.py`：注册最终 MCP 参数和描述，输出 JSON 文本。
- 创建 `LLM/vision_mcp/README.md`：配置、启动、调用和隐私说明。
- 创建 `tests/test_vision_mcp.py`：业务层、API 请求及 MCP 适配测试。
- 修改 `LLM/conf.py`：注册 vision MCP 子进程。
- 修改 `LLM/policy.py`：给 elder 白名单加入 `see_what`，admin 保持全量，ward 不开放。
- 修改 `LLM/tests/test_policy_tools.py`：验证角色与服务器 roles 的交集。
- 修改 `docs/log.md`：记录功能落地与验证结果。

### 任务 1：锁定图像来源与业务接口

- [ ] 在 `tests/test_vision_mcp.py` 编写失败测试：`image="camera"` 调用 `grab_camera_jpeg(channel)`；文件路径调用 `load_image_file()`；空 image、空 prompt、非法 channel 返回 `ok:false`。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest tests/test_vision_mcp.py -q`，确认因当前 `image_path` 接口和固定通道而失败。
- [ ] 修改 `vision_client.see_what(image, prompt, channel=1)`，严格区分 `camera` 与文件路径，并让摄像头元信息回显实际通道。
- [ ] 保留路径白名单、魔数、大小限制，重命名误导性的 `load_image_jpeg` 为 `load_image_file`。
- [ ] 重跑测试确认通过。

### 任务 2：锁定 Qwen 请求与降级行为

- [ ] 在 `tests/test_vision_mcp.py` 编写失败测试：请求包含原样 prompt、正确 data URL、模型、超时与 token；云端异常、缺 choices、空回答均转成 `SeeError` 或稳定失败 JSON。
- [ ] 运行目标测试确认失败原因是缺少相应行为或当前默认模型不合规格。
- [ ] 将默认模型设为可通过 `VISION_SEE_MODEL` 覆盖的正式 Qwen 视觉模型；保持百炼兼容端点与 `DASHSCOPE_API_KEY` 懒加载。
- [ ] 最小化调整 `_extract_text()` 和 `ask_qwen()` 使测试通过，不记录密钥或 base64。
- [ ] 重跑 `tests/test_vision_mcp.py`。

### 任务 3：锁定 MCP 工具契约

- [ ] 在 `tests/test_vision_mcp.py` 编写失败测试：`_see_what(image, prompt, channel)` 正确转发并返回 JSON 文本；`build_server()` 暴露 `see_what` 的三个参数。
- [ ] 运行目标测试确认当前 `image_path` 两参数接口失败。
- [ ] 修改 `see_server.py` 的 handler、docstring 与工具描述，明确 MCP 名为 `see_what`、`camera` 语义、云端上传及非业务检测边界。
- [ ] 让 MCP 框架与云端能力缺失时只影响子进程，确保模块导入不崩。
- [ ] 重跑 MCP 测试。

### 任务 4：后端注册与角色权限

- [ ] 在 `LLM/tests/test_policy_tools.py` 增加失败测试：模拟 vision server 的 `see_what` schema，elder 可见、admin 可见、ward 不可见；并断言配置中的 `roles` 为 `elder/admin`。
- [ ] 运行目标测试确认 `MCP_SERVERS` 尚无 vision 且 elder 白名单尚无 `see_what`。
- [ ] 在 `LLM/conf.py` 用 `sys.executable` 注册 `vision` 子进程，参数指向 `LLM/vision_mcp/see_server.py`，环境变量空串继承，roles 为 `elder/admin`。
- [ ] 在 `LLM/policy.py` 的 elder 白名单加入 `see_what`。
- [ ] 重跑角色测试和 MCP 启动测试。

### 任务 5：使用文档与完整验证

- [ ] 创建 `LLM/vision_mcp/README.md`，写明 `DASHSCOPE_API_KEY`、可选环境变量、摄像头服务、白名单目录、独立启动和 JSON 调用示例。
- [ ] 更新 `docs/log.md`，记录工具接口、权限、安全边界和验证命令。
- [ ] 运行 `git diff --check`。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest tests/test_vision_mcp.py LLM/tests/test_policy_tools.py tests/test_mcp_startup.py -q`。
- [ ] 运行 `.venv\Scripts\python.exe -c "import LLM.server; print('server import ok')"`，证明可选功能未破坏后端导入。
- [ ] 若环境存在有效 `DASHSCOPE_API_KEY`，启动 mock 摄像头并进行一次真实 Qwen 调用；否则明确记录未执行真实云端验收。
