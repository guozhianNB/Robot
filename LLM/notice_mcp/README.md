# 护士传达 MCP：`notify_nurse`

把小车大模型的一句话**真的送到护士台**的 MCP 工具（需求文档模块 11「报警模块」的上报出口之一）。

规格：`docs/superpowers/specs/2026-09-18-llm-notify-nurse-mcp-design.md`。

## 用途与边界

**用途**：老人说身体不舒服 / 要找护士 / 问到用药、病情（红线是医疗只读——"这个我不懂，我帮您问护士"
说完就该调它转达）；或现场有需要护士知道的情况（疑似摔倒、长时间没动静、房间有异常）。

**边界**：
- 它是**单向通知**，护士不会用这个工具回话；也不是急停——需要停车请调 `robot_stop`。
- 它**不自己造通道**：投递到后端免鉴权投递口 `POST /api/notifications`（通知中心规格 D4），
  由 `LLM/agent/notify.py::ingest()` 落库 → 写审计 → `bus.publish("notification")` 广播，
  护士台（SSE）**5 秒内**弹卡；合并键 = `(source, type, uid, 正文)`，未处理通知在 60 秒内
  **同样内容**重复上报会合并计数（**内容不同则各自成条**，且合并级别只升不降，见规格 M1）。
- 不新增任何第三方依赖：客户端只用标准库 `urllib`。

## 工具输入

| 参数 | 必填 | 说明 |
|---|---|---|
| `message` | ✅ | 要传达的话，一条通知只说一件事（≤500 字，超了自动截断）。**把老人的称呼写进这句话** —— 你通常拿不到 uid |
| `level` | | `info`（默认）/ `warning`（想让护士来看看）/ `critical`（摔倒·呼救·胸痛·意识不清；护士台置顶 + 响提示音）。大小写不敏感，`high`/`severe`/`urgent` 之类近义词按**往上归**处理，认不出的词才落 `info` |
| `uid` | | 老人 uid，能确定才填；留空不影响送达，只是护士卡上没有「姓名 · 床号」 |

**返回**（JSON 字符串，`mcp_client` 只读文本块，故必须是字符串）：
`{"ok": true, "id": 12, "level": "critical", "deduped": false, "detail": "已通知护士"}`；
失败：`{"ok": false, "error": "连不上护士后台（http://127.0.0.1:8000）：…"}` —— 此时**不要**对老人说
"已经通知护士了"，改说"我这就想办法联系护士"。

服务端固定字段：`source="cart"`（对上护士台「小车」筛选页签）、`type="message"`（标题缺省「通知」）。

## 配置

`LLM/conf.py`：

```python
NOTICE_BACKEND_URL = …                    # 默认 http://127.0.0.1:8000（env 同名变量可覆盖）
MCP_SERVERS["notice"] = {
    "command": sys.executable,            # 必须是**后端同一个解释器**（换 python 会缺 mcp）
    "args": [BASE_DIR/"LLM"/"notice_mcp"/"notice_server.py"],
    "env": _notice_mcp_env(),             # NOTICE_BACKEND_URL + 真正设过的 NOTICE_TIMEOUT_S/NOTICE_MCP_LOG
    "enabled": True,
    "roles": ["elder", "ward", "admin"],  # 服务器级闸门（第一道）
}
```

**三个坑**（与 `vision_mcp` 同族）：
1. `command` 必须是后端同一解释器（Windows：`.venv\Scripts\python.exe`）；
2. `roles` 只是第一道闸门 —— 还得把 `"notify_nurse"` 写进 `LLM/agent/policy.py` 对应角色的
   `allowed_tools`（白名单是**天花板**），否则模型根本看不见这个工具；
3. MCP 工具没有 per-tool 开关，只挂全局 `settings.mcp_enabled`（默认 `false`，要在管理端设置页打开
   并**重启后端**才会拉起子进程）。

**跨机部署**：把 `NOTICE_BACKEND_URL` 指向后端地址即可（`.env` 里设或在 `conf.py` 改），代码零改动。
`notice_client.py` 只用标准库，可以整包拷到小车/另一台机器上单独跑。

## 启动

```powershell
# 后端（会自动以 stdio 子进程拉起本服务；mcp_enabled 打开时）
.venv\Scripts\python.exe -m uvicorn LLM.server:app --host 0.0.0.0 --port 8000

# 单独调试本服务（直接起，看它能连上哪个后端）
.venv\Scripts\python.exe LLM/notice_mcp/notice_server.py
```

**验证通道（不依赖模型）**：

```bash
curl -X POST http://127.0.0.1:8000/api/notifications -H "Content-Type: application/json" \
     -d "{\"source\":\"cart\",\"type\":\"message\",\"level\":\"critical\",\"message\":\"张爷爷说胸口疼\"}"
```

护士台（`http://<局域网IP>:8000/nurse/`）应在 5 秒内出现红卡。

## 权限与隐私

- **两道闸门**：`MCP_SERVERS["notice"]["roles"]` ∩ `policy.role_policy(role)["allowed_tools"]`，
  外加全局 `mcp_enabled`。三层角色都给（elder/ward/admin）：声纹识别失败会 fail-closed 落到
  `ward`，那里堵死等于"老人求助喊不出来"（R3 精神）。**代价**：未识别的说话人也能刷护士台
  （只有 60 秒去重兜底）—— 不想给就把 `conf.py` 里 `roles` 与 `policy.py` 里 `ward` 的
  `notify_nurse` 一并删掉。
- **审计**：投递口每次上报写 `notify_ingest` 审计；工具调用走 `tool` 审计/工具日志（`message`
  内容同既有口径）；**被闸门拒绝**时参数走 `tools.py::_audit_args()` 的脱敏分支，只留
  `{"level": …, "has_uid": …}`，不留原文。
- 通知内容会经**免鉴权 SSE** 广播给局域网（既有暴露面，非本工具新增）。

## 错误与降级

| 情况 | 行为 |
|---|---|
| 没装 `mcp` | 子进程写 stderr + **退出码 2**；`mcp_client` 记 `connect_error`，后端照常启动（只是没这个工具） |
| `mcp_enabled=false` | 不拉起子进程（既有机制）；模型看不到该工具 |
| 后端没起 / 正在重启 | **启动时不探活**（否则后端一重启工具就失踪）；每次调用返回 `ok:false` + 人话原因 |
| 后端 400 / 5xx | 透出后端 `detail`；HTTP 超时默认 5s（`NOTICE_TIMEOUT_S`，会经 `conf._notice_mcp_env()` 传给子进程），远小于工具总超时 30s |
| 任何异常 | 一律 `ok:false`，绝不抛给对话链路；stdout 一个字都不写（会污染 MCP 协议） |

日志：`LLM/notice_mcp/notice_mcp.log`（同时写 stderr；路径可用 `NOTICE_MCP_LOG` 改，**每次调用留一行**：
`notify_nurse level=… has_uid=… ok=… deduped=… id=…`，**不记 message 原文**）。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_notice_mcp.py -q
```

HTTP 桩是本进程起的 `127.0.0.1` 随机端口 `ThreadingHTTPServer`：**不连 8000、不起 uvicorn**，
无进程级外部副作用（测试红线）。
