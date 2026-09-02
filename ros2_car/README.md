# 小车 ROS2 工作区（SLAM 建图 + Nav2 自主导航）

RDK X5（Ubuntu 22.04 / ROS2 Humble）小车端：**激光雷达 + 里程计 + SLAM 建图 + Nav2 导航**。

## 包结构

```
car_ws/src/
├── robot_interfaces/   自定义服务定义（robot/move、robot/turn、robot/navigate_to，ament_cmake）
├── robot_chassis/      底盘驱动（cmd_vel → STM32 USB CDC 协议；STATUS 心跳 → /odom + tf）
├── robot_bringup/      一键启动（launch + nav2/slam 参数 + URDF + rviz 配置）
└── robot_navigation/   大模型端对接（move/turn/navigate 服务 + exec_state/arrived 状态 + 急停）
```

## 环境

```bash
source /opt/ros/humble/setup.bash
source ~/ros2/yahboomcar_ws/install/setup.bash   # ydlidar 驱动、rf2o（bashrc 已自动加载）
source ~/ros2/car_ws/install/setup.bash          # 本工作区
```

硬件：YDLidar Tmini Plus（/dev/ttyUSB0，230400）｜ STM32 麦轮底盘（/dev/ttyACM0，USB CDC，按 `docs/USB车控接口.md`）。

## 一键启动

```bash
# ① 建图（默认 odom_source:=rf2o，无底盘可用激光里程计兜底）
ros2 launch robot_bringup bringup.launch.py mode:=mapping

# ② 另开终端，键盘开小车逛房间
ros2 run teleop_twist_keyboard teleop_twist_keyboard

# ③ 逛完保存地图
ros2 run nav2_map_server map_saver_cli -f ~/ros2/car_ws/maps/my_map

# ④ 自主导航（加载刚存的地图）
ros2 launch robot_bringup bringup.launch.py mode:=navigation map:=~/ros2/car_ws/maps/my_map.yaml
```

导航模式在 rviz 里点 **2D Pose Estimate** 给出初始位姿（AMCL 定位），再点 **2D Goal Pose** 下发目标。
免 rviz 也能发目标：

```bash
ros2 run robot_navigation navigate_to_pose --x 1.0 --y 0.5 --yaw 90
```

## 分步启动（调试用）

| 组件 | 命令 |
|---|---|
| 雷达 | `ros2 launch robot_bringup lidar.launch.py` |
| 里程计(激光) | `ros2 launch robot_bringup odom.launch.py odom_source:=rf2o` |
| 里程计(底盘) | `ros2 launch robot_bringup odom.launch.py odom_source:=chassis` |
| SLAM 建图 | `ros2 launch robot_bringup slam.launch.py mode:=mapping` |
| SLAM 定位 | `ros2 launch robot_bringup slam.launch.py mode:=localization map:=~/ros2/car_ws/maps/my_map.yaml` |

## 底盘接入（STM32 接上后）

```bash
ros2 launch robot_bringup bringup.launch.py mode:=mapping odom_source:=chassis
```

### 标定（真机必做，按顺序）

1. **轴方向**：`ros2 topic pub -r 10 /cmd_vel geometry_msgs/msg/Twist "{linear: {x: 0.1}}"`，
   车应**直行不偏转**；歪了改 `chassis_params.yaml` 的 `sign_vx/sign_wz`。
   同理测 `linear.y`（横移，改 `sign_vy`）与 `angular.z`（自转，改 `sign_wz`）。
2. **轮径/旋转半径**：直行 1m 看 `/odom` 读数，偏差按比例修 `wheel_radius`；
   原地转 360° 看 yaw 读数，修 `rotate_radius`。
3. **四轮方向**：若某轮装反，用 `wheel_signs` 单独取反。

> 固件侧轮径/旋转半径宏：`stm32/control` 的 `MC_WHEEL_DIAMETER_MM`、`MC_ROTATE_RADIUS_MM`，
> 两侧参数需一致。

## 安全机制

- **看门狗**：底盘驱动 0.5s 未收到 `/cmd_vel` 自动补发零速（键盘遥控请保持 ≥2Hz 按键节奏或调大 `watchdog_timeout`）。
- **限速**：`max_vx≤0.5 m/s`、`max_wz≤0.8 rad/s` + 加速度斜坡（养老院场景，改 `chassis_params.yaml`）。
- **急停**：`ros2 topic pub -1 /robot/cmd_stop std_msgs/msg/Bool "{data: true}"` →
  底盘驱动立即制动；`robot_navigation/cmd_stop` 节点同时取消 Nav2 目标（对接大模型端契约）。

## 常见问题

- **雷达不出数**：`ls /dev/ttyUSB*`、`ros2 topic hz /scan`；确认 `lidar_tmini_plus.yaml` 与型号匹配。
- **TF 报错**：`ros2 run tf2_tools view_frames.py` 看树；`odom→base_link` 由里程计源发布，`base_link→laser_link` 由 URDF 发布。
- **导航不动/规划失败**：AMCL 粒子是否收敛（rviz 里重新 2D Pose Estimate）；`/map` 是否已加载；`/scan` 是否有数据。
- **底盘 USB 断开**：驱动自动重连；`/odom` 停更时查 `/dev/ttyACM0` 与 `dmesg`。

## 后续对接（大模型端）

契约见 `docs/目标文档及说明/ROS底盘接口需求.md`。已实现（navigation 模式自动带起 `robot_actions` 节点）：

```bash
# robot/move：直线/横移，到位自动停（forward|back|left|right）
ros2 service call /robot/move robot_interfaces/srv/Move "{direction: forward, distance_m: 1.0}"
# robot/turn：原地转向，到位自动停（角度，正=左转）
ros2 service call /robot/turn robot_interfaces/srv/Turn "{angle_deg: 90}"
# robot/navigate_to：Nav2 导航到坐标（地点表后置，place 暂不支持）
ros2 service call /robot/navigate_to robot_interfaces/srv/NavigateTo "{place: '', x: 1.0, y: 0.5, theta: 90}"
# 状态话题：robot/exec_state（idle/moving/navigating/error）、robot/arrived（bool）
ros2 topic echo /robot/exec_state
# 急停
ros2 topic pub -1 /robot/cmd_stop std_msgs/msg/Bool "{data: true}"
```

待后续（第二批）：`robot/obstacle` / `robot/battery` 状态、地点表 + 语义区域框（建图后标注坐标）。
