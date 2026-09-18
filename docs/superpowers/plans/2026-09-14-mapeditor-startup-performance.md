# 地图编辑器启动性能修复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让后端不再等待 MCP 握手即可提供服务，并消除地图编辑器首屏的事件循环阻塞、重复请求和轮询堆积。

**架构：** MCP 连接仍运行在现有专属线程，但 `start()` 只调度不等待；地图领域层保持同步，FastAPI 首屏路由统一在线程池执行；前端由 `App.vue` 独占地点和区域数据请求；当前地图识别按输入指纹短时缓存。

**技术栈：** Python 3.11、FastAPI、asyncio、pytest、Vue 3、TypeScript、Vite、Node 源码契约测试。

---

## 文件结构

- 修改 `LLM/mcp_client.py`：非阻塞 MCP 启动、连接任务生命周期与幂等停止。
- 创建 `tests/test_mcp_startup.py`：MCP 启动延迟和停止回归测试。
- 修改 `LLM/server.py`：首屏地图同步工作在线程池运行。
- 修改 `LLM/locator.py`：当前地图识别 TTL 缓存及失效入口。
- 修改 `LLM/conf.py`：集中定义当前地图识别缓存 TTL。
- 修改 `frontend/packages/mapeditor/src/App.vue`：顺序化初始化、状态轮询防重入、面板共享数据。
- 修改 `frontend/packages/mapeditor/src/pages/PlacePanel.vue`：使用父组件地点列表。
- 修改 `frontend/packages/mapeditor/src/pages/ZonePanel.vue`：使用父组件区域列表。
- 创建 `frontend/packages/mapeditor/scripts/test-startup-contract.mjs`：验证首屏请求所有权和轮询防重入契约。
- 修改 `frontend/packages/mapeditor/package.json`：登记性能契约测试命令。

### 任务 1：MCP 非阻塞启动

- [ ] 编写 `tests/test_mcp_startup.py`，用阻塞协程替换 `_connect_all`，断言 `start()` 在 0.2 秒内返回，连接 Future 仍在后台运行。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest tests/test_mcp_startup.py -q`，确认因 `Future.result()` 同步等待而失败。
- [ ] 在 `LLM/mcp_client.py` 保存 `_connect_future`，调度后立即返回；连接完成回调更新 `_started` 并记录整体降级状态。
- [ ] 在 `stop()` 中先取消未完成 Future，再沿用现有会话和线程清理；重复 `start()` 时直接返回现有 schemas。
- [ ] 重跑 MCP 测试，预期全部通过且无未回收任务警告。

### 任务 2：地图首屏路由不阻塞事件循环

- [ ] 在 `tests/test_mapeditor.py` 增加并发回归测试：假 MapStore 的 `list()` 阻塞时，同时请求 `/api/health`，健康请求必须在地图请求释放前完成。
- [ ] 运行该测试，确认当前 `map_list()` 在事件循环直接调用同步 Store 而失败。
- [ ] 在 `LLM/server.py` 为地图列表、当前地图、元数据、PNG、地点、区域和汇总状态增加同步工作函数，路由通过 `await asyncio.to_thread(...)` 调用。
- [ ] 保持异常转换、响应字段、审计和写入顺序不变；只把完整同步工作单元移入线程。
- [ ] 运行新增并发测试与 `tests/test_mapeditor.py -q`，预期通过。

### 任务 3：当前地图识别短时缓存

- [ ] 在 `tests/test_mapeditor.py` 增加测试：相同 `/map` 指纹连续调用只执行一次 `store.list()`；调用缓存失效入口后再次扫描。
- [ ] 运行该测试，确认当前每次调用都扫描。
- [ ] 在 `LLM/conf.py` 增加 `MAP_CURRENT_CACHE_TTL_S`；在 `LLM/locator.py` 以 topic 指纹和 TTL 保存识别结果，并提供 `clear_current_map_cache()`。
- [ ] 在地图元数据、重命名、复制、删除和像素保存成功路径调用失效入口；测试注入/清除地图时也失效。
- [ ] 重跑相关测试，确认缓存复用和失效均通过。

### 任务 4：前端去重与轮询防重入

- [ ] 创建 `frontend/packages/mapeditor/scripts/test-startup-contract.mjs`，断言地点/区域 fetch 只存在于 `App.vue`，两个面板接收 `items` props，`statusTick` 有 in-flight 防重入。
- [ ] 运行 `pnpm --filter mapeditor test:startup`，确认因面板仍自行请求且没有防重入而失败。
- [ ] 修改 `PlacePanel.vue` 和 `ZonePanel.vue` 接收父组件列表，删除读请求、加载状态与 `mapName` 触发的重复加载；保存/删除后只发 `changed`。
- [ ] 修改 `App.vue` 传入列表；初始化先等待 `loadMaps()` 完成选图，再读取状态；给 `statusTick()` 增加 `statusPending` 守卫和 `finally` 复位。
- [ ] 在 `package.json` 增加 `test:startup`，重跑契约测试和 TypeScript 检查。

### 任务 5：完整验证

- [ ] 运行 `.venv\Scripts\python.exe -m pytest tests/test_mcp_startup.py tests/test_mapeditor.py -q`，预期零失败。
- [ ] 运行 `pnpm --filter mapeditor test:startup`，预期零失败。
- [ ] 运行 `pnpm --filter mapeditor exec vue-tsc --noEmit`，预期零错误。
- [ ] 运行 `pnpm --filter mapeditor build`；若环境仍触发已知子进程限制，运行 `pnpm --filter mapeditor build:sandbox` 并明确记录。
- [ ] 启动隔离后端，验证监听端口在 MCP 连接完成前可访问；随后运行 `node scripts/e2e_smoke.mjs http://127.0.0.1:<port>/mapeditor/`。
- [ ] 检查 `git diff --check` 和目标文件 diff，确认未改动无关用户文件。
