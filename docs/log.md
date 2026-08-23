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
