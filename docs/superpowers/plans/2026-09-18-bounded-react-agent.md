# 有界原生 ReAct Agent 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 `chat_stream()` 增加可纠错、可多步执行、必定收尾的原生 ReAct 循环，并让 auto 模式在取得工具 Observation 后自动升至 high 思考。

**架构：** 继续以 OpenAI/DeepSeek 原生 `tool_calls` 表示 Act、`role="tool"` 表示 Observation。首次请求沿用现有路由；工具执行后仅 auto 模式升至 high；服务端以四轮工具预算约束循环，耗尽后用一次不携带 tools 的请求强制形成最终答复。

**技术栈：** Python 3.11、OpenAI Chat Completions 流式协议、pytest、既有 SSE 事件协议

---

## 文件结构

- 创建 `LLM/agent/prompt/react.md`：模型使用工具及处理 Observation 的规则。
- 创建 `LLM/tests/test_react_agent.py`：ReAct 状态转换和请求参数的聚焦测试。
- 修改 `LLM/conf.py`：ReAct 提示词路径、服务端工具轮数预算。
- 修改 `LLM/agent/chat.py`：提示加载、安全参数解析、有界循环、auto 升档和强制收尾。
- 修改 `LLM/tests/test_thinking_mode.py`：锁定手动档位及空正文兜底。
- 修改 `docs/log.md`：记录最终实现和验证结果。

## 任务 1：ReAct 提示词与配置

**文件：**
- 创建：`LLM/agent/prompt/react.md`
- 创建：`LLM/tests/test_react_agent.py`
- 修改：`LLM/conf.py:19,263-265`
- 修改：`LLM/agent/chat.py:16-19,93-120,293-343`

- [ ] **步骤 1：编写失败测试**

```python
# LLM/tests/test_react_agent.py
import pytest

from LLM import conf
from LLM.agent import chat


def test_react_budget_is_server_constant():
    assert conf.REACT_MAX_TOOL_ROUNDS == 4
    assert "react_max_tool_rounds" not in conf.DEFAULT_SETTINGS


def test_react_prompt_body_is_in_system_prompt(monkeypatch, tmp_path):
    prompt = tmp_path / "react.md"
    prompt.write_text("说明文字\n<!-- PROMPT -->\nREACT_RULE_SENTINEL", encoding="utf-8")
    monkeypatch.setattr(chat, "REACT_PROMPT_FILE", prompt)
    monkeypatch.setattr(chat, "_load_prompt_base", lambda: "base")
    monkeypatch.setattr(chat, "_load_role_prompt", lambda role: "role")
    system = chat.build_system("ward_101", {}, principal={
        "uid": "ward_101", "role": "ward", "ward_uid": "ward_101"
    })
    assert "REACT_RULE_SENTINEL" in system
    assert "说明文字" not in system
```

- [ ] **步骤 2：运行并确认红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py -q`

预期：FAIL，配置和加载器尚不存在。

- [ ] **步骤 3：添加配置与加载器**

```python
# LLM/conf.py
PROMPT_DIR = Path(__file__).resolve().parent / "agent" / "prompt"
PROMPT_FILE = PROMPT_DIR / "base.md"
REACT_PROMPT_FILE = PROMPT_DIR / "react.md"
REACT_MAX_TOOL_ROUNDS = 4
```

```python
# LLM/agent/chat.py
_REACT_PROMPT_FALLBACK = (
    "只有确实需要外部信息或动作时才调用工具。收到工具结果后先核对是否成功，再决定继续调用或回答。"
    "失败时可修正参数、改用其他工具，或如实说明失败。信息充分后立即回答。"
    "不要输出内部思考、工具协议或虚构结果。"
)
_react_prompt_warned = False


def _load_react_prompt() -> str:
    global _react_prompt_warned
    try:
        raw = REACT_PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        if not _react_prompt_warned:
            _react_prompt_warned = True
            audit.log("chat", action="prompt_react_missing", file=str(REACT_PROMPT_FILE))
        return _REACT_PROMPT_FALLBACK
    _react_prompt_warned = False
    lines = raw.splitlines()
    marker = max((i for i, line in enumerate(lines)
                  if line.strip() == "<!-- PROMPT -->"), default=None)
    return "\n".join(lines[marker + 1:] if marker is not None else lines).strip()
