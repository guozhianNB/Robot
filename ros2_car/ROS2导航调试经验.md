# ROS2 小车 Nav2 导航调试经验（2026-09-05 实测定稿 + 文档勘误）

> 本文记录 2026-09-05 在板卡（RDK X5，Ubuntu 22.04 aarch64，ROS2 Humble）上
> 复测"Nav2 点了 Goal 小车不动"整条链路的实测结论与踩坑，并**更正本文档/老文档里的错误信息**。
> 阅读顺序：`ros2_car/README.md` → `上板核对清单.md` → 本文。

---

## 〇、先更正两个贯穿性错误（影响所有文档）

### 1. 工作区真实路径
板卡上本仓库的实际路径是 **`/home/sunrise/Robot/ros2_car`**（git 仓库），
不是老文档/交接文档里的 `~/ros2/car_ws` 或 `D:\_project\Robot\ros2_car`（Windows/旧布局）。
上板所有命令里的 `~/ros2/car_ws` 都要换成 `/home/sunrise/Robot/ros2_car`。
`ros2_car/README.md`、`上板核对清单.md`、`/home/sunrise/小车端ROS2导航调试交接.md` 均已含此错误路径。

### 2. rviz 发 Nav2 目标的工具名
老文档写 rviz 里用 **"2D Goal Pose"** 发目标——这是 ROS1/旧工具 `rviz_default_plugins/SetGoal`（发 `/goal_pose`），
**Nav2 不订阅 `/goal_pose`**。正确工具是 **"Nav2 Goal"（`nav2_rviz_plugins/GoalTool`）**，走 `/navigate_to_pose` action。
`robot_bringup/rviz/navigation.rviz` 已改；`ros2_car/README.md` 第 41 行仍写旧名，需改。

---

## 一、交接文档声称"已修复、待上板验证"的两处 Nav2 Bug —— 板上源码实际并未修复

交接文档（`/home/sunrise/小车端ROS2导航调试交接.md`）说两个根因"已定位并修复，待上板验证"，
但**本板卡 `src/` 里这两个文件仍是未修复版**（git 记录停在 `f81c2a9 "ros2更新"`）：

| 交接文档说修了 | 板上实际 | 本次处理 |
|---|---|---|
| `navigation.rviz` 用 `nav2_rviz_plugins/GoalTool` | 仍是 `rviz_default_plugins/SetGoal` → `/goal_pose` | ✅ 已替换为 GoalTool，重编译进 `install/share` |
| `navigate_to_pose.py` 改 `sys.argv` 兜底 | 仍是 `remove_ros_args(args or [])`（丢参数→总发 (0,0)） | ✅ 已改，并补 `import sys` |

⚠️ 教训：**交接文档/仓库 ≠ 板上运行源码**。接手的 agent 先 `git log` 核对文件，不要假设文档状态已落地。

---

## 二、底盘轴向标定——关键实测纠正（文档里最易写错的坑）

### sign 参数只改 odom 读数，不改下发命令
`robot_chassis/robot_chassis/chassis_driver.py` 里 `sign_vx/sign_vy/sign_wz` **只作用于 odom 解码**
（269–271 行），**不作用于下发的 `/cmd_vel`**。所以：
- 要改"车实际往哪走"，改的是**命令侧**（见下）；
- sign 改的是"odom 报哪边是正"。

### 实测结果（需要真机 + 有人目测物理方向才能定，无相机时别盲猜）
真机目测 + odom 读数（命令 `+vx` 车头物理前进；`+vy` 物理右；`+wz` 物理右）：

| 项 | odom 侧 sign（当前已定稿） | 说明 |
|---|---|---|
| `sign_vx` | **+1.0**（原 -1 错，已改回） | +vx 物理前进，odom 原记 −x |
| `sign_vy` | **-1.0** | 物理右 → odom −y（"左=+y"成立，无需改） |
| `sign_wz` | **-1.0** | 物理右转 → odom −yaw |

**命令侧**：odom 已是标准约定（+y=左、+wz=左转）且自洽，但真机 `+vy/+wz` 都驱动到"物理右"（与 odom 正方向相反）。
若命令与 odom 符号相反，Nav2 这类闭环控制会**转向发散/朝反方向开**。已给 `chassis_driver.py` 下发命令加**镜像：`vy/wz` 取反**（`vx` 不动）。改后实测 `+wz` 变物理左转、odom +20.1°。

