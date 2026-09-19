# 开发日志

> 本文件记录"做了什么、有什么用"，不是教程。按日期追加。

---

## 2026-08-19 · 大模型端：LLM 对话 + RAG + 工具调用（联网）+ 定时提醒 + 前端

**依据：** `docs/2.pre/大模型端开发目标.md`（模块 1/2/4/6/8/9 的演示最小集）。

### 做了什么

后端（`LLM/` 目录，由单文件扩展为包，全部零新增第三方依赖，纯 stdlib + 已有 openai/fastapi）：

| 文件 | 内容 |
|------|------|
| `server.py` | 重写：FastAPI 入口 + 全部 API 路由；启动时初始化 SQLite、种演示档案、拉起提醒调度线程和广播任务 |
| `conf.py` | 集中配置：路径、默认设置、思考路由关键词、上下文窗口参数、记忆分级规则 |
| `db.py` | SQLite 数据层：老人档案 / 记忆 / 提醒 / 工具日志 / 对话历史 / 设置 / 摘要，WAL + 写锁 |
| `vectors.py` | 轻量向量检索（字符 n-gram 哈希 + TF 余弦），零依赖，代替 ChromaDB/FAISS（Python 3.14 下装这些有兼容风险，演示规模也用不上） |
| `memory.py` | RAG 混合检索路由（结构化字段查档案表 + 偏好/事件走向量）+ 记忆沉淀分级（医疗只人工 / 偏好进待处理 / 事件带 TTL） |
| `tools.py` | 联网工具：`web_search`（有 BOCHA/SERPAPI key 走官方 API，否则 DuckDuckGo + 新闻语料关键词兜底）、`get_news`（人民网/中新网等 RSS 并发抓取，健康类自动加"仅供参考"）；结果过滤惊悚类内容 |
| `chat.py` | 对话编排：护工角色 + 安全红线 System Prompt、RAG 记忆注入、思考路由层（关键词预分类，健康/敏感问题自动深思考）、滚动窗口 + 历史摘要、工具调用循环（最多 2 轮） |
| `reminder.py` | 独立线程的定时提醒调度器：每日/一次性两种、错过补报、送达状态机（pending→triggered→confirmed/unconfirmed）、确认超时升级告警、静默时段判定 |
| `bus.py` | 事件总线：提醒/告警通过 SSE 广播给前端，与对话进程解耦 |
| `log.py` | JSONL 审计日志（对话 / 记忆改动 / 提醒 / 工具调用 / 设置变更），落 `LLM/data/audit.jsonl` |

API：`/api/chat`（流式）、`/api/profiles`、`/api/memories`（查看/审核/人工录入/沉淀）、`/api/reminders`、`/api/tools/log`、`/api/settings`、`/api/events`（广播 SSE）、`/api/context`（看某老人记住了什么）、`/api/health`。

前端：

- `UI/index.html`（新增）：单文件 SPA，5 个页签 —— 对话（流式 + 思考路由 chip + 工具调用过程 chip + Markdown）、记忆（档案表单 + 已确认/待处理记忆 + 医疗人工录入）、提醒（护士建议录入 + 列表 + 确认/删除，广播实时刷新）、工具日志表、设置开关（持久化）。
- `UI/chat.html`（重写）：改为跳转到 `index.html`，保留原 URL 可用。

数据与演示：首次启动自动种"张建国"示例档案（文档示例），用药自动同步为每日服药提醒；`.gitignore` 排除了运行期 DB 与审计日志。

### 有什么用

- 对话不再是"裸聊"：System Prompt 带护工身份与安全红线（实测"降压药能减半吗"会拒绝并转达护士，且自动深思考）；RAG 让机器人记得老人档案与偏好，跨轮不遗忘；联网工具让"今天有什么新闻"这类问题能拿到真实 RSS 内容并摘要播报。
- 定时提醒独立线程跑，对话卡死不影响"到点必须响"；错过触发点会补报；护士/家人可在前端确认服药结果，未确认超时升级告警并留审计。
- 前端一个文件管 5 件事，演示/调试够用，设置页开关一键关联网/提醒等。

### 已实测验证

- 日常问候：thinking 路由判"日常闲聊"→ 快速回答。
- 问新闻：`get_news(国内)` 工具被调用并执行成功，回答带真实新闻摘要。
- "降压药能减半吗"：路由命中关键词「药」→ thinking on，回答遵守医疗只读红线。
- 提醒：一次性提醒 50 秒后准时广播；已过期每日提醒触发"错过补报"；前端确认后状态变已确认。
- 记忆沉淀：对话后自动提取出"想孙子（事件→直接入库带 TTL）/偏好称呼（→待处理）"等，分级正确；医疗关键词被拒写。
- 档案加药 → 自动生成对应每日服药提醒；设置持久化重启不丢。

### 已知限制（后续可做）

- `web_search` 无 key 时走新闻语料关键词匹配，命中率有限（约 4~5s）；配 `BOCHA_API_KEY` 或 `SERPAPI_KEY`（.env）即走真实搜索引擎。
- 向量检索是演示级（n-gram 余弦），老人数多或记忆量大时再换 bge/ChromaDB。
- 语音链路（ASR/TTS）、主动交互、报警推送仍为占位开关，未接真实服务。
- 提醒的"播报"目前是前端 toast 广播，未接 TTS 语音播报。

---

## 2026-08-19 · 二轮修复：RAG 记忆质量 + 思考路由覆盖 + 记忆整理时机

**实测暴露的三个问题及修复：**

### 1. RAG 不再把玩笑当真、不再每轮都写

- 原因：原实现每轮对话后都让模型提取记忆，"打趣的话"（如"怕不是领带都打不来"）被当成事实入库，且每轮一条、越积越多。
- 改为**批量整理**（`memory.py` 重写）：
  - 触发时机：**话题结束才整理**——老人空闲 N 秒不再说话（默认 30s，可配），或上下文窗口已满；
  - 整理时把整段对话 + 已有记忆一起交给模型，只提取"新信息"，并**明确规则"玩笑、打趣、比喻、假设不是事实"**；
  - 提取条目分 `add / skip / merge / conflict`：重复的跳过、补充细节 merge 写回原条目、与已有记忆矛盾标 conflict 进待处理；
  - 服务端再用向量相似度做一次去重兜底（已确认 + 待处理都查）。
- 实测："穿西装 + 打领带玩笑"对话整理后，含"领带"的记忆 = 0；同一话题重复聊 2 轮，记忆数不增。

### 2. 思考路由从"关键词"升级为"关键词 + 情绪词 + LLM 预判"

- 原因：只有敏感词表覆盖不了"国际形势动荡，美国怎么这么坏啊"这类问题（原实现直接秒回）。
- `chat.py` 路由改为三层：
  1. 主题/健康/敏感关键词（扩充：国际、美国、形势、政策、经济、军事、手术、失眠、遗产等）；
  2. 情绪/负面词（生气、委屈、害怕、骂、哭、恨等，只用多字词避免"气"误中"天气"）；
  3. 规则未命中且消息够长（≥10 字）时，用 LLM 快速预判（`{"deep": bool, "reason"}`）兜底。
- meta 事件带 method（keyword/emotion/llm/manual），前端 chip 显示命中来源。
- 实测："现在国际形势动荡，美国怎么这么坏啊"→ 命中「国际」深思考；"晒太阳"等日常句不再误判。

### 3. 记忆可修正、精简：新增"老人画像"

- 每次整理同时让模型输出：
  - `entries`（记忆条目，上述分级去重逻辑）；
  - `digest`（话题一句话摘要，并入历史摘要）；
  - `portrait`（**老人画像**：整合档案 + 已有记忆 + 本次对话，≤150 字的精简档案卡，含性格/习惯/偏好/健康注意/说话风格），存 portraits 表，注入 System Prompt，随整理自动更新。
- 关于"生成 skills 之类"的讨论结论：**现阶段没必要**。老人对话的长期价值已由"画像 + 风格 prompt + 记忆"覆盖；"skill"（程序化技能）要有可执行的动作才成立，当前机器人没有自主执行链路，等底盘/主动交互成熟再考虑。
- 前端：设置页新增"路由 LLM 预判""记忆整理"两个开关 + "整理空闲秒数"输入；记忆页新增"老人画像"卡片；对话页 chip 显示思考命中来源。

### 运维备注

- uvicorn 需以包方式从项目根启动：`python -m uvicorn LLM.server:app --port 8000`；
- 换端口/重启前先确认旧进程已退（Windows 下杀后台任务可能留下孤儿 python 进程占着 8000 端口，导致新代码没生效——本次排查发现并清理）。

---

## 2026-08-19 · 对话历史持久化 + 前端刷新恢复

- 记忆/档案/提醒/设置/审计本来就存 SQLite（`LLM/data/brain.db`），后端重启不丢；丢的只是**前端页面上显示的对话气泡**（刷新浏览器就没了）。
- 后端新增：`GET /api/chat/history?uid=`（回读某老人历史对话）、`DELETE /api/chat/history?uid=`（清空历史）。
- 前端：启动和切换老人时自动回读历史渲染成气泡；对话页新增"清空对话"按钮（只清对话，不影响记忆）。
- 实测：连聊多轮 → 重启后端 → 历史 10 条、已确认/待处理记忆、画像、摘要全部还在。

---

## 2026-08-20 · 小车端：ROS2 SLAM 建图 + Nav2 导航代码（方向二·核心任务1）

**依据：** `docs/目标文档及说明/ROS2小车端开发目标.md`（模块 1/3/4）、`docs/1.pre/教程_ROS2_SLAM小车实战.md`、板卡 `.hermes/skills/robotics` 与 `SLAM_Car` 项目方案。
**运行载体：** RDK X5（Ubuntu 22.04 / ROS2 Humble）+ YDLidar Tmini Plus + STM32 麦轮底盘（USB CDC）。

### 板卡环境实测（2026-08-20，SSH 100.65.82.93）

- 官方示例工作区 `~/ros2/yahboomcar_ws` 已编译：`ydlidar_ros2_driver`、`slam_gmapping`、`rf2o_laser_odometry`。
- 雷达实测为 **YDLidar Tmini Plus**（TOF，/dev/ttyUSB0，230400 波特，10Hz，frame `laser_frame`）；官方默认 TminiPro.yaml 可直接跑通扫描（X4.yaml 配置会连不上扫描）。
- 底盘（STM32，/dev/ttyACM0）当时未连接；已装 `pyserial`。
- **未安装**：slam_toolbox / nav2 / robot-localization（apt 源有候选版本）。

### 做了什么（代码在 `ros2_car/`，本地镜像 `D:\_project\Robot\ros2_car\`）

| 包 | 内容 |
|------|------|
| `robot_chassis` | 底盘驱动：`cmd_vel`→STM32 USB CDC 帧（`docs/USB车控接口.md` v1.0：SET_CAR_VEL 0x03 / STOP 0x01 / GET_STATUS 0x05，xor 校验）；STATUS(0x82) 四轮 RPM → 麦轮逆运动学 → 积分 → `/odom` + `odom→base_link` tf；看门狗 0.5s 零速兜底、限速（vx≤0.5 / vy≤0.3 / wz≤0.8）+ 加速度斜坡、`/robot/cmd_stop` 急停、串口断线自动重连。`usb_protocol.py` 独立编解码模块，**单测已通过**（帧头/校验/增量解析/坏帧重同步/STATUS 解码） |
| `robot_bringup` | 一键启动：5 个 launch（lidar / odom / slam / navigation / bringup）+ 参数（`lidar_tmini_plus.yaml`、`slam_toolbox_params.yaml`、`nav2_params.yaml`、`ekf_params.yaml`）+ `car.urdf`（TF：odom→base_link→laser_link）+ 建图/导航两套 rviz 配置。`odom_source:=chassis|rf2o` 二选一（无底盘用 rf2o 激光里程计兜底）；navigation 复用 nav2_bringup 的 localization + navigation 组合（AMCL 定位 + DWB 局部规划 + NavFn 全局规划 + 代价地图避障） |
| `robot_navigation` | 辅助节点：`navigate_to_pose`（命令行发 Nav2 目标，免 rviz）、`cmd_stop`（订阅 `/robot/cmd_stop` → 取消 Nav2 目标 + 发零速，对接大模型端契约 `docs/ROS底盘接口需求.md`） |

**说明文档：** `ros2_car/README.md`（环境/一键启动/分步调试/底盘标定/安全机制/常见问题/大模型端对接）。

### 状态与待办（截至日志）

- ⏸ 依赖安装 `sudo apt install ros-humble-navigation2 nav2-bringup slam-toolbox robot-localization` **未完成**（板卡网络慢，下载中途按用户要求停止，dpkg 无残留、无锁，缓存可复用）。
- 📤 代码**未上传板卡**：用户改为手动操作（scp `D:\_project\Robot\ros2_car` → `~/ros2/car_ws` → `colcon build --symlink-install`）。
- 📋 板卡手动步骤（验证顺序）：雷达 `/scan` 出数 → rf2o `/odom` → `bringup mode:=mapping` 键盘建图 → `map_saver_cli` 存图 → `bringup mode:=navigation` AMCL 定位 + 2D Goal 导航。
- 🔧 待办：装完依赖后核对 nav2 参数（nav2_bringup 实际版本）、底盘接入后轴方向/轮径/旋转半径标定（`chassis_params.yaml`）、大模型端 `robot/move`/`turn`/`navigate_to` 服务扩展。

### 已知限制

- 麦轮底盘暂按差速模式导航（nav2 `max_vel_y: 0.0`），横移导航后置。
- `nav2_params.yaml` 中 `map_server.yaml_filename`、`bt_navigator` 的 bt_xml 路径按 humble 默认写，装完需与板卡实际版本核对。
- EKF（robot_localization）参数已备好但默认关闭，需底盘 + rf2o 双里程计才生效。

### 2026-08-20 追加：launch 条件写法 bug 修复 + git 产物清理

- **Bug**：`bringup.launch.py` / `odom.launch.py` 用 `PythonExpression(['odom_source == "chassis"'])` 写条件，启动报
  `NameError: name 'odom_source' is not defined`。根因：Humble `launch.substitutions.PythonExpression` 的
  `perform()` 是 `eval(perform_substitutions(expr), {}, math.__dict__)`，**locals 为空、globals 只有 math，
  不解析裸标识符**为 launch 参数。
- **修复**：改为把 `LaunchConfiguration` 对象嵌进表达式并加引号：
  `PythonExpression(["'", odom_source, "' == 'chassis'"])`（替换后为 `'chassis' == 'chassis'` 再 eval）。
  共 4 处：odom.launch.py 2 处（chassis/rf2o）、bringup.launch.py 2 处（mapping/navigation）。
  本地模拟 eval 验证通过；旧写法可精确复现原报错。
- **git 清理**：误把板卡 `colcon build` 产物（`ros2_car/build/ install/ log/`，197 个文件）提交进仓库；
  已 `git rm -r --cached` 移出跟踪（磁盘保留），根 `.gitignore` 追加 `ros2_car/build|install|log/`、
  `__pycache__/`、`maps/*.pgm|*.yaml` 忽略规则。**后续板卡同步后必须重新 `colcon build`**
  （install/share 里还是旧 launch），建议用 `--symlink-install` 便于调试期改 launch 即改即用。

### 2026-08-24 追加：LLM 工具系统改造 —— 装饰器注册表 + per-tool 开关 + 前端工具页开关

- **背景**：原 `tools.py` 中 schema 声明、实现函数、`run_tool` 手写 if 分发三处分离，新增工具要改多处且易不同步；`web_search_enabled` 一把梭管所有工具，无法单独开关。
- **改造（`LLM/tools.py`）**：
  - 新增装饰器 `@tool(name, description, parameters, enabled=True)`：schema 与实现写在一起，import 时自动注册进 `_TOOL_REGISTRY`。
  - `TOOLS` / `TOOL_ENABLED_KEYS` / `TOOL_DEFAULTS` 由注册表自动生成（`TOOLS` 对外接口不变，chat/server 旧引用兼容）。
  - `run_tool` 改为注册表分发；新增 `_run_fn` 用 `inspect.signature` 过滤模型传来的参数，缺省交给函数默认值兜底（避免内部 TypeError 被误判重试）。
  - 新增 `effective_tools(settings)`（按 `<工具名>_enabled` 过滤传给模型的 schema）、`tools_with_state(settings)`（给前端带 enabled/switch_key）。
- **联动改动**：`chat.py` 改用 `effective_tools(settings)` 过滤；`db.py` 的 `get_settings/set_settings` 合并 `TOOL_DEFAULTS`（新工具开关 key 自动可读写持久化，无需改 db/conf）；`server.py` `/api/tools` 返回每工具开关状态。
- **前端（`UI/index.html`）**：工具日志页顶部新增「工具开关」卡片（名称+描述+switch，保存走 `/api/settings`）；设置页移除原 `web_search_enabled` 总开关（统一在工具页管理）。
- **新增工具现在只需写一处**（见 tools.py docstring「三步走」）：装饰器 + 实现函数，分发/清单/开关/前端展示全部自动生效。
- **验证**：`py_compile` 4 文件通过；模块级冒烟（注册表/分发/未知工具/开关过滤）通过；后端 8011 端口启动后 `/api/tools`、`/api/settings` 读写、未知 key 丢弃、开关保存恢复均通过（测试后已恢复默认全开）。

---

## 2026-08-24 · 模块状态弹窗 + 系统退出按钮

**背景：** 护士/家属需要一眼看清后端可选能力（语音 / Embedding / RAG / 知识图谱）是否可用、缺了什么依赖；同时给出一键安全退出——停提醒、释放语音设备、关广播，而不是直接杀进程。

### 后端（`LLM/server.py`）

- 新增 `GET /api/modules/status` 聚合接口：一次返回 `voice`（`voice_api.get_status()`）、`embed`（`embed.status()`）、`ragstore`（`ragstore.status()`）、`graph`（`graph.status()`）四模块状态；各模块缺依赖时自行降级（`available=False` / `status=unavailable`），接口照常 `{"ok": True}` 返回，不因单模块故障而报错。
- 新增 `POST /api/system/shutdown` 优雅退出，按序执行：
  1. 落审计（`log("system", action="shutdown")`）；
  2. `reminder.stop()` 停提醒调度线程（不再触发新提醒）；
  3. `voice_api.stop_voice()` 停语音 worker（释放麦克风/扬声器）；
  4. `bus.stop()` 停 SSE 广播扇出；
  5. `_bg.shutdown(wait=False)` 停后台任务线程池；
  6. `_delayed_exit()` 延迟 1 秒 `os._exit(0)`——给 uvicorn 留出时间把 200 响应发回前端再杀进程。

