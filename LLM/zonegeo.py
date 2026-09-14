# -*- coding: utf-8 -*-
r"""
区域几何判定（点在不在某个区域内）—— 纯 stdlib、无 IO、无外部依赖、不抛异常。

规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md §4.5
口径照抄前端既有实现 frontend/packages/mapeditor/src/lib/coords.ts:44 pointInPolygon()
（射线法），保证前后端对"点是否在区域内"给出一致答案：
  * shape == "polygon"：射线法（顶点顺序不限，凸凹多边形都行）；
  * shape == "rect"   ：用 polygon 的**外接矩形**判定（矩形区域只存 4 个角，容差更稳）；
  * 顶点数 < 3 / 数据损坏 → False —— fail-safe：宁可判"不在"，也不抛异常打断对话。
"""
import json


def _points(poly) -> list[tuple[float, float]]:
    """[[x, y], ...] → [(x, y), ...]；坏点直接丢弃（不抛异常）。"""
    out = []
    for pt in poly or []:
        try:
            out.append((float(pt[0]), float(pt[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


def point_in_polygon(x: float, y: float, poly) -> bool:
    """射线法：点 `(x, y)` 是否在多边形 `poly`（`[[x, y], ...]`，**米坐标**）内。"""
    pts = _points(poly)
    n = len(pts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_bbox(x: float, y: float, poly) -> bool:
    """点是否落在 `poly` 的外接矩形内（`shape='rect'` 的判定口径）。"""
    pts = _points(poly)
    if not pts:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys)


def _as_poly(zone: dict) -> list:
    """从区域行里取几何：优先已解析的 `polygon`，退回 `polygon_json` 字符串。"""
    poly = zone.get("polygon")
    if poly is None:
        poly = zone.get("polygon_json") or []
    if isinstance(poly, str):
        try:
            poly = json.loads(poly or "[]")
        except ValueError:
            poly = []
    return poly if isinstance(poly, list) else []


def zone_hit(zone: dict, x: float, y: float) -> bool:
    """点是否落在这个区域行里（`zone` = `db.list_zones()` 返回的行，含 `shape`/`polygon`）。"""
    if not zone:
        return False
    poly = _as_poly(zone)
    if str(zone.get("shape") or "polygon") == "rect":
        return in_bbox(x, y, poly)
    return point_in_polygon(x, y, poly)
