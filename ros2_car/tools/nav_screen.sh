#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 板卡侧建图/导航的一键后台启动器（screen 托管，ssh 断开不掉进程）。
#
# 位置：板卡 ~/tools/nav_screen.sh（板卡侧运维脚本，不进 git、不污染仓库）
# 用法：
#   ~/tools/nav_screen.sh lidar                      # 起雷达（ydlidar）
#   ~/tools/nav_screen.sh base [chassis|rf2o|fused]  # 起基础节点（rsp+lidar+odom），默认 fused
#   ~/tools/nav_screen.sh slam [mapping|localization] [地图]  # 起 slam_toolbox（rviz 关闭），默认 mapping
#   ~/tools/nav_screen.sh nav [地图]                 # 起 Nav2（rviz 关闭、autostart 关闭，之后跑 activate_nav.py）
#   ~/tools/nav_screen.sh map                        # 列出 maps/ 下可用地图与当前默认
#   ~/tools/nav_screen.sh lat                        # 起 rosbridge（Foxglove 远程可视化，9090）
#   ~/tools/nav_screen.sh teleop                     # 键盘遥控（screen -r teleop 开车；i/, 前后 j/l 转向 k 停）
#   ~/tools/nav_screen.sh save <前缀路径>            # 存图（如 ~/Robot/ros2_car/maps/my_map2）
#   ~/tools/nav_screen.sh log <base|slam|nav|lidar|lat> [行数]
#   ~/tools/nav_screen.sh kill <base|slam|nav|lidar|lat|all>
#   ~/tools/nav_screen.sh list
#
# 地图参数（nav / slam localization 的第 3 个位置参数）：
#   可写「名字」「名字.yaml」「名字.pgm」或绝对路径；只给名字时默认在 $WS/maps/ 下找，
#   相对路径按工作区根解析（不按 ssh 当前目录）。脚本内部统一归一成同名 .yaml：
#   map_server / AMCL / slam_toolbox 只吃 yaml，.pgm 只是它的数据文件（给 .pgm 也认）。
#   默认 my_map.pgm（≈ my_map.yaml）。**换图只需换这一个参数**：map_server 的 yaml_filename
#   由 launch 的 map:= 经 nav2_common 的 RewrittenYaml 覆盖（2026-09-13 板上用 RewrittenYaml
#   探针实测确认），nav2_params.yaml 里那行 yaml_filename 走 launch 时**不生效**。
#
# 设计：每个服务在自己的 screen 会话里 source 环境后执行，输出 tee 到 /tmp/<svc>.log；
#       kill 只杀对应 screen 会话，不会误伤其他进程。
set -u

WS=/home/sunrise/Robot/ros2_car
MAPDIR=$WS/maps
DEF_MAP=my_map.pgm
SETUP="source /opt/ros/humble/setup.bash && source \$HOME/ros2/yahboomcar_ws/install/setup.bash && source $WS/install/setup.bash && export ROS_LOG_DIR=/home/sunrise/Robot/.diag/roslog"

usage() { awk 'NR>1 && /^set -u/{exit} NR>1' "$0"; exit 1; }
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

resolve_map() {                 # resolve_map <名字|路径> -> 打印归一后的绝对 .yaml 路径
  local a="${1:-$DEF_MAP}"
  case "$a" in
    /*)  ;;                     # 绝对路径：原样
    */*) a="$WS/$a" ;;          # 相对路径：按工作区根解析（ssh 一行式 cwd 不可靠）
    *)   a="$MAPDIR/$a" ;;      # 只给名字：默认在 $WS/maps/
  esac
  a="${a%.pgm}"; a="${a%.yaml}"; a="${a%.yml}"   # 去扩展名（.pgm 归一成同名 .yaml）
  printf '%s\n' "$a.yaml"
}

require_map() {                 # 地图不存在就拒绝启动，并列出可用地图（别静默加载错图）
  [ -f "$1" ] && return 0
  {
    echo "[err] 地图不存在: $1"
    echo "可用地图（$MAPDIR）："
    ls -1 "$MAPDIR"/*.yaml 2>/dev/null | sed 's|.*/|  |'
    echo "用法: $0 $cmd [地图名|路径]   （默认 $DEF_MAP）"
  } >&2
  exit 1
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
      MAP=$(resolve_map "${3:-}"); require_map "$MAP"; echo "[map] $MAP"
      start slam "ros2 launch robot_bringup slam.launch.py mode:=localization map:=$MAP rviz:=false"
    else
      start slam "ros2 launch robot_bringup slam.launch.py mode:=mapping rviz:=false"
    fi ;;
  nav)
    MAP=$(resolve_map "${2:-}"); require_map "$MAP"; echo "[map] $MAP"
    start nav "ros2 launch robot_bringup navigation.launch.py map:=$MAP rviz:=false autostart:=false" ;;
  map)
    echo "地图目录: $MAPDIR"
    ls -1 "$MAPDIR" 2>/dev/null | sed 's/^/  /'
    echo "默认地图: $(resolve_map)"
    echo "用法: $0 nav [地图名|路径]   /   $0 slam localization [地图名|路径]" ;;
  lat)
    start lat "ros2 launch rosbridge_server rosbridge_websocket_launch.xml" ;;
  teleop)
    # 键盘遥控（i/, 前后，j/l 左右转，k 停）。用法：screen -r teleop，退出按 Ctrl-A 再按 D
    # ⚠️ 本板 ros-humble-teleop-twist-keyboard 是 **2.4.1**，没有 `--repeat`（那是 ROS1 的参数）：
    #    写成 `--ros-args --repeat 10.0` 会被 rclpy 当成未知 ROS 参数 → UnknownROSArgsError → 节点秒退，
    #    screen 会话随之消失（现象 = `screen -r teleop` 报 no screen to resume）。
    #    所以这里不传 --repeat，靠「按住方向键（终端自动重复发键）」压住底盘 0.5s 看门狗；
    #    速度用只读参数压低（默认 speed 0.5 / turn 1.0）。
    # ⚠️ 输出要经 tee 管道，Python 会块缓冲 → 界面一直空白（2026-09-13 实测困惑点）：
    #    加 PYTHONUNBUFFERED=1 让键位菜单/状态行立刻显示。
    start teleop "PYTHONUNBUFFERED=1 ros2 run teleop_twist_keyboard teleop_twist_keyboard --ros-args -p speed:=0.3 -p turn:=0.5" ;;
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
