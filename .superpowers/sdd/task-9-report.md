# Task 9 报告：shared REST 与 SSE 契约

- 状态：DONE
- 范围：`frontend/packages/shared/src/api/{client,plans}.ts`、`events.ts`、`index.ts`、`tests/{client,plans}.test.ts`

## 实现

- 新增 Plan 列表、详情、创建、调级、取消、确认、重试和护士台页面解锁客户端，全部复用 shared `apiGet/apiPost`；Plan API 与页面解锁均不附加 `X-Surface`。
- shared client 的非 2xx 路径继续 reject，并保持原错误消息；新增 `ApiError.status/url/body`，409 调用方可直接读取后端的 `error` 与 latest Plan，不必额外 GET。
- 类型逐项对应 Task 7 与 SQLite 返回字段；创建响应使用无 Attempt 的 `PlanCreated`，详情/人工迁移使用 Attempt 必填的 `PlanDetail`，两类快照不再混为可选字段。
- 创建请求使用 P1-P3 优先级、五种 action、三种 wait/manual 步骤的判别联合，并将 report 限制为两个布尔开关，前端不能通过类型注入执行状态或 P0。
- `plan_created`、`plan_updated`、`plan_step_changed`、`plan_needs_review` 同时进入 `BusEvent` 联合和 `KNOWN_TYPES`。后端当前未发送业务 `kind`，故该字段可选；未来增加业务分类时只使用 `kind`，不占用总线 `type`。

## TDD 与审查

- 首轮红灯：`plans.test.ts` 因 `../src/api/plans` 不存在而失败。
- barrel 回归红灯：临时移除 `index.ts` 导出后，测试明确得到 `undefined`；恢复导出后转绿。
- 类型红灯：`tsc` 捕获创建响应缺 `summary/attempts`、事件错误要求 `kind`，以及创建输入允许 P0/任意 step/report；收紧契约后 Task 9 类型错误清零。
- 独立审查提出的两个 Important（事件 `kind` 漂移、创建输入过宽）均已修复，并补真实后端 payload 与编译期负例。
- Task 9 正式 review 的 Important 以 409 回归红灯稳定复现：普通 `Error` 没有 `status/body`；实现 `ApiError` 后转绿。同步修复 Minor，将创建快照与完整详情类型拆开，并用编译期条件断言锁定 Attempt 可选性。

## 验证

```text
pnpm --dir frontend --filter shared test
7 files / 51 tests passed

shared 源码入口 tsc --noEmit
passed

pnpm --dir frontend build
admin / kiosk / nurse / mapeditor passed

git diff --check
passed

jscpd@5 frontend/packages/shared/src frontend/packages/shared/tests
15 files / 0 clones
```

完整 shared `tsconfig` 的类型检查仍被两个既有测试问题阻断：`client.test.ts` 的 `afterEach` 返回类型，以及 `kiosk-thinking-request.test.ts` 缺少 Node 类型声明；本次新增文件无类型错误，源码入口独立检查通过。
