# 小车 Plan 表实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 实现可持久化、可连续自动执行、可人工管理的 Plan 系统，并让 admin 与带页面 PIN 的护士台实时查看和操作 Plan。

**架构：** SQLite 保存 Plan、Step 和 Attempt；`LLM.agent.plan` 负责校验、编译和状态迁移，`LLM.agent.plan_scheduler` 用独立线程推进线性步骤。Plan 使用独立工具权限，通过共享动作闸门与对话争用同一个车控槽位；前端通过 shared REST/SSE 契约在 admin 和 nurse 两端复用同一套数据。

**技术栈：** Python 3、FastAPI、SQLite、现有 MCP 客户端、pytest、Vue 3、TypeScript、Vite、Vitest。

**设计规格：** `docs/参考资料/plan表设计-初版.md`

---

## 复用调查

```text
Name layer:      create_plan / plan_executor / plan_step / PlansPage -> 只命中设计规格，无生产实现
Behavior layer:  scheduler / verify password / resolve target / publish event / API client -> reminder.py、db.verify_admin_password、CarNav、bus.publish、shared/api/client.ts
Reference layer: notification feature opened -> notify.py + db.py + server.py + test_notify.py + shared notifications/events + nurse App.vue
```

复用决定：调度线程沿用 `reminder.start()/stop()`；护士台 PIN 复用 `db.verify_admin_password()`；目标解析复用 `CarNav`；MCP 调用复用 `mcp_client.call_tool()`；前端继续使用 shared 的 HTTP 与事件工具。

## 文件结构

- 创建 `LLM/agent/plan.py`：校验、目标编译、创建和人工状态迁移。
- 创建 `LLM/agent/plan_scheduler.py`：独立线程、派发、轮询、超时和恢复。
- 创建 `LLM/agent/action_gate.py`：对话与 Plan 共用的动作所有权。
- 创建 `LLM/tool/plan_tool.py`：`create_plan` 本地工具。
- 修改 `LLM/store/db.py`：三张表及事务操作。
- 修改 `LLM/agent/tools.py`、`LLM/car_mcp/car_server.py`、`LLM/agent/reminder.py`、`LLM/server.py`、`LLM/conf.py`：调用契约和装配。
- 创建 `LLM/tests/test_plan_db.py`、`test_plan.py`、`test_plan_scheduler.py`、`test_plan_api.py`，扩展车控、权限和 shutdown 测试。
- 创建 `frontend/packages/shared/src/api/plans.ts` 与测试，扩展 shared 事件。
- 创建 admin `PlansPage.vue`、nurse `PlanPanel.vue` 和 `NursePinGate.vue`。
- 回填护士台规格、`AGENTS.md` 和 `docs/log.md`。

---

### 任务 1：车控受理返回稳定 `task_id`

**文件：**
- 修改：`LLM/car_mcp/car_server.py:139-162`
- 修改：`LLM/tests/test_car_mcp.py`

- [ ] **步骤 1：编写失败测试**

```python
def test_started_action_returns_registered_task_id(server_rig):
    module, link, _nav, executor = server_rig
    out = load_json(module._move(link, executor, "forward", 0.5))
    assert out["status"] == "started"
    assert out["task_id"] == link.tasks[0]["task_id"]
```

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_car_mcp.py::test_started_action_returns_registered_task_id -q`

预期：FAIL，成功响应没有 `task_id`。

- [ ] **步骤 3：最少实现**

在 `_start()` 成功 JSON 中加入 `"task_id": task_id`，不改变其他字段。

- [ ] **步骤 4：验证绿灯并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_car_mcp.py -q`

预期：全部通过。

```powershell
git add LLM/car_mcp/car_server.py LLM/tests/test_car_mcp.py
git commit -m "fix: 车控受理结果返回任务编号"
```

---

### 任务 2：Plan 数据库与乐观锁

**文件：**
- 修改：`LLM/store/db.py`
- 创建：`LLM/tests/test_plan_db.py`

