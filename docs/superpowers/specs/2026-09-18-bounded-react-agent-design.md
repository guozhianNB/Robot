# 有界原生 ReAct Agent 设计（2026-09-18）

> 目标：让陪护机器人在保持日常聊天低首延时的同时，能够围绕工具结果持续执行
> Reason → Act → Observation → Reason，并保证最终向用户作答。

## 一、背景与现状

`LLM/agent/chat.py::chat_stream()` 已具备原生 function calling 循环：模型产生
`tool_calls`，后端执行工具，将结果以 `role="tool"` 消息回填，再请求模型继续生成。
它已经包含 ReAct 的基本数据流，但存在三个缺口：

1. 循环固定为两次模型请求；第二次若仍调用工具，执行后循环直接耗尽，没有机会基于最后一次
   Observation 形成最终回答。
2. 没有明确告诉模型：拿到工具结果后应重新评估目标、处理失败，并决定继续行动或结束。
3. `auto` 模式只按用户原始输入决定思考深度。工具结果通常包含需要核对、解释或纠错的新信息，
   但后续模型请求不会因此自动加深思考。

参考资料 `docs/参考资料/ReAct官方提示.md` 展示的是“先生成完整 JSON 计划，再按计划执行”的
Plan → Execute 实现。本设计吸收其中的任务分解和结果反馈思想，但不照搬静态计划协议：机器人面对的
地图、视觉、设备状态和工具错误会在执行中变化，更适合每次 Observation 后动态重规划。

## 二、设计原则

### D1. 使用原生 tool calls 表达 Act

继续使用 OpenAI/DeepSeek 的 `tools`、`tool_calls` 和 `role="tool"` 协议：

- Reason：由模型内部推理完成；现有 `reasoning` 事件按当前思考档位协议上屏。
- Act：模型输出结构化 `tool_calls`。
- Observation：后端执行工具，把完整结构化结果写入 `role="tool"` 消息。
- 下一步 Reason：模型读取 Observation，决定继续调用工具或给出最终回答。

不解析 `Thought:`、`Action:`、`Observation:` 文本，不要求模型输出自定义 JSON 计划，也不新增
可见或持久化的原始 Thought。现有 reasoning 仍然只上屏，不进 TTS、不落历史、不进记忆。

### D2. 首次决策优先首延时，工具后优先质量

首次模型请求完全保留现有五档语义和安全网：

- `auto`：由关键词、情绪词和 LLM 预判路由决定，日常请求可不思考以降低首延时。
- `none`：普通问题不思考，敏感内容仍由安全网升至 `high`。
- `low` / `high` / `max`：尊重手动档位。

当且仅当本轮模式为 `auto`，且至少一个工具已经实际执行并把结果写入 messages 后，下一次模型请求
自动使用 `reasoning_effort="high"`。工具成功或失败都触发升档：失败后的换参数、换工具和降级说明
同样需要推理。后续 ReAct 请求维持 `high`，直到本轮结束。

不自动升至 `max`，因为收益不稳定、延时更高，也更容易耗尽 token 导致只有 reasoning 没有正文。
手动模式不被工具结果覆盖：`none`、`low`、`high`、`max` 均保持用户选择（敏感安全网例外维持现状）。

若当前实际 effort 不是 `high`，升档时在下一次模型请求前发送一条 `meta`：

```json
{
  "type": "meta",
  "router": {
    "on": true,
    "effort": "high",
    "reason": "已取得工具结果，自动加深思考",
    "method": "tool_observation",
    "mode": "auto",
    "uid": "实际主体 uid"
  }
}
```

该事件只报告实际生效状态，不改变前端事件类型集合。若 auto 首次路由本来就是 `high`，工具后
继续保持 `high`，无需发送内容重复的第二条 meta。

### D3. 有界动态 ReAct，不预先锁死计划

在 `LLM/conf.py` 增加 `REACT_MAX_TOOL_ROUNDS = 4`，表示一轮对话最多允许四轮工具行动。
每轮行动可以包含模型在同一个响应中产生的多个 tool call；这些调用沿用当前顺序执行语义，并共同
计为一轮。配置是服务端安全预算，不放入用户 settings，也不允许客户端覆盖。

