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
