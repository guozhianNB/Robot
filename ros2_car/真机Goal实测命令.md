# 真机 Nav2 Goal 实测命令（2026-09-05 修通 bt_navigator 后）

> 前置：小车放在四周 >1m 开阔处、底盘已上电。**务必先有急停手段**：
> `ros2 topic pub -1 /robot/cmd_stop std_msgs/msg/Bool "{data: true}"`（车会立刻取消导航并零速）。
> 建议有人盯车/看摄像头，随时能按急停。
> 工作区 /home/sunrise/Robot/ros2_car，本机即板卡，无需 ssh。

## 0) 环境（每个终端都要）
```bash
source /opt/ros/humble/setup.bash
source /home/sunrise/Robot/ros2_car/install/setup.bash
export ROS_LOG_DIR=/home/sunrise/Robot/.diag/roslog
```

## 1) 起基础节点：TF + 雷达 + 底盘里程计（三选一起，车不动）
终端 A：robot_state_publisher（base_link/laser 等静态 TF）
```bash
ros2 run robot_state_publisher robot_state_publisher --ros-args \
  -p robot_description:="$(cat /home/sunrise/Robot/ros2_car/src/robot_bringup/urdf/car.urdf)"
```
终端 B：雷达
```bash
ros2 launch robot_bringup lidar.launch.py
```
终端 C：底盘里程计（STM32, /dev/ttyACM0 → /odom + odom→base_link TF）
```bash
ros2 launch robot_bringup odom.launch.py odom_source:=chassis
```
自检（应有数/有 TF）：`ros2 topic hz /scan`、`ros2 topic hz /odom`、`ros2 run tf2_ros tf2_echo odom base_link`。

## 2) 起 Nav2 全套节点（autostart:=false，先不激活；含 map_server/amcl 定位 + controller/planner/behavior/bt_navigator/waypoint/velocity）
终端 D：
```bash
ros2 launch robot_bringup navigation.launch.py \
  map:=/home/sunrise/Robot/ros2_car/maps/my_map.yaml rviz:=true autostart:=false
```

## 3) 按序激活，bt_navigator 放最后（关键）
```bash
python3 /home/sunrise/Robot/ros2_car/tools/activate_nav.py
```
看到 `全部节点 active，bt_navigator 已就绪，/navigate_to_pose 可用` 即成功
（map/amcl/controller/smoother/planner/behavior/waypoint/velocity/bt 全 active）。
> 若某节点卡在激活，多半是缺 odom/scan/TF（第 1 步没起来）——先补第 1 步再重跑。

## 4) 下初始位姿 + 发 Goal
rviz：工具栏 "2D Pose Estimate" 给小车当前位置（约对准地图原点），
再用 **"Nav2 Goal"(GoalTool，nav2_rviz_plugins)** 点远处的开阔目标 → 应出现绿色路径并自动开过去。
命令行替代（参数现在能正常传）：
```bash
ros2 run robot_navigation navigate_to_pose --x 1.0 --y 1.0 --theta 0.0
```
观察：车按规划开动 → 到目标停 → exec_state 变 idle/arrived。
急停随时：`ros2 topic pub -1 /robot/cmd_stop std_msgs/msg/Bool "{data: true}"`

## 备注
- bt_navigator 修复根因 = behavior_server 插件名 `back_up`→`backup`（见 ROS2导航调试经验.md 四.4，
  git 3fe0902）。别改回 `back_up`。
- 若没出现绿色路径/车不动：查 `ros2 topic echo /robot/exec_state` 与
  `ros2 topic echo /plan`（空=没规划出），及 rviz 里 map/costmap 是否显示。
- Nav2 发目标是走 action `/navigate_to_pose`，不走 ROS1 的 `/goal_pose` topic。