```

在 `build_system()` 的角色片段之后追加 `_load_react_prompt()` 返回值。

- [ ] **步骤 4：创建外置提示词**

```markdown
# ReAct 工具使用规则
> `<!-- PROMPT -->` 以下内容进入 System Prompt。
<!-- PROMPT -->
【使用工具时】
1. 只有确实需要外部信息或执行动作时才调用工具。
2. 收到工具结果后先核对成功与否、信息是否足够，再决定继续调用或回答。
3. 失败时修正参数、改用其他可用工具，或停止并如实说明。
4. 信息充分后立即回答；不要输出内部思考、工具协议或虚构结果。
5. 工具前若需要说话，只说一句进度说明，不提前给出未经验证的结论。
```

- [ ] **步骤 5：验证绿灯并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py -q`

预期：2 passed。

```powershell
git add LLM/conf.py LLM/agent/chat.py LLM/agent/prompt/react.md LLM/tests/test_react_agent.py
git commit -m "feat(agent): 添加 ReAct 工具决策规则"
```

## 任务 2：工具 Observation 后 auto 升档

**文件：**
- 修改：`LLM/tests/test_react_agent.py`
- 修改：`LLM/agent/chat.py:455-565`
- 修改：`LLM/tests/test_thinking_mode.py:107-211`

- [ ] **步骤 1：添加流式测试夹具**

```python
from types import SimpleNamespace


def chunk(content=None, reasoning=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning,
                            tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def tool_call(call_id="call-1", arguments='{"value":1}', index=0):
    return SimpleNamespace(index=index, id=call_id,
                           function=SimpleNamespace(name="probe", arguments=arguments))


class Client:
    def __init__(self, responses):
        class Completions:
            def __init__(self, items):
                self.responses, self.requests = list(items), []
            def create(inner, **kwargs):
                inner.requests.append(kwargs)
                return iter(inner.responses.pop(0))
        self.chat = SimpleNamespace(completions=Completions(responses))


@pytest.fixture(autouse=True)
def isolated_chat(monkeypatch):
    monkeypatch.setattr(chat.db, "append_history", lambda *a, **k: None)
    monkeypatch.setattr(chat.db, "load_history", lambda *a, **k: [])
    monkeypatch.setattr(chat.db, "get_summary", lambda *a, **k: "")
    monkeypatch.setattr(chat.db, "log_tool", lambda *a, **k: None)
    monkeypatch.setattr(chat.audit, "log", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_expression_hint", lambda uid: "")
    monkeypatch.setattr(chat, "_load_prompt_base", lambda: "base")
    monkeypatch.setattr(chat, "_load_role_prompt", lambda role: "")
    monkeypatch.setattr(chat, "_load_react_prompt", lambda: "react")
    monkeypatch.setattr(chat.rag, "recall_v3", lambda uid, q: {"context": ""})
```

- [ ] **步骤 2：编写升档矩阵的失败测试**

```python
@pytest.mark.parametrize("mode,second", [
    ("auto", "high"), ("none", None), ("low", "low"),
    ("high", "high"), ("max", "max"),
])
def test_tool_observation_only_promotes_auto(monkeypatch, mode, second):
    client = Client([
        [chunk(tool_calls=[tool_call()], finish="tool_calls")],
        [chunk(content="完成", finish="stop")],
    ])
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *a: [{"type": "function"}])
    monkeypatch.setattr(chat.tool_mod, "run_tool", lambda *a: {"ok": True, "result": "观察"})
    monkeypatch.setattr(chat, "route_thinking", lambda *a, **k: {
        "on": False, "reason": "日常", "method": "rule"
    })
    events = list(chat.chat_stream(client, "m", "u", "查看", mode, {}))
    assert client.chat.completions.requests[1]["reasoning_effort"] == second
    promoted = [e for e in events if e["type"] == "meta"
                and e["router"].get("method") == "tool_observation"]
    assert bool(promoted) is (mode == "auto")
```

再加一例 auto 首次路由已是 high：两次请求均为 high，且不重复发送 `tool_observation` meta。

再加流式边界测试：无工具响应分两个 content chunks 时，事件顺序保持两个独立 content 后接 done；
工具响应先输出“我帮您看看”再输出 tool call 时，该进度说明立即成为 content，并与工具后的最终正文
共同组成 `done.assistant`，不把 reasoning 混入正文。

- [ ] **步骤 3：运行并确认红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py -q -k promote`

预期：FAIL，auto 第二次请求仍未升档。

- [ ] **步骤 4：实现本轮 effort 状态**

```python
base_effort = effort
current_effort = effort
tool_rounds = 0

def tool_observation_meta():
    nonlocal current_effort
    if mode != "auto" or current_effort == "high":
        return None
    current_effort = "high"
    return {"type": "meta", "router": {
        "on": True, "effort": "high",
        "reason": "已取得工具结果，自动加深思考",
        "method": "tool_observation", "mode": mode, "uid": data_uid,
    }}
