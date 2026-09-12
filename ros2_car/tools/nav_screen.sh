#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 板卡侧建图/导航的一键后台启动器（screen 托管，ssh 断开不掉进程）。
#
# 位置：板卡 ~/tools/nav_screen.sh（板卡侧运维脚本，不进 git、不污染仓库）
# 用法：
#   ~/tools/nav_screen.sh lidar                      # 起雷达（ydlidar）
#   ~/tools/nav_screen.sh base [chassis|rf2o|fused]  # 起基础节点（rsp+lidar+odom），默认 fused
#   ~/tools/nav_screen.sh slam [mapping|localization] # 起 slam_toolbox（rviz 关闭），默认 mapping
#   ~/tools/nav_screen.sh nav                        # 起 Nav2（rviz 关闭、autostart 关闭，之后跑 activate_nav.py）
#   ~/tools/nav_screen.sh lat                        # 起 rosbridge（Foxglove 远程可视化，9090）
#   ~/tools/nav_screen.sh save <前缀路径>            # 存图（如 ~/Robot/ros2_car/maps/my_map2）
#   ~/tools/nav_screen.sh log <base|slam|nav|lidar|lat> [行数]
#   ~/tools/nav_screen.sh kill <base|slam|nav|lidar|lat|all>
#   ~/tools/nav_screen.sh list
#
# 设计：每个服务在自己的 screen 会话里 source 环境后执行，输出 tee 到 /tmp/<svc>.log；
#       kill 只杀对应 screen 会话，不会误伤其他进程。
set -u

WS=/home/sunrise/Robot/ros2_car
SETUP="source /opt/ros/humble/setup.bash && source \$HOME/ros2/yahboomcar_ws/install/setup.bash && source $WS/install/setup.bash && export ROS_LOG_DIR=/home/sunrise/Robot/.diag/roslog"

usage() { sed -n '2,20p' "$0"; exit 1; }
cmd="${1:-}"; [ -z "$cmd" ] && usage

start() {                       # start <会话名> <要执行的命令>
  local name="$1"; shift
  if screen -ls 2>/dev/null | grep -q "\.${name}[[:space:]]"; then
    echo "[skip] 会话 $name 已在运行（先 kill $name）"; return 0
  fi
  screen -dmS "$name" bash -lc "$SETUP && $* 2>&1 | tee /tmp/${name}.log"
  sleep 1
  echo "[ok] 已启动 $name"
}

case "$cmd" in
  lidar)
    # 必须用本仓库的 launch（指向实测定稿的 lidar_tmini_plus.yaml：frame_id=laser_link、
    # 230400/10Hz）。yahboomcar_ws 的 ydlidar_launch.py 默认 TminiPro.yaml → frame_id=laser_frame，
    # 与 URDF/slam 的 laser_link 不一致，TF 里会多出一段 static laser_frame。
    start lidar "ros2 launch robot_bringup lidar.launch.py" ;;
  base)
    src="${2:-fused}"
    start base "ros2 launch robot_bringup robot_base.launch.py odom_source:=$src" ;;
  slam)
    mode="${2:-mapping}"
    if [ "$mode" = "localization" ]; then
      start slam "ros2 launch robot_bringup slam.launch.py mode:=localization map:=$WS/maps/my_map.yaml rviz:=false"
    else
      start slam "ros2 launch robot_bringup slam.launch.py mode:=mapping rviz:=false"
    fi ;;
  nav)
    start nav "ros2 launch robot_bringup navigation.launch.py map:=$WS/maps/my_map.yaml rviz:=false autostart:=false" ;;
  lat)
    start lat "ros2 launch rosbridge_server rosbridge_websocket_launch.xml" ;;
  teleop)
    # 键盘遥控（改键位：i/, 前后，j/l 左右转，k 停；--repeat 10Hz 持续重发，
    # 对抗 0.5s 看门狗 + SSH 输入延迟）。用法：screen -r teleop，退出按 Ctrl-A 再按 D
    start teleop "ros2 run teleop_twist_keyboard teleop_twist_keyboard --ros-args --repeat 10.0 -r cmd_vel:=/cmd_vel" ;;
  save)
    [ -z "${2:-}" ] && { echo "用法: nav_screen.sh save <前缀路径>"; exit 1; }
    ros2_cmd="ros2 run nav2_map_server map_saver_cli -f $2"
    echo "存图中（失败可重试，async slam 的 /map 每 5s 才更新）..."
    bash -lc "$SETUP && timeout 60 $ros2_cmd" ;;
  log)
    svc="${2:-base}"; n="${3:-40}"
    echo "===== /tmp/$svc.log (tail $n) ====="; tail -n "$n" "/tmp/$svc.log" 2>&1 ;;
  kill)
    svc="${2:-}"
    names="$svc"
    [ "$svc" = "all" ] && names="lidar base slam nav lat"
    [ -z "$svc" ] && { echo "用法: nav_screen.sh kill <base|slam|nav|lidar|lat|all>"; exit 1; }
    for n in $names; do
      if screen -ls 2>/dev/null | grep -q "\.${n}[[:space:]]"; then
        screen -S "$n" -X quit && echo "[ok] 已停 $n"
      else
        echo "[skip] $n 未运行"
      fi
    done ;;
  list)
    screen -ls; echo "--- 活跃 ROS 节点数 ---"
    bash -lc "$SETUP && ros2 node list 2>/dev/null | wc -l" ;;
  *) usage ;;
esac