### 前端（`UI/index.html`）

- 顶栏新增「🔍 模块状态」「⏻ 退出」按钮 + modal 基础样式（3 个提交：modal 样式与按钮 → 弹窗与警示色 → 退出确认与遮罩）。
- 「模块状态」弹窗：拉取 `/api/modules/status` 逐模块展示——✅ 可用 / ⚠️ 未启动或设置中关闭（非故障）/ ❌ 缺失依赖（列出缺失项）；**任一模块异常时顶栏按钮变警示色（warn）**；「🔄 重新检测」按钮刷新列表；后端离线时弹窗显示离线提示。
- 「退出」按钮：确认弹窗（取消无副作用）→ 确认后 `POST /api/system/shutdown` → 全屏「系统已退出」遮罩（按钮禁用、SSE 断开）；后端约 1 秒后自行退出，刷新页面显示「后端离线」。

### 测试

- 新增 `tests/test_modules_status.py`（`/api/modules/status` 响应结构：4 个模块键齐全）、`tests/test_shutdown_hooks.py`（reminder 停止钩子：start 先 clear 再 stop 置事件；bus 停止置标志）。
- 全量回归：`pytest tests -q` → **35 passed**（8 个测试文件，无回归）。
- 手动端到端验证（启动 uvicorn + 浏览器操作 + 退出）由控制者执行，结果见任务 7 报告。

---

## 2026-08-24 · 模块状态弹窗警告/错误日志视图

**背景：** 模块状态弹窗只告诉护士/家属"哪个模块不可用"，但看不出**为什么**——缺依赖、语音出错、提醒告警等都在审计日志里，前端却无处可查。本功能在弹窗内直接提供"警告/错误日志"视图，服务端过滤、前端红/橙分色展示，排查问题不用再登服务器翻 `audit.jsonl`。

### 后端（`LLM/log.py` + `LLM/server.py`）

- `log.py` 新增 `read_warnings(limit=50)`：顺序读 `audit.jsonl` 全文，逐行过滤出警告/错误类条目，返回末尾 `limit` 条。命中任一条规则即保留：
  - `action` 或 `event` 含 `error`（覆盖 `*_error` 事件与 `tick_error` 等 action）；
  - `event == "alarm"`（提醒升级告警）；
  - `level` 含 `warn`（`warning`/`warn` 等级）；
  - `event == "voice_degraded"`（语音降级，可选依赖缺失）；
  - 记录自带 `error` 字段。
  - 健壮性：文件不存在 → 空列表；单行 JSON 解析失败 → 跳过（坏行不影响其余）；合法 JSON 但非对象（数组/数字/字符串）→ 跳过（审查发现修复）。
- `server.py` 新增 `GET /api/logs/warnings?limit=`：调 `read_warnings` 返回 `{"ok": True, "logs": [...]}`，服务端过滤，前端拿到的就是纯警告/错误列表。

### 前端（`UI/index.html`）

- 模块状态弹窗 footer 新增「📋 警告/错误日志」按钮；点击后同一弹窗内切换到日志列表视图（无记录时显示"✅ 暂无警告/错误记录"）。
- 日志行分色：`❌` 红色 = error 事件/action 或带 `error` 字段；`⚠️` 橙色 = alarm 或 level 含 warn；每行显示时间 + 事件名 + action + error 详情。
- 「← 返回模块状态」切回模块列表；**视图快照一致性**（审查发现，两轮修复）：打开弹窗时重置快照、每次检测后同步快照，避免切回时回滚到过期数据。

### 测试

- 新增 `tests/test_log_warnings.py`（3 个用例）：过滤规则（正常事件剔除、error/alarm/voice_degraded 保留，按写入顺序）、`limit` 截断 + 文件缺失返回空、坏行跳过（解析失败行 + 合法 JSON 非对象行）。
- 全量回归：`pytest tests -q` → **38 passed**（9 个测试文件，无回归）。
- 手动端到端验证（uvicorn 启动 + curl `/api/logs/warnings` + 注入错误审计 + 浏览器弹窗操作）由控制者执行。

---

## 2026-08-24 · 语音前端实时联动（SSE 事件推送）

**做了什么：**
- 后端 `LLM/voice/worker.py`：新增 `_publish()` 兜底助手，在唤醒命中 / ASR 出文本 / TTS 开始 / 播报结束 / 对话落库后推送 `voice_state`（wake/recognized/speaking/idle）与 `chat_new`（uid/user/assistant）事件，全部 try/except 兜底，不影响语音主循环。
- 前端 `UI/index.html`：对话页顶部新增语音状态指示条；`connectEvents()` 监听 `voice_state`（状态条）与 `chat_new`（同老人且对话页激活 → 原位追加对话轮并防重复；uid 不一致 → 自动切换到对应老人对话页；其他页签 → toast 提示）；抽 `switchTab()` 公共函数。**最终审查修复：** SSE 消息无 `event:` 字段、浏览器按 `message` 类型派发，具名 `addEventListener` 永不触发——改为 `_es.onmessage` 按 `data.type` 分发（顺带修复既有 reminder/alarm 监听的同款隐患）；另补 toast 详情转义、无 uid 守卫、状态指示条 12s 失败兜底自动隐藏。
- 测试 `LLM/tests/test_worker_events.py`：5 个事件广播单元测试。

**有什么用：** 语音对话（唤醒→识别→回复→播报）全程在前端实时可见，护士不再需要刷新页面才能看到老人和机器人的对话；另一位老人说话时页面自动切过去。

### 测试

- 新增 `tests/test_worker_events.py`（5 个用例）：`bus.publish` 后 SSE 扇出 `voice_state` / `chat_new` 事件广播（事件类型、uid 字段、任意线程触发）。
- 全量回归：`pytest LLM/tests -q` → **28 passed**（5 个测试文件，无回归；含声纹批次后续新增的 3 个用例，本批次基线 25）。
- 后端启动 + 事件流集成验证由控制者执行：uvicorn 启动 + health + SSE keep-alive + `bus.publish` 注入 `voice_state`/`chat_new` 线上格式（纯 asyncio 直连事件总线，INTEGRATION OK）；真实链路验证（uvicorn + Node EventSource 直连 `/api/events`，触发真实提醒事件经 `onmessage` 按 `data.type` 收到，确认 `data` 行按 `message` 类型派发、onmessage 分发机制端到端成立）实测通过。

---

## 2026-08-24 · 老人注册流程：注册向导 + 两步式声纹 API + 声纹档案合并

**依据：** `docs/superpowers/plans/2026-08-24-elder-registration-flow.md`（实现提交 `7f8fdcb` `2b30892` `86d142a` `90691e1` `78bfa53` `d180258` `8b947b8` `2465adc`）。

### 前端（`UI/index.html`）：老人注册向导

- 4 步向导：**基本信息 → 声纹录制 → 人脸占位 → 完成**（`8b947b8`）：UID 自动生成（`elder_NNN`，可改）+ 姓名/称呼/床位等表单 → `POST /api/profiles` 建档后进声纹步；人脸步异步拉 `/api/face/status` 置灰占位；完成后「完成，切换到该老人」自动切换对话/记忆页（`2465adc` 补成功页显示姓名 + 录制竞态防护）。
- 声纹录制步：**试听 / 重录 / 保存 / 跳过**——录制 15s → 试听 wav → 不满意重录（丢弃暂存）→ 保存建档（`POST /api/voice/enroll`，首次 append）；语音模块不可用时提示可跳过，稍后在记忆页追加。
- 记忆页新增「身份样本」区块（`8b947b8`）：展示声纹样本数、可**追加声纹**（录制→试听/重录/保存合并入档）与**清除声纹档案**（`DELETE /api/voice/speakers/{uid}`）；人脸占位卡片置灰（未接入）。

### 后端（`LLM/server.py` + `LLM/voice_api.py`）：声纹两步式 API

- `POST /api/voice/record`（`7f8fdcb` 新增录制常量：默认秒数/暂存 TTL）——录制并**暂存不落档**，返回 `recording_id`（`90691e1`）；
- `GET /api/voice/record/{id}/audio` —— 试听暂存录音（wav）；`DELETE /api/voice/record/{id}` —— 丢弃暂存（幂等）；
- `POST /api/voice/enroll` —— 第 2 步**合并入档**（带 `recording_id`，`append` 默认 True = 追加合并；旧行为兼容：无 `recording_id` 时录 N 秒覆盖建档）；
- `DELETE /api/voice/speakers/{uid}` —— 清除该老人声纹档案（不影响基本信息档案）；
- `GET /api/face/status` —— **人脸占位路由**（`78bfa53`），本期未接入，返回 `{"ok": True, "status": "unavailable"}`，前端据此置灰按钮。

### 声纹档案合并平均（`LLM/voice/speaker.py`）

- `merge_profile(old, old_count, new)` 纯函数（`2b30892`）：按样本数加权平均，返回合并 emb + 新 count；
- `enroll_embedding(uid, emb, append)`（`86d142a`）：追加时与旧档案合并平均，`emb + count` 落盘 npz——**追加越录越准**；覆盖模式重置 count=1；
- **兼容旧 npz**：老档案无 `count` 字段按 1 样本处理；支持删除档案文件。

### 降级保障

- `d180258`：`_wav_bytes` 去掉定义期 numpy 注解，**无 numpy 环境模块仍可导入**（守「可选依赖缺失必须降级运行」红线）。

### 测试

- 新增 3 个测试文件共 24 用例：`test_speaker_enroll.py`（7：追加合并/覆盖重置/删除/旧 npz 兼容）、`test_voice_api_enroll.py`（9：暂存/提交/试听/丢弃/清除/降级路径/TTL 清理）、`test_server_voice_routes.py`（8：record/enroll/audio/discard/delete speakers/face status 路由）；`test_speaker_math.py` 增补 merge_profile 用例（上批基线已含）。
- 全量回归：`pytest LLM/tests -q` → **52 passed**（8 个测试文件，无回归；上批基线 28 → 本批 +24）。
- 端到端手工验证（启动后端 + 浏览器走完注册向导 + 声纹试听/追加/清除）由控制者执行，结果见任务 7 报告。

---

## 2026-08-29 · 移除 web_search / get_news 本地工具，联网能力切换为 MCP

**背景：** 原 `web_search`（博查/SerpAPI/DuckDuckGo + 本地 RSS 语料兜底）与 `get_news`（RSS 抓取）两条联网工具链路不再维护，统一改用 MCP 服务器提供联网/新闻能力（`LLM/mcp_client.py` + `conf.MCP_SERVERS` 已有完整支撑：启动拉起、schema 转换、run_tool 分发、降级运行）。

### 做了什么

- **删除** `LLM/tool/web_search.py`、`LLM/tool/get_news.py`、`LLM/tool/_rss.py`（共享辅助，仅被前两者使用）。
- `LLM/conf.py`：删除 `web_search_enabled` 默认设置与 `FEEDS_FILE`；`MCP_SERVERS` 的 `fetch` 服务器 command 改为平台自适应（Windows 用 `npx.cmd`，Linux 用 `npx`），此前硬编码 `npx` 在 Windows 上会静默拉起失败。
- `LLM/tools.py`：模块 docstring 更新——本地工具注册与 MCP 工具两条路径说明，标注旧工具已移除。
- 前端 `frontend/packages/admin/src/pages/SettingsPage.vue`：设置页移除「联网搜索」开关，新增「MCP 外部工具」总开关（`mcp_enabled`，改后重启后端生效）。

### 有什么用

- 联网能力收敛为单一入口：想换搜索/新闻服务 = 在 `conf.MCP_SERVERS` 加一台 MCP 服务器，改配置不改代码；大模型侧工具名由 MCP 服务器 `tools/list` 决定。
- 移除 RSS 兜底后，无 key 环境下不再有 4~5s 的语料匹配延迟路径。

### 注意

- 历史库 `settings` 表可能残留 `web_search_enabled` 行，无害（注册表已无此工具，`effective_tools` 不再读取）。
- `docs/2.pre/大模型端开发目标.md` 模块 9 仍按旧设计描述 `web_search`/`get_news`，待 MCP 方案定型后更新。
- 验证：`python -c "import LLM.tools"` 正常；本地注册表为空、MCP 工具合并路径不受影响；MCP 冒烟可跑 `LLM/tool/_mcp_demo.py`。

---

## 2026-09-05 · 小车端：Nav2 点 Goal 不动 实机复测与修复 + 底盘轴向标定 + Nav2 bringup 配置勘误

**依据：** `/home/sunrise/小车端ROS2导航调试交接.md`。实测环境：板卡 `/home/sunrise/Robot/ros2_car`（RDK X5 / ROS2 Humble）。

### 做了什么
- **核对交接文档两处 Nav2 Bug**：文档称"已修复待上板验证"，但板上 `src/` 实际仍是未修复版（git 停在 `f81c2a9`）。
  已在板上重新应用并 `colcon build --symlink-install` 重编译：
  - `robot_bringup/rviz/navigation.rviz`：`rviz_default_plugins/SetGoal`(/goal_pose) → `nav2_rviz_plugins/GoalTool`("Nav2 Goal")；
  - `robot_navigation/navigate_to_pose.py`：`args or []` → `sys.argv if args is None else args`（+`import sys`），命令行发目标不再丢参。
- **真机 bring up 验证**：雷达 `/scan` 10Hz、底盘 `/odom` ~9Hz、TF 正常。
- **底盘轴向标定**（需要人目测物理方向，别盲猜）：
  - `sign_*` 只改 odom 读数不改命令；实测 odom `sign_vx=+1`（原 -1 错）/`sign_vy=-1`/`sign_wz=-1`；
  - `chassis_driver.py` 下发命令对 `vy/wz` 取反（命令侧镜像，vx 不动）——否则命令与 odom 符号相反会致 Nav2 转向发散；改后 `+wz` 实测变左转。
  - "旋转只执行 ~35%"是**限速/加速度限制**（非打滑），odom 准确，闭环仅慢不发散。
- **建图并存盘** `/home/sunrise/Robot/ros2_car/maps/my_map.{pgm,yaml}`（未知格=灰205，map_saver 需重试，因 /map 为 TRANSIENT_LOCAL 按需发布）。
- **Nav2 bringup 配置修复**（从"完全起不来"修到 定位+controller/planner/behavior 全 active）：
  - AMCL `robot_model_type` → `nav2_amcl::DifferentialMotionModel`；
  - 两处 `general_goal_checker` 补 `plugin`；
  - `map_server.yaml_filename` 硬编码真实地图；**导航别走 bringup**（双层 include 丢 map 参数），改基础节点 + `navigation.launch.py` 直连。
- **文档勘误**：`ros2_car/README.md`、`上板核对清单.md` 修正工作区路径(`~/ros2/car_ws`→`/home/sunrise/Robot/ros2_car`)、rviz 工具名("2D Goal Pose"→"Nav2 Goal")、sign 语义等；新增 `ros2_car/ROS2导航调试经验.md`。

### 有什么用
- "点 Goal 车不动"的两个真实根因已按交接文档在板上应用生效；底盘轴向已与 odom 自洽；地图已可正确保存；Nav2 定位链路打通。

### 注意 / 剩余阻塞
- **bt_navigator 未通**：nav2 无条件创建 navigate_through_poses 服务器并加载其 BT，激活时 1s 内等不到 `backup` 动作即失败（behavior_server 已 active、/back_up 已发布仍失败；去插件无效）。无 bt_navigator → 无 `/navigate_to_pose` action 接口 → GoalTool/Goal 无人接。待对照 nav2_bringup 官方默认行为/动作命名排查。
- 全程细节见 `ros2_car/ROS2导航调试经验.md`。

---

## 2026-09-06 · 记忆系统对标 MaiBot：P0-P3 全部落地

**背景：** 调研 MaiBot 记忆子系统（A_memorix v2.0）与说话风格学习（learners），产出 `docs/maibot参考/2026-09-05-MaiBot记忆风格对标报告.md`，随后按其 P0-P3 路线逐批落地。参考材料：源码 `D:\_project\MaiBot\src`、文档 `D:\_project\maibot_docs\zh`。

### P0 —— 安全/防重复（commit 51f9b04、ce2b45f）

- **external_id 幂等**：`rag_memories` 增 `external_id` 列 + `external_refs` 表（uid+external_id 复合唯一）；`ragstore.add` 带 external_id 时写前查重跳过；consolidate 的 digest 用对话内容 sha256 指纹作 external_id——同段对话重复整理只落一条 episode，根治"定时摘要/并发触发导致记忆重复"。
- **软删除 + 回收站**：memories/core/rag 三表软删列 + `delete_operations` 快照表；`delete_memory/delete_core_memory/delete_rag_memory` 改为软删（返回 op_id），TTL 到期与画像内部覆盖改用 `*_hard` 物理删（不进回收站）；ragstore 增 `delete_by_chroma_id`（软删联动清向量，检索立即失效）与 `reindex_row`（恢复时重建向量）。
- **API**：reject/delete 返回 op_id；新增 `/api/memories/recycle`（list/restore/purge）、`/api/memories/rag/{rid}` DELETE。
- **账本定稿分层**：`core_memories` 增 `authority`（llm=AI归纳 uncertain / nurse=护士定稿），按 source 自动推断（manual/correct:/nurse → nurse，llm/migrate → llm）；recall_v3 注入时 llm 记忆标注"（AI 归纳，仅供参考）"；API `/api/memories/core/{mid}/confirm|unconfirm`。防"把 AI 猜错当老人真相"。

### P1 —— 沉淀异步与纠错联动（commit a42cd49、d879aa6）

- **consolidate 租约**：`_in_flight` 集合，同 uid 同一时刻只跑一个 consolidate（空闲定时器/上下文满/手动 suggest 竞争防重）。
- **回收站周期深清**：reminder tick 顺带 `purge_soft_deleted`（`recycle_purge_days` 默认 30，settings 可调）。
- **反馈纠错 stale 联动**：`correct_from_feedback` 定位旧记忆（core/rag 内容匹配）→ 软删 + 清 Chroma 向量 → 写回正确内容（护士入口定稿 authority=nurse）；API `POST /api/memories/correct`；医疗/身份红线照常拦截。对标 MaiBot "否定=软失效 + 三层撤退 + 可回滚"。

### P2 —— 对话体验（commit 79697d2、737f0de）