- [ ] **步骤 1：编写建表和事务失败测试**

```python
def test_create_plan_roundtrip():
    made = db.create_plan(
        {"title": "去 A 点", "priority": "P2", "status": "queued",
         "kind": "care", "preemption": "queue", "source_kind": "manual"},
        [{"seq": 1, "step_type": "action", "action": "robot_goto_point",
          "args_json": {"x": 1.0, "y": 2.0, "yaw_deg": 0.0},
          "status": "pending", "retry_policy": "safe_goto_only"}],
    )
    got = db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert got["version"] == 1
    assert got["steps"][0]["args_json"]["x"] == 1.0
```

另测：第二步插入失败时整张 Plan 回滚；版本 1 更新成功并升 2，旧版本再写返回 conflict；同一 `(step_id, attempt_no)` 不能重复。

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_db.py -q`

预期：FAIL，缺少表和函数。

- [ ] **步骤 3：增加 schema**

在 `SCHEMA` 增加 `plans`、`plan_steps`、`plan_step_attempts`，包含规格全部字段。约束至少包括：

```sql
UNIQUE(step_id, attempt_no),
UNIQUE(idempotency_key),
FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
```

为 `(status, priority, created_at)`、`(plan_id, seq)` 和 `car_task_id` 建索引。

- [ ] **步骤 4：实现精确数据 API**

```python
create_plan(plan: dict, steps: list[dict]) -> dict
get_plan(plan_id: int, *, include_steps=False, include_attempts=False) -> dict | None
list_plans(states: tuple[str, ...] = (), limit=50, before_id=0) -> list[dict]
plan_counts() -> dict
update_plan_versioned(plan_id: int, version: int, **changes) -> tuple[str, dict | None]
update_step(step_id: int, **changes) -> dict | None
prepare_plan_attempt(step_id: int, attempt_no: int, request: dict) -> dict
update_plan_attempt(attempt_id: int, **changes) -> dict | None
active_plan_step() -> dict | None
recover_running_plan_steps() -> int
```

状态只返回 `updated/conflict/missing`。JSON 编解码只留在 db 层。

- [ ] **步骤 5：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_db.py -q`

预期：全部通过。

```powershell
git add LLM/store/db.py LLM/tests/test_plan_db.py
git commit -m "feat: 增加 Plan 持久化与乐观锁"
```

---

### 任务 3：三类步骤、目标编译和 `create_plan`

**文件：**
- 创建：`LLM/agent/plan.py`
- 创建：`LLM/tool/plan_tool.py`
- 创建：`LLM/tests/test_plan.py`
- 修改：`LLM/agent/policy.py`
- 修改：`LLM/tests/test_policy_tools.py`

- [ ] **步骤 1：编写失败测试**

```python
def test_place_compiles_to_point(fake_resolver):
    out = plan.create_candidate({
        "title": "去护士站", "priority": "P2",
        "steps": [{"type": "action", "action": "robot_goto_place",
                   "args": {"place": "护士站"}, "label": "去护士站"}],
    }, creator={"uid": "elder_1", "role": "elder", "slot": "kiosk"},
       resolver=fake_resolver)
    step = out["plan"]["steps"][0]
    assert step["action"] == "robot_goto_point"
    assert step["args_json"] == {"x": 1.0, "y": 2.0, "yaw_deg": 30.0}
    assert step["target_json"]["source_action"] == "robot_goto_place"
```

