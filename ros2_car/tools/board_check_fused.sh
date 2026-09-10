#!/bin/bash
# 双源融合（odom_source:=fused）板卡端到端验收：清场 → 起 launch → 采集 → 收场
# 在板卡上跑：bash tools/board_check_fused.sh     结果同时写到 /tmp/fused_report.txt
cd "$HOME/Robot/ros2_car" || exit 1
source /opt/ros/humble/setup.bash
source "$HOME/ros2/yahboomcar_ws/install/setup.bash"
source install/setup.bash
export ROS_LOG_DIR="$HOME/Robot/.diag/roslog"

killall_ros() {
  pkill -f '[r]obot_base.launch.py'; pkill -f '[c]hassis_driver'
  pkill -f '[e]kf_node'; pkill -f '[r]f2o'; pkill -f '[o]dom_relay'; pkill -f '[y]dlidar'
  # 之前跑过的 launch 会留下孤儿 rsp / tf 监听，名字相同会让 node list 出现重复
  pkill -f '[r]obot_state_publisher'; pkill -f '[t]ransform_listener'
}

echo "=== 清场（含重启 ROS 守护进程，避免图表缓存残留节点）==="
killall_ros
ros2 daemon stop >/dev/null 2>&1
sleep 2
killall_ros
sleep 1
pgrep -f '[c]hassis_driver|[r]f2o|[e]kf_node|[y]dlidar' && echo "⚠ 仍有残留" || echo "  端口已空"

setsid ros2 launch robot_bringup robot_base.launch.py odom_source:=fused \
  > /tmp/fused_check.log 2>&1 < /dev/null &
sleep 25

{
  echo "### 节点"
  ros2 node list | sort
  echo
  echo "### 频率"
  for t in /odom /odom_laser_raw /odom_laser /odom_filtered; do
    printf '%-18s %s\n' "$t" "$(timeout 6 ros2 topic hz "$t" 2>&1 | grep -m1 average)"
  done
  echo
  echo "### TF 实际发布频率（odom→base_link 应 ≈20Hz 且只有一段）"
  timeout 10 python3 tools/check_tf_authors.py 6 2>&1 | tail -12
  echo
  echo "### /odom_filtered 一帧（看位姿是否有限值、协方差是否非 0）"
  timeout 5 ros2 topic echo /odom_filtered --once 2>&1 | sed -n '1,20p'
  echo
  echo "### EKF 日志中的 error/nan/critical/died"
  grep -Ei 'error|nan|critical|died' /tmp/fused_check.log | tail -6 || echo "  (无)"
} > /tmp/fused_report.txt 2>&1

killall_ros
echo "报告见 /tmp/fused_report.txt"
cat /tmp/fused_report.txt