> 交接文档的"三个 sign 先全取反"其实把 vx 改反了；正确是 **vx=+1, vy=−1, wz=−1 + 命令侧 vy/wz 镜像**。
> 这些值因人/因机而异，务必**真机逐个目测**再定稿。

### "旋转慢/横移慢"不是打滑，是限速/加速度限制
真机命令 +wz 只执行了 ~35%（odom 也这么记），**odom 是准确的**，不是轮子打滑，而是底盘
**限速 + 加速度斜坡**（`ang_accel_limit` 等）导致电机转得慢。闭环导航因此只是慢，不会发散。
（此前误判为打滑/欠行会误导后续排查。）

---

## 三、建图与存图

### 正确存图：未知格必须保留为灰(205)
ROS 的 `OccupancyGrid` 里未知=-1。若手写 saver 把未知当自由格写白(254)，Nav2 会把**没测绘区当可走区**，
规划进去会撞墙。正确做法用标准工具或写未知=205。
- `map_saver_cli` 会报 `Failed to spin map subscription`（async slam 的 `/map` 是 TRANSIENT_LOCAL，按需更新）；
  重试几次（每 5s 更新一次）即可，或写一个 transient_local 订阅的 saver。
- 地图 yaml 里 `mode: trinary`、`occupied_thresh: 0.65`、`free_thresh: 0.25`。

本仓库地图存于 `/home/sunrise/Robot/ros2_car/maps/my_map.{pgm,yaml}`。

### 建图前要把车放四周都空的地方，别贴墙
车贴墙时原地转会被后轮甩尾顶墙打滑，odom/转速都失真、图也建不全。先在开阔处原地转一圈确认四周 >1m 再扫。

---

## 四、Nav2 bringup 的一串真实配置 Bug（从"完全起不来"修到"定位+controller/planner 全 active"）

### 1. AMCL：`robot_model_type: "differential"` → `"nav2_amcl::DifferentialMotionModel"`
报错：`class differential ... does not exist. Declared types are nav2_amcl::DifferentialMotionModel ...`。
`上板核对清单.md` 早已预警此项（linorobot2 用插件名），实测确实必须改。

### 2. controller_server：`general_goal_checker` 缺 `plugin`
报错 `Can not get 'plugin' param value for general_goal_checker`（FATAL）。
`nav2_params.yaml` 里 controller 与 waypoint 两处 `general_goal_checker` 都少了
`plugin: "nav2_controller::SimpleGoalChecker"`，已补。

### 3. 通过 `bringup.launch.py mode:=navigation map:=...` 起导航时地图参数丢失
现象：`map_server` 报 `yaml_filename is not initialized`、controller 报 `No critics defined for FollowPath`
（都因为 nav2 实际没拿到完整参数）。定位到：**双层 include（bringup→navigation.launch→nav2 localization/navigation_launch）
里 RewrittenYaml 对 `map` 的覆盖没生效**，`yaml_filename` 被写成空。
绕开办法（当前采用）：
- 把 `map_server.yaml_filename` **硬编码**成真实地图路径（不再依赖会被清空的 `map:=` 覆盖）；
- **不要用 bringup 起导航**，改成分步：先起基础节点（robot_state_publisher+lidar+chassis odom），
  再 `ros2 launch robot_bringup navigation.launch.py map:=...`（此路径 map 能正确传，实测 map_server 配置成功）。

### 4. bt_navigator 激活失败 —— ✅ 已修复（2026-09-05 深夜，无头复现+根因+验证）
现象（确定性复现）：`bt_navigator_navigate_through_poses_rclcpp_node: "backup" action server not available after waiting for 1.00s`，随后
`Exception when loading BT / Error loading XML ...navigate_through_poses_w_replanning_and_recovery.xml`，`ros2 lifecycle set /bt_navigator activate` 返回 Transitioning failed。

**两层根因（都要满足才能激活成功）：**
1. **无条件创建 navigate_through_poses**：nav2 的 bt_navigator（humble 1.1.x，源码 `nav2_bt_navigator/src/bt_navigator.cpp`
   on_configure/on_activate）**无论 `plugins` 参数写什么，都会同时 `make_unique` 并 activate
   `NavigateToPoseNavigator` + `NavigateThroughPosesNavigator`**，两个都无条件加载自己的 BT。没有参数能关掉它。