再测 zone 编译；time wait 必须有 `wake_at`；manual 无 action；LLM P0、白名单外动作和原始完成判据均被拒；任一步失败不落半张 Plan。

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py -q`

预期：FAIL，模块不存在。

- [ ] **步骤 3：实现领域层**

`plan.py` 定义 `PRIORITIES`、`STEP_TYPES`、`PLAN_ACTIONS`，实现：

```python
compile_steps(raw_steps: list[dict], resolver) -> list[dict]
create_candidate(payload: dict, creator: dict, resolver=None) -> dict
create_from_tool(title, steps, priority="P2", owner_uid="", report=None) -> dict
```

resolver 缺省时函数内延迟构造 `CarNav()`；place/zone 编译成 point，并保存原名、地图名、坐标和 tags 指纹。

- [ ] **步骤 4：注册本地工具并传递 principal**

`plan_tool.py` 用 `@tool(... roles={"ward","elder","admin"})`。`tools.run_tool()` 在调用本地函数前用 `ContextVar` 设置 principal，finally reset；工具函数内延迟 import `agent.plan`，避免循环导入。

- [ ] **步骤 5：更新角色白名单测试**

ward、elder、admin 都能看到 `create_plan`；ward 仍看不到 `robot_move/goto_*`。执行 Plan 不读取 creator role。

- [ ] **步骤 6：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py LLM/tests/test_policy_tools.py -q`

预期：全部通过。

```powershell
git add LLM/agent/plan.py LLM/tool/plan_tool.py LLM/agent/policy.py LLM/agent/tools.py LLM/tests/test_plan.py LLM/tests/test_policy_tools.py
git commit -m "feat: 增加 Plan 编译器与创建工具"
```

---

### 任务 4：共享动作闸门与 Plan 专用 MCP 入口

**文件：**
- 创建：`LLM/agent/action_gate.py`
- 修改：`LLM/agent/tools.py`
- 创建：`LLM/tests/test_plan_scheduler.py`
- 修改：`LLM/tests/test_policy_tools.py`

- [ ] **步骤 1：编写失败测试**

```python
def test_dialog_action_is_busy_while_plan_owns_slot(monkeypatch):
    token = action_gate.claim("plan", "plan:7:step:2")
    try:
        out = tools.run_tool("robot_move", {"direction": "forward", "distance_m": 0.2}, ELDER)
        assert out["ok"] is False and out["status"] == "busy"
    finally:
        action_gate.release(token)
```

另测 `robot_stop` 绕过、旧 token 不能释放新 owner、Plan 白名单外动作被拒。

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_scheduler.py LLM/tests/test_policy_tools.py -q`

预期：FAIL，模块和入口不存在。

- [ ] **步骤 3：实现闸门**

`action_gate.py` 用 `RLock` 保护 `{owner, ref, generation}`，提供 `claim()`、`release()`、`snapshot()`；测试 fixture 直接 monkeypatch 状态，不暴露生产 reset 路由。

- [ ] **步骤 4：实现 Plan 调用入口**

在 `tools.py` 增加 `run_plan_tool(name,args)`：只允许 `robot_goto_point/move/turn/status/stop`，检查 `mcp_enabled`、工具已注册且 server 为 car，然后复用 `mcp_client.call_tool()`；不读取 principal。普通 `run_tool()` 的车控动作检查 gate，status/stop 永远放行。

- [ ] **步骤 5：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_scheduler.py LLM/tests/test_policy_tools.py -q`

预期：全部通过。

```powershell
git add LLM/agent/action_gate.py LLM/agent/tools.py LLM/tests/test_plan_scheduler.py LLM/tests/test_policy_tools.py
git commit -m "feat: 统一对话与 Plan 的车控仲裁"
```

---

### 任务 5：确定性调度器与三层结果解析

**文件：**
- 创建：`LLM/agent/plan_scheduler.py`
- 修改：`LLM/agent/plan.py`
- 修改：`LLM/conf.py`
- 修改：`LLM/tests/test_plan_scheduler.py`

- [ ] **步骤 1：编写结果解析失败测试**

```python
@pytest.mark.parametrize("raw,kind", [
    ({"ok": False, "message": "断开"}, "transport_error"),
    ({"ok": True, "result": '{"ok":false,"status":"error"}'}, "action_error"),
    ({"ok": True, "result": '{"ok":true,"status":"unavailable"}'}, "unavailable"),
    ({"ok": True, "result": "not json"}, "malformed"),
])
def test_parse_result_is_fail_closed(raw, kind):
    assert plan_scheduler.parse_tool_result(raw).kind == kind
```

