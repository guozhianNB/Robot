# Task 11：护士台 PIN 门和 Plan 面板

状态：完成

## 实现

- 新增 `NursePinGate.vue`，使用当前管理员口令调用 `unlockNursePage`。仅后端返回成功后写入固定的 `sessionStorage` key `nurse-page-unlocked-v1`；不保存 PIN、无 TTL，刷新当前 tab 保持解锁，关闭 tab 后由浏览器自然清除。
- `App.vue` 在未解锁时只挂载 PIN 门，不加载通知、不挂载 PlanPanel、不建立 EventSource。解锁后显示“通知 / 计划”页签，并由 App 的唯一 SSE 连接同时分发通知和 Plan 刷新信号。
- 新增紧凑型 `PlanPanel.vue`，支持列表/详情、结构化创建、调级、取消、人工确认、safe goto retry、attempts 展示和 409 最新快照提示；所有请求使用 shared API，不附加 `X-Surface` 或其他鉴权标志。
- 新增 shared `planUi.ts`，admin 与 nurse 共用终态操作资格、确认/重试矩阵、状态标签、优先级排序和结构化步骤构建/校验，避免 Task 10 修复在两端漂移。
- 增加异步收口：App 卸载后进行中的通知请求不能重新排轮询；PlanPanel 用 load/open generation 丢弃同类与跨路径乱序响应。
- 按集成审查要求，将缺失的 Task 7 后端提交 `1c42c25`、`a2d12af` cherry-pick 为 `654d635`、`15af064`，确保 `/api/nurse/page-unlock` 与 `/api/plans*` 在当前分支真实存在。

## TDD

- 首轮红灯：新增 4 个护士台源码契约，两个组件不存在、admin 尚未复用 shared 规则、App 尚无解锁门。
- shared 规则红灯：测试因 `src/planUi.ts` 不存在而失败；实现状态矩阵和步骤校验后转绿。
- 生命周期红灯：源码契约要求 `workspaceActive`，修复卸载后异步重排轮询后转绿。
- 请求竞态红灯：源码契约要求 load/open generation 以及人工展开作废旧 reload；实现后转绿。
- 最终 `plans.test.ts`：33 passed。

## 验证

```text
pnpm --dir frontend --filter shared test
7 files / 65 tests passed

pnpm --dir frontend --filter nurse build
passed

pnpm --dir frontend --filter admin build
passed（验证 shared 规则抽取未回归 Task 10）

D:\_project\Robot\.venv\Scripts\python.exe -m pytest \
  LLM/tests/test_plan_api.py tests/test_shutdown_hooks.py -q \
  --basetemp=.pytest-task11-api
13 passed，1 个 pytest cache 目录权限 warning
```

`git diff --check` 通过。`vue-tsc/tsc` 在当前 worktree 没有可执行入口，未能单独运行；Vite 生产构建已通过。未进行真实浏览器 PIN、刷新/关 tab 与多窗口 SSE 人工验收。

## 审查与复用扫描

- 两轮独立审查发现的后端路由缺失、轮询泄漏、Plan 请求乱序均已修复；复审后无已知 Critical/Important 遗留。
- `jscpd@5` 扫描 shared/admin/nurse：73 files，重复率 1.24%。命中主要是两端不同主题下的 Plan 列表加载与结构化表单模板；易漂移的业务规则已抽到 `planUi.ts`。继续抽取 Vue 页面状态会让无 Vue 依赖的 shared 包承担 UI 运行时依赖，因此本任务不扩大重构。
- literal/type 扫描确认 session key 只有 PIN 门一处；Plan 类型仍以 `api/plans.ts` 为事实来源，状态矩阵与表单校验以 `planUi.ts` 为事实来源。

## Review 修复（增量）

- `PlanStepDraft` 的 move/turn/x/y/yaw 输入改为覆盖浏览器真实形态的 `number | string`，统一 strict finite parser；空串、纯空白、数字字符串、NaN 和 Infinity 均拒绝，不再被 `Number("")` 静默转为 0。
- `buildPlanCreateStep` 同样使用 strict parser，调用方即使绕过显式校验也会 fail closed；admin 与 nurse 的“添加步骤”均先显式校验，错误路径不 push、不调用 API。
- admin/nurse 当前计划标题增加 `min-width: 0` 与 `overflow-wrap: anywhere`，窄屏长连续字符不会把状态控件推出视口。
- shared 源码契约与边界测试增至 45 项；shared 全量 `77 passed`，nurse/admin 生产构建通过。
