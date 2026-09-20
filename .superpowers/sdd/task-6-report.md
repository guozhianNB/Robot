# Task 6 报告：Plan 人工迁移、取消与通知

- 状态：DONE
- 范围：`LLM/agent/plan.py`、`LLM/agent/plan_scheduler.py`、`LLM/store/db.py`、Plan 测试

## 实现

- 增加 `change_priority`、`cancel_plan`、`confirm_step`、`retry_step`。
- 所有人工写操作先检查状态和 `version`，通过 `transition_plan_execution` 在单个 SQLite 事务内更新 Plan、Step、Attempt；版本冲突返回最新 Plan，不产生部分提交。
- `manual` 步骤仅接受 `complete`；时间/设备等待不能伪造确认；`needs_review` 仅接受带备注的 `mark_succeeded`、`mark_failed`、`retry`。
- 重试只允许 `safe_goto_only` 的 `robot_goto_point`，在同一聚合事务创建新的 `prepared` Attempt；相对移动/旋转以及不允许状态不重试。
- 排队取消只改数据库，不调用 `robot_stop`。运行取消先一次性进入 `cancelling`/`interrupting`，急停失败立即进入复核；调度器仅在新鲜空闲车况后标记 `cancelled`，超过恢复宽限期进入 `needs_review`。
- Plan 完成、失败、待复核和取消结果按精确 `source="plan"`、通知类型及 `ref="plan:<id>"` 投递通知。通知失败只写审计，不回滚已提交的聚合。

## 验证

```text
$env:OPENAI_API_KEY='test'; ..\..\.venv\Scripts\python.exe -m pytest --basetemp=.pytest-local LLM/tests/test_plan.py LLM/tests/test_plan_scheduler.py -q
60 passed

$env:OPENAI_API_KEY='test'; ..\..\.venv\Scripts\python.exe -m pytest --basetemp=.pytest-local LLM/tests/test_notify.py -q
29 passed
```

`compileall` 和 `git diff --check` 通过。默认 pytest 临时目录在本机被权限策略拒绝，因此验证显式使用仓库内 `.pytest-local` basetemp；该目录为测试产物，不纳入提交。

## 审查修复

根据 `task-6-review.md` 的 Important 项完成二轮修复：

- 急停严格解析 MCP 外层 envelope 与内层 JSON；内层 `ok:false`、非法 JSON 和错误状态均立即复核。
- 急停失败在同一聚合事务将正在派发的 Attempt 改为 `uncertain`，后续允许显式安全重试创建新 Attempt。
- 排队取消仅取消未完成步骤，保留 `succeeded/failed/skipped/interrupted` 历史，并发送 `plan_done` 取消终态通知。
- scheduler 给步骤快照携带 Plan version；`_finish`、`_poll`、`_poll_cancel`、`_handle_wait` 和 review 迁移全部使用 `expected_version`，旧 tick 的结果不能覆盖人工迁移。
- 人工操作审计分别保留 `actor_uid`、`actor_role`、`actor_surface`。
- 取消宽限期在恰好 15.0 秒时进入复核（`>=`），并修正该异常分支调用不存在函数的问题。

二轮回归：`test_plan.py + test_plan_scheduler.py + test_notify.py` 共 **94 passed**。

随后又收紧了 Attempt 准备与 Plan 取消之间的竞态：`prepare_plan_attempt()` 支持
`expected_version`，调度器若在准备前后观察到 `cancelling/cancelled/needs_review` 会放弃派发，
不会把人工终态改回 `running`。上述 94 个回归测试仍全部通过。

增量复审修复：Attempt 准备 CAS 失败时，scheduler 先比较最新 Plan version；确认是调级、人工完成或其他 API 写入造成的旧 tick 后直接放弃，不再调用 `_mark_review`。补充了 priority/manual confirm 并发回归，最终相关测试共 **96 passed**。