再测：第一步 started 后不发第二步；匹配的 last 成功只推进一次；错 task_id 不污染当前 attempt。

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_scheduler.py -q`

预期：FAIL，调度器不存在。

- [ ] **步骤 3：添加配置**

在 `conf.py` 安全解析 `PLAN_TICK_S=1.0`、`PLAN_STATUS_GRACE_S=15.0`、`PLAN_GOTO_TIMEOUT_S=200.0`、`PLAN_MOVE_TIMEOUT_S=45.0`、`PLAN_TURN_TIMEOUT_S=45.0`、`PLAN_EXECUTOR_ENABLED=True`；非法、非有限或非正数回退默认值。

- [ ] **步骤 4：实现纯函数和单轮状态机**

```python
parse_tool_result(raw: dict) -> ToolResult
reconcile_step(step: dict, status_payload: dict, now: float) -> str
tick_once(now: float | None = None) -> None
```

严格区分外层失败、内层失败、unavailable、uncertain 和明确 task_id 结果。相对动作不重试；goto 只有旧任务明确终止且重新校验后才可创建新 attempt。

- [ ] **步骤 5：实现线程生命周期**

复用 reminder 的 Event 模式：`start()` 幂等，`_run()` 用 `_stop_evt.wait(PLAN_TICK_S)`，单轮锁避免重叠，`stop()` 置位。启动先把遗留 `dispatching/running/interrupting` 置为 `needs_review`。

- [ ] **步骤 6：补超时与恢复测试**

覆盖：move/turn 超时 call 数仍为 1；unavailable 15 秒内等待、超时复核；MCP 重启丢 current/last 后复核；gate 忙只等待；重复和迟到结果幂等。

- [ ] **步骤 7：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_scheduler.py LLM/tests/test_car_mcp.py -q`

预期：全部通过且不访问 rosbridge。

```powershell
git add LLM/agent/plan_scheduler.py LLM/agent/plan.py LLM/conf.py LLM/tests/test_plan_scheduler.py
git commit -m "feat: 实现 Plan 确定性调度器"
```

---

### 任务 6：人工迁移、取消和通知

**文件：**
- 修改：`LLM/agent/plan.py`
- 修改：`LLM/agent/plan_scheduler.py`
- 修改：`LLM/tests/test_plan.py`
- 修改：`LLM/tests/test_plan_scheduler.py`

- [ ] **步骤 1：编写失败测试**

覆盖版本冲突、manual complete、time wait 禁止人工确认、needs_review 三种 decision、排队取消不 stop、运行取消进入 `cancelling/interrupting`、重复取消只 stop 一次、stop 失败或 15 秒无新鲜车况转复核并通知。

```python
def test_running_cancel_is_idempotent(fake_stop):
    first = plan.cancel_plan(7, 3, "护士取消", NURSE)
    second = plan.cancel_plan(7, first["plan"]["version"], "重复点击", NURSE)
    assert second["plan"]["status"] == "cancelling"
    assert fake_stop.calls == 1
```

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py LLM/tests/test_plan_scheduler.py -q`

预期：FAIL，操作函数不存在。

- [ ] **步骤 3：实现操作函数**

```python
change_priority(plan_id, version, priority, actor) -> dict
cancel_plan(plan_id, version, reason, actor) -> dict
confirm_step(plan_id, version, step_id, decision, note, actor) -> dict
retry_step(plan_id, version, step_id, note, actor) -> dict
```

所有函数先验证状态，再乐观锁提交，最后广播并 `audit.log("plan", ...)`。确认规则逐字落实规格，running/dispatching/interrupting 返回 conflict。

- [ ] **步骤 4：接通知中心**

只调用 `notify.ingest("plan", type, level=..., uid=..., ref=f"plan:{id}")`，type 为 `plan_needs_review/plan_failed/plan_done`。通知失败只审计，不回滚 Plan。

- [ ] **步骤 5：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py LLM/tests/test_plan_scheduler.py LLM/tests/test_notify.py -q`

