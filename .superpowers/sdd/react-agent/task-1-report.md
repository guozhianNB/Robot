# Task 1 报告：ReAct 提示词与配置

## 红灯

命令：

```text
D:\_project\Robot\.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py -q
```

结果：4 failed。失败原因符合预期：`REACT_MAX_TOOL_ROUNDS`、`REACT_PROMPT_FILE`、`_load_react_prompt()` 和 `react.md` 尚不存在。首次使用 worktree 内相对 `.venv` 的命令还因 worktree 不含被 gitignore 的虚拟环境而未进入 pytest，随后改用共享虚拟环境绝对路径取得有效红灯。

## 绿灯

命令：

```text
D:\_project\Robot\.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py LLM\tests\test_prompt_layers.py -q
```

结果：`30 passed`。

另行执行 `compileall`，`LLM/conf.py` 和 `LLM/agent/chat.py` 编译通过。

## 改动文件

- `LLM/conf.py`：新增 `PROMPT_DIR`、`REACT_PROMPT_FILE` 和 `REACT_MAX_TOOL_ROUNDS = 4`；未加入 `DEFAULT_SETTINGS`。
- `LLM/agent/chat.py`：新增 ReAct 正文加载器、内置中文保底、缺失告警去重/恢复，并在角色片段后、动态上下文前注入。
- `LLM/agent/prompt/react.md`：新增带 `<!-- PROMPT -->` 分隔线的 ReAct 工具决策规则。
- `LLM/tests/test_react_agent.py`：覆盖常量、marker 截取、加载顺序、缺失保底与一次性审计、恢复重置及正文规则。

## 自审结论

- 未改变现有 base/role prompt 加载器、工具权限或工具循环行为。
- ReAct 文件每次调用 `build_system()` 时重新读取；文件缺失或编码读取失败时不阻断对话。
- 审计使用现有约定 `event="chat"`、`action="prompt_react_missing"`。

## 提交 SHA

`c21e1bd7204ed984de9eaa90a971899005bc9246`

## 全量回归疑虑

使用 dummy `OPENAI_API_KEY` 后全量 `LLM/tests` 为 `307 passed, 2 failed, 7 errors`：2 个既有 MCP 策略测试受运行环境默认 `mcp_enabled=False` 影响，7 个声纹测试受系统临时目录权限错误影响；未涉及本次改动文件。
