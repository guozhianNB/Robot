#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按序激活 Nav2 生命周期节点，bt_navigator 放最后（慢板卡上修 bt_navigator 激活竞态）。

用法（先已把节点进程拉起、autostart 关掉，本脚本只做 configure+activate）:
    source /opt/ros/humble/setup.bash
    source /home/sunrise/Robot/ros2_car/install/setup.bash
    export ROS_LOG_DIR=/home/sunrise/Robot/.diag/roslog

    # ① 另开终端/后台把全套 Nav2 节点进程拉起（不含自动激活）:
    #   ros2 launch robot_bringup navigation.launch.py map:=<map.yaml> rviz:=true autostart:=false
    #   （该 launch 会把 map_server/amcl + controller/smoother/planner/behavior/bt_navigator/waypoint/velocity
    #     都起为 lifecycle 节点，但停在 unconfigured）

    # ② 跑本脚本激活:
    #   python3 /home/sunrise/Robot/ros2_car/tools/activate_nav.py

说明：nav2 1.1 的 bt_navigator 无论 plugins 如何都会无条件 activate navigate_through_poses 并预加载其 BT，
要求在它 1s 等待窗内 controller(follow_path)+planner(compute_path_through_poses)+behavior(backup/spin/wait)
动作服务都已就绪。因此必须先把 controller/planner/behavior 激活，等它们的动作服务出现后再 activate bt_navigator。
配套修复：behavior_server 的插件名必须是 "backup"（非 "back_up"），见 ROS2导航调试经验.md 四.4。
"""
import subprocess
import sys
import time

# 这些生命周期节点都要 configure+activate，顺序即依赖顺序（costmap 由其父节点自动激活）。
NODES = [
    'map_server',
    'amcl',
    'controller_server',
    'smoother_server',
    'planner_server',
    'behavior_server',
    'waypoint_follower',
    'velocity_smoother',
]
BT_NODE = 'bt_navigator'

# bt_navigator activate 前必须已就绪的动作服务（来自其无条件加载的 navigate_through_poses/navigate_to_pose BT）
BT_NEEDED_ACTIONS = ['/follow_path', '/compute_path_through_poses',
                     '/backup', '/spin', '/wait', '/compute_path_to_pose']


def sh(cmd, timeout=30):
    """在已 source 的 bash 里执行 ros2 命令，返回去尾空白文本。"""
    base = ("source /opt/ros/humble/setup.bash; "
            "source /home/sunrise/Robot/ros2_car/install/setup.bash; "
            "export ROS_LOG_DIR=/home/sunrise/Robot/.diag/roslog; ")
    try:
        r = subprocess.run(['bash', '-lc', base + cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 'TIMEOUT'


def wait_node(n, tries=20, delay=1.5):
    full = '/' + n
    for i in range(tries):
        out = sh("ros2 node list 2>/dev/null")
        if full in out.split():
            return True
        time.sleep(delay)
    return False


def transition(n, tr):
    return sh(f"ros2 lifecycle set /{n} {tr}")


def get_actions():
    return sh("ros2 action list 2>/dev/null").split()


def main():
    print("== 等待节点被发现 ==")
    missing = [n for n in NODES + [BT_NODE] if not wait_node(n)]
    if missing:
        print("超时未见节点(可能没拉起/autostart 没关/名字不同):", missing)
        print("先把节点进程拉起来（见脚本头注释），再重跑。")
        return 1

    print("== configure 所有节点 ==")
    for n in NODES + [BT_NODE]:
        print(f"  configure {n}: {transition(n, 'configure')}")
        time.sleep(0.3)

    print("== 按依赖顺序 activate（bt_navigator 最后） ==")
    for n in NODES:
        r = transition(n, 'activate')
        print(f"  activate {n}: {r}")
        time.sleep(1.5)

    # 等 bt_navigator 需要的动作服务都出现
    print("== 等待 bt_navigator 依赖动作就绪 ==")
    for tries in range(30):
        acts = set(get_actions())
        have = [a for a in BT_NEEDED_ACTIONS if a in acts]
        if len(have) == len(BT_NEEDED_ACTIONS):
            print("  所需动作服务均已就绪:", sorted(BT_NEEDED_ACTIONS))
            break
        time.sleep(1.5)
    else:
        print("  警告: 部分动作服务未就绪:", [a for a in BT_NEEDED_ACTIONS if a not in acts])
        # 仍尝试激活 bt，看报错

    print(f"== activate {BT_NODE} (最后) ==")
    print(f"  activate {BT_NODE}: {transition(BT_NODE, 'activate')}")
    time.sleep(2)

    print("== 最终状态 ==")
    allok = True
    for n in NODES + [BT_NODE]:
        st = sh(f"ros2 lifecycle get /{n}").splitlines()
        line = st[0] if st else '?'
        print(f"  {n:22s} => {line}")
        if 'active' not in line:
            allok = False
    if allok:
        print("\n全部节点 active，bt_navigator 已就绪，/navigate_to_pose 可用。")
        print("rviz 里用 'Nav2 Goal'(GoalTool) 发目标，或命令行发：")
        print("  ros2 run robot_navigation navigate_to_pose --x 1.0 --y 1.0")
    else:
        print("\n仍有节点未 active，请看上方 bt_navigator 日志排查。")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