预期：全部通过。

```powershell
git add LLM/agent/plan.py LLM/agent/plan_scheduler.py LLM/tests/test_plan.py LLM/tests/test_plan_scheduler.py
git commit -m "feat: 增加 Plan 人工操作与通知"
```

---

### 任务 7：Plan API、护士 PIN 与 lifespan

**文件：**
- 修改：`LLM/server.py`
- 创建：`LLM/tests/test_plan_api.py`
- 修改：`tests/test_shutdown_hooks.py`

- [ ] **步骤 1：编写 API 和 PIN 失败测试**

```python
def test_nurse_unlock_tracks_current_admin_password(client):
    db.set_admin_password("2468")
    assert client.post("/api/nurse/page-unlock", json={"pin": "2468"}).json()["ok"] is True
    db.set_admin_password("1357")
    assert client.post("/api/nurse/page-unlock", json={"pin": "2468"}).json()["ok"] is False
    assert client.post("/api/nurse/page-unlock", json={"pin": "1357"}).json()["ok"] is True
```

再测 Plan API 无 `X-Surface` 可读写、版本冲突 409、非法迁移 409、不存在 404、请求不能注入 creator/status/completion。

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_api.py -q`

预期：FAIL，路由不存在。

- [ ] **步骤 3：添加严格请求模型和 PIN 冷却**

PIN 使用独立失败计数，连续 3 次错误冷却 10 秒；调用 `db.verify_admin_password()`，不读取 `admin_auth_required`，不签发 cookie/token。

- [ ] **步骤 4：实现七条 Plan API**

实现列表、详情、创建、priority、cancel、confirm、retry。同步数据库和编译操作全部 `asyncio.to_thread`。409 返回 `{ok:false,error,plan:latest}`。

- [ ] **步骤 5：装配生命周期**

MCP 启动后启动 scheduler；退出先停止 scheduler，再停止 MCP。shutdown 测试断言调用顺序。

- [ ] **步骤 6：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan_api.py tests/test_shutdown_hooks.py -q`

预期：全部通过，无真实外部副作用。

```powershell
git add LLM/server.py LLM/tests/test_plan_api.py tests/test_shutdown_hooks.py
git commit -m "feat: 提供 Plan API 与护士台页面口令"
```

---

### 任务 8：提醒幂等创建 Plan

**文件：**
- 修改：`LLM/agent/reminder.py`
- 修改：`LLM/tests/test_plan.py`

- [ ] **步骤 1：编写失败测试**

构造 once reminder，连续运行 `_tick_once()` 两次，断言只创建一张 `(source_kind=reminder, source_id=rid)` Plan；missed 同样一次；不能生成合法步骤的提醒只通知并审计 skipped。

