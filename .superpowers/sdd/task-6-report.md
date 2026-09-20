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