- **记忆召回节流缓存**：chat.build_system 的 RAG 召回走短 TTL 缓存（同 uid 15s 复用），避免短时间多轮重复向量检索省 embedding；缓存上限 32 uid。
- **表达习惯注入**：`expressions` 语录表（situation/style/count/checked/authority，同 situation+style merge 累加）；chat 注入【表达习惯参考】块（≤3 条已审语录），带护工身份边界措辞（"酌情自然使用，不逐字模仿"）。
- **风格学习（expression）**：consolidate 时 LLM 顺带提取老人表达习惯（CONSOLIDATE_PROMPT 增 expressions 输出 ≤3 条）；`_apply_expression` 服务端自审（医疗/身份/脏话红线多字词过滤、长度限制）→ 落待审（checked=0）；API `/api/memories/expressions` + approve/reject（对标 MaiBot checked_only 审核双环）。

### P3 —— 保护与导入（commit 03d1869）

- **核心记忆保护**：`core_memories` 增 `pinned`（护士保护：不被自动清理、不被画像整体覆盖——persona 覆盖跳过 pinned）；API pin/unpin。
- **批量导入中心**：`db.import_memories` 按段落/行切分（超长按句切 ≤160 字）入 pending 待护士审核；API `POST /api/memories/import`。导入不绕过审核，医疗红线由护士把关。
- **暂缓项**：多路分数校准（Robot 单路 Chroma+图谱一跳，跨路不可比问题弱）与关系半衰期演化（记忆走 TTL + 核心稳定 + pinned），ROI 低，记 TODO。

### 前端与测试

- `frontend/packages/admin` 记忆页升级：核心记忆 定稿/退AI/保护/删除 + RAG 删除 + 待确认 确认/拒绝 + 批量导入 + 回收站（恢复/深清）；已构建 dist。
- 新增 `LLM/tests/test_memory_v4.py` 9 用例（幂等/软删/恢复/定稿/纠错/表达合并与红线/导入/pinned 画像守卫）；全量回归 **62 passed**。

### 验证

- `pytest LLM/tests -q` → 62 passed；临时库冒烟（init_db + migrate + settings + 新列）；`import LLM.server` 路由 52+ 无冲突；前端 pnpm build 通过。
- 端到端（真实起服 + 浏览器走记忆管理）待控制者执行。

---

## 2026-09-06（下）· 对标 MaiBot 渐进补齐：R1 纠错信号预筛 + R2 画像防退化

**背景：** P0-P3 落地后，用户希望继续渐进补齐（不入整仓 fork MaiBot）。本批做两件高 ROI 项，commit b2e342e（R1）、c0c91b2（R2）。

### R1 —— correct_instant 信号词预筛（commit b2e342e）

- 原实现每轮对话后都让 LLM 判断"这句话是否在纠正旧记忆"（`_post_chat_jobs` → `correct_instant` 无条件调 LLM），很费 token。
- 新增 `CORRECT_SIGNALS`（24 词：不是/不对/错了/记错/说错/其实/应该是/以后别/我姓/我不叫…，对标 MaiBot feedback_signal_tokens）；无信号词直接 `no_signal` 早退，不调 LLM。
- 超长句（>80 字）不预判，交给 consolidate 批量处理。
- 效果：日常闲聊每轮省一次 LLM 调用；纠正语不遗漏（宁多调不放过）。

### R2 —— 画像防退化 + 护士手动维护（commit c0c91b2）

- **AI 画像防退化**：`_upsert_portrait` 中 AI 归纳的新画像与现 persona 字符 n-gram 相似度 ≥ `PORTRAIT_SKIP_SIM`(0.90) → 跳过重写。防 consolidate 每轮 LLM 输出微小抖动反复覆盖画像导致退化。
- **护士手动维护优先**：`source="nurse:manual"` 写入 persona 时 authority=nurse 且 pinned；之后 AI consolidate 一律不覆盖护士维护的画像（对标 MaiBot 画像 override 与"指纹相同只续期"）。
- API：`POST /api/memories/portrait`（护士手动画像）。

### 取舍记录

- **R3 访问强化/降权暂缓**：Robot 记忆架构是"核心全量注入 + 话题 RAG 检索"，无关系衰减场景；访问强化（MaiBot 主要用于关系记忆半衰期）在此架构收益低，硬做增加回归风险——与 P3 半衰期同类判断，记 TODO。
- 剩余可选项：Episode 细粒度多段化、多路分数校准，均已在报告标注 ROI 低，等真有检索质量问题时再做。

### 测试

- `test_memory_v4.py` 新增 3 例（correct_instant 信号门 ×3 断言、画像防退化 + 护士 pinned 守卫）；全量回归 **64 passed**。

### 补充（同日）：admin 记忆页画像维护卡片

- R2 的 `POST /api/memories/portrait` 补前端入口：记忆页新增「老人画像」卡片（textarea + 保存为护士维护版），显示当前版本状态（AI 归纳 / 护士维护 pinned），护士可直接修正画像且 AI consolidate 不再覆盖——R2 闭环收尾。前端已构建 dist，回归 64 passed。

### 补充（同日）：前端位置文档翻新 + 注册向导迁移待办

- 背景：前端早已（2026-08-27）从单文件 `UI/index.html` 重构为 `frontend/`（Vue3 + pnpm monorepo：`packages/admin` 管理端 / `packages/kiosk` 车载端 / `packages/shared` 共享层），旧文件改名到 `UI(old)/`。但 AGENTS.md 等文档仍指向旧位置，本次翻新：
  - `AGENTS.md`：目录树 / 快速上手（开发 `pnpm dev:admin` :5173、`pnpm dev:kiosk` :5174，代理 `/api`→8000；生产 `scripts/build_frontend.ps1` 构建后由后端挂载 `/admin`、`/kiosk`）/ SSE 耦合约定（前端消费侧唯一源 = `frontend/packages/shared/src/events.ts`）/ 前端节 / 文档导航路径（目标文档在 `docs/目标文档及说明/`、开发日志在 `docs/log.md`）／API 端点摘要补全（alarm、session/user、memories 一族、voice/record、face/status、modules/status 等）。
  - `docs/目标文档及说明/大模型端开发目标.md` 模块 8：旧的「复用 `UI/chat.html`」引用更新为 `frontend/packages/admin`。
- **已知缺口（TODO）**：老人注册向导尚未迁入 Vue admin——旧入口在 `UI(old)/index.html`「➕ 注册老人」4 步向导（基本信息→声纹→人脸占位→完成），后端 `/api/profiles` + `/api/voice/enroll` 齐备，规格见 `docs/superpowers/specs/2026-08-24-elder-registration-flow-design.md`。补"注册老人"时按规格从旧实现迁移，别从零重造。

### 补充（同日）：老人注册向导迁入 Vue admin（TODO 闭环）

- 按 2026-08-24 规格把注册向导迁为 admin 新页签「老人注册」（第 2 页签）：新建 `frontend/packages/admin/src/pages/RegisterPage.vue`（4 步：基本信息 → 声纹两步式 record/enroll `append:false` → 人脸占位 `face/status` → 完成），`App.vue` 登记页签与分支。
- 行为对齐旧 `UI(old)/index.html`：uid 自动 `elder_00N`（profiles 最大编号 +1，可手改）；声纹可试听/重录/跳过（语音不可用不卡流程）；人脸置灰展示后端 reason。
- 注册后切换老人（规格第 4 步"自动切换到新老人"的 Vue 实现）：完成时新 uid 写 `localStorage("uid")`；`ChatPage.vue` / `MemoriesPage.vue` 初始 uid 改为读 `localStorage("uid") ?? "elder_001"`。
- 验证：`pnpm --filter admin build` 通过（42 modules，dist 内含注册向导代码与关键文案）。vue-tsc 2.0.0 在 Node 24 下不可用（MODULE_NOT_FOUND，工具链问题与代码无关；admin 无 typecheck script）。

## 2026-09-07 · 语音链路：流式 ASR 双引擎（本地/云端）+ 实时字幕 + 重启切换

- 需求要点（用户原话）："不管是本地 asr 还是云端 asr 都能流式识别；说的什么实时在前端显现；有切换按钮选本地/云端，重启即切换"。云端 = 火山引擎「豆包语音」控制台的 API Key（流式识别大模型2.0 / Seed-ASR）。
- asr.py 重写为统一流式接口（start_session / accept→partial / finish / abort / close），本地 sherpa online recognizer 改增量解码出字；保留 transcribe() 整段转写作兜底。新增 LLM/voice/asr_cloud.py：火山流式识别2.0 SAUC WebSocket（wss://openspeech.bytedance.com/api/v3/sauc/bigmodel，握手 header X-Api-Key + X-Api-Resource-Id: volc.seedasr.sauc.duration，PCM16 16k 每 100ms 一帧，每句一条连接），帧编解码为纯函数便于单测；websocket-client 属可选依赖。
- worker.py 流式编排：VAD 管句边界不变，LISTENING 期间 VAD 判定开口 → 起 ASR 会话（回补 config.ASR_ONSET_TAIL_S=0.5s 前沿防丢句首），边说边喂，文本变化即 publish voice_state=asr_partial（SSE → kiosk 实时字幕）；VAD 整段弹出 → finish() 取最终文本 → 原声纹/LLM/TTS 链路（recognized/chat_new 事件不变）；起会话失败等走整段转写兜底，不崩主循环。
- 引擎切换：conf.DEFAULT_SETTINGS += asr_provider(local|cloud)，worker 启动时读取（重启生效）；.env += VOLC_ASR_API_KEY / VOLC_ASR_RESOURCE_ID；/api/voice/status 增 asr_provider 字段、modules.asr 显示当前引擎；云端缺依赖/未配 key → 启动降级并给出可读原因。
- kiosk 前端：App.vue 状态条下新增实时字幕行（voice_state=asr_partial 驱动，recognized/speaking/idle 清空，视觉状态保持 listening）；SettingsSheet 设置弹层新增「识别引擎」单选（本地/云端），标注"重启服务后生效"。events.ts VoiceStateEvent 注释同步 asr_partial。
- 验证：后端改动 py_compile 通过、asr_cloud 帧编解码离线自测通过；本地引擎与真云端链路需在板卡/带 sherpa+真 key 环境回归（本机无火山 key，云端握手错误会经 status 上报）。

### 补充（同日）：防自声识别修复（播报回声把自己话当用户语句）

- 现象：机器人 TTS 播报时自己的声音被麦克风拾入 → VAD 攒成语音段 → 播报结束回到收听态后把这些"自己的话"当用户语句弹给 ASR 转写（自问自答）；自声还可能误触发打断（过了 0.3s 宽限后）。
- 修复（LLM/voice/worker.py + oice/config.py）：新增 _flush_vad()（sherpa VoiceActivityDetector.reset()，退化兜底 pop 丢弃），在播报**自然结束**与**打断**两个出口清空 VAD 缓冲、丢掉自声段；自然播报结束另设 SPEAK_TAIL_BLANK_S=0.25s 回声尾巴静音窗（该窗口内丢弃麦克风块、不喂 VAD/ASR，且 sink.is_done()+finish_speaking() 保证打断路径不误套此窗）；打断式插话不套静音窗、立即收音。
- 认知：单麦无 AEC 时软件无法区分"机器人自己的声音"与"老人声音"——外置麦克风拉远扬声器/降低喇叭音量能压低自声电平，silero VAD 阈值化后自然不判为语音，是最有效的硬件配合手段。

### 补充（同日）：崩溃加固 + 默认云端 + kiosk 引擎一键切换

- 报错：uvicorn 退出码 3221225477 = 0xC0000005（原生访问违规，非 Python 异常）。审计定位：崩溃前 ~2 分钟内 4 次「长播报（16~53s 整段）→ 2s 左右被 barge_in 打断」；Ignore OOV 洪流来自 sherpa TTS 转写 markdown 故事回复（**、半角引号、ZywOo/SSG/DANK1NG 等词表外内容），属噪音非崩溃主因。候选根因 A（无 faulting module 证据，按最可能修复）：AudioSink 整段一次 write + stop() 与写线程 finally 双关闭的 PortAudio 竞态。
- LLM/voice/audio.py 加固：写入线程分块阻塞写（0.2s/块），流对象仅由写线程创建/唯一一次关闭；stop() 置停止事件 + abort() 唤醒阻塞 write 再 join，消除双关闭竞态。
- LLM/voice/tts.py：新增 sanitize_tts_text() —— 播报前清洗（去 URL/HTML/markdown 装饰/emoji/半角引号等词表外符号），消除 OOV 刷屏与朗读错乱；清洗后为空则不播报（worker._speak 长度 0 早退）。
- 默认引擎改 cloud：conf.DEFAULT_SETTINGS sr_provider=cloud；worker 云端不可用（未配 VOLC_ASR_API_KEY/缺 websocket-client）时自动回退本地并 audit sr_provider_fallback，不再整机降级。注意老库 settings 表存的 local 会压过新默认——本机已 db.set_settings 置 cloud，其它部署升级后需切一次或照做。
- kiosk：主界面底部新增「识别：云端/本地」一键切换按钮（POST /api/settings，提示重启生效），设置弹层原有单选保留；生产 dist 已重建（pnpm --filter kiosk build 成功，08-27 → 09-07 产物）。
- 引擎语义：本地 sherpa streaming-zipformer-zh-14M = 单向（因果）流式；云端火山 bigmodel = 双向流式（火山另有 bigmodel_async / bigmodel_nostream 未采用）。本地 14M 精度有限，默认云端以提升识别质量。
- 验证：后端 py_compile / 导入冒烟 / sanitize 单测通过；崩溃修复是否根治需真机复测（若复现，请补事件查看器 faulting module）。

---

## 2026-09-07（下）· 语音链路：云端 TTS（豆包语音 2.0）+ 句级流式播报

- 需求要点（用户原话）：「本地/云端 TTS 都要流式输出；LLM 流式内容实时上屏 kiosk；有切换按钮选本地/云端，重启即切换」——与同日 ASR 批次对称：语音链路新增 `tts_provider`(cloud|local) 引擎切换（worker 启动时读取、重启生效），播报从「整段合成」升级为「LLM 流式 → 逐字上屏 + 句级合成无缝播放」。
- 依据：`docs/superpowers/specs/2026-09-07-cloud-tts-streaming-design.md`、`docs/superpowers/plans/2026-09-07-cloud-tts-streaming.md`（remote_tts 分支，计划提交 `9e5d125` 起，任务 1-6 分派收尾）。

### 后端（LLM/voice/…）

- `6a19895`（任务 1）：`conf.DEFAULT_SETTINGS += tts_provider="cloud"`（db.py set_settings 白名单 = DEFAULT_SETTINGS ∪ TOOL_DEFAULTS，新键自动纳入）；`LLM/voice/config.py` 云端 TTS 配置延迟读取：`VOLC_TTS_API_KEY`（未配回落 ASR 同一把控制台 key）、`VOLC_TTS_SPEAKER`、`VOLC_TTS_RESOURCE_ID`（默认 `seed-tts-2.0`）、`VOLC_TTS_ENDPOINT`（默认 `https://openspeech.bytedance.com/api/v3/tts/unidirectional`）。
- `ba6f041`（任务 2）：新增 `LLM/voice/tts_buffer.py` SentenceBuffer 分句缓冲器（纯逻辑无 IO，8 单测：标点切分/连续标点不切/右引号归前句/换行断点/长度兜底 `_force_cut`（窗口内远断点优先、保序不丢字符、兜底置 flush_rest）/跨 feed 缓冲）。**计划内部矛盾（测试强制换行切空 vs 规格「未完成尾部留缓冲」）上呈用户裁定 = 维持现状**：换行切句语义 flush_rest 保留、零返工，真机听感再评估。
- `c146e68`（任务 3）：`LLM/voice/audio.py` AudioSink 改队列播放器：`enqueue()` 分块写（16k 单声道）无缝拼接句间无爆音、`end_of_stream()` 队列清空后自然收流、`stop()` 清队（打断语义）；真实 PortAudio abort 唤醒行为列为真机验证项。
- `5113d18`（任务 4）：新增 `LLM/voice/tts_cloud.py` CloudStreamTTS（provider="cloud"、sample_rate=16000）：豆包语音 TTS 2.0 **HTTP 单向流式**（蓝本 GizClaw/doubao-speech-go tts_v2.go）——POST `{endpoint}/api/v3/tts/unidirectional`，body `{user.uid, req_params:{text, speaker, audio_params:{format:"pcm", sample_rate:16000}}}`，header `X-Api-Key`（同 ASR 控制台 key）+ `X-Api-Resource-Id=seed-tts-2.0`；响应 NDJSON 逐行 `{code(0/20000000 成功)/done/data=base64(PCM16 分片)}`，逐分片 yield float32；**requests 实现免 websocket-client**（缺 requests 仅云端不可用：模块可导入、实例化抛错、worker 降级，不污染后端导入链）；按句短请求首音最快（双向 WS 文本增量流 YAGNI 未用；端点/鉴权以 `docs/2.pre/tts2_probe.py` 实测为准）；`build_request_body`/`parse_stream_line` 纯函数便于单测。本地 `tts.py` 同步补 `synthesize_chunks`/`sample_rate`，`e8fb26b` 再补 provider="local" 对齐云端接口——worker 统一按 chunks 接口驱动双引擎。
- `f970d62`（任务 5 主改造）：`LLM/voice/worker.py` 流式问答编排：整段识别文本后 `_start_answer` 起应答线程（单活跃，上一轮滞留则 abort 接管），`_consume_reply` 同步消费 chat_stream——content 事件 → publish `chat_partial{uid,delta}` 逐字上屏 → SentenceBuffer 切句 → 完整句 `sanitize_tts_text` 清洗 → 逐句 `synthesize_chunks` 合成入队（仅 speak 模式；云端句失败 audit 并按句回退本地，不中断播报）→ done 后 flush 尾句；收尾 publish `chat_new{uid,user,assistant}` 终稿覆盖 + post_turn 落库，`finally` 里 `sink.end_of_stream()` 自然收流（任务 3 发现「play()-only 调用方不调 end_of_stream 则永不收流」由此闭环）；首句入队经 started 事件触发一次 speaking/start_speaking；旧整段 `_speak` 删除。打断沿用 barge_in → abort 置位，合成前/入队前双出口检查。
- `5260504`（任务 5 审查修复轮，二轮 review clean）：R1 打断 abort 先行 + enqueue 双闸；R2 Session 状态机六方法加锁（语义零改）；用户批准 R3 speak=False 跳过切句/合成（chat_partial 逐字上屏不受影响）、R4 audit action 按引擎区分（tts_cloud_sentence/tts_local_sentence）、R5 测试清理未用 import。**用户裁定不修**：started.set() 时机（R2 加锁后窗口已消失）。
- `LLM/voice_api.py`：worker 构造接 `_stream_fn`（chat_stream 事件流，VoiceWorker 直用）；`GET /api/voice/status` 增 `tts_provider` 字段，`modules.tts` 显示实际引擎（`sub_status["tts"]=self.tts.provider`，与 asr 对称）；云端不可用（缺 key/缺 requests/合成失败）→ 构造失败自动回退本地 + audit `tts_provider_fallback`，不整机降级。