- [ ] **步骤 2：验证红灯**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py -k reminder -q`

预期：FAIL，没有提醒接线。

- [ ] **步骤 3：实现首次迁移接线**

在 reminder 更新为 `triggered/missed` 后调用 `plan.create_from_reminder(rem)`；数据库为 `(source_kind,source_id)` 提供幂等约束。禁止创建零步骤 Plan。

- [ ] **步骤 4：验证并提交**

运行：`.venv\Scripts\python.exe -m pytest LLM/tests/test_plan.py LLM/tests/test_notify.py -q`

预期：全部通过。

```powershell
git add LLM/agent/reminder.py LLM/tests/test_plan.py
git commit -m "feat: 提醒触发时幂等创建 Plan"
```

---

### 任务 9：shared REST 与 SSE 契约

**文件：**
- 创建：`frontend/packages/shared/src/api/plans.ts`
- 修改：`frontend/packages/shared/src/events.ts`
- 修改：`frontend/packages/shared/src/index.ts`
- 创建：`frontend/packages/shared/tests/plans.test.ts`

- [ ] **步骤 1：编写失败测试**

```typescript
it("修改优先级携带 version", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) }));
  await changePlanPriority(7, "P1", 3);
  expect(fetch).toHaveBeenCalledWith("/api/plans/7/priority", expect.objectContaining({
    method: "POST", body: JSON.stringify({ priority: "P1", version: 3 }),
  }));
});
```

另测四类 Plan 事件都被 `parseBusPayload` 接受，未知事件仍返回 null。

- [ ] **步骤 2：验证红灯**

运行：`pnpm --dir frontend --filter shared test -- plans.test.ts`

预期：FAIL，API 和事件不存在。

- [ ] **步骤 3：实现 shared API 和类型**

定义 `PlanSummary/PlanDetail/PlanStep/PlanAttempt/PlanCounts`；实现 list/get/create/priority/cancel/confirm/retry/unlock。Plan API 不传 surface。

- [ ] **步骤 4：扩展事件唯一事实来源**

将 `plan_created/plan_updated/plan_step_changed/plan_needs_review` 同时加入接口 union 与 `KNOWN_TYPES`，业务分类只用 `kind`。

- [ ] **步骤 5：验证并提交**

运行：`pnpm --dir frontend --filter shared test`

预期：全部通过。

```powershell
git add frontend/packages/shared/src frontend/packages/shared/tests/plans.test.ts
git commit -m "feat: 增加前端 Plan API 与事件契约"
```

---

### 任务 10：admin Plan 页面

**文件：**
- 创建：`frontend/packages/admin/src/pages/PlansPage.vue`
- 修改：`frontend/packages/admin/src/App.vue`
- 修改：`frontend/packages/shared/tests/plans.test.ts`

- [ ] **步骤 1：编写源码契约失败测试**

读取 `PlansPage.vue`，断言使用 shared API、提供创建/调级/取消/确认入口，且不存在裸 `fetch("/api/plans")`。

- [ ] **步骤 2：验证红灯**

运行：`pnpm --dir frontend --filter shared test -- plans.test.ts`

预期：FAIL，页面不存在。

- [ ] **步骤 3：实现页面**

紧凑工具栏包含新建、刷新和状态筛选；当前 Plan 在顶部，列表按优先级/时间排列；展开显示步骤和 attempts。人工创建只能选择动作与对应字段，不提供原始 JSON。取消、确认和重试使用图标按钮与确认对话框。

- [ ] **步骤 4：挂载页签和 SSE**

`App.vue` 增加“计划”页签。页面复用 `parseBusPayload` 监听四类事件并 reload，卸载时关闭 EventSource；断线按现有 3 秒模式重连，重连成功立即 reload。

- [ ] **步骤 5：验证并提交**

运行：

```powershell
pnpm --dir frontend --filter shared test
pnpm --dir frontend --filter admin build
```

预期：测试和构建通过。

```powershell
git add frontend/packages/admin/src frontend/packages/shared/tests/plans.test.ts
git commit -m "feat: 管理台增加 Plan 管理页"
```

---

### 任务 11：护士台 PIN 门和 Plan 面板

**文件：**
- 创建：`frontend/packages/nurse/src/components/NursePinGate.vue`
- 创建：`frontend/packages/nurse/src/components/PlanPanel.vue`
- 修改：`frontend/packages/nurse/src/App.vue`
- 修改：`frontend/packages/shared/tests/plans.test.ts`

- [ ] **步骤 1：编写源码契约失败测试**

断言 PIN 组件调用 `unlockNursePage`、只在成功后写 `sessionStorage`、没有 TTL；PlanPanel 使用 shared API；App 未解锁时不连接 SSE、不拉通知或 Plan。

- [ ] **步骤 2：验证红灯**

运行：`pnpm --dir frontend --filter shared test -- plans.test.ts`

预期：FAIL，组件不存在。

- [ ] **步骤 3：实现 PIN 门**

固定 key 为 `nurse-page-unlocked-v1`。当前 tab 刷新继续解锁，关闭 tab 自动失效；不保存 PIN，不向 Plan API 附加标志。

- [ ] **步骤 4：实现 PlanPanel 和 App 生命周期**

PlanPanel 提供列表、创建、调级、取消、确认和重试。App 增加“通知/计划”标签；只有解锁后才加载数据并连接 SSE。保持护士台工作台尺度，不使用大号标题或嵌套卡片。

- [ ] **步骤 5：验证并提交**

运行：

```powershell
pnpm --dir frontend --filter shared test
pnpm --dir frontend --filter nurse build
```

预期：测试和构建通过。

```powershell
git add frontend/packages/nurse/src frontend/packages/shared/tests/plans.test.ts
git commit -m "feat: 护士台增加页面口令与 Plan 面板"
```

---

### 任务 12：规格回填与全量验证

**文件：**
- 修改：`docs/superpowers/specs/2026-09-18-nurse-console-design.md`
- 修改：`AGENTS.md`
- 修改：`docs/log.md`

- [ ] **步骤 1：回填 D9-D11**

保留历史文字并追加 2026-09-21 覆盖说明：护士台使用当前管理员口令的页面门；当前 tab 无超时；通知和 Plan API 免鉴权；护士台增加 Plan 查看和人工操作。

- [ ] **步骤 2：更新仓库说明和实现台账**

`AGENTS.md` 增加 Plan 模块、API、事件和护士台职责；`docs/log.md` 记录实际文件、测试、偏差及真机未验项。

- [ ] **步骤 3：运行后端聚焦测试**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_car_mcp.py LLM/tests/test_plan_db.py LLM/tests/test_plan.py LLM/tests/test_plan_scheduler.py LLM/tests/test_plan_api.py LLM/tests/test_policy_tools.py tests/test_shutdown_hooks.py -q
```

