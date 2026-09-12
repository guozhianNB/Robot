# -*- coding: utf-8 -*-
"""无头验证 robot_actions 的到位判定几何逻辑（不 import rclpy，直接复制关键公式）。"""
import math


def _normalize_angle(a):
    while a > math.pi:
        a -= 2.0 * math.pi
    while a <= -math.pi:
        a += 2.0 * math.pi
    return a


def local_projection(x, y, x0, y0, yaw0):
    """车体位姿相对起点在起点坐标系下的投影。"""
    dx, dy = x - x0, y - y0
    return (dx * math.cos(yaw0) + dy * math.sin(yaw0),
            -dx * math.sin(yaw0) + dy * math.cos(yaw0))


# 1) 角度归一化
assert abs(_normalize_angle(3.5) - (3.5 - 2 * math.pi)) < 1e-9  # 200.5° → -159.5°
assert abs(_normalize_angle(-3.5) - (-3.5 + 2*math.pi)) < 1e-9
assert abs(_normalize_angle(0.1) - 0.1) < 1e-9
assert abs(_normalize_angle(math.pi) - math.pi) < 1e-9   # 边界: pi 保留
assert abs(_normalize_angle(-math.pi - 0.01) - (math.pi - 0.01)) < 1e-9

# 2) 直行 forward：起点朝向 0.3rad，沿局部 x 前移 0.5m（车头方向位移）
yaw0, d = 0.3, 0.5
x0, y0 = 1.0, 2.0
x1 = x0 + d * math.cos(yaw0)
y1 = y0 + d * math.sin(yaw0)
lx, ly = local_projection(x1, y1, x0, y0, yaw0)
assert abs(lx - d) < 1e-6 and abs(ly) < 1e-6, (lx, ly)

# 3) 横移 left：局部 y+（正=左），车身朝向无关
x2 = x0 - d * math.sin(yaw0)   # 左移 → 全局坐标 x 减
y2 = y0 + d * math.cos(yaw0)
lx2, ly2 = local_projection(x2, y2, x0, y0, yaw0)
assert abs(ly2 - d) < 1e-6 and abs(lx2) < 1e-6, (lx2, ly2)

# 4) back/right 反向
lx3, ly3 = local_projection(x0 - d*math.cos(yaw0), y0 - d*math.sin(yaw0), x0, y0, yaw0)
assert lx3 < 0
lx4, ly4 = local_projection(x0 + d*math.sin(yaw0), y0 - d*math.cos(yaw0), x0, y0, yaw0)
assert ly4 < 0

# 5) 转弯到位：起点 yaw0，目标转 +90°；车实际已转到 yaw0+90° → d_yaw 应 ≈ +90°（达标）
for yaw0, target, actual_yaw in [
    (0.7,  math.pi / 2, 0.7 + math.pi / 2),          # +90°
    (-0.4, math.pi / 2, -0.4 + math.pi / 2),         # +90°（跨 0 附近）
    (0.7, -math.pi / 2, 0.7 - math.pi / 2),          # -90°
    (2.8,  math.pi / 2, 2.8 + math.pi / 2),          # +90°（总角 > π，需包装）
    (-2.8, -math.pi / 2, -2.8 - math.pi / 2),        # -90°（总角 < -π，需包装）
]:
    d_yaw = _normalize_angle(actual_yaw - yaw0)
    # 达标判据（与 robot_actions._turn_until 一致）：d_yaw 与 target 同向且幅度达目标-容差
    sign = 1.0 if target >= 0 else -1.0
    tol = 0.035
    ok = (d_yaw * sign) >= (abs(target) - tol) or abs(d_yaw - target) < tol
    assert ok, (yaw0, target, actual_yaw, d_yaw)
    # 且没有转过目标太多（防止多转一圈也算达标）
    assert (d_yaw * sign) <= abs(target) + 0.2, (yaw0, target, actual_yaw, d_yaw)

print("robot_actions 几何逻辑断言全部通过")
