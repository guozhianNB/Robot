# Task 8 报告：提醒幂等创建 Plan

- 状态：DONE
- 基线：`f71f84a`
- 范围：提醒首次触发接线、Plan 来源幂等约束、零步骤防护及回归测试

## 实现

- `reminder._tick_once()` 保持原状态机判断、落库和广播顺序；只在本轮完成
  首次 `pending -> triggered/missed` 后调用 `plan.create_from_reminder()`；daily reminder 后续日期的
  `triggered/unconfirmed -> triggered/missed` 不重复调用。
- 提醒转换为一张 `kind=reminder`、`priority=P1`、`source_kind=reminder`、
  `source_id=<rid>` 的 Plan，正文（缺省时标题）成为单个 `manual` 步骤。
- `plans(source_kind, source_id)` 增加非空来源唯一索引。并发创建时数据库裁决唯一写入，
  其余调用返回幂等 duplicate，不依赖先查后写。
- `db.create_plan()` 拒绝空步骤列表；标题和正文均为空的提醒不创建 Plan，保留原提醒广播并写
  `create_from_reminder_skipped` 审计。
- Plan 创建或适配异常由 reminder 侧捕获并写 `plan_create_error`，不回滚提醒状态，
  也不阻断同轮后续 reminder。
- 既有 DB 测试夹具的人工 Plan `source_id` 改为空值，符合“人工来源 ID 可空”的数据契约，
  避免用同一个伪来源 ID 创建多张 Plan 与新唯一约束冲突。

## TDD 证据

红灯：

```text
..\..\.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py -k reminder -q
6 failed, 39 deselected
```

失败分别证明当时缺少 reminder 接线、`create_from_reminder`、来源唯一约束、skipped 审计和
异常隔离。

第二轮边界红灯：已触发 daily reminder 在下一日期仍调用适配器，单测为 `1 failed`；收紧为
迁移前状态必须是 `pending` 后，reminder 聚焦集为 `7 passed, 39 deselected`。

绿灯：

```text
..\..\.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py -k reminder -q
6 passed, 39 deselected

$env:DEEPSEEK_API_KEY='test-only'
..\..\.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py LLM/tests/test_notify.py -q
74 passed

..\..\.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_db.py LLM/tests/test_plan.py `
  LLM/tests/test_plan_scheduler.py LLM/tests/test_notify.py -q
128 passed
```

worktree 不含根目录 `.env`，涉及 `LLM.server` 导入的测试使用假的 `DEEPSEEK_API_KEY`；
测试没有调用模型或真实硬件。

`py_compile`、`git diff --check` 均通过。`PYTHONUTF8=1` 下隐私脚本通过；它只报告仓库历史中的
已知遗留样本警告，本次索引和未忽略未跟踪文件均干净。

## 复用检查

```text
Name layer:      create_from_reminder / reminder_plan -> 生产代码无既有实现
Behavior layer:  reminder + triggered/missed + source_kind/source_id -> 仅规格、现有状态机和 Plan 字段
Reference layer: reminder.py -> db/notify/audit/bus；plan.py -> compile_steps/db.create_plan
```

完成 sweep 未发现重复适配器、重复来源约束或可替代的新工具函数。

## 审查修复

独立审查指出并修复两个 Important 问题：

- 唯一索引不再由 `SCHEMA` 在旧数据迁移前直接创建。`init_db()` 先检测历史重复来源，保留
  最早 Plan 的来源关联，其余 Plan 不删除、仅将 `source_id` 置空，再建立唯一索引；每组迁移写
  `dedupe_source_migration` 审计。带重复旧数据的回归测试证明启动不会失败且记录不会丢失。
- reminder 到期更新改为比较 `status + last_trigger_date` 的原子条件更新。并发 tick 即使同时读到
  pending，也只有数据库更新成功的一方会广播和调用 `create_from_reminder()`；barrier 并发测试
  锁住了单次广播和单次适配调用。

审查修复后的后端相关回归为 **128 passed**。