预期：全部通过且没有真实 rosbridge 连接。

- [ ] **步骤 4：运行全量验证**

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests tests -q
pnpm --dir frontend test
pnpm --dir frontend build
.venv\Scripts\python.exe scripts/check_privacy.py
git diff --check
```

预期：无新增后端失败；前端测试和四包构建通过；隐私和格式检查通过。既有失败必须与开工前基线逐项比较并记录。

- [ ] **步骤 5：执行 reuse-first sweep**

若仓库已有 clone detector，覆盖所有改动文件及 siblings；否则运行：

```powershell
rg -n "PLAN_TICK_S|PLAN_STATUS_GRACE_S|PLAN_GOTO_TIMEOUT_S|nurse-page-unlocked-v1|plan_needs_review" LLM frontend docs
rg -n "def .*plan|function .*Plan|interface Plan" LLM frontend/packages
```

确认常量、事件、状态枚举和 API 类型各有一个事实来源；删除 `*2/New/V2` 式副本。

- [ ] **步骤 6：人工浏览器验收**

验证护士台当前管理员口令、刷新/关 tab 行为；两端实时同步；版本冲突刷新；取消排队 Plan 不急停；假 MCP 两步严格串行；1280×720 与 390×844 无重叠。

- [ ] **步骤 7：提交文档**

```powershell
git add AGENTS.md docs/superpowers/specs/2026-09-18-nurse-console-design.md docs/log.md
git commit -m "docs: 回填 Plan 系统实现与验收结果"
```

---

## 实施顺序约束

1. 任务 1-4 顺序执行；任务 5 依赖它们；任务 6 依赖任务 5。
2. 任务 7、8 依赖后端状态机完成。
3. 任务 9 完成后再做 10、11；二者都修改 `plans.test.ts`，不得无协调并行写同一文件。
4. 任务 12 最后执行，不得提前把规格标成已实现。

## 开工前基线

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests tests -q
pnpm --dir frontend test
pnpm --dir frontend build
.venv\Scripts\python.exe scripts/check_privacy.py
```

原样记录基线。任何测试若可能访问真实板卡、地图编辑器或摄像头，先补隔离 fixture；禁止用真实硬件副作用换取测试通过。
