#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 板卡侧：验 ROS 侧的 /imu 链路（chassis_driver 解析 0x83 → /imu → EKF imu0）。
# 只读，不发任何运动指令。前提：~/tools/nav_screen.sh base chassis 已起。
#
# 用法（板卡上）: bash ~/tools/verify_imu_topic.sh
# 注意：不要 set -u —— ROS 的 setup.bash 会引用未定义变量，一开就报 unbound variable。
WS=/home/sunrise/Robot/ros2_car
source /opt/ros/humble/setup.bash
source "$HOME/ros2/yahboomcar_ws/install/setup.bash"
source "$WS/install/setup.bash"
export ROS_LOG_DIR=/home/sunrise/Robot/.diag/roslog

echo "=== 节点 ==="
ros2 node list 2>/dev/null

echo "=== imu 相关话题 ==="
ros2 topic list 2>/dev/null | grep -i imu

echo "=== /imu 帧率（约 10s，期望 ≈20Hz）==="
timeout 12 ros2 topic hz /imu 2>&1 | head -4

echo "=== /imu 一帧（看 frame_id 与协方差是否非零）==="
timeout 8 ros2 topic echo /imu --once 2>&1 | head -30

echo "=== EKF 是否用上 imu0（/diagnostics）==="
timeout 8 ros2 topic echo /diagnostics --once 2>&1 | head -45

echo "=== /odom 与 /odom_filtered 帧率 ==="
for t in /odom /odom_filtered; do
  echo "--- $t ---"
  timeout 8 ros2 topic hz "$t" 2>&1 | head -2
done
