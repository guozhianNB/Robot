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

## 复审整改

针对独立审查的 5 项 Important 和 2 项 Minor 已全部整改：

- 本地工具入口在签名过滤前检查顶层必填项和未知字段；action/wait/manual 分别使用独立字段白名单，wait/manual 不再持久化任意 `args`。
- `CarNav` 从解析目标所用的同一份 tags 快照计算并返回 `sha1:<40 hex>` 内容指纹；Plan 编译强制要求非空地图名、规范坐标和合法 SHA1 指纹。
- Plan 新增结构化 `report_json` 持久化列，已有数据库由 `init_db()` 自动补列，新建记录补齐两个布尔默认值。
- move/turn/goto 超时分别由 `conf.py` 的 45/45/200 秒配置控制，支持环境变量覆盖；非法或非正整数会安全回退，并由主后端启动层写入 `config_warning` 审计（审计失败不阻断启动，保留待重试）。
- `db.create_plan()` 在事务提交前构造完整返回快照；提交后的审计失败只降级告警，不再把已经成功创建的 Plan 报成失败。
- 回归测试补齐真实 `run_tool()` 入口、真实 `CarNav` 单快照指纹、审计失败、嵌套/异常嵌套/跨线程 ContextVar 隔离，以及 elder 工具集精确相等断言。

整改 TDD 红灯：`15 failed, 46 passed`。实现后的定向测试：`61 passed`；首轮扩展联合回归：`206 passed`；主后端角色路由与聊天/TTS 邻接回归：`31 passed`。独立复核补出的配置审计测试先以缺少冲刷函数红灯失败，补齐启动层冲刷、精确 SHA1 和带数据迁移断言后，最终受影响集合 `238 passed`。

完整 `LLM/tests` 回归结果为 `603 passed, 1 failed`；唯一失败是既有 `/api/face/status` 路由引用未定义的 `face_api`，该单测独立重跑仍失败。本次虽修改了 `server.py` 的配置告警冲刷，但未触及该 face_api 路由问题；此任务外基线问题不影响上述 Plan 整改回归。

本次没有扩展请求 ID 或数据库幂等键 schema。残余风险是服务端已成功返回、但响应在传输途中丢失时，客户端重试仍可能重复创建 Plan；该问题需要后续在端到端调用协议中设计稳定请求 ID 后解决。`plan_created` 广播仍按原分期留待 Task 6/9，与前端事件契约同步接入。