### 前端（frontend/，cab39f8，任务 6）

- `shared/events.ts`：新增 `ChatPartialEvent{type:"chat_partial", uid, delta}`（类型联合 + parseBusPayload 白名单同步）。
- kiosk `App.vue`：recognized → 新气泡；chat_partial 驱动当前气泡**逐字渐进增长**；chat_new 终稿**覆盖去重**（播完与历史一致）；设置弹层 + 主界面「合成引擎：云端/本地」一键切换按钮（POST /api/settings，标注重启生效）；admin `VoiceStatusPage.vue` 语音状态页增显示「识别引擎/合成引擎/实际 TTS」（含 tts_fallback 回退标记）。
- shared vitest 11/11 通过（含临时 chat_partial 断言，已还原）；vue-tsc（pnpm store 残缺）+ vite build（沙箱 esbuild EPERM 已知环境限制）→ 类型检查与产物构建交用户侧执行。

### 测试

- 新增 `test_tts_buffer.py`（8）、`test_tts_cloud.py`（4，纯函数）；`test_audio_sink.py` 队列语义 3 例（monkeypatch）；`test_worker_events.py` 按流式契约整体重写（7 例，含 `test_consume_reply_streams_partial_and_chat_new`）。
- 全量回归轨迹：批次前基线 **61 passed + 3 failed**（3 红态 = test_worker_events.py 旧签名漂移，与 pristine HEAD 一致、回归中性）→ 任务 5 重写随批清除 → 批次末 `pytest LLM/tests -q` → **80 passed / 0 failed**（12 个测试文件）。

### 待办 / 用户侧

- 真机复测清单：①语音问答边说边播、首句延迟可接受、句间连续无爆音；②kiosk 气泡逐字渐进、播完与历史一致；③播放期/合成期插话打断都生效、无自问自答复发；④kiosk/admin 切本地/云端 → 重启后端 → `/api/voice/status` modules.tts 变化；⑤断网/错 key 时云端回退本地不崩。
- 任务 0 探针（`docs/2.pre/tts2_probe.py` 尚未创建）实测定案端点鉴权——真机若 401/404 则 tts_cloud 按规格改双向 WS（/api/v3/tts/bidirection）蓝本；本机 `.env` 收尾核查已含 `VOLC_TTS_SPEAKER=zh_female_vv_uranus_bigtts`（批次内曾记「用户称已写、实测未见」，现已补齐；云端引擎初始化即依赖该值），板卡部署需随 .env 同步。
- 用户侧跑 vue-tsc（pnpm store 残缺）+ `scripts/build_frontend.ps1`（沙箱 esbuild EPERM 已知限制）。
- root `tests/test_unlock_switch.py` 陈旧红态为**基线既有**（voice 批次遗留，3 failed，与 LLM/tests 回归无关），迁移与否交用户定夺。

## 2026-09-13 · kiosk 输入框回复逐句自动播报

- `/api/chat` 新增向后兼容的 `speak=false` 请求字段；仅 kiosk 输入框发送 `speak=true`，admin 保持静音。
- SSE content 增量经 `voice_api` 降级门面投递到 `VoiceWorker` 后台队列，复用 `SentenceBuffer`、云端失败本地回退、`AudioSink` 和原子化轮次取代，合成不阻塞文字上屏。
- 只有 `done` flush 尾句；异常/断开丢弃未完成尾句；完成会话的 post-chat jobs 在 done 后关闭时仍恰好一次。
- worker 未运行、TTS 关闭、合成或审计失败时只降级播报，文字聊天继续工作。
- RED：`rg -n 'speak: true' frontend/packages/kiosk/src/App.vue` 退出码 1，无匹配。
- GREEN/静态验证：kiosk 同一 `rg` 退出码 0、命中 1 处；admin 同一检查退出码 1、无匹配；`D:\_project\Robot\.venv\Scripts\python.exe -m py_compile LLM\server.py LLM\voice_api.py LLM\voice\worker.py` 退出码 0；`git diff --check` 退出码 0。
- 全量验证原始摘要：`$env:DEEPSEEK_API_KEY='test-key'; D:\_project\Robot\.venv\Scripts\python.exe -m pytest LLM\tests -q --basetemp .superpowers/pytest-tmp-task4` → `97 passed in 16.48s`；`frontend/packages/kiosk` 与 `frontend/packages/admin` 均执行 `node_modules\\.bin\\vite.CMD build` → Vite 构建成功（分别 37、42 modules）。`vue-tsc` 沿用任务前已知损坏安装（`vue-tsc/index.js` 缺失），未修复、未计为本任务回归。
- 合并前审查修复：文本播报入口不再同步等待 `AudioSink.stop()`；后台交接锁串行停止旧播放，首句通过 `AudioSink.play()` 复位停止标志后再入队，避免新轮被旧声卡停止状态吞掉。新增声卡重启与首包不阻塞回归测试；修复后全量 `pytest LLM/tests -q` → `99 passed`。使用临时 `vue-tsc@2.0.29` 仅检查 kiosk `src` → 通过；仓库原始配置包含的 `vite.config.ts` 仍因缺 `@types/node` 报 `node:url`，未纳入本次改动。

---

## 2026-09-14 · 地图编辑器落地（第三个前端 `/mapeditor` + 地图标记唯一真相 + 地图文件 SSH 读写）

- 规格：`docs/superpowers/specs/2026-09-14-map-editor-design.md`（A 篇看图/标地点/划区域/管地图文件，B 篇像素修图）。本轮把规格落到代码，文档回填见文末「已知限制 / 未做」。

### 后端（`LLM/`，新增 5 个模块 + 改动 4 个文件）

- `LLM/mapstore.py`（新）：`MapStore` 协议 / `LocalMapStore`（纯 stdlib）/ `SshMapStore`（主形态，paramiko 优先、退回 `ssh.exe` 子进程）/ `MapCache` 离线缓存（`DATA_DIR/mapcache/`，键 = `sha1(io_mode+root+name+ext)`）/ `get_store()/reset_store()/io_status()/io_test()` / 名字白名单 `check_name()` / `backup()` + `_prune_backups()`（备份到 `maps/.backup/<名>.<YYYYmmdd-HHMMSS>.{pgm,yaml}`，只按自己的命名规则删，保留最近 `MAPS_BACKUP_KEEP`=10 组）。
- `LLM/maptags.py`（新）：`<图名>.tags.json` 唯一真相的读写（`resolve`/原子 `save`/`replace_all`）、单向刷索引缓存 `sync_map()`、`fingerprint_check()`、地点/区域增删改、`learn_here()`、`record_room_polygon()`、`reindex()/reindex_all()`。
- `LLM/mapserver.py`（新）：PGM(P2/P5) 解析、灰度 PNG 编码（纯 stdlib 手写 zlib + struct）、未知率三分、`meters_to_pixel()/pixel_to_meters()`、`validate_point()`、`map_info()`。
- `LLM/roslink.py`（新）：rosbridge(websocket) 连接层、降级、假数据注入。
- `LLM/locator.py`（新）：位姿 `pose_payload()`、`set_pose_for_test()`、当前地图指纹识别 `current_map()`。
- `LLM/db.py`：新增三张**只读索引缓存**表 `map_tags_manifest` / `destinations` / `zones`，主键 `(map_name, uid)`，加一组访问函数（业务代码不得直接写这三张表，唯一写入口是 `maptags.sync_map()`）。
- `LLM/conf.py`：新增 `MAPS_IO`(默认 `ssh`) / `MAPS_DIR` / `MAPS_SSH_*` / `MAPS_MAX_PGM_BYTES` / `MAPS_MAX_YAML_BYTES` / `MAPS_BACKUP_KEEP` / `MAP_RE_NAME_RE` / `ROSBRIDGE_*`，以及 `DEFAULT_SETTINGS` 的 `current_map` / `map_boundary_margin_m` / `map_topic_fingerprint_enabled` / `mapeditor_auto_switch_map`。
- `LLM/server.py`：新增地图编辑器全部路由（源码里有注释锚点 `# 地图编辑器（第三个前端 /mapeditor）`）；`lifespan` 新增一行 `audit.log("map_io_change", ...)`（不新增任何启动步骤）；静态挂载新增 `/mapeditor`（优先 dist，未构建则回退 `public/`）。

### 前端（`frontend/packages/mapeditor/`，新包）

- `public/pixel-editor.html`：上游 `ROS-SLAM-Map-Editor/editor.html` 的副本，**LF**；实测 `git diff --no-index` 对上游 git blob（43165 字节）只有 **7 增 5 删**（＝5 条 CDN URL 换本地 vendor + 1 行 `<script src="./pixel-netio.js">`），文件 43178 字节。
- `public/pixel-netio.js`：接线层（读注入 + 写截获 + 状态条）。
- `public/vendor/`：5 个自托管资源（jQuery 3.4.1 / js-yaml 4.1.0 / Bootstrap 4.4.1 CSS+JS / Font Awesome 4.7.0 含 webfonts）+ `ROS-SLAM-Map-Editor.LICENSE`（上游 MIT 原文）。
- `src/`：Vue3 应用（`App.vue` / `pages/{MapCanvas,PlacePanel,ZonePanel,MapFiles}.vue` / `lib/{coords,colorize,api,types}.ts`）。
- `frontend/package.json` 加 `dev:mapeditor`；`vite.config.ts` 为 base `/mapeditor/`、端口 5175、proxy `/api`→8000（可用 `VITE_API_TARGET` 指向板卡后端）。
- 仓库根 `scripts/vendor_mapeditor_assets.py`（幂等下载/落盘 5 个 vendor 资源）、`scripts/e2e_smoke.mjs`（无头浏览器冒烟脚本）。

### 验证

- 后端测试：`D:\_project\Robot\.venv\Scripts\python.exe -m pytest tests/test_mapeditor.py -q` → **46 passed**（单跑该文件）。
- 全量 `pytest tests -q` → **91 passed / 4 failed**，4 个红态**均在 `test_mapeditor.py` 之外**且属既有基线漂移：`tests/test_modules_status.py::test_modules_status_shape`（断言模块集合）、`tests/test_unlock_switch.py` 3 例（`VoiceWorker.__init__() got an unexpected keyword argument 'chat_fn'`，测试与 `voice/worker.py` 签名漂移）。本轮未修（非本任务范围）。
- 前端构建：`frontend/packages/mapeditor/dist/` 产物存在，但**本开发沙箱（DSH workspace-write）里 `vite build` 跑不起来**：① 浏览器进程无法启动（headless Edge 直接被沙箱杀掉），② Node 子进程不能用管道 stdio（`spawn EPERM`，esbuild 服务进程起不来）。为此 `packages/mapeditor/scripts/build.mjs` 用文件句柄 stdio 起子进程跑真正的 `vite build`（正常机器上与 admin 等价）；撞上 EPERM 时**不静默兜底**，明确失败并提示改用 `pnpm --filter mapeditor build:sandbox`（= `scripts/verify-build.mjs`，同一份 `vite.config.ts` 的等价构建，实测产物与 `vite build` 逐字节相同）。**因此产物尚未在真实开发机上用 `vite build` 复验过，也没做过真实浏览器联调**。Linux/正常 Windows 开发机上应直接 `cd frontend && pnpm install && pnpm --filter mapeditor build`。
- 板卡：`ssh sunrise@100.65.82.93` **实测连接超时**，故板卡真机验收（规格 §九 7~10、§B11.3 16~19）与 `MAPS_IO=ssh` 真机读写**均未做**。

### 已知限制 / 未做

- 真实 `vite build` 复验、真实浏览器联调（`scripts/e2e_smoke.mjs` 在沙箱里跑不起来，正常机器上可用）、板卡真机验收。
- 规格 §十二 / §B十四 的前置仍成立：需重扫一张地图、对齐 AMCL 初始位姿、板卡可达（实测不通）。
- 文档回填：`AGENTS.md`、规格 `2026-09-14-map-editor-design.md`（文首状态 + 文末「实现台账与偏差」）、`ros2_car/建图与导航操作手册.md`（补「改完必须重启导航」+ 浏览器入口 + 备份位置）、`2026-08-27-frontend-multi-end-design.md`（补 vendor 静态资源一句）本轮已做；**板卡上那份操作手册副本（`/home/sunrise/Robot/ros2_car/建图与导航操作手册.md`）因 ssh 不通未同步，需另行同步**。

---

## 2026-09-14（续）—— 摄像头共享服务（vision/）审查与加固

### 背景

用户问"摄像头调用代码是否完善"。审查 `vision/`（2026-08-27 提交，此后未改动）后确认：协议设计与进程隔离思路是对的，但存在 **4 处确定性缺陷**，且该模块**零测试、零消费方**（`LLM/`、`frontend/` 全局 grep `vision|CameraClient|9540` 无命中）。

### 修复（不动架构）

1. **`get_next_frame` 永久挂死**（最严重）：原实现 `settimeout(None)`，服务端在新帧到达前不发任何字节，"对端已死"与"仍在等待"无法区分。实测传大 `last_id` 后**永久阻塞**，且卡死后同一连接上 `info()` 也超时（client 实例报废），`_io_timeout=10.0` 形同虚设。
   - 新增构造参数 `wait_timeout` 与单次 `timeout=` 覆盖；超时抛新异常 `CameraTimeout`。
   - **关键**：`CameraTimeout` **不继承** `ConnectionError`（别被当成断线吞掉）；超时后丢弃连接（服务端那笔迟到应答无法撤回，复用会串话——实测 `info()` 会读到帧头），下次调用自动重连；**游标仅在真正取到帧后推进，故不丢帧**。
2. **JPEG 帧头宽高与实际编码尺寸不符**：payload 是 16 对齐后编码的，帧头却写通道原始尺寸。默认 1920x1080 → 实际编码 **1920x1072**，帧头报 1080，消费方按帧头建 buffer 必错位。改为记录 `_jpeg_size`（编码器真实输出）并写进帧头，同时 `info()` 暴露 `jpeg_size`。
3. **`frames()` 连接泄漏**：`try` 从 `sendall` 才开始，连接建立后若 `setsockopt`/`sendall` 抛错则永不释放。改为 `try/finally` 覆盖建连后的全部步骤。
4. **`--status` 与 `--bind` 语义冲突**：`--status` 拿**监听**地址当**连接**地址，服务以 `--bind 0.0.0.0` 启动时查不到。新增独立 `--host`（`0.0.0.0`/`::` 自动换回环）。

### 新增：HTTP 桥（`vision/webbridge.py`）—— 上位机可达

裸 TCP 浏览器说不了，故桥成 HTTP，**后端跑在 PC 上即可用浏览器看板卡画面**：

- `GET /api/vision/status`：不可用时 `ok:True` + `status:"unavailable"`（遵「系统稳健性」：服务健康 ≠ 功能可用）
- `GET /api/vision/snapshot?channel=&quality=`：单帧 JPEG（不可用 → 503 `ok:False`）
- `GET /api/vision/stream?channel=&fps=`：MJPEG（不可用 → **直接结束流**，不无限空转）

**零新增第三方依赖**：服务端开了 `--enable-jpeg` 走硬件编码；否则退回自写的**纯 stdlib 基线 JPEG 编码器**，响应头 `X-Vision-Source: hardware|software` 标明路径。

> 软件编码器实现时踩了 3 个坑，均为"结构合法但 Pillow 报 broken data stream"，值得记录：
> ① **DQT 必须按 zigzag 顺序存**（内部量化表是自然顺序，须重排）；
> ② **SOF0 分量字节错位**（`struct` 打包把分量描述串了位）；
> ③ **位写入器 `acc` 无界膨胀**（拼接长码字后未屏蔽已写出字节，导致比特流与熵编码器预期不符）。
> 定位手法：先与 Pillow 产出的同尺寸参考图**逐段 diff**（这一步直接暴露 ①②），再对小块（8x8 灰）核对熵编码字节（`2803`）确认 ③ 以外部分正确。最终以 Pillow 真解码 + 像素梯度单调性断言验收。
> 另注：`vision/webbridge.py` 对 `LLM.log` 的导入做成可选（`vision/` 需能独立使用）。

### 测试

新增 `tests/test_vision.py`，**51 项全过**，全程 `--mock`，不依赖板卡/摄像头/opencv（`numpy`/`Pillow` 用 `importorskip`）。覆盖协议往返、错误恢复（ERR 后同连接不串话）、**超时不再挂死且自动重连**、`frames()` 释放连接、**JPEG 帧头尺寸回归**、软件编码器可解码 + 像素正确性、`/api/vision/*` 降级行为、并发多客户端。

全量 `pytest tests -q` → **151 passed / 4 failed**；4 个红态与上一条日志（2026-09-14）记录的**同一批既有基线漂移**完全一致（`test_modules_status` 1 例 + `test_unlock_switch` 3 例 `VoiceWorker` 签名漂移），frozenset 未变，本轮未修（非本任务范围）。

### 未做 / 已知限制

- **板卡真机未验收**：`--enable-jpeg`（JPU 硬件编码）路径与真实 MIPI 摄像头取帧**均未在板卡上实测**，仅在 Windows + `--mock` 下验证。硬件编码分支的帧头尺寸改动是按代码逻辑 + 替身编码器测的，需真机确认。
- 前端未接：`/api/vision/*` 目前只有后端接口，admin/kiosk 里**没有摄像头页面**（用户当前目标是"多程序共享摄像头"，故未强加 UI）。
- 未接真实消费方（目标检测/拍照/LLM 视觉）——仍是"能力就绪、无人使用"状态。
- 沙箱里 `TestClient(app)` 会卡在 lifespan（MCP/voice 启动），故路由层测试改为**直接 await 路由函数**；真机/正常环境下不受此限。

---

## 2026-09-14（续二）—— 摄像头来源可选：板卡 MIPI / Windows USB（`--source auto`）

### 背景与用户原话

> 「我希望摄像头不仅可以用板卡的，还能用Windows电脑的，方便调试。」
> 「我的意思就是说优先用板卡的摄像头，如果不行，说明在windows系统中，用windows的摄像头」

即：不是两路同时接入，而是**自动判断当前在哪、能用哪个就用哪个，优先板卡**。
规格：`docs/superpowers/specs/2026-09-14-vision-webcam-source-design.md`（commit `bbfcb8b`）。

### 实现

**新增 `vision/webcam.py`**（OpenCV 后端，**进程内**）：