```

所有后续 `_thinking_extra()` 和 `reasoning_effort` 使用 `current_effort`。一轮工具全部执行且 tool
messages 全部追加后再升档并 yield meta。首次 meta 仍报告原始路由结果。

- [ ] **步骤 5：锁定空正文兜底参数并验证**

给 `test_thinking_mode.py::_FakeCompletions` 增加 `tools_seen`，在既有空正文测试中断言：

```python
assert client.chat.completions.reasoning_efforts == ["max", None]
assert client.chat.completions.tools_seen[1] is None
```

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py LLM\tests\test_thinking_mode.py -q`

预期：全部 PASS。

- [ ] **步骤 6：提交**

```powershell
git add LLM/agent/chat.py LLM/tests/test_react_agent.py LLM/tests/test_thinking_mode.py
git commit -m "feat(agent): 工具结果后自动加深思考"
```

## 任务 3：多步执行、失败纠正与安全参数解析

**文件：**
- 修改：`LLM/tests/test_react_agent.py`
- 修改：`LLM/agent/chat.py:455-565`

- [ ] **步骤 1：编写失败后重新行动的测试**

```python
def test_failed_tool_can_be_replanned_and_retried(monkeypatch):
    client = Client([
        [chunk(tool_calls=[tool_call("call-1", '{"value":1}')], finish="tool_calls")],
        [chunk(tool_calls=[tool_call("call-2", '{"value":2}')], finish="tool_calls")],
        [chunk(content="第二次成功", finish="stop")],
    ])
    calls = []
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *a: [{"type": "function"}])
    def run_tool(name, args, principal):
        calls.append(args)
        return ({"ok": False, "error": "参数不匹配"} if len(calls) == 1
                else {"ok": True, "result": "done"})
    monkeypatch.setattr(chat.tool_mod, "run_tool", run_tool)
    events = list(chat.chat_stream(client, "m", "u", "执行", "auto", {}))
    assert calls == [{"value": 1}, {"value": 2}]
    assert events[-1] == {"type": "done", "assistant": "第二次成功"}
    assert sum(m["role"] == "tool" for m in
               client.chat.completions.requests[2]["messages"]) == 2
```

- [ ] **步骤 2：编写非法参数不执行工具的测试**

```python
def test_invalid_arguments_become_observation_without_execution(monkeypatch):
    client = Client([
        [chunk(tool_calls=[tool_call(arguments="{broken")], finish="tool_calls")],
        [chunk(content="参数有误，未执行", finish="stop")],
    ])
    called = []
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *a: [{"type": "function"}])
    monkeypatch.setattr(chat.tool_mod, "run_tool", lambda *a: called.append(a))
    events = list(chat.chat_stream(client, "m", "u", "执行", "auto", {}))
    assert called == []
    assert next(e for e in events if e["type"] == "tool_result")["ok"] is False
    observation = next(m for m in client.chat.completions.requests[1]["messages"]
                       if m["role"] == "tool")
    assert '"type": "invalid_arguments"' in observation["content"]
```

再加一例同响应两个 tool calls，断言按 index 顺序执行、产生两个匹配 call id 的 tool messages，
但只消耗一轮工具预算。

- [ ] **步骤 3：运行并确认红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py -q -k "replanned or invalid_arguments or multiple_tools"`

预期：两轮固定循环无法进行第三次总结；非法 JSON 被静默替换成 `{}` 并错误执行。

- [ ] **步骤 4：实现安全解析和动态循环**

```python
def _parse_tool_arguments(raw: str):
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        return None, {"ok": False, "type": "invalid_arguments",
                      "error": f"工具参数不是合法 JSON：{exc}"}
    if not isinstance(parsed, dict):
        return None, {"ok": False, "type": "invalid_arguments",
                      "error": "工具参数必须是 JSON 对象"}
    return parsed, None
```

以 `while True` 替换 `range(2)`。收到 tool calls 时先 `tool_rounds += 1`，按 index 执行全部 calls；
解析失败时 `result = parse_error` 且不调用 `run_tool()`，其余日志、SSE 和 tool message 走同一路径。
非法参数的 `tool_start.args` 使用 `{}`，日志状态为 error。

- [ ] **步骤 5：验证多步与脱敏回归**

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py LLM\tests\test_prompt_layers.py -q`

预期：全部 PASS；完整视觉结果只进入 tool message，持久化日志仍脱敏。

- [ ] **步骤 6：提交**

```powershell
git add LLM/agent/chat.py LLM/tests/test_react_agent.py
git commit -m "feat(agent): 支持多步 ReAct 与失败纠正"
```

