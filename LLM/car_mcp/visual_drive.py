#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""VM 车控可视化测试：让模拟底盘跑一段动作，描出小车运动轨迹/朝向并存 PNG。

【跑法】（本机先 source /opt/ros/jazzy/setup.bash；此脚本会自己拉起模拟底盘并做动作）
    python3 LLM/car_mcp/visual_drive.py                    # 输出到 LLM/car_mcp/drive_traj_POSEX.png
    python3 LLM/car_mcp/visual_drive.py --out /tmp/t.png --moves "forward 0.4 back 0.2 left 0.3 turn 90 forward 0.3 turn -60"
说明：
  - matplotlib 用 Agg 后端 headless 出 PNG，无需显示器窗口；
  - 轨迹点来自 odom（x,y），每点带小车朝向箭头(yaw)；
  - 同时打印真机语义动作的返回值，确认"到位自动停"。
"""
import argparse
import math
import os
import subprocess
import sys
import threading
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402

# 让中文标题/图例正常显示（注册系统 CJK 字体）
from matplotlib import font_manager       # noqa: E402
_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if os.path.exists(_CJK):
    try:
        font_manager.fontManager.addfont(_CJK)
        plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "Noto Sans CJK JP",
                                           "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass

import rclpy                              # noqa: E402
from rclpy.node import Node               # noqa: E402
from nav_msgs.msg import Odometry         # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from car_controller import CarController  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- odom 记录
class Tracker(Node):
    def __init__(self):
        super().__init__("drive_tracker")
        self.points = []                     # (x, y, yaw)
        self._sub = self.create_subscription(Odometry, "/odom", self._cb, 10)

    def _cb(self, msg):
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                         1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        self.points.append((p.x, p.y, yaw))


def main():
    ap = argparse.ArgumentParser(description="高层车控可视化：默认按 --moves 走；或 --loop 循环【走 step 米→转90°】N 段")
    ap.add_argument("--out", default=os.path.join(HERE, "drive_traj_pose.png"))
    ap.add_argument("--moves", default="",
                    help="自定义动作串，如 \"forward 1 turn 90 ...\"")
    ap.add_argument("--loop", type=int, default=0,
                    help="循环段数：每段【前进(默认1m)→原地左转90°】，适合画方形闭合轨迹")
    ap.add_argument("--step", type=float, default=1.0,
                    help="每段前进米数（配合 --loop）")
    a = ap.parse_args()

    def _flt(tok, default):
        try:
            return float(tok)
        except (TypeError, ValueError):
            return default

    # ---------- 构建动作序列 ----------
    if a.loop and a.loop > 0:
        # 每段：forward <step> ，若未到最后一段再 turn 90，让轨迹走出规则折角
        actions = []
        for n in range(a.loop):
            actions.append(("forward", a.step))
            if True:
                actions.append(("turn", 90.0))      # 每走一步都转90°（含最后一段，便于闭环/看清转弯）
        seq_label = f"循环 {a.loop} 段：每段前进 {a.step}m + 原地左转90°"
    else:
        tokens = (a.moves or "forward 0.4 back 0.2 left 0.3 turn 90 forward 0.3 turn -60").split()
        actions = []
        i = 0
        while i < len(tokens):
            act = tokens[i]
            try:
                _ = float(act)
                i += 1
                continue
            except ValueError:
                pass
            if i + 1 < len(tokens):
                try:
                    val = float(tokens[i + 1])
                    actions.append((act, val))
                    i += 2
                    continue
                except ValueError:
                    pass
            actions.append((act, None))
            i += 1
        seq_label = " ".join(f"{k}{(' '+str(v)) if v is not None else ''}" for k, v in actions)

    print("动作序列:", actions)
    print("说明:", seq_label)

    # ---------- 1) 起模拟底盘 ----------
    sim_run = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "odom_sim_driver.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    # ---------- 2) 记录器 + 控制器 ----------
    rclpy.init()
    tracker = Tracker()
    threading.Thread(target=lambda: rclpy.spin(tracker), daemon=True).start()

    ctrl = CarController()
    if not ctrl.start(timeout_s=8):
        print("[ERR] 控制器启动失败")
        sys.exit(1)

    # ---------- 3) 依次执行动作 ----------
    results = []
    for i, (act, val) in enumerate(actions):
        if act in ("forward", "back", "left", "right"):
            r = ctrl.robot_move(act, _flt(val, 1.0))
        elif act == "turn":
            r = ctrl.robot_turn(_flt(val, 90))
        elif act == "stop":
            r = ctrl.robot_stop()
        else:
            r = {"ok": False, "error": f"未知动作 {act}"}
        results.append((act, r))
        tag = f"[{i + 1}/{len(actions)}]"
        print(f"{tag} {act} {val or ''} → {r}")
        time.sleep(0.4)                       # 停留让轨迹记录稳定

    time.sleep(0.8)                           # 确保停在位、轨迹平滑收尾
    # ---------- 4) 画图 ----------
    pts = list(tracker.points)
    if not pts:
        print("[WARN] 没采到 odom 点？")
    plt.figure(figsize=(8, 8))
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    plt.plot(xs, ys, "-", color="#4c9be8", lw=2.5, alpha=0.9, label="轨迹(odom)")
    # 起点/终点
    plt.scatter([xs[0]], [ys[0]], color="green", s=80, zorder=5, label="起点")
    plt.scatter([xs[-1]], [ys[-1]], color="red", s=120, zorder=5, label="终点")
    # 每若干点画朝向箭头
    step = max(1, len(pts) // 40)
    for x, y, yaw in pts[::step]:
        dx = math.cos(yaw) * 0.1; dy = math.sin(yaw) * 0.1
        plt.arrow(x, y, dx, dy, color="darkorange", width=0.008,
                  head_width=0.05, length_includes_head=True, alpha=0.7)
    plt.axis("equal"); plt.grid(alpha=0.3)
    plt.title("LLM 车控运动测试（模拟底盘 /odom）\n" + seq_label)
    plt.xlabel("x (m)"); plt.ylabel("y (m)")
    plt.legend(loc="upper left")
    plt.tight_layout()
    plt.savefig(a.out, dpi=120)
    print("\n已保存轨迹图:", a.out)
    total = math.hypot(xs[-1] - xs[0], ys[-1] - ys[0])
    print("起点→终点直线位移 ≈ %.3f m" % total)

    # ---------- 5) 清理 ----------
    ctrl.shutdown()
    if rclpy.ok():
        rclpy.shutdown()
    sim_run.terminate()


if __name__ == "__main__":
    main()