- `bgr_to_nv12(frame, w, h)`：纯 numpy 函数，BT.601 **limited range**（与 `Frame.bgr()` 用的
  `COLOR_YUV2BGR_NV12` 口径配套，改一个必须改另一个，否则整体偏色）；色度按 **2x2 平均**
  下采样（不是取左上角，否则块状色噪）；UV 交织顺序为 U 在前 V 在后（与 I420 不同）。
- `WebcamBackend`：实现既有后端契约 `open()/next_frame()/close()`。**cv2 惰性导入**（守住
  「可选依赖不进导入链」红线）。Windows 优先 `CAP_DSHOW`（MSMF 慢且对不存在设备会卡）。
- `list_cameras()`：逐个**试读一帧**确认设备号。设计理由：注册表/设备管理器会把"曾经装过
  的"设备也列出来（用户既有教训），据此判"有没有摄像头"会误判。

**改 `vision/camera_server.py`**：

- `--source auto|mipi|webcam|mock`（默认 `auto`）、`--device N`、`--list-cameras`；
  `--mock` 保留为 `--source mock` 别名。
- `source_candidates()`：板卡（Linux 且有 `hobot_vio`）→ `[mipi, webcam]`，其余 → `[webcam]`。
  **`hobot_vio` 不可用时不在 auto 里列 mipi** —— 它不可能成功，试它只会白起一个子进程
  再等超时（实测很拖沓）；显式 `--source mipi` 时仍会尝试，以便报出真实原因。
- `make_backend()`：逐个候选尝试，**全部失败时把所有原因逐条汇总**（对齐项目"缺多个依赖
  时别只报第一个"口径）。构造与 `open()` 都包在 `try/except Exception` 里（子进程/队列
  权限、缺依赖、设备占用都可能失败）。
- `info()`：`mode` 由 `real|mock` 细化为 **`mipi|webcam|mock`**，并加 `source`/`device`。

**改 `LLM/conf.py` + `vision/webbridge.py`**：

- 新增 `VISION_HOST`/`VISION_PORT`（env 可覆盖，默认 `127.0.0.1:9540`）。`webbridge` 原先
  **硬编码** `P.DEFAULT_HOST/DEFAULT_PORT`，导致「后端在 PC、摄像头在板卡」这一形态根本
  跑不通 —— 这是用户"优先板卡"能落地的前提。
- 默认取**本机**而非板卡（与 `ROSBRIDGE_URL` 默认指向板卡不同）：摄像头服务在 PC（USB）
  和板卡（MIPI）上都可能跑，默认本机才不会让"PC 调试"先要改配置。
- `/api/vision/status` 增加 `target` 字段回显实际连的地址，排查时不用猜配置。

### 关键决策

- **D1 一律归一化成 NV12**：cv2 给 BGR，但下游（client/webbridge/软件 JPEG 编码器）全按
  NV12 处理。归一化让下游**一行都不用改**。
- **D3 WebcamBackend 先做进程内**：板卡那边隔离子进程是因为 `libsrcampy.get_img()` 长时间
  占 GIL 饿死分发线程；`cv2.read()` 在 C 层通常释放 GIL，无此问题。**留了验证步骤**：若
  实测 PING 延迟劣化（>100ms）再挪进子进程（接口一致，改动局部）。
- **设备不支持请求分辨率时按实际尺寸产帧**：设备只给 640x480 而请求 1920x1080 时，产
  640x480 并把真实尺寸写进帧头/`info()`。这是刻意选择 —— 强行按请求尺寸声明会让 NV12
  长度与帧头不符，**正是 2026-09-14 早些时候修掉的同类 bug**。
- **多通道从同一次读帧缩放**：一个物理摄像头不可能同时以两种分辨率出图。

### 测试

`tests/test_vision.py` 扩到 **86 passed / 1 skipped**（skip 的是 cv2 往返用例，本机没装 cv2）。
新增覆盖：NV12 已知色值/UV 交织顺序/2x2 平均（并断言**不等于**只取左上角）/尺寸校验；
**假 cv2** 驱动 `WebcamBackend`（协议形状、frame_id 递增、open·close 幂等、失败即释放句柄、
持久读失败抛错、设备能力收窄、多通道单次读）；`--source` 与 auto 候选顺序（monkeypatch
平台与 hobot_vio）；**汇总错误含全部候选原因**；失败回退到第二候选；`list_cameras` 只报
可读设备；**cv2 缺失时 `vision.*` 仍可导入**（红线）；端到端 webcam→client→webbridge JPEG
可解码。

全量 `pytest tests -q` → **186 passed / 4 failed / 1 skipped**；4 个红态仍是既有基线漂移
（`test_modules_status` 1 例 + `test_unlock_switch` 3 例），**与本次改动无关**（改动前即存在）。

### 已知限制（重要）

- **本机装不了 opencv**：到 `pypi.org`/清华/阿里云 PyPI 的 SSL 全部失败、`pip download`
  超时，全盘也没有已装 cv2 的 Python 环境。故**真实"读 Windows 摄像头"这一跳未经验证**，
  需要用户执行 `pip install opencv-python` 后共同验收。
- **本机是否有可用摄像头未确认**：注册表有 `Camera`/`USB\Class_0e` 多条记录，正属"历史设备
  也会列出来"的情形；`Get-PnpDevice` 在本机报 CIM 不可用。这正是加 `--list-cameras` 的原因。
- **板卡 MIPI 路径未回归**：改动不应影响 mipi，但本机无 MIPI 硬件、ssh 也不通，未实测。
- **GIL 风险未实测**：见 D3，需在装好 cv2 的真机上量 PING 延迟。

---

## 2026-09-14（续三）—— 分层用户体系 P0：管理层 / 集体层 / 老人层（15 任务落地）

### 背景与用户原话

> 「我想创立一个管理员用户，与老人用户分开。前端可以切换。我的想法是分层级，比如管理员层，
> 众人层（这个先放着不做），老人层，方便给每个层级的用户分配不同的权限和提示词。比如老人层
> 可以使用声纹识别，发出简单的指令（比如让小车从病房出去），管理层可以发布所以指令」
> 「挂人身份上。但不要声纹，管理员是特殊账号，以免误识别。但语音链路要保留，以实现管理员也可以语音控制」
> 「先不管mcp，先完成用户系统。还有，管理员的口令可以直接修改，甚至可以关闭。」
> 「**集体层**的设计是想让小车与"大家"对话。比如小车进入某个病房，与大家打招呼，公布消息之类的。
> 然后就可以根据声纹切换到特定老人与他对话。我想把同一个病房的老人打包成一个病房用户放在集体层，
> 集体层的消息上下文也可以被老人用户读取到。」

规格 `docs/superpowers/specs/2026-09-14-layered-user-roles-design.md`（§14 = 二次复审结论）；
实现计划 `docs/superpowers/plans/2026-09-14-layered-user-roles.md`（v2，15 任务 TDD，已执行完）。
**代码区间：`b83c594`（任务 1 `zonegeo.py`）→ `5c3e1db`（任务 14 admin 登录门与两个页签）**，
基线 `6295df3`。该区间内混有**并行会话**的 vision / mapeditor 提交（`bbfcb8b`/`ff82042`/`29fa314`/
`a69aba7`/`3516839`），不属于本批。

### 实现（15 个任务）

**新增模块**

- `LLM/zonegeo.py`（任务 1，约 30 行，纯 stdlib）：`point_in_polygon()`（射线法）+ `zone_hit()`
  （吃缓存行；`shape='rect'` 用外接矩形；点数 <3 一律 `False`；**任何坏入参都不抛异常**）。
  口径与前端 `packages/mapeditor/src/lib/coords.ts` 对齐。
- `LLM/policy.py`（任务 4）：三角色策略包 `POLICY_DEFAULTS` + `role_policy()`（未知角色 fail-closed
  落集体层，返回浅拷贝防全局白名单被污染）。纯数据 + 纯函数，不做 IO；P1 的 `check_action()` 不留空壳。
- `LLM/session.py`（任务 5/6/7）：`derive_role()`（uid→role 唯一权威，按 `profiles.kind`，**不靠前缀**）、
  双槽 `get_principal()/set_subject()`、`login_admin()/logout()`、口令改/关/开/首启生成、
  `current_ward()/manual_set_ward()/running_map_name()/autoswitch_state()`、`tick()`。
- `LLM/prompt/{ward,elder,admin}.md`（任务 8）：角色片段，叠加在共用 base `prompt.md` 之上；
  装载口径取自 `role_policy(role)["prompt_file"]`，缺文件 → 空串 + 审计 `prompt_role_missing`。

**改造**

- `db.py`（任务 2）：`profiles` 纯加列 `kind`/`ward_id`/`ward_map`/`ward_zone`；`upsert_ward`/
  `set_ward_zone`/`set_profile_ward`/`list_wards`/`get_profile_kind`/`list_profiles(kind=)`；
  管理员口令 PBKDF2-SHA256（20 万轮、随机盐）存 `settings` **raw key**；`list_zones(kind=)`、
  `get_zone(uid, map_name="")` 向后兼容扩参。**口令 key 不进 `GET /api/settings`**（堵泄露）。
- `conf.py`（任务 3）：`admin_auth_required`/`admin_session_ttl_s`/`ward_context_window`/
  `ward_autoswitch_enabled`/`ward_switch_debounce`/`ward_zone_default_r`/`manual_override_sec`/
  `ward_map_source`（`current_map` 一期已有，未重复添加）。
- `chat.py`（任务 8）：`build_system(..., principal=)`、`_load_role_prompt()`、`_ward_context()`
  （**R5 单向**：只从病房 uid 往外读，且必须先确认"真的是病房档案"）。
- `tools.py`（任务 9）：`@tool(roles=)`、`effective_tools(settings, principal)`、`run_tool()` 二次校验；
  **MCP 工具纳入角色白名单交集**（服务器未声明 `roles` = 仅 admin）。
- `memory.py`（任务 10）：`note_turn()` 对 `role=ward` 不沉淀。
- `voice_api.py` + `voice/worker.py`（任务 10）：会话持有权移交 `session.py`（同名函数转发）；
  worker 按角色分支——**管理员不降权**（审计 `voice_spk action=ignored_in_admin`），未识别不改主体。
- `server.py`（任务 11）：新增 `/api/session/{user,login,logout,password,admin-auth}`、`/api/wards`
  （+`/{uid}/zone`）、`/api/profiles/{uid}/ward`、`/api/policy/roles`；业务接口按请求头
  `X-Surface: kiosk|admin` 取 principal（**非法值 400**）；`lifespan` 补首启口令、两条 WARN、
  每秒 `session.tick()`；广播 `session_expired`/`admin_auth_changed`/`ward_changed`。

**前端（任务 12-14）**

- `shared`：`src/api/client.ts` 加 `X-Surface`、`src/api/session.ts` 加登录/口令/病房 API、
  `src/events.ts` +3 事件（`ward_changed`/`session_expired`/`admin_auth_changed`）并扩展 `user_changed` 载荷。
- `kiosk`：左侧层级栏（管理层/集体层/老人层）+ 状态条角色徽标与 TTL 倒计时（换人入口在
  `components/VoiceStatusBar.vue`）。
- `admin`：登录门 + Header 身份/退出 + 「身份与权限」「病房管理」两个页签（注册在 `App.vue` 的 `tabs`）
  + 注册向导加病房归属下拉。

### 测试（本机实跑）

- `.venv\Scripts\python.exe -m pytest LLM/tests -q` → **222 passed in 52.05s**。
- `.venv\Scripts\python.exe -m pytest tests -q` → **4 failed, 186 passed, 1 skipped in 64.05s**。
  那 4 个红态是**既有基线漂移、非本批引入**（`tests/test_modules_status.py::test_modules_status_shape`
  1 例 + `tests/test_unlock_switch.py` 3 例 `VoiceWorker.__init__() got an unexpected keyword
  argument 'chat_fn'`），与规格 §12 记的基线一致，**按约定未修**。
- 本批**新增 11 个测试文件、123 条用例**（`--collect-only` 逐文件实测）：`test_zonegeo.py` 6、
  `test_ward_db.py` 16、`test_settings_roles.py` 3、`test_policy_roles.py` 7、`test_session_roles.py` 26、
  `test_ward_autoswitch.py` 15、`test_prompt_layers.py` 11、`test_policy_tools.py` 12、
  `test_ward_memory.py` 4、`test_worker_roles.py` 9、`test_server_roles_routes.py` 14。
  （规格 §13 写"10 个新测试文件"，**实际 11 个**——口令族独立进了 `test_ward_db.py`，
  任务 5/6 共用 `test_session_roles.py`；口径以本条为准。）另有 1 个既有文件
  `LLM/tests/test_chat_text_tts.py` 随 `/api/chat` 新签名改断言（条数不变）。

### 端到端验收（13 条，本机 TestClient，不进 lifespan）

一次性脚本 `.superpowers/sdd/task-15-acceptance.py`（**跑完即弃、未提交**；临时库 + 逐条重建隔离；
位姿用 `locator.set_pose_for_test` 注入，**不动车、不用板卡**）。结果：**13/13 PASS**。

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 三层可切（admin 建病房 → kiosk 槽 role=ward → 切老人 uid → role=elder） | PASS |
| 2 | 口令门：连错 3 次后第 4 次正确口令也被冷却拦住（error 含"10 秒"） | PASS |
| 3 | 口令可改可关（旧口令失效、`login(null)` 直进且 `source=auth_disabled`） | PASS |
| 4 | R1：`POST /api/session/user` 带 `role` → 400、带 `uid="admin"` → 400 | PASS |
| 5 | 集体层 System Prompt 无老人档案（无"糖尿病"/姓名）、含本病房上下文 | PASS |
| 6 | R5 单向：老人读得到集体层上下文；老人私聊不出现于集体层 | PASS |
| 7 | 跨病房隔离（`elder_102_1` 看不到 `ward_101` 消息） | PASS |
| 8 | 双槽隔离（admin 槽登录不影响 kiosk 槽角色） | PASS |
| 9 | R3：`POST /api/alarm` 在集体层/老人层/管理层都 `ok:true` | PASS |
| 10 | 位置自动切病房（`ward_map_source="setting"` + 注入位姿 + 3 次 `tick()` → `ward_102`；走廊位姿不切） | PASS |
| 11 | 位置源不可用即降级（`ward_autoswitch_enabled=False` → `enabled:false, reason:"disabled"`，对话照常） | PASS |
| 12 | 手动覆盖（`manual_set_ward` 后位置判定不抢，`reason:"manual_override"`） | PASS |
| 13 | 降级自检：`import LLM.server` 成功 | PASS |

**本脚本未单列、改由单测覆盖的 4 条**（提示词把"逐条重跑规格 §11 的 16 条"收窄为"本机可自动化部分"）：
§11.8 管理员 TTL 到期降权（`test_session_roles.py::test_ttl_expiry_*`）、§11.10 管理员语音不降权
（`test_worker_roles.py::test_worker_admin_is_not_downgraded`）、§11.11 未识别默认态
（`test_session_roles.py::test_derive_role_by_kind` + 验收 #5）、§11.14 不打断私聊
（`test_ward_autoswitch.py::test_does_not_steal_elder_private_chat`）。**真机（rosbridge 真位姿 +
真地图指纹）未验**，留待 MCP/导航线重启后一并做。

### 实现偏差（详情见规格 §15 与计划文末「实现台账与偏差」）

- `GET /api/session/user` 走 `asyncio.to_thread`（`autoswitch_state()` 会碰位姿/地图，占事件循环）。
- 解锁（`POST /api/session/user {locked:false}`）= 回**当前病房**的集体层（规格 §4.5 语义），前端传的 uid 被忽略。
- 改口令：**只要已设过口令就必须验旧口令**，与口令门开关无关（堵"关门→改口令→开门"永久占住口令的链）。
- `POST /api/session/user` 额外拒绝 `uid="admin"`；`X-Surface` 非法值 400。
- `remote`-MCP 工具纳入角色白名单**交集**（白名单是天花板）；MCP 服务器未声明 `roles` = 仅 admin。
- 集体层白名单 = `["robot_status","robot_stop"]`（**R3 急停必须可用**），不是空列表。

### 已知限制与暂缓项

- **前端构建/单测在本机跑不了（环境限制，非代码问题）**：`frontend` 下 `pnpm --filter shared test`
  → `Error: spawn EPERM`（栈底是 `esbuild@0.21.3/lib/main.js ensureServiceIsRunning`，
  即沙箱不允许子进程管道通信）。故**三个前端包的 `vitest` 与 `vite build` 均未在本机复验**，
  需**用户侧**跑 `cd frontend && pnpm test && pnpm -r build` 后复验；`vite build` 产物未重建。
- **P1 暂缓（D16，用户 2026-09-14：「先不管 mcp，先完成用户系统」）**：car MCP 的 `robot_goto`、
  地点白名单解析、风险分级与二次确认状态机、admin「地点白名单」页签（规格 §6.3/§7）——**本轮未做**，
  待 MCP 线重启后另立计划。
- **真实语音链路未联调**：声纹→角色→提示词分层这条链在本机只有单测覆盖，未接麦克风实测。

---

## 2026-09-15 —— 全量测试跑不完的根因：`LLM/bus.py` 收尾死锁（已修）+ YOLO 人脸检测起步

### 背景与用户原话

> 「后台跑完大概需要多久」→「哪里卡住了 / 图片读取失败，找找原因，需要我做什么」
> 「按你说的做，如果仍旧超时就主动告知我，给出可能的错误原因，我来排查」
> 「使用超时轮询的改动方式」

现象：全量套件跑 4 分多钟仍无输出。实测进程存活 258 s 却只耗 27 s CPU —— **在等，不是在算**。

**纠正一条旧归因**：本文件 2026-09-14（续一）§未做 第 4 条写的是"沙箱里 `TestClient(app)` 会卡在
lifespan（MCP/voice 启动）"。实测**不是**：卡点在 lifespan 的**收尾**，且与 MCP/voice 无关
（`tests/test_mcp_startup.py` 单独跑 1.4 s 通过）。

### 根因（抓栈定位，可复现）

`tests/test_modules_status.py` 单文件 >120 s 不返回。用 `faulthandler.dump_traceback_later` +
自建看门狗（打印**线程名**：faulthandler 自己不给名字）拿到三方互等：

- `asyncio_0`（事件循环**默认线程池**的 worker）停在 `concurrent/futures/thread.py:58 run` → `queue.get()`（**无超时阻塞取**）；
- `Thread-2 (_do_shutdown)` 停在 `base_events.py:580 _default_executor.shutdown(wait=True)` → `t.join()`；
- 主线程停在 `starlette/testclient.py:709 __exit__` → anyio portal `thread.join()`。

