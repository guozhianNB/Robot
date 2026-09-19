#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# lidar_loc 上板冒烟测试。
#
# 不碰串口、不发任何运动指令：用 robot_state_publisher（给 base_link→laser_link）
# + 静态 odom→base_link 顶替真实里程计，只验证定位链路的"接线"是否通。
#
# 起：rsp + 静态 TF + 雷达（含 scan_filter）+ map_server + lidar_loc
# 验：① lidar_loc 收到 latched 的 /map（证明 TRANSIENT_LOCAL QoS 选对了）
#     ② /scan_filtered 有数据（证明 scan_filter 在跑）
#     ③ 给初始位姿后 /amcl_pose 有输出、map→odom TF 出现
#     ④ 初始位姿故意给错 3m 时，护栏应生效：位姿**不**跟着跑飞
# 结束：按进程组整组杀掉（只杀 launch 的 pid 会留下孤儿节点）
#
# 用法：bash ~/Robot/ros2_car/tools/smoke_lidar_loc.sh [地图.yaml]
set -u

WS=${WS:-$HOME/Robot/ros2_car}
MAP=${1:-$WS/maps/my_map.yaml}
OUT=${OUT:-/tmp/lidar_loc_smoke}
URDF="$WS/install/robot_bringup/share/robot_bringup/urdf/car.urdf"

export ROS_LOG_DIR="$OUT/roslog"
mkdir -p "$OUT" "$ROS_LOG_DIR"

# ⚠️ source ROS 的 setup.bash 期间必须关掉 set -u：那些脚本会引用未定义的
#    AMENT_TRACE_SETUP_FILES 等变量，开着就是 "unbound variable" 直接退出。
set +u
source /opt/ros/humble/setup.bash
source "$HOME/ros2/yahboomcar_ws/install/setup.bash" 2>/dev/null || true
source "$WS/install/setup.bash"
set -u

PGIDS=()
spawn() {  # spawn <日志名> <命令...>
  local name="$1"; shift
  # setsid：让每个子进程自成进程组。`ros2 launch` 会再 fork 出真正的节点，
  # 只杀 launch 的 pid 把它们留成孤儿（实测：两轮遗留的 map_server 重名冲突），
  # 所以收摊必须按**进程组**整组杀。
  setsid "$@" > "$OUT/$name.log" 2>&1 &
  PGIDS+=($!)
}
cleanup() {
  echo "--- 收摊 ---"
  for pg in "${PGIDS[@]:-}"; do kill -TERM -- "-$pg" 2>/dev/null || true; done
  sleep 3
  for pg in "${PGIDS[@]:-}"; do kill -KILL -- "-$pg" 2>/dev/null || true; done
}
trap cleanup EXIT

[ -f "$MAP" ] || { echo "[err] 地图不存在: $MAP"; exit 1; }
[ -f "$URDF" ] || { echo "[err] URDF 不存在: $URDF"; exit 1; }

echo "=== 1/5 起 robot_state_publisher + 静态 odom→base_link ==="
# ⚠️ URDF 必须经 --params-file 传，**不能**用 `-p robot_description:=$(cat ...)`：
#    多行 URDF 会让 rcl 的全局参数解析失败（Failed to parse global arguments →
#    RCLInvalidROSArgsError），robot_state_publisher 直接 Aborted，
#    现象是 laser_link 这个 frame 在 TF 里根本不存在。
{
  echo "robot_state_publisher:"
  echo "  ros__parameters:"
  echo "    robot_description: |"
  sed 's/^/      /' "$URDF"
} > "$OUT/rsp_params.yaml"
spawn rsp ros2 run robot_state_publisher robot_state_publisher \
  --ros-args --params-file "$OUT/rsp_params.yaml"
spawn static_tf ros2 run tf2_ros static_transform_publisher \
  --x 0 --y 0 --z 0 --roll 0 --pitch 0 --yaw 0 \
  --frame-id odom --child-frame-id base_link
sleep 4

echo "=== 2/5 起雷达（含 scan_filter） ==="
spawn lidar ros2 launch robot_bringup lidar.launch.py
sleep 12

echo "=== 3/5 起 map_server + lidar_loc ==="
spawn lidar_loc ros2 launch robot_bringup lidar_loc.launch.py map:="$MAP" rviz:=false
sleep 15

