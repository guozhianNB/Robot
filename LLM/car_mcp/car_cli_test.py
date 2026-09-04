#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""VM 上对高层车控控制器做端到端自测（配合模拟底盘 odom_sim_driver.py）。

【跑法】（本机需先 source /opt/ros/jazzy/setup.bash；无需 python-mcp，只需 rclpy）
  终端 A（起"假底盘"，产生底盘数据 /odom）:
      python3 LLM/car_mcp/odom_sim_driver.py
  终端 B（跑本脚本，驱动它）:
      python3 LLM/car_mcp/car_cli_test.py move forward 0.5     # 前移0.5m自动停
      python3 LLM/car_mcp/car_cli_test.py turn 90               # 原地左转90°自动停
      python3 LLM/car_mcp/car_cli_test.py status                # 查位姿/是否在动
      python3 LLM/car_mcp/car_cli_test.py stop                  # 急停

验证点（“在 VM 上拿底盘数据测试”）：
  - robot_move 朝 forward 走 0.5m 后应自动停，返回 moved_m≈0.5，且 /odom 确有变化；
  - robot_turn 转 90° 后应自动停，返回 turned_deg≈90；
  - 中途可另开终端跑 car_cli_test.py --stop 立即急停。
"""
import argparse
import math
import sys
import time

sys.path.insert(0, ".")
# 本脚本常以 `python3 LLM/car_mcp/car_cli_test.py` 直接运行，
# sys.path[0] 是脚本所在目录 → 可当同目录兄弟模块导入。
from car_controller import CarController  # noqa: E402


def _wait_ready(c: CarController, tries: int = 20):
    if not c.start():
        print("[ERR] 控制器 ROS 启动失败")
        sys.exit(1)
    # 等首个 /odom（模拟底盘已在跑）到达，确保能判到位
    for _ in range(tries):
        x, y, yaw = c._read_pose()
        if math.hypot(x, y) > 1e-9 or abs(yaw) > 1e-9:
            return
        time.sleep(0.1)
    # 未等到也不阻塞，仅提示


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    pm = sub.add_parser("move")
    pm.add_argument("direction", choices=["forward", "back", "left", "right"])
    pm.add_argument("distance", type=float, default=0.5, nargs="?")

    pt = sub.add_parser("turn")
    pt.add_argument("angle", type=float)

    sub.add_parser("stop")
    sub.add_parser("status")

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return

    c = CarController()
    _wait_ready(c)
    try:
        if a.cmd == "move":
            r = c.robot_move(a.direction, float(a.distance))
            print(r)
        elif a.cmd == "turn":
            r = c.robot_turn(float(a.angle))
            print(r)
        elif a.cmd == "stop":
            r = c.robot_stop()
            print(r)
        elif a.cmd == "status":
            r = c.robot_status()
            print(r)
    finally:
        c.shutdown()


if __name__ == "__main__":
    main()