2. **行为插件名写错导致动作名不匹配（真正的 bug）**：navigate_through_poses 的 BT（以及 navigate_to_pose 的 BT）
   里的 recovery `<BackUp>` 节点，会在 activate 时用**动作名 `backup`** 去连 action server；而 behavior_server
   的 action server 名 = `behavior_plugins` 列表里的插件 key。官方 nav2_bringup 用的是 **`backup`**（无下划线），
   我们这里误写成了 **`back_up`** → behavior_server 只发布 `/back_up`，bt_navigator 永远等不到 `backup` → 激活失败。
   （`spin`/`wait` 拼写一致所以没暴露；只有 back_up 差一个下划线。）

**修复**：`src/robot_bringup/config/nav2_params.yaml` behavior_server 段：
```
behavior_plugins: ["spin", "backup", "wait"]   # 原误写 "back_up"
backup:                                        # 原误写 "back_up"
  plugin: "nav2_behaviors/BackUp"
```
改后 behavior_server 发布 `/backup`，bt_navigator activate 即成功（须 controller/planner/behavior 已 active）。

**无头验证（不接真机、不运动）**：起 controller_server + planner_server + behavior_server + bt_navigator，
按序 configure+activate（controller→planner→behavior→**bt_navigator 放最后**），
bt_navigator 成功 `active [3]`，日志 `Creating bond ... activated`，无 error；action list 含
`/backup /compute_path_through_poses /compute_path_to_pose /follow_path /spin /wait /navigate_to_pose /navigate_through_poses`。

**激活时序要求**：bt_navigator 在 activate 时会先把整棵 navigate_through_poses BT 预加载并检查其中动作，
要求 controller(`follow_path`) + planner(`compute_path_through_poses`) + behavior(`backup/spin/wait`)
的动作服务在**它的 1s 等待窗内**都已就绪。慢板卡上并行 autostart 可能竞态，稳妥做法是手动按序 activate，
**bt_navigator 放最后**并确认前四个动作服务已出现后再 activate。
> 已尝试但无效（记录备查）：从 bt_navigator `plugins` 去掉 NavigateThroughPosesNavigator —— bt_navigator 仍硬加载，无效。
> 不要指望把那个 1s 等待改大：humble 1.1 的 BT 动作等待时间不可配置（相关 PR 见 nav2 #3960，未合入 humble）。

---

## 五、ROS 实用技巧（本次踩坑）

- **/scan 是 BEST_EFFORT QoS**：自定义订阅节点要 `ReliabilityPolicy.BEST_EFFORT`，否则收不到（工具 `ros2 topic hz` 默认能收，但自写节点会静默）。
- **/map 是 TRANSIENT_LOCAL + RELIABLE**：抓图订阅要 `DurabilityPolicy.TRANSIENT_LOCAL`。
- **探测脚本要能干净退出**：长驻 `rclpy.spin` 退出后进程会滞留到工具 60s 超时被 kill，若输出还经过 `grep` 管道，块缓冲会在 kill 时丢失 → 看似"没输出"。用 `os._exit(0)` + `print(..., flush=True)`，少套 `grep`。
- **别用 `pkill -f "关键词"` 杀含该关键词的进程**：命令自身命令行也含关键词会把自己杀掉（本会话多次踩）。用 `ps | grep | awk | xargs kill`，且模式别和当前命令行重叠。
- 底盘急停/保命：`ros2 topic pub -1 /robot/cmd_stop std_msgs/msg/Bool "{data: true}"`；自主控制前务必先有紧急停手手段。

---

## 六、本次修改文件清单（都在 `ros2_car/`，build/install 已重编译）

| 文件 | 改动 |
|---|---|
| `src/robot_bringup/rviz/navigation.rviz` | `SetGoal`→`nav2_rviz_plugins/GoalTool`（Nav2 Goal） |
| `src/robot_navigation/robot_navigation/navigate_to_pose.py` | `args or []`→`sys.argv if args is None else args` + `import sys` |
| `src/robot_chassis/config/chassis_params.yaml` | `sign_vx` → `+1.0`（odom 前进轴） |
| `src/robot_chassis/robot_chassis/chassis_driver.py` | 命令侧下发 `vy/wz` 取反（镜像，vx 不动） |
| `src/robot_bringup/config/nav2_params.yaml` | AMCL `robot_model_type`；两处 `general_goal_checker` 补 `plugin`；`map_server.yaml_filename` 硬编码真实地图；bt_navigator 去掉 NavigateThroughPosesNavigator（仍不够，见四.4） |
| `maps/my_map.{pgm,yaml}` | 本次正确存的地图 |