echo "=== 4/5 给初始位姿（故意给错 ~3m：真值未知，看护栏是否拦住跑飞） ==="
timeout 10 ros2 topic pub --once /initialpose \
  geometry_msgs/msg/PoseWithCovarianceStamped \
  "{header: {frame_id: map}, pose: {pose: {position: {x: 0.0, y: 0.0, z: 0.0}, \
orientation: {x: 0.0, y: 0.0, z: 0.0, w: 1.0}}}}" > "$OUT/pub.log" 2>&1 || true
sleep 10

echo "=== 5/5 观测 ==="
FAIL=0

if grep -q "首次加载地图" "$OUT/lidar_loc.log"; then
  echo "[PASS] lidar_loc 收到了 /map（TRANSIENT_LOCAL 生效）"
  grep -m1 "首次加载地图" "$OUT/lidar_loc.log" | sed 's/^/       /'
else
  echo "[FAIL] lidar_loc 没收到 /map"; FAIL=1
fi

if grep -q "scan_filter: /scan" "$OUT/lidar.log"; then
  echo "[PASS] scan_filter 已启动"
  grep -m1 "scan_filter: /scan" "$OUT/lidar.log" | sed 's/^/       /'
else
  echo "[FAIL] scan_filter 没起来"; FAIL=1
fi

# 注意：这些命令都要用 `timeout ... || true`，别拿退出码判断 ——
# timeout 到点杀进程返回 124，用 && 串会直接把"其实成功"判成失败。
timeout 8 ros2 topic hz /scan_filtered > "$OUT/scan_hz.log" 2>&1 || true
if grep -q "average rate" "$OUT/scan_hz.log"; then
  echo "[PASS] /scan_filtered 有数据: $(grep -m1 'average rate' "$OUT/scan_hz.log")"
else
  echo "[WARN] /scan_filtered 没测到频率（雷达未出数据时属正常）"
fi

timeout 10 ros2 topic echo /amcl_pose --once > "$OUT/amcl_pose.log" 2>&1 || true
if grep -q "position" "$OUT/amcl_pose.log"; then
  echo "[PASS] /amcl_pose 有输出"
  grep -E "^ +[xyz]: " "$OUT/amcl_pose.log" | head -3 | sed 's/^/       /'
else
  echo "[FAIL] /amcl_pose 无输出"; FAIL=1
fi

timeout 8 ros2 run tf2_ros tf2_echo map odom > "$OUT/tf.log" 2>&1 || true
if grep -q "Translation" "$OUT/tf.log"; then
  echo "[PASS] map→odom TF 存在"
  grep -m1 -A1 "Translation" "$OUT/tf.log" | sed 's/^/       /'
else
  echo "[FAIL] map→omd TF 不存在"; FAIL=1
fi

# 护栏断言：初始位姿给错（(0,0,0) 而车实际不在原点）时，解必须被拒，
# 位姿应停在初始位姿附近，而不是"每帧都有改进"地一路漂走。
PX=$(grep -m1 -E "^ +x: " "$OUT/amcl_pose.log" 2>/dev/null | sed 's/.*x: *//')
PY=$(grep -m1 -E "^ +y: " "$OUT/amcl_pose.log" 2>/dev/null | sed 's/.*y: *//')
if [ -n "$PX" ] && [ -n "$PY" ] \
   && awk -v x="$PX" -v y="$PY" 'BEGIN{exit !((x<0? -x:x)<=0.5 && (y<0? -y:y)<=0.5)}'; then
  echo "[PASS] 错种子下位姿未跑飞（护栏生效）：($PX, $PY)"
elif grep -q "偏离过大" "$OUT/lidar_loc.log"; then
  echo "[PASS] 护栏已拒绝超限解（日志有'偏离过大'）"
else
  echo "[WARN] 没观察到护栏生效，也没看到拒绝日志；核对 lidar_loc.log"
fi

echo
echo "日志目录: $OUT"
if grep -qi "Traceback" "$OUT/lidar_loc.log"; then
  echo "--- lidar_loc 日志里的异常 ---"
  grep -i -m5 -A3 "Traceback" "$OUT/lidar_loc.log" | sed 's/^/  /'
fi
echo "结论: $([ "$FAIL" -eq 0 ] && echo '冒烟通过' || echo '有失败项，见上')"
exit "$FAIL"
