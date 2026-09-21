# Task 7 报告：Plan API、护士台 PIN 与生命周期

## 状态

完成。基于 Task 6 Plan 数据层/状态迁移实现了免鉴权 Plan HTTP API、护士台页面口令门，并将 Plan scheduler 接入主后端 lifespan。

## 改动

- `LLM/server.py`
  - 新增严格 Pydantic 请求模型（`extra=forbid`），请求不能注入 `creator`、`status`、`completion` 等后端字段。
  - 新增 `GET/POST /api/plans`、详情、priority、cancel、confirm、retry 七组路由；不依赖 `X-Surface`，写操作通过 `asyncio.to_thread` 调用 Plan/SQLite 层。
  - 版本或状态冲突统一返回 HTTP 409，并附带最新 Plan；不存在返回 404。
  - 新增 `POST /api/nurse/page-unlock`：仅调用 `db.verify_admin_password()`，独立三次失败/10 秒冷却，不读取 `admin_auth_required`，不建立 cookie/token/session。
  - MCP 启动后才启动 scheduler；退出时先 `plan_scheduler.stop()`，再停止 MCP。
- `LLM/tests/test_plan_api.py`：覆盖 PIN 随管理员口令变化、独立冷却、无 `X-Surface` 读写、409 最新 Plan、404 与请求字段注入。
- `tests/test_shutdown_hooks.py`：断言 MCP→scheduler 启动顺序和 scheduler→MCP 停止顺序。

## TDD / 验证

先写 API 测试并运行，缺失 `_nurse_pin_fail` 路由实现时按预期失败；随后实现接口，测试转绿。

最终验证命令：

```powershell
D:\_project\Robot\.venv\Scripts\python.exe -m py_compile LLM/server.py
D:\_project\Robot\.venv\Scripts\python.exe -m pytest --basetemp=D:\_project\Robot-task7\.pytest-tmp LLM/tests/test_plan_api.py tests/test_shutdown_hooks.py LLM/tests/test_plan.py LLM/tests/test_plan_db.py LLM/tests/test_plan_scheduler.py -q
```

结果：`96 passed`。

未连接真实车辆或 MCP；生命周期测试全部使用可控替身。

## Review 修复

根据 `task-7-review.md` 的 3 项 Important 完成补强：

1. PIN 校验收敛到同步 `_verify_nurse_pin()`，由 `asyncio.to_thread` 调用；冷却判断、PBKDF2 校验、失败计数更新在同一把锁内。冷却到期先清零，重新满足“连续 3 次”的语义。
2. lifespan 与 `/api/system/shutdown` 共用 `_stop_runtime()`，固定先停止 Plan scheduler，再停止 mapeditor 和 MCP；延迟退出调度独立封装，测试可 monkeypatch，不会触发真实 `os._exit`。
3. scheduler 启动/恢复异常写 `scheduler_start_failed` 审计并降级；停止阶段逐组件捕获并继续，最终以 `shutdown_degraded` 聚合审计。lifespan 使用 `try/finally`，确保请求阶段异常也执行清理。

新增红灯测试覆盖冷却重置、并发串行、启动降级、停止异常聚合和系统退出顺序。API/shutdown 专项结果：`13 passed`。
