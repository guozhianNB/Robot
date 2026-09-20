# Task 3 实施报告

## 状态

已完成三类步骤校验、导航目标编译、`create_plan` 本地工具、principal 上下文传递和角色白名单更新。

实现文件：

- `LLM/agent/plan.py`
- `LLM/tool/plan_tool.py`
- `LLM/agent/tools.py`
- `LLM/agent/policy.py`
- `LLM/tests/test_plan.py`
- `LLM/tests/test_policy_tools.py`
- `LLM/tests/test_policy_roles.py`

`test_policy_roles.py` 虽未列在原简报文件清单中，但它精确锁定 ward 白名单；父任务已确认这是新增 `create_plan` 后必须同步的既有契约测试。

## 红绿证据

红灯 1：

```text
python -m pytest LLM/tests/test_plan.py -q
ImportError: cannot import name 'plan' from 'LLM.agent'
```

失败原因是目标模块尚不存在，符合简报预期。

红灯 2（自审补充）：

```text
test_omitted_owner_does_not_silently_become_creator
AssertionError: assert 'elder_9' is None
```

据此修正了 `owner_uid=""` 被错误回填为创建者 uid 的行为；服务对象保持可空，创建者另存审计字段。

绿灯：

```text
python -m pytest LLM/tests/test_plan.py -q
15 passed

python -m pytest LLM/tests/test_plan.py LLM/tests/test_policy_tools.py \
  LLM/tests/test_policy_roles.py LLM/tests/test_plan_db.py -q
68 passed

DEEPSEEK_API_KEY=test-only python -m pytest \
  LLM/tests/test_server_roles_routes.py LLM/tests/test_chat_text_tts.py -q
31 passed
```

附加验证：四个生产模块 `py_compile` 通过，`git diff --check` 通过，`scripts/check_privacy.py` 通过。隐私脚本只报告仓库历史中的已知声纹/录音/体积警告，本次索引和未跟踪文件均干净。

## 提交

提交主题：`feat: 增加 Plan 编译器与创建工具`。本报告与实现放在同一提交中，最终哈希见任务回报。

## 自审

- `create_candidate` 在调用 `db.create_plan` 前完成全部步骤校验和目标编译；底层写入继续使用 Task 2 的单事务 API，不会落半张 Plan。
- 地点、区域和直接坐标均复用 `CarNav` 的解析与安全校验；没有新增第二套地点/区域解析器。默认路径对标准化 tags 内容生成 `sha1:` 快照，供后续执行前复核。
- LLM 输入不能注入 `status`、`completion`、`on_failure`、`retry_policy` 等执行控制字段；P0 和白名单外动作在落库前拒绝。
- `run_tool` 只在本地工具调用边界设置 principal，并在 `finally` 中按 token 恢复；MCP 分发路径不变。
- ward 仅新增 `create_plan`，仍看不到 `robot_move/turn/goto_*`；Plan 的后续执行不依赖 `creator_role`。
- 按父任务分期裁定，本任务只记录 Plan 创建审计，不提前发布 `plan_created`。该 SSE 事件及 `frontend/packages/shared/src/events.ts` 契约留待 Task 6/9 同步接入，避免后端先发布前端未知事件。

当前无阻断疑虑。剩余风险是默认 `CarNav` 目标解析后需再次读取标准化 tags 来计算内容指纹；若两次读取之间地图标记变化，后续执行前的目标/指纹双重比对必须拒绝该步骤，而不能静默使用旧坐标。