流程为：

```text
首次模型决策
  ├─ 输出正文/无需工具 → 结束
  └─ tool_calls → 执行并回填 Observation
                    ↓
              auto 升至 high
                    ↓
              下一次模型决策
               ├─ 继续 Act（预算内）
               └─ 输出最终回答
```

System Prompt 增加简短的工具工作规则，而不是固定步骤模板：

1. 只有确实需要外部信息或动作时才调用工具。
2. 每次收到工具结果后，重新判断它是否足以回答；不要假设工具成功。
3. 失败时根据错误选择修正参数、改用其他可用工具，或停止并如实说明。
4. 信息充分后立即回答，不为展示过程而继续调用工具。
5. 不向用户输出内部 Thought、工具协议或虚构的工具结果。
6. 若工具调用前需要说话，只能给用户一句简短的进度说明（如“我帮您看看”），不能提前给出尚未
   经工具验证的结论。

规则放入独立的 `LLM/agent/prompt/react.md`，由 `build_system()` 加载在角色提示之后、动态上下文之前。
文件缺失时返回内置保底文本并只审计一次，确保后端可降级运行。ReAct 规则对 ward/elder/admin 一致，
可见工具仍由 `role_policy()` 和 `effective_tools()` 裁剪，提示词绝不扩大权限。

### D4. 预算耗尽必须强制收尾

第四轮工具行动执行并回填后，不再把 tools 提供给模型，而是额外发起一次“禁用工具”的收尾请求。
因此配置限制的是工具行动轮数，不限制最后的总结机会。收尾请求：

- `tools=None`，防止产生不可执行的第五轮调用；
- 携带全部本轮 assistant tool_calls 与 tool observations；
- auto 模式使用工具后已经生效的 `high`；手动模式保持其当前有效 effort；
- 追加一条临时 system 指令：工具预算已用完，请根据已有结果直接回答，并如实说明未完成部分；
- 不把这条临时指令写入历史。

审计记录 `action="react_budget_exhausted"`、uid、工具轮数和实际 effort。用户仍只收到现有
`content` 与 `done` 事件，不新增错误事件。

若收尾仍无正文，沿用现有 `thinking_empty_fallback`：关闭思考再请求一次；该请求同样禁止工具。

### D5. 工具失败是 Observation，不是编排异常

`run_tool()` 返回 `ok:false` 时仍把完整结果写入 tool message，让模型决定改参、换工具或说明失败。
现有 `tool_result.ok`、工具日志和审计保持不变。只有模型 API、消息解析或编排代码抛出的异常才走
现有 `error` 事件。

畸形工具参数不静默伪装成 `{}`：JSON 解析失败时不执行工具，构造一个 `ok:false` 的结构化
Observation（错误类型为 invalid_arguments），照常发出 `tool_start`、`tool_result` 并回填模型。
这样模型能够自行修正参数，也避免错误参数触发带默认值的危险动作。

### D6. 对话输出边界保持不变

- SSE 类型仍为 `meta/reasoning/content/tool_start/tool_result/done/error`。
- reasoning 只上屏，不进入 TTS、对话历史、RAG 或记忆。
- 历史只保存用户原文和最终 `full_assistant`；中间 tool messages 不跨对话轮持久化。
- 工具权限继续以 principal、角色白名单、工具 roles 和开关为准。
- 医疗只读、危险信号、安全动作永远放行等规则不因 ReAct 改变。
- `see_what` 的日志脱敏边界不变；完整视觉结果只存在于本轮模型上下文。

## 三、组件与数据流

| 文件 | 职责变化 |
|------|----------|
| `LLM/conf.py` | 新增服务端常量 `REACT_MAX_TOOL_ROUNDS = 4` |
| `LLM/agent/prompt/react.md` | ReAct 工具决策规则，带供人阅读的头部与 `<!-- PROMPT -->` 正文分隔 |
| `LLM/agent/chat.py` | 加载 ReAct 提示；把固定两次循环改为有界工具轮；工具后 auto 升档；参数错误 Observation；预算耗尽禁用工具收尾 |
| `LLM/tests/test_react_agent.py` | 覆盖 ReAct 循环、升档、失败纠正、预算和输出边界 |
| `LLM/tests/test_thinking_mode.py` | 锁定五档与工具后升档的组合语义，防止回归 |