即：**关闭事件循环要先 join 默认线程池的 worker，而该 worker 永远卡在 `_q.get()` 上**。
`LLM/bus.py` 的 `await loop.run_in_executor(None, _q.get)` 就是那一行。关键认知：
**`stop()` 置 `_stop` 标志打不断一个正在阻塞的调用** —— 而 `bus.stop()` 早已在 `server.py:1140`
被调用，形同虚设。影响面不止测试：**uvicorn 重启 / Ctrl+C 同样退不掉，只能强杀**。

### 实现（用户选定的"超时轮询"方案）

`LLM/bus.py`：

1. 新增 `POLL_SECS = 0.5`；
2. `_drain()` 改 `run_in_executor(None, _q.get, True, POLL_SECS)`，并**单独** `except queue.Empty: continue`
   （"没消息"不是异常，不能落进原来的退避 `sleep(0.5)` 分支）；
3. `start_drain()` 复位 `_stop`（顺手加固：`stop()` 永久置位，同进程二次 lifespan 会让第二次的
   drain 立刻自杀 → 提醒/报警推送**静默失效**，比报错更难查）。

行为差异**只在收尾**：有消息时仍立即送达（实测 4 ms），无消息时每 0.5 s 回来看一次 `_stop`。

### 测试（本机 Windows 实跑）

| 项 | 修复前 | 修复后 |
|---|---|---|
| 全量 `tests/ + LLM/tests/` | **跑不完**（4.3 min 仍无输出，27 s CPU） | **457 passed, 0 failed, 105.9 s** |
| `tests/test_modules_status.py` | >120 s 不返回 | 3.6 s |
| uvicorn 收 CTRL_BREAK（真发信号，等价 Ctrl+C） | 永不退出 | **0.24 s 干净退出** |
| 真 HTTP：订阅 `/api/events` + `POST /api/alarm` | —— | **4 ms 收到**（实时推送未受影响） |

- **新增 `tests/test_bus.py`（7 条）**：载荷/跨线程 publish/端到端 SSE 扇出/`stop()` 后一个窗口内退出/
  订阅队列满的降级；核心回归 `test_loop_shutdown_not_blocked_even_without_stop`（**故意不调 `stop()`**，
  旧实现在此永久挂住）。
- `tests/test_modules_status.py`：修两处陈旧断言 —— 聚合键要含 `mcp`（后加的维度）、`voice.status`
  词表要含 `degraded`（`worker.py` 的完整词表是 running / degraded / disabled / stopped）；
  并断言 mcp 的 `available` / `missing_deps` / `servers` 形状。
- `tests/test_unlock_switch.py`：**重写**（本文件 L573 记的 3 例"签名漂移"，实际停在**两代之前**的契约上）
  —— ① `chat_fn` → `stream_fn`（返回 chat_stream **事件流**而非字符串）；② 两参
  `_handle_speech(seg, settings)` → 三参 `(seg, text, settings)`；③ **锁定语义已移交会话层**
  （`session._shared["locked"]`），直接赋值 `w.locked_uid` 不参与任何判定（那只是语音可用时才同步的
  兼容镜像）。重写版改为驱动会话层，并把两处读库（`derive_role` / `db.get_profile`）换内存替身，
  只测"这轮算谁说的"；另补第 4 条"锁定 + 未识别"组合。
- `tests/test_vision.py`：`test_bgr_to_nv12_roundtrips_through_cv2_if_available` 的**期望值不成立** ——
  拿随机噪声图要求 4:2:0 往返 `mean|err| < 12`，任何正确实现都过不了（实测 44~46；色度 2x2 平均对
  逐像素噪声必然大量丢失）。拆成"平滑内容严格断言（实测 **1.52**，阈值 <3，才拦得住色序/口径写错）"
  + "噪声只验不变量（Y 平面与 cv2 `BGR2YUV_I420` 同口径、误差有界 <60）"。

复现/验证用临时脚本（`.ptmp_shim/`，未跟踪、可随时删）：`hang_probe2.py`（20 s 抓线程名+全栈）、
`verify_uvicorn_shutdown.py`、`verify_sse_e2e.py`、`run_tests_per_file.py`（逐文件计时，用来把"挂"与"慢"分开）。

### 顺带：本机 pytest 的沙箱坑（环境限制，非代码）

沙箱下用 `mode=0o700` 建的目录会变成**不可列举**（`listdir` → WinError 5），而 pytest 的 tmpdir
插件处处用 0o700 建 basetemp / `tmp_path` → `tests/conftest.py` 的 autouse 夹具在每个用例上直接
PermissionError（87 例全 ERROR，看着像代码坏了）。绕过：`.ptmp_shim/ptmode.py` 仅测试期把 0o700
放宽为 0o777，配 `pytest -p ptmode` 使用。

### 另：YOLO 人脸检测起步（`vision/face.py`）

- 模型：`deepghs/yolo-face` 的 `yolov8n-face` **ONNX**（12.1 MB，经 **hf-mirror** 下载 —— 本机
  huggingface.co 直连不通、raw.githubusercontent 超时；`vision/models/` 已入 `.gitignore`）。
  输出 `[1, 5, 8400]` = 4 框 + 1 类置信度（**无人脸关键点**）。
- 运行期只需 `onnxruntime` + `cv2`（**不引入 torch**；与板卡"onnx → hbm"转换链路同口径）。
  可选依赖缺失 / 模型不在盘上 → `available()` 返回原因、`FaceDetector` 抛 `FaceUnavailable`
  （遵「系统稳健性」红线；`vision/__init__.py` **不**导入它）。
- 实测：Lena 1 张脸（score 0.664，框位目视正确）；**Solvay 1927 合影 29 张**（真值 29 位与会物理学家）；
  无人脸纹理图 **0 误检**；640 输入 30~73 ms、1280 输入 ~100 ms（CPU）。
- `tests/test_face.py` 26 条（含 5 条真模型用例，缺权重时 skip）。
- 硬件侧结论：板卡 RDK X5 **未接摄像头**（VIO 扫全部支持 sensor 的 chip ID 全 `0x00`、官方样例
  `get chn from 1920x1080 failed` 同样失败、i2c-4/5/6/7 无器件），`/dev/video*` 不存在；
  PC 侧真实摄像头可用，但**沙箱禁止设备访问**（提权后 `idx=0` DSHOW/MSMF/ANY 全部可开）。
  跨机链路（PC 后端 → 板卡 `camera_server`，`VISION_HOST=100.65.82.93`）已用 `--mock` 验证通；
  板卡 `vision/` 是 8-27 快照（只有 `--mock`，无 `--source`）。

### 已知限制

- **`bus.py` 的修复未在真机复验**：本机覆盖了单测 + 真 uvicorn + 真 SSE，但车前屏真实浏览器与
  Tailscale 远程长连接（`/api/events` 挂数小时）未测。
- 人脸只做"人脸在哪"：**识别（谁）与活体检测未做**，注册/比对链路（`docs/temp/face-recognition-notes.md`）
  未动；板卡上跑 YOLO 需 BPU 转换（`.hbm`），本机只验了 CPU ONNX 路径。
- 板卡 `vision/` 与仓库当前版本存在代差（缺 `--source` / `--list-cameras` / 新版诊断），插上
  MIPI 摄像头后建议先同步该目录再上板复测。

---

## 2026-09-15（续）—— 人脸接口层：`LLM/face_api.py` 的"连续 N 帧一致"判定（为身份切换打底）

### 背景与用户原话

> 「是摄像头和光线的问题，正常情况下可以检测人脸，暂时无需添加两级检测，现在把"连续 N 帧一致"
> 的判定写进接口层（为身份切换打底）」

即：**不做** crop-and-zoom 两级检测（先把拍摄条件弄好），但要把"什么时候算稳定，可以据此切主体"
这条口径先立到接口层。前置实测依据（同日上一条）：单人 15 帧里 **1 帧漏检**、两人场景远处那位分数
只有 **0.46~0.53**、框会随身体晃动漂移 —— 按单帧切主体必然"有人路过就误切"。

### 分层与实现

沿用 `voice_api.py` 的三层结构：`vision/face.py`（纯检测）→ `LLM/face_api.py`（本模块：装配 +
取帧 + 判定 + 降级）→ `LLM/server.py`（路由）。

**判定口径（`StabilityTracker`，纯逻辑、无 numpy/onnx 依赖）**：

1. **跨帧关联**：每帧的人脸框按 IoU（`FACE_TRACK_IOU`，默认 0.3）贪心关联到已有轨迹；
2. **逐轨迹连续命中** `hits`：该轨迹**连续**被看到的帧数（中间丢一帧即归零，短暂遮挡保留轨迹
   至 `FACE_TRACK_MAX_AGE` 帧）；
3. **整体一致帧数** `frames`：连续多少帧"看到的是同一组轨迹"——**这就是"连续 N 帧一致"**；
4. **`stable`**：`frames >= FACE_STABLE_FRAMES`（默认 5）且有轨迹 `hits` 够且**平均置信度**
   `>= FACE_STABLE_CONF`（默认 0.60，低分轨迹不计入）；
5. **`switchable`**：还要 `count == 1`（两人同时稳定 = 歧义，不切）**且身份也连续够 N 帧**。

**关键设计点：身份不进 `frames` 的指纹。** 一开始我把 `identity` 并进"一致帧数"的指纹，两个用例
立刻红了 —— 那会让"识别第一次给出结果"或"某帧没给出身份"把**检测**稳定也清零。实际是两件事：
`frames` 管"脸连续出现了几帧"（检测层），`identity_hits` 管"同一身份连续几帧"（识别层），
各按轨迹单独计。`reason` 因此区分 `no_face` / `not_enough_frames` / `low_score` /
`multiple_faces` / `no_identity` / `identity_unstable` / `ok`。

**为身份切换预留的接法**：识别模型（ArcFace 等）接上后，只要在每个 face 字典里补
`{"identity": uid}`，轨迹会自动统计 `identity_hits`，`switchable` 自动变可用 —— **判定逻辑一行都不用改**。
在此之前 `identity` 恒为 `None`、`switchable` 恒为 `False`（能判"有人脸稳定"，不能判"是谁"）。

**配置（`conf.py`，新增 FACE_* 块）**：`FACE_DETECT_IMGSZ=640`（注释里写了实测依据：同一批帧
1280 反而只检到 1 人、耗时 2.6 倍，要提升远处小脸应做"裁脸放大"而非整体放大输入）、
`FACE_DETECT_CONF=0.25`、`FACE_CAMERA_CHANNEL=1`、`FACE_STABLE_FRAMES=5`、`FACE_STABLE_CONF=0.60`、
`FACE_TRACK_IOU=0.30`、`FACE_TRACK_MAX_AGE=5`。

**路由**：`GET /api/face/status` 从"占位 unavailable"升级为真状态（`detector` 与 `camera`
**分开报** —— 两者缺一个都表现为"没人脸"，分开才好排查）；新增 `POST /api/face/probe`
（抓一帧 + 检测 + 推进判定；取不到帧 → 503 + `ok:False`，与 `/api/vision/snapshot` 同口径）与
`GET /api/face/state`（纯读，不取帧不推进）。**诊断器不做常驻后台抓帧** —— 由前端按 1~3 fps 轮询
probe 攒判定，避免后端默认占着摄像头。

### 测试

- **新增 `tests/test_face_api.py`：21 条**（不需要摄像头/模型）——判定器纯逻辑（连续 N 帧才稳、
  中断归零、换位置新轨迹、抖动仍同一轨迹、两人歧义、低分不计入、轨迹老化、纯读不推进、reset）
  + identity 预演（接上即可切换、身份变化/丢失重算）+ 路由层替身（status 形状、state 不取帧、
  probe 的 503/成功/连续 5 次转稳、channel 参数、缺依赖不抛穿）。
- `LLM/tests/test_server_voice_routes.py::test_face_status_route`：不能再断言固定 `unavailable`
  （有依赖+模型时它就是 running），改为断言形状 + 词表 + `switchable` 恒 False。
- **活体验证**（真摄像头 + 真后端 + 真 HTTP，`.ptmp_shim/live_api_face_probe.py`）：

```
#     张数  score  frames stable switchable reason             框                     耗时ms
1     1     0.857  1      False  False      not_enough_frames  [572,561,722,720]      849   ← 含模型加载
2~4   1     0.856  2~4    False  False      not_enough_frames  [572,561,722,720]      264~329
5     1     0.856  5      True   False      no_identity        [576,560,724,720]      276   ← 判定翻真
6~8   1     0.73~  6~8    True   False      no_identity        框抖动数 px             251~340
```
  轨迹 `id=1 / hits=8 / misses=0 / avg_score=0.837`（抖动靠 IoU 关联吃住了，没断）；`GET /api/face/state`
  连读两次 `frames` 不变（纯读不推进 ✅）。

### 已知限制

- **未接身份识别**：`identity` 恒 `None`、`switchable` 恒 `False`；识别（ArcFace + 样本库）与活体检测
  仍未做（`docs/temp/face-recognition-notes.md`）。
- **未接常驻 watcher 与 SSE**：现在靠调用方轮询 probe。要做"后端自己盯着"需加 watch 循环；若要往前端
  推 `face_state` 事件，按 `AGENTS.md` 约定必须**同步**改 `frontend/packages/shared/src/events.ts`
  （本轮未动前端契约）。
- 前端未接 UI（kiosk 没有"检测到人脸"提示/开关）。
- 板卡未验：本机验的是 PC webcam + CPU ONNX；板卡要 `--source mipi` + BPU 转换，且**目前板卡没有
  摄像头硬件**（同日上一条已证）。
- `probe` 单次实测 251~340 ms（取帧 + NV12 解码 + YOLO ~190 ms），故前端轮询别超过 ~3 fps，
  否则会排队。

---

## 2026-09-15（续二）—— 接入 ArcFace 人脸识别 + 样本库 + 坑清单文档

### 背景与用户原话

> 「先把之前测试yolo建立的临时文件夹删掉，然后接入arcface模型，顺便在vision中新建一个md文件
> 把你刚刚所说的可能出现的问题写入文件方便我之后查找」

前情：上一条已把"连续 N 帧一致"判定做进接口层，但 `identity` 恒为 None（没有识别模型），
`switchable` 因此恒为 False。本轮把识别接上，并把踩坑点固化成文档。

### 清理（用户点名）

- 删 `.ptmp_artifacts/`（5.4MB，含**真人摄像头帧**）与 `.ptmp_shim/face_dl/`（4.5MB）。
- **但把 3 张正式测试图挪到 `vision/testdata/`**（Lena 单人脸 / graf 无脸负对照 / Solvay 合影 29 人），
  并改 `tests/test_face.py` 的路径 —— 否则那 3 条真模型用例会**静默 skip**（不报错，最难发现）。
  图为二进制不入库（`.gitignore`），来源与重取方式写在 `vision/testdata/README.md`。

### 实现

| 文件 | 内容 |
|---|---|
| `vision/faceid.py`（新） | ArcFace：对齐（框外扩）+ 预处理（**RGB**、`(x-127.5)/127.5`）+ 512 维归一化指纹 + 余弦；CLI `--status/--download/--compare/--embed` |
| `LLM/face_lib.py`（新） | 样本库：`data/faces/<uid>/<时间>.jpg+.npz`、多样本**平均指纹**、1:N 比对（阈值 + 与第二名差距）、坏样本跳过、uid 白名单、数量上限 |
| `LLM/face_api.py` | 串起来：`_annotate_identities()`（检测→裁脸→提指纹→比对→填 identity）；`enroll()`；`delete_person()`；`library()`；`status()` 增加 embedder/library |
| `LLM/server.py` | 新增 `GET /api/face/people`、`POST /api/face/enroll`、`DELETE /api/face/people/{uid}`；`probe` 响应带 `identity_score/identity_margin` |
| `LLM/conf.py` | `FACE_EMBED_VARIANT/DIM`、`FACE_ALIGN_MARGIN`、`FACE_DIR`、`FACE_MATCH_THRESHOLD/MIN_MARGIN`、样本与人数上限 |

**模型选型（实测定的）**：两个变体都下到 `vision/models/`（不入库）——
`w600k_r50`（ArcFace+ResNet50，174MB）**323.6 ms/张**，`w600k_mbf`（ArcFace+MobileFaceNet，13.6MB）
**27.1 ms/张**（差 12 倍）。默认取 **mbf**（整条链还要叠加检测，用 R50 会让一轮到 500 ms 以上）。
来源仍是 **hf-mirror.com**（huggingface.co 本机不通）；输出均为 512 维。

**比对两道闸门**（不是单阈值）：`FACE_MATCH_THRESHOLD`（绝对阈值）+ `FACE_MATCH_MIN_MARGIN`
（与第二名的差距）——"两位老人长得像/同一人多角度"会让前两名咬得很近，差距太小宁可不认
（`reason="ambiguous"`，与声纹"宁问勿猜"一致）。

### 测试（本机实跑）

- **新增 `tests/test_face_lib.py`（20 条）**：存列删、平均指纹（归一化后平均再归一化）、
  两道闸门（含"库里只有一人时 margin 必须是 None 而不是 0"）、坏 npz/维度不符跳过、
  uid 目录穿越拒绝、人数与样本上限。