## 任务 4：工具预算耗尽后强制收尾

**文件：**
- 修改：`LLM/tests/test_react_agent.py`
- 修改：`LLM/agent/chat.py:455-605`

- [ ] **步骤 1：编写预算测试**

```python
def test_budget_exhaustion_disables_tools_and_forces_answer(monkeypatch):
    actions = [[chunk(tool_calls=[tool_call(f"call-{i}")], finish="tool_calls")]
               for i in range(1, chat.REACT_MAX_TOOL_ROUNDS + 1)]
    client = Client(actions + [[chunk(content="根据已有结果，尚未完全完成。", finish="stop")]])
    audits = []
    monkeypatch.setattr(chat.tool_mod, "effective_tools", lambda *a: [{"type": "function"}])
    monkeypatch.setattr(chat.tool_mod, "run_tool", lambda *a: {"ok": True, "result": "partial"})
    monkeypatch.setattr(chat.audit, "log", lambda *a, **k: audits.append((a, k)))
    events = list(chat.chat_stream(client, "m", "u", "长任务", "auto", {}))
    requests = client.chat.completions.requests
    assert len(requests) == chat.REACT_MAX_TOOL_ROUNDS + 1
    assert requests[-1].get("tools") is None
    assert requests[-1].get("tool_choice") is None
    assert "工具调用次数已达上限" in requests[-1]["messages"][-1]["content"]
    assert any(k.get("action") == "react_budget_exhausted" for _, k in audits)
    assert events[-1]["assistant"].endswith("尚未完全完成。")
```

再加一例：强制收尾只返回 reasoning，下一次空正文兜底必须仍为 `tools=None`、effort=None，并最终输出正文。

- [ ] **步骤 2：运行并确认红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py -q -k budget`

预期：FAIL，当前实现没有四轮预算与禁用工具收尾。

- [ ] **步骤 3：实现强制收尾**

```python
_REACT_FINALIZE_PROMPT = (
    "工具调用次数已达上限。不要再调用工具；请仅根据已有工具结果直接回答用户，"
    "并如实说明尚未完成或无法确认的部分。"
)

# Observation 全部回填之后
if tool_rounds >= REACT_MAX_TOOL_ROUNDS:
    force_finalize = True
    messages.append({"role": "system", "content": _REACT_FINALIZE_PROMPT})
    audit.log("chat", action="react_budget_exhausted", uid=data_uid,
              tool_rounds=tool_rounds, effort=current_effort)

# 下一次模型请求
request_tools = None if force_finalize else (tools or None)
request_choice = None if force_finalize else ("auto" if tools else None)
```

强制收尾响应若仍给 tool calls，不执行；按空正文路径做一次无思考、无 tools 兜底。所有空正文兜底
统一显式传 `tools=None, tool_choice=None`。

- [ ] **步骤 4：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py LLM\tests\test_thinking_mode.py -q`

预期：全部 PASS。

```powershell
git add LLM/agent/chat.py LLM/tests/test_react_agent.py LLM/tests/test_thinking_mode.py
git commit -m "feat(agent): 限制 ReAct 工具预算并强制收尾"
```

## 任务 5：完整验证与日志

**文件：**
- 修改：`docs/log.md`

- [ ] **步骤 1：运行聚焦与完整测试**

```powershell
.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py LLM\tests\test_thinking_mode.py LLM\tests\test_prompt_layers.py -q
.venv\Scripts\python.exe -m pytest LLM\tests -q
```

预期：全部 PASS。若出现基线外失败，记录准确测试名和输出，不修改无关模块。

- [ ] **步骤 2：验证可选依赖缺失不影响导入**

运行：`.venv\Scripts\python.exe -c "import LLM.server; print('server import ok')"`

预期：输出 `server import ok`。

- [ ] **步骤 3：追加开发日志**

```markdown
- 对话 Agent 增加有界原生 ReAct：auto 首次决策维持低延时，获得工具 Observation 后自动升至
  high；最多四轮工具行动，耗尽后禁用工具强制总结。
- 工具失败和非法 JSON 参数均作为 Observation 回填；非法参数不会执行工具。ReAct 不新增或
  持久化 Thought，reasoning 继续只上屏、不进 TTS/历史/记忆。
```

- [ ] **步骤 4：检查范围并提交**

```powershell
git diff --check
git status --short
git diff --stat
git add docs/log.md
git commit -m "docs: 记录 ReAct Agent 实现"
```

预期：只有计划内文件变化；用户的 `docs/参考资料/ReAct官方提示.md` 不进入提交。