不修改 `server.py`、前端或 `frontend/packages/shared/src/events.ts`，因为没有新增 SSE/总线事件类型。

## 四、关键状态

`chat_stream()` 在单轮请求内维护：

- `base_effort`：首次路由得到的 effort，用于保留手动模式语义。
- `current_effort`：下一次实际传给模型的 effort；auto 工具后从 `None/low/...` 变为 `high`。
- `tool_rounds`：已执行的工具行动轮数，最大为 `REACT_MAX_TOOL_ROUNDS`。
- `observed_tool`：是否已有真实工具 Observation，用于保证只在执行后升档。
- `full_assistant`：仅累计面向用户的正文，维持历史和 TTS 边界。
- `reasoning_text`：仅累计 reasoning 字符数用于审计，维持现状。

所有 `content` 仍按 delta 立即发布，不等待一次模型响应结束，保证普通聊天的首字延时与流畅度。
若模型在同一响应中先输出 content 再发出 tool calls，该 content 作为面向用户的进度说明保留、播报并
计入 `full_assistant`；提示词约束它不能包含未经工具验证的结论。最终回答在 Observation 后继续追加。
这既允许用一句“我帮您看看”覆盖工具等待，也不需要为了识别 tool call 缓存整段普通回复。

## 五、测试与验收

### 自动化测试

1. 无工具请求：只调用模型一次，auto 日常请求保持 `reasoning_effort=None`，正常输出正文。
2. 单工具请求：首次不思考；执行工具后第二次请求为 `high`，发出
   `method="tool_observation"` 的 meta，并形成最终回答。
3. 工具失败后重试：第一次 `ok:false`，模型修正参数再次调用，第二个 Observation 后回答。
4. 手动档位：工具后 `none/low/high/max` 不被 auto 规则覆盖；敏感安全网仍按既有规则生效。
5. 多工具同响应：全部按顺序执行，只消耗一个工具轮次，每个 call id 与 tool message 正确对应。
6. 畸形 JSON 参数：工具函数不被调用，模型收到 `invalid_arguments` Observation 后可纠正。
7. 预算耗尽：恰好执行四轮工具行动；第五次模型请求不带 tools 并产生最终回答；审计包含
   `react_budget_exhausted`。
8. 收尾空正文：触发一次 `thinking_empty_fallback`，重试仍不带 tools。
9. 流式边界：无工具回复的 content delta 立即发布；工具前进度说明也保留，且不得被当作内部 Thought。
10. 权限与脱敏：角色不可见工具仍不可调用，视觉结果仍不写工具日志。

### 验证命令

```powershell
.venv\Scripts\python.exe -m pytest LLM\tests\test_react_agent.py -q
.venv\Scripts\python.exe -m pytest LLM\tests\test_thinking_mode.py LLM\tests\test_prompt_layers.py -q
.venv\Scripts\python.exe -m pytest LLM\tests -q
```

### 人工验收

1. auto 模式普通闲聊直接回复，首延时与改造前相当。
2. auto 模式请求查看环境：首次快速决定调用视觉工具；工具返回后界面显示已自动加深至 high，
   最终回答能引用真实 Observation，TTS 只朗读最终正文。
3. 模拟工具失败：模型不声称动作成功，能在预算内修正或明确告知失败及下一步。
4. 连续诱导工具调用超过预算：系统停止调用并基于已有信息给出可理解的最终答复。

## 六、不做的事

- 不实现预生成完整 JSON 计划、步骤占位符引用或计划持久化。
- 不开放无限工具循环，不允许客户端修改工具轮次预算。
- 不把 ReAct Thought 作为新事件展示或记录。
- 不改变工具并发语义；同一响应的多个工具仍顺序执行，以避免机器人动作竞态。
- 不新增前端设置项；工具后升档是 `auto` 的固定语义。