- **新增 `tests/test_faceid.py`（18 条）**：对齐几何（外扩比例、贴边用边缘复制不补黑边）、
  预处理形状/范围/**RGB 通道顺序**（BGR 直接送不报错、只会识别变差，必须用测试盯）、
  归一化与余弦、降级；**3 条真模型用例**（512 维已归一化 / 同一张脸不同裁剪相似 / 不同人明显更低）。
- `tests/test_face_api.py` 扩到 32 条：新增身份链路端到端（**替身识别器 + 真样本库**）——
  库里已注册 → 连续 N 帧识别一致 → `switchable` 变真；未注册 → 不瞎认；空库 → `empty_library`；
  `enroll` 存样本/取最大脸/只存 112×112 对齐小图；`no_face` 是 200 业务结果，依赖缺失才是 503。

### 实测数据（阈值标定的起点）

| 情形 | 余弦相似度 |
|---|---|
| 同一个人（同一张脸不同裁剪，mbf） | **0.514 ~ 0.777** |
| 同一个人（**真摄像头**连续帧 vs 已注册样本） | **0.93**（好帧）/ 0.66（一般）/ 0.20（扭头那帧） |
| 不同的人（Solvay 合影取 4 张两两） | **0.137 ~ 0.386** |
| 人脸 vs 无脸纹理图 | 0.093 |

→ 同人最低 0.514、异人最高 0.386，**留白 0.128**，中点 0.450 即默认阈值来源
（`FACE_MATCH_THRESHOLD=0.45`）。**换模型/换摄像头/换对齐方式都要重标**。

**活体验证（真摄像头 + 真后端 + 真 HTTP，`.ptmp_shim/live_arcface.py`）**：

```
注册本人 elder_test：HTTP 200 ok=True added=4/4 photos=4  用时 3.6s
连续 probe：identity 前 4 帧 = elder_test（相似度 0.92~0.93）
            第 5~6 帧扭头 → identity 丢失（0.204 / 0.446）→ identity_frames 重新起算
            第 7 帧恢复（0.663）
最终：stable=True（检测层稳） switchable=False reason=identity_unstable  ← 正是设计意图
性能：识别 86 ms / 检测 343 ms / 整轮 473 ms（三者同机抢 CPU，比单独跑慢）
```

这轮正好演示两层计数的价值：**"有张脸稳定出现"与"确定这是谁"是两件事** —— 前者成立
（stable=True），后者因身份抖动没够 N 帧，系统**不切换**（switchable=False）。

### 文档

- **新增 `vision/人脸识别注意事项.md`（12 节）**：无关键点导致对齐打折（含根治方案）、
  红外夜视掉识别率、阈值必须自标定（含上表）、检测输入别盲目调大（640 vs 1280 实测）、
  单帧不可信、性能预算、活体检测缺失、隐私红线、注册质量、多人歧义、环境与权限、自查清单。
- `vision/README.md` 增加"人脸检测/识别"一节并链到该文档。

### 已知限制

- **对齐仍是无关键点的妥协**：侧脸/歪头掉分（实测同一个人扭头帧只有 0.20）。根治要换
  SCRFD/RetinaFace 这类带 5 点的检测器。
- **未做活体**：照片可骗过；正式上线前必须补（动作指令 / 红外深度 / 静默活体）。
- **阈值只在本机标了起点**：仅 1 位真人 + 公开测试图，**未用真人群体标定**，也**未标红外条件**。
- `elder_test` 这个开发用身份仍留在 `LLM/data/faces/`（本人 4 张样本）；删：
  `DELETE /api/face/people/elder_test`。
- 板卡侧完全未验：BPU 转换（onnx→hbm）没做，量化后阈值需重标。
- 全量套件出现**间歇性失败**（时序敏感用例，累加已见 2 例：`test_chat_text_tts.py::…body_closes_after_done`、
  `test_vision.py::test_end_to_end_webcam_source_serves_decodable_jpeg`）；两条都单独跑与整文件跑均通过
  —— 疑似全量并发下的抖动，与本次改动无关（隔离验证结论见下）。

---

## 2026-09-15（续三）—— 图片入库 / 图片识别（"拍一次照就记住"）+ 清空真人样本

### 背景与用户原话

> 「1.先把我的个人数据删掉，后面需要测试的时候再拍 3.活体测试以后在加 4.进行更改
> 对于第二点，要求能够记忆已经拍过照的人脸，在第二次检测时可以认出这个人，
> 我给你发的文件是我之前做过的项目，可以识别人脸并记录，你可以进行参考」

附参考文件：旧项目 `face_rec.py`（`ultralytics` YOLOv11n-face 检测 + OpenCV **LBPH/EigenFace/FisherFace**
识别，数据集 `<人名>/*.jpg` → **训练** → `.yml` + labels JSON，数据变更自动重训）。

### 用户数据清理

- 删除 `LLM/data/faces/elder_test/`（本人 4 张照片 + 4 个 npz 指纹），库已空。
- 顺带补了**隐私红线**：`.gitignore` 之前**没排除生物特征数据** —— `git status` 里能直接看到
  `LLM/data/faces/`，声纹的 `LLM/data/speakers/` 同样漏了。已加规则并用 `git check-ignore` 复核。

### 与旧方案的取舍（结论：保留 ArcFace 路线，吸收旧项目的工程点）

| 维度 | 旧方案 LBPH/Eigen/Fisher | 现方案 ArcFace |
|---|---|---|
| 加一个人 | 数据集加图 → **重训** | **写一张 npz，无需训练、立刻生效** |
| 依赖 | torch/ultralytics + opencv-contrib | onnxruntime + opencv（无 torch） |
| 相似度 | 距离（越小越像，LBPH ~90） | 余弦（越大越像，0.45） |
| 板卡 | 无 BPU 路径 | ONNX → `.hbm` |
| 吸收的点 | 数据集布局、画面显示名字、样本数提示 | `enroll-dir`、`draw()` 画身份、`under_sampled` |

"无需重训"正是"拍一次就记住"的关键：指纹＝磁盘上的 npz，比对**每次都重新扫目录** →
新人立刻生效、重启后仍在、不需要旧方案那套"数据变了自动重训"的复杂度。

### 实现（本轮新增）

| 位置 | 内容 |
|---|---|
| `LLM/face_api.py` | `enroll_photo()`（单图入库，ndarray 或 **base64** 都能吃）、`enroll_dir()`（批量导入 `<目录>/<人名>/*.jpg`，兼容旧数据集）、`identify_photo()`（静态图识别，**不**推进稳定判定）、`_decode_image_b64()`（容忍 `data:` URL 前缀）、**CLI** `python -m LLM.face_api status\|people\|delete\|enroll-photo\|enroll-dir\|identify` |
| `LLM/server.py` | 新增 `POST /api/face/enroll_photo`、`POST /api/face/identify`（错误码口径与 `/enroll` 一致：503 依赖 / 400 参数 / 200+`no_face` 业务结果） |
| `vision/face.py` | `draw()` 画框时把 `identity`（姓名/uid）一起画出来 |
| `LLM/face_lib.py` | `stats()` 增加 `min_samples` / `under_sampled`（样本太少的人点名提示） |
| `LLM/conf.py` | `FACE_MIN_SAMPLES_PER_UID`；**`FACE_DIR` 支持环境变量重定位**（换加密盘/测试用） |

**修掉一个显示 bug**：`identify` 的 `count` 曾同时被当作"检出数"和"返回数"用（`topk=1` 时
合影 29 张脸却报"检出 1 张"）。现在区分 `detected`（实际检出）与 `count`（返回几张）。

### 测试（本机实跑）

`tests/test_face_api.py` 扩到 46 条、`test_face_lib.py` 20 条、`test_faceid.py` 18 条、
`test_face.py` 26 条 —— **四个文件 110 passed**。本轮新增覆盖：
图片入库→同图识别（替身）、base64 非法/非图片/无脸/`data:` 前缀、`topk` 只截断返回但如实报检出数、
数据集目录批量导入（含单人覆盖）、`under_sampled` 提示；以及 **3 条真模型端到端**：照片入库→识别同一张
认出、**跨进程 CLI 认出（等价重启）**、未入库的合影全部"未知"。

### 实测（离线、不用摄像头，`FACE_DIR` 指到临时目录）

```
① python -m LLM.face_api enroll-photo lena_fixture portrait_lena.jpg
   → ok=True added=1 samples=1
② 另一个进程 people   → per_uid {"lena_fixture": 1}，under_sampled ["lena_fixture"]
③ 再一个进程 identify portrait_lena.jpg → 检出 1 张脸，返回 1 张 → lena_fixture(1.000)
④ identify group_solvay.jpg（未入库的 29 人合影）→ 未识别（0.113 < 0.45），不瞎认
⑤ delete lena_fixture → removed=2（jpg + npz）
```

### 已知限制（本轮未变）

- 活体检测按用户要求**以后再加**（当前照片可骗过）。
- 阈值仍只是本机起点；真机群体标定与红外条件未标。
- 无关键点对齐、板卡 BPU 未验（详见 `vision/人脸识别注意事项.md`）。

---

## 2026-09-15（续四）—— 真机闭环验收通过：注册 → 重启后端 → 认出并「可切换」

用户要求「进行真机检验」，选定范围为 **PC + 真摄像头闭环**（板卡无摄像头硬件，认脸动作无法在板上验）。
脚本 `.ptmp_shim/live_closed_loop.py`（临时工具，可重跑）：起真摄像头服务 + 真后端 → 倒计时 → 拍照注册 →
**重启后端** → 连打 10 次 `/api/face/probe`。

**实测结果（uid 自动取到真实档案 `elder_001`）**：

```
① 注册：HTTP 200 ok=True added=5/5 samples_total=5 photos=5  用时 4.4s
        库：uids=1 samples=5 per_uid {"elder_001":5} under_sampled []
② 重启后端：库仍 uids=1 samples=5            ← 记忆落盘（不是内存），服务重启不丢
③ 连续识别 10 次：10/10 全部认出 elder_001
   identity_score 0.652~0.793（阈值 0.45，余量充足）
   frames 1→10 递增；**第 5 次起 switchable=True 且保持**，reason="ok"
   最终：stable=True switchable=True identity=elder_001 identity_frames=10/5
   性能：检测 564 ms / 识别 125 ms / 整轮 ~725 ms（三个进程同机抢 CPU，比单独跑慢）
```

**结论**：用户要求的两件事都成立且可复现 —— ① 拍过照就被记住；② 之后再检测能认出这个人，
而且身份足够稳定到可以据此切换主体（`switchable`）。重启后端不影响（记忆是文件）。

**验收留下的现场数据**：`elder_001` 名下 5 张样本（本人真人样本）**保留**在 `LLM/data/faces/elder_001/`；
删除：`DELETE /api/face/people/elder_001` 或 `python -m LLM.face_api delete elder_001`。
真人样本与声纹样本均已被 `.gitignore` 排除，不会误入库。

**本次暴露的性能现实**：单轮 ~725 ms（≈1.4 帧/秒），攒够 5 帧稳定约需 3.5 s。原因是检测、识别、
摄像头服务三者在同一台 PC 上抢 CPU（检测单独跑曾实测 190 ms，这里 564 ms）。生产上摄像头服务
应跑在板卡/独立进程，前端轮询控制在 1~1.5 fps；板卡走 BPU 后这一项可大幅改善。

---

## 2026-09-18（续五）—— 真机踩坑与修正：**切换的门槛该看"认得多像"，而不是"框得多准"**

### 背景与用户原话

> 「将我的uid记为000，先将我注册」→（注册后追问）」→「命名为admin」
> （另：此前选定的方案是"检测门槛降到 0.45 + 新增身份分门槛 0.55"）

### 第一步：注册 uid=000 成功，但"不给切换"

```
建档 uid=000（kind 默认 elder）→ 拍 5 帧（added=5/5）→ 重启后端（库仍 5 样本，记忆落盘）
连续识别 6 次：6/6 帧认出 000，身份相似度 0.947~0.970
但 switchable 一直 False，第 5 帧起 reason 变成 **low_score**
```

原因不在识别，在**检测框置信度**：那批帧 YOLO 检测分只有 **0.437~0.534**（弱光/稍远），
而 `FACE_STABLE_CONF` 当时是 **0.60** —— 于是"检测层不算稳定出现"，一路否决切换。

### 第二步：按用户选定方案改成**两道独立门槛**

`LLM/conf.py`：

- `FACE_STABLE_CONF` **0.60 → 0.45**（只管"框得多准"，负责"算不算稳定出现"）；
- 新增 `FACE_IDENTITY_CONF = 0.55`（只管"认得多像"，负责"能不能据此切换"）。

`LLM/face_api.py`：`_Track` 增加身份相似度的**连续段累计**（`ident_sum/ident_n` + `avg_identity_score`，
与 `identity_hits` 同步重置）；`StabilityTracker` 增加 `identity_conf`；`_verdict()` 新增闸门与
`identity_low_score` 原因；`status().thresholds` 暴露两道门槛。相似度为 `None`（单测替身）时**跳过**
该闸门以保持兼容。测试：新增 5 条（低分拦住切换/达标放行/检测分 0.47 与 0.40 的边界/
身份分取连续段平均/无分数跳过闸门），相关五个测试文件 **122 passed**。

### 第三步（当天最重要的发现）：同一个人**跨条件掉到 0.50**

改了门槛后再测（这次用户坐得更亮更近）：

| 场景 | 检测分 | 身份相似度 | 结果 |
|---|---|---|---|
| 注册时（偏暗、稍远） | 0.44~0.53 | **0.95~0.97** | 识别帧与样本**同条件** |
| 换光照/距离再识别 | **0.70~0.77** | **0.50~0.51** | `identity_low_score`，仍不给切换 |
| **补拍 3 张当前条件的样本后** | 0.76~0.81 | **0.87** | **第 5 次探测起 `switchable=True`，reason=ok** |

即：**跨条件的相似度损失远大于"图像清不清晰"** —— 注册样本只有单一条件时，换个光照就腰斩。
这正是 `vision/人脸识别注意事项.md` §9"每人 2~3 张不同光照/角度"的实测证据，也说明**新增的身份分
闸门是有价值的**：它把"认出来了但不够确定"显式报成 `identity_low_score`（去补拍），而不是默默按
一个弱匹配去切主体。

### 收尾状态

- 档案：`uid=000`，姓名 **`admin`**（用户指定；**纯显示名** —— 角色仍由 `profiles.kind='elder'`
  决定，已核实 `derive_role` 只认 `uid=="admin"` 特例与 `kind`，**不会因此获得管理员权限**）；
  另有演示档案 `elder_001`（张建国）/`elder_002`（aaa）。
- 人脸库：`000` **8 张样本**（5 张原条件 + 3 张补拍），`under_sampled` 为空。
- `docs`：`vision/人脸识别注意事项.md` 新增 **§15 实机实测记录**（两道门槛表、跨条件 0.95→0.50→0.87
  的三段数据、完整闭环、按 `reason` 排查的口诀）。
- 新增工具：`scripts/update_elder.py`（改档案字段的**安全读改写** —— `POST /api/profiles` 是全量
  upsert，只传 uid+name 会清空病史/用药/备注，这个脚本只改点名要改的字段并写审计）。

### 已知限制

- `elder_002`（"aaa"）是演示档案，未做人脸样本。
- 阈值仍是"单机单摄像头、单人"标定的起点：**未用真人群体**标定，也**未标红外/夜间**条件。
- 活体检测仍未做（用户要求以后再加）；无关键点对齐、板卡 BPU 未验（同前）。

### 补记（同日）：稀疏轮询下轨迹关联会断（已修）+ 改名 + 复测通过

用户随后要求「打开摄像头检测一下是否能正常将我认出」，第一轮实测暴露第二个真机坑：

```
认出 8/8 帧（相似度 0.653~0.782，检测分 0.80~0.85），第 5 帧 switchable=True
但第 6/7/8 帧 frames 变成 1/1/2 —— 刚亮就灭；且那时 reason 显示 "ok" 而 stable=False（误导）
```

**根因**：后端是"轮询一帧算一帧"（~0.8 s/轮），人在 0.8 秒里自然挪动，而跨帧关联**只用 IoU ≥ 0.30**；
IoU 对位移极敏感 → 同一人被当成新目标 → 轨迹集合变 → "连续 N 帧"重新数。

**修法**（`LLM/face_api.py` + `conf.py`）：

1. `_Track.assoc()` 两级判据：IoU 命中优先；否则**中心位移 ≤ `FACE_TRACK_MAX_JUMP`(1.0) × 框短边
   且尺寸比 ∈ [0.5, 2]** 仍算同一个人（新增配置 `FACE_TRACK_MAX_JUMP`）；
2. `_verdict()` 新增 `reason="frames_reset"` —— 轨迹集合刚变过时不再谎报 `ok`。

**复测（同一台摄像头）**：`frames` 1→8 连续不断，第 5 帧起 `switchable=True` 并**持续为真**，
`reason=ok`，8/8 帧认出 `000`（相似度 0.631~0.757）。新增 4 条单测（兜底续上轨迹 / 远处新目标仍分开 /
尺寸差 3 倍不算同一人 / `frames_reset`），相关五个测试文件 **126 passed**。

**教训**：**"连续 N 帧一致"的关联判据必须与采样间隔匹配** —— 视频流（33 ms/帧）用 IoU 没问题，
轮询式（0.5~1 s/帧）必须用中心位移/尺寸这类更宽容的判据。

**同日改名**：`uid=000` 的姓名 `admin` → **`fuze`**（用户要求）。用 `scripts/update_elder.py` 改，
只改 `name` 字段、其余原样回写；已核实 uid/kind/人脸样本（8 张）均未动。
`vision/人脸识别注意事项.md` 补写 §15.5（含修复前后 `frames` 序列对比）。

### 补记二：全量套件"偶发红"的真凶找到了 —— SSE 收尾依赖 GC（已修）

本会话多次出现"全量跑偶发 1 例红、单独跑必绿"，红的一直是
`LLM/tests/test_chat_text_tts.py::test_chat_submits_completed_turn_when_body_closes_after_done`。
早先怀疑是并发抖动，这次把它**量了出来**（`.ptmp_shim/flaky_chat_probe.py`，同一路径 20 次）：

```
只用 aclose()（现状）：20 次里 8 次「收尾没被调用」   ← 40% 失败率
aclose 后 gc.collect()：20 次里 0 次
```

**根因**：`/api/chat` 的 SSE 是一个**同步生成器**，其 `finally` 里做两件要紧事 ——
`voice_api.end_text_reply(...)`（**把尾句送去播报**）与 `_bg.submit(_post_chat_jobs, ...)`（后台沉淀记忆）。
而 starlette 1.6 的 `iterate_in_threadpool` 在 async 迭代器被 `aclose()`/取消时**不会同步关闭**
底层同步生成器，`finally` 只能等 CPython 回收 → **"客户端收到 done 就断开"时尾句与沉淀有时丢**。
这不是测试挑剔，是**真实缺陷**：老人听到的最后半句会丢，且那轮对话可能不沉淀。

**修法**（`LLM/server.py`）：新增 `_closing_stream(sync_iter)` —— 仍用线程池消费（不阻塞事件循环），
但在 `finally` 里**显式 `sync_iter.close()`**，把"收尾时机"从 GC 手里拿回来。
修复后同一探针 **0/20**；`test_chat_text_tts.py` 连跑 3 次 4 passed。
**教训**：凡"依赖生成器 `finally` 做关键收尾"的地方，都要确认框架在关闭时真的 close 了它 ——
流式接口尤其如此（客户端随时可能断开）。

**顺带更新**：此前本文件与 `vision/人脸识别注意事项.md` 里把这类失败记为"疑似并发抖动、与本轮改动无关"，
现更正为：**是 SSE 收尾的确定性缺陷，已修**。

---

## 2026-09-19 —— 图形界面 `vision_test_start`：人脸录入 / 人脸检测（姓名画在头上）/ 删除数据

### 背景与用户原话

> 「帮我写一个前端界面，命名为 `vision_test_start.py`，放在 vision 文件夹，可以编写其它程序但要
> 通过 `vision_test_start.py` 启动……1. 人脸录入（二级小窗开摄像头 + 「开始录入」；**没检测到人脸
> 就退出、不动数据库**；有脸则弹三级小窗填姓名/称呼/床位/年龄，**uid 默认前一个 uid 加一**，录入完
> 显示「已录入」，3 秒后回一级小窗）2. 人脸检测（二级小窗开摄像头，用 `face_check.py --live` 检测，
> 用 yolo 找到人脸后与后台库比对，**把姓名显示在对应个人头上**）3. 删除数据（二级小窗输入姓名，
> **按姓名反查 uid**，用 `update_elder.py uid --delete --yes` 删除后回一级小窗）。按 ctrl＋c 退出小窗。」

### 交付物

| 文件 | 内容 |
|---|---|
| `vision/vision_test_start.py`（新） | **唯一入口**：参数解析 → 定摄像头端口并写环境变量 → 起 Tk → mainloop；Ctrl+C/关窗退出 |
| `vision/vtest/service.py`（新） | 摄像头共享服务子进程的起停 + 端口等待 + 日志尾巴（失败时把服务自己的报错带出来） |
| `vision/vtest/pipeline.py`（新） | 后台取帧线程：抓帧 → 检测（+可选识别）→ 发布"最新一帧 + 人脸"；`identify`/`enabled` 两个开关 |
| `vision/vtest/people.py`（新） | 档案侧：`next_uid`（前一个 uid + 1）、`check_uid`、`find_people`（按姓名反查）、`create_cmd`/`delete_cmd`、`run_update_elder`（子进程） |
| `vision/vtest/ui.py`（新） | 三个 Tkinter 窗口（一级/二级/三级）+ Canvas 画框画字 + 非交互模式（`--auto`） |
| `LLM/face_api.py` | 抽出 `analyze(bgr, identify=True, track=True)` —— 吃**现成帧**做检测+识别；`probe()` 改为 `analyze(_grab_frame())`（返回值形状不变） |
| `tests/test_vtest.py`（新，52 条） | uid 生成/校验、姓名反查、命令拼装、取帧线程的开关与容错、服务小工具、入口参数 |

### 三件需求的落法

1. **人脸录入**：二级小窗实时画面，只有"当前帧有脸"时「开始录入」才可点（没脸时按钮灰着）；
   点击时**再查一次**，仍没脸 → 弹提示 + 回一级小窗，**档案与样本库一个字节都不写**（实测断言过）。
   有脸 → 三级小窗（姓名/称呼/床位/年龄 + uid 预填"前一个 uid + 1"）→ 先采人脸样本、再调
   `update_elder.py <uid> --name … --nickname … --bed … --age …` 写档案 → 显示「已录入」→ **3 秒后自动回一级小窗**。
2. **人脸检测**：二级小窗把 YOLO 检出的人脸画框、把库里比对出的**姓名**画在框上方
   （不在库里画"未知" + 相似度），底部给稳定/可切换判定与等价命令。
3. **删除数据**：二级小窗输入姓名 → 反查（uid 全等 > 姓名/称呼全等 > 包含）→ 候选列表 → 确认弹窗
   （写明不可撤销 + 会自动备份）→ `update_elder.py <uid> --delete --yes` → 输出回显 → 成功 3 秒后回一级小窗。

### 关键设计取舍（都有理由，不是随手写的）

- **"人脸检测"没有去 subprocess 起 `face_check.py --live`**（用户原话是"使用指令"）：`face_check.py --live`
  会**自己再起一个 `camera_server`**，而摄像头同一时刻只能被一个进程独占 —— 两个服务必然抢设备，
  后起的那个直接打不开。改为**同进程复用 `LLM.face_api.analyze/probe` + 同一个 `camera_server`**，
  判定门槛与轨迹逻辑和 `--live` 是同一份代码；附带好处是框与画面**同帧**（不会"框追不上脸"）。
  窗口里直接标出等价命令，界面上不藏这个差异。
- **`analyze()` 是为 GUI 抽的**：`probe()` 自己抓帧，GUI 的预览循环已经抓过一帧，再让 probe 抓一次
  等于白做一次取帧、且画出来的框图与屏幕差一帧。别的地方别用 `analyze` 绕过 `probe` 的降级包装。
- **中文姓名画在 Tk Canvas 上**：cv2 的 Hershey 字体**画不了中文**（"张桂芳"会变 `???`），
  所以预览是 Canvas：底图 `PhotoImage` + 框/文字是 Canvas 图形项，坐标按画面→控件缩放换算。
- **摄像头按需起停**：进二级小窗才起服务，返回一级小窗立刻停（用户是隐私敏感的，不常开摄像头）；
  删除数据窗口用不到摄像头，所以从不起。
- **抓帧/推理在工作线程，主线程只画**（`after(33)`，约 30fps 上限）：Tk 只能主线程碰控件，
  放主线程会在每次推理（~250ms）卡住窗口。两张脸挂在 Canvas 图元上，脏数据（框不是数字）直接跳过。
- **录入失败的回滚是有条件的**：先采样本、后写档案；档案写失败时**只有该 uid 原本没有任何样本**才回滚
  刚采的样本（原来就有样本则明说"未回滚"，避免误删旧样本）。
- **`FrameWorker` 的停止标志不能叫 `self._stop`**：`threading.Thread` 内部有私有方法 `_stop()`，
  `join()` 会调它 —— 同名属性把它盖掉后 `join` 直接 `TypeError: 'Event' object is not callable`（实测踩到）。
- **`_run_update_elder` 用临时文件收输出而不是管道**：管道在某些受限环境会被拒绝，且输出多时写满即死锁。

### 验证（本机实跑，全部可重跑）

- 全量 `pytest tests LLM/tests -q` → **602 passed**（含本轮新增 `tests/test_vtest.py` 52 条）；
  `tests/test_face_api.py` 55 条在 `analyze` 重构后全绿（返回值形状未变）。
- 无摄像头的通路验证（`.ptmp_shim/check_vtest_pipeline.py`）：mock 源起服务 → 线程取到 1280x720 帧、
  帧号递增、`identify=False` 时 `reason=detect_only`、暂停模型后**预览继续抓帧而不再推理**、
  `stop()` 后线程与服务都干净退出。
- 真库路径验证（`.ptmp_shim/check_vtest_db.py`，跑完自动清理）：① 采样失败 → 档案与样本库**逐一比对快照、
  确认零改动**；② 采样成功（替身）→ 真调 `update_elder.py` 建档，字段（uid/姓名/称呼/床位/年龄/kind）
  逐项核对；③ 按删除命令连根删净（档案、人脸目录、声纹 npz 都不剩）。
- 界面路径（Tk 真跑，`--source mock`）：`--selftest` 起停干净；`--open enroll --click start_enroll --auto`
  → 打印"未检测到人脸 → 退出录入，档案与样本库都没有改动"；`--open delete --click find,delete
  --click-arg <姓名> --auto --auto-yes` → 打桩档案 997 被查中 → 确认 → 删除 → 3 秒后回一级小窗 → 复查已不存在。
- 画框画字（`.ptmp_shim/check_vtest_canvas.py`，喂合成帧 + 假人脸）：两张脸 → 2 框 2 文字，
  文字为 `fuze（000）  0.71` / `未知  0.31`，颜色分已知/未知，坐标全部落在控件内、名字贴在框上沿，
  状态栏报出"认出：fuze（000）"与"可切换主体=True"。

### 另：`no_identity` 的排查口径（同日，用户问「终端中 no identity 是什么原因」）

`no_identity` 是**整体结论**（"检到脸但说不清是谁"），`verdict.reason` 与每张脸的
`face.identity_reason`（`below_threshold`/`empty_library`/`ambiguous`）是两个粒度。`scripts/face_check.py`
的 `--live` 结论改为：把最后一次探测的 `identity_reason` + 相似度 + 进库阈值 + 与第二名差距一起打出来，
并区分"这一帧其实已经认出、只是连续帧没够"（免得用户白去补拍）；该段抽成 `report_conclusion()`
以便脱离摄像头用合成结果验证四条分支。`vision/人脸识别操作手册.md` §5 补了对应问答与
`identity_reason` 对照表。

### 已知限制

- **真摄像头 + 真人脸这一跳未在本机复验**：DSH 沙箱打不开摄像头设备（`摄像头设备 0 打开失败`），
  本机验的是 mock 源 + 合成帧 + 假人脸。需**用户侧**跑一次 `vision_test_start.py`（不带 `--source`）
  做最终验收：`── ② 人脸检测` 应看到自己头上出现 `fuze（000）`。
- 三级小窗只收了姓名/称呼/床位/年龄（用户点名的四个字段）；性别/备注/说话风格仍要命令行补。
- 界面未接"活体检测""红外"等后续能力；阈值、无关键点对齐、板卡 BPU 等限制同前（见
  `vision/人脸识别注意事项.md`）。
- 板卡上跑界面需要 `--source mipi`（且板卡目前没有摄像头硬件）。

### 补记（同日）：用户报"摄像头异常卡顿" —— 查出两个软件真凶并修掉

用户原话：「摄像头异常卡顿，可能原因是什么」。查下来**两个都是软件问题**（不是摄像头坏），
而且都能量化：

| # | 真凶 | 实测 | 修法 |
|---|---|---|---|
| 1 | **NV12 转换走纯 numpy 全幅 float32**（`vision/webcam.py::bgr_to_nv12`） | 1280x720 **34.1 ms/帧**、640x480 10.0 ms/帧；面授的 `face_check.py`/GUI 都用 `--channels 1280x720,640x480` → 服务端每帧要算 **44 ms**，硬上限 ~22fps，还和 YOLO 抢 CPU | 新增 cv2 快路径：**Y 用 `BGR2YUV_I420`，色度自己按 2x2 平均**（720p **5.81 ms**，6 倍；640x480 1.15ms，9 倍）。逐像素与纯 numpy 参考差 ≤1（用例锁 ±2） |
| 2 | **预览与推理在同一个线程里串行**（`vtest/pipeline.py`） | 一轮检测+识别 250~700ms → 画面每 0.25~0.7 秒才换一张；相机本身是 15~30fps | 拆成**抓帧线程 + 推理线程**：预览 ~30fps（`fps`），框 ~3fps 滞后（`analyze_fps`）。回归用例断言"1 秒内预览 +30 帧、推理 +3 次"（改前该断言必红） |

**踩到的坑（值得记）**：不能整幅直接用 `cv2.COLOR_BGR2YUV_I420` 换掉纯 numpy —— OpenCV 那份
I420 的色度是**取每个 2x2 块的左上角像素**（实测与"2x2 平均"最大差 55），会产生块状色噪，
而且正是既有用例 `test_bgr_to_nv12_chroma_is_2x2_average_not_top_left` 守着的性质；也**不能**用
`COLOR_BGR2YUV`（它的 Y 与 I420 差到 18、V 系数是另一套，实测与参考差 26）。所以快路径只借它
拿 Y 平面，色度另算。

**顺手加固的三处**（都在怀疑清单里，成本极低）：

- USB 摄像头**优先要 MJPG**（`_try_mjpg`）：不设 FOURCC 时很多设备默认 YUY2，720p 在 USB2 上
  只有 5~10fps；顺序是 FOURCC 在前、W/H 在后（设 FOURCC 可能重置分辨率）。
- `CAP_PROP_BUFFERSIZE=1`：否则 `read()` 拿到的是驱动队列里**最旧**那帧，画面延迟越积越大
  （"人已经不动了，画面还在动"）。两者都用 `getattr` 探测属性，驱动不支持就静默跳过。
- 取帧改用 `CameraClient.get_next_frame()`（只在**真有新帧**时返回）：原先用 `get_frame()` 会把
  同一帧反复解码 —— 实测空转到 **139 次/秒**，白烧 CPU，还把"帧率"显示成假的（真机率 15fps
  被显示成 139）。
- 摄像头打开时在服务日志打一行**实际协商格式**（`[webcam] 设备 0 实际格式：1280x720 @30.0fps
  FOURCC=MJPG`）—— 排查卡顿的第一手证据：若显示 YUY2 或个位数帧率，瓶颈就在摄像头/USB 侧。

**GUI 侧配套**：默认 `--channels 640x480`（只请求一路；多要一路让服务端每帧成本翻倍，而检测
本来就把画面缩到 640）、`--camera-fps 30`、新增 `--channels` 开关；预览按**等比居中**缩放
（4:3 画面塞进 16:9 画布不再压扁、框也不会错位）；状态栏把 `预览 fps / 推理 次/秒 / 检测 ms`
分开报，并在 `vision/人脸识别注意事项.md` §6.1 与 `vision/README.md` 写了"卡顿怎么查"的对照表。

**测试**：`tests/test_vision.py` 88 → **90 passed**（新增"快路径 vs 纯 numpy 参考 ±2"、"无 cv2 时
退回 numpy 且逐字节一致"）；`tests/test_vtest.py` **53 passed**（含新的解耦回归）；全量
`pytest tests LLM/tests` → **603 passed**。

### 补记二（同日）：查"照片会不会被提交到 GitHub" —— 人脸库是干净的，但挖出声纹已在线上

用户问「检测一下照片数据库是否会提交到 github 上」，随后要求「确保为人脸识别所拍的照片
不会被上传到 github 即可」。

**查证结论（三重独立确认）：**

| 检查 | 命令 | 结果 |
|---|---|---|
| 索引里有没有 | `git ls-files LLM/data` | 只有 `feeds.json` + 两个声纹 npz，**没有任何 `faces/`** |
| 历史里有没有 | `git log --all -- LLM/data/faces` / `git rev-list --all --objects` | 无记录、无对象 |
| 线上能不能下到 | 抓 `raw.githubusercontent.com/.../LLM/data/faces/000/2026…jpg` | **404** |
| 盘上的样本是否被忽略 | `git status --porcelain --ignored -- LLM/data/faces` | `!! LLM/data/faces/`（git 当它不存在） |

`brain.db`（姓名/床位）、`audit.jsonl`、`chroma/`、`backup/`、`.env`、`vision/models/`、
`vision/testdata/*.jpg` 同样：未跟踪 + 历史无 + 线上 404。

**但顺手挖出一个真问题**：`LLM/data/speakers/elder_001.npz`、`elder_003.npz`（**声纹，生物特征**）
是 `8d77d9b`（2026-08-27「debug」）提交进去的，**现在仍在 `origin/main` 里**，抓
`raw.githubusercontent.com/guozhianNB/Robot/main/LLM/data/speakers/elder_001.npz` 能**无凭据拿到
文件**（octet-stream）—— 顺带说明该仓库对这些路径是公开可读的。`.gitignore` 第 35 行那条
`LLM/data/speakers/` 是后来补的，而 **`.gitignore` 管不了已跟踪文件、更管不了历史**（本次事故的
教科书案例）。同类还有 `.research_sherpa/speech_0.wav`（5.61s 16kHz 人声，`4157ba3` 进来）、
`.research_sherpa/` 整目录、`LLM/*/node_modules/**`（**3642 个文件**）—— 都是"规则写了但文件已跟踪"。
另有 `.ptmp_shim/`（开发临时探针 + 测试日志 69 个文件）当时**没被忽略**，`git add -A` 会带进去。

**本轮交付：把"不会进 GitHub"做成可执行的闸门**（用户选择自己处理声纹，故未做任何提交/清史）

| 文件 | 作用 |
|---|---|
| `scripts/check_privacy.py`（新） | 查**三条通道**：① `git ls-files`（下次 commit）② `git ls-files -o --exclude-standard`（下次 `git add -A`）③ `git rev-list --all --objects`（**已经推到 GitHub 的**）。命中人脸样本 → 退出码 1 并给出处理办法；`--strict` 把声纹/wav/db/模型/node_modules 也算失败；`--quiet` 供钩子用；`--no-history` 求快 |
| `tests/test_privacy.py`（新，23 条） | ① 规则单测（认得出 `LLM/data/faces/**`、**时间戳命名**、`faces/<名>.jpg|npz`、`det_*.jpg`；**不误伤** `robot_interfaces/` 这种含 "faces/" 的同名目录）② 真仓库体检：索引/未跟踪/历史三处都没有人脸样本 + **盘上每个样本都确实被忽略** + 整条闸门跑一遍为 0。**只要 pytest 绿，就没有一张人脸照片能被提交** |
| `.githooks/pre-commit`、`.githooks/pre-push`（新） | 启用：`git config core.hooksPath .githooks`。`pre-commit` 查索引+未跟踪，`pre-push` 连历史一起查（**推之前最后一道门**，因为上传的就是历史对象）；找不到 python 就跳过（绝不因环境问题阻断提交），要临时放行用 `--no-verify` |
| `.gitignore` | 补 `vision/testdata/*.{jpeg,bmp,webp,gif}`、`/det_*.jpg|png`、根目录 `/*.jpg|jpeg|png`（防"摄像头帧落在仓库根"）、`.ptmp_shim/` |
| 文档 | `vision/人脸识别注意事项.md` 新增 **§8.1「怎么确保照片没被推到 GitHub」**（含"`.gitignore` 给不了这个保证"的说明与清史办法）；`vision/README.md`、`AGENTS.md`（已知坑）各加一条 |

**识别方式不依赖路径**：靠 `face_lib.add_sample()` 的固定文件名格式
`<年月日>_<时分秒>_<微秒>.jpg/.npz` —— 所以 `FACE_DIR` 被环境变量重定位到仓库内任何角落都拦得住。

**实测（本机）**：

```
python scripts/check_privacy.py
  ① 索引 ✅ 干净   ② 未跟踪但未忽略 ✅ 干净   ③ 历史 ✅ 干净
  ④ 盘上已被正确忽略的样本：人脸样本 22 个文件、声纹样本 0 个文件
  ✅ 通过：人脸照片与指纹没有出现在索引、未忽略的未跟踪文件、或 git 历史里。   rc=0

模拟事故 1（git add -f 一张假样本到 LLM/data/faces/）→ ❌ 拦截，rc=1
模拟事故 2（把样本命名格式的文件放在 vision/ 下，未跟踪未忽略）→ ❌ 拦截（②通道），rc=1
清理后复检 → rc=0
```

**已知限制**：`.githooks/` 里的 sh 钩子**没能在本机实跑**（DSH 沙箱禁止命名管道，
MSYS bash 起不来：`couldn't create signal pipe, Win32 error 5`）；钩子调用的 python 入口
（`--quiet`/`--quiet --no-history`）都单独验过（干净 rc=0、违规 rc=1），钩子本体逻辑只有
"找 python → 调它 → 按退出码决定"，请在**你自己的普通终端**里首次提交时确认一次。
`speech_0.wav` 的来源无法从仓库判定（没有任何脚本写它，只有一个 VAD 测试脚本读它）——
若是自己录的请按生物特征对待。
