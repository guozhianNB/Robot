# Task 5 报告：确定性 Plan 调度器与三层结果解析

## 状态

完成。实现位于 `LLM/agent/plan_scheduler.py`，并补齐 Plan 调度配置与离线测试。

## 实现摘要

- `parse_tool_result()` 严格解析 MCP 外层 envelope、内层 JSON 和车控结果；外层失败、内层动作失败、`unavailable`、畸形 JSON、缺字段均 fail-closed。`started` 只表示受理，必须带正整数 `task_id`，绝不直接判定成功。
- `reconcile_step()` 只接受与当前 attempt 绑定的 `car_task_id`；迟到/其他任务结果不会污染当前步骤。匹配的 `last.ok` 才能完成，`last.status=uncertain` 进入复核，`current` 仅表示仍运行。
- 独立 daemon 调度线程、`Event` 停止信号、非重入单轮锁；慢 MCP 查询不进入 `session.tick()` 或 FastAPI 事件循环。
- `prepare_plan_attempt()` 被视为完整原子操作，调度器不会重复写 `dispatching`。动作槽位统一走 Task 4 `action_gate`；抢不到槽位只等待。
- `move/turn` 超时进入 `needs_review` 且不重试；goto 也不会在单纯超时、断线或不确定结果时自动重发。只有明确终态和后续人工/策略确认才可创建新 attempt。
- 启动前调用 `recover_running_plan_steps()`，遗留活动步骤/未决 attempt 转入 `needs_review`；检测到多个活动步骤时整体 fail-closed。
- `conf.py` 增加 `PLAN_TICK_S`、`PLAN_STATUS_GRACE_S`、`PLAN_*_TIMEOUT_S`、`PLAN_EXECUTOR_ENABLED`，对非法、非有限和非正数环境值回退默认值并记录配置警告。

## TDD 与验证

先添加解析、task_id、不可用宽限期、单次派发/完成和相对动作超时测试，再实现模块。

聚焦结果：

```
pytest LLM/tests/test_plan_scheduler.py -q --basetemp D:\_project\Robot\pytest-tmp4
14 passed
```

相邻回归（注入占位 `DEEPSEEK_API_KEY`，不访问 rosbridge）：

```
pytest LLM/tests/test_plan_scheduler.py LLM/tests/test_plan_db.py \
  LLM/tests/test_car_mcp.py LLM/tests/test_plan.py LLM/tests/test_policy_tools.py -q
196 passed
```

测试使用假 MCP 返回，不启动真实车控或 rosbridge。

## 注意事项

- `plan_scheduler.start()/stop()` 已提供生命周期入口；主后端 lifespan 接线应在调度线程启动顺序中调用，且仅由配置为执行端的实例启动。
- SQLite 的 `car_task_id` 列是 TEXT，调度器在不放宽任务绑定的前提下，严格比较其与 MCP 正整数 ID 的规范化表示。
