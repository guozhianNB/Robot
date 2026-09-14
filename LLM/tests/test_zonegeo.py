# -*- coding: utf-8 -*-
"""区域几何判定测试（纯函数，无 DB、无 IO、无 ROS）。"""
from LLM import zonegeo


def test_point_inside_and_outside_square():
    poly = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    assert zonegeo.point_in_polygon(1.0, 1.0, poly) is True
    assert zonegeo.point_in_polygon(3.0, 1.0, poly) is False
    assert zonegeo.point_in_polygon(-1.0, -1.0, poly) is False


def test_concave_polygon_notch_is_outside():
    """L 形：缺口里那点必须判在外（射线法要能处理凹多边形）。"""
    poly = [[0.0, 0.0], [4.0, 0.0], [4.0, 1.0], [1.0, 1.0], [1.0, 4.0], [0.0, 4.0]]
    assert zonegeo.point_in_polygon(0.5, 3.0, poly) is True
    assert zonegeo.point_in_polygon(2.0, 2.0, poly) is False


def test_degenerate_input_is_false_never_raises():
    """点数 <3 / 空 / None 一律 False（fail-safe：判"不在"，不抛异常打断对话）。"""
    assert zonegeo.point_in_polygon(0.0, 0.0, [[0.0, 0.0], [1.0, 1.0]]) is False
    assert zonegeo.point_in_polygon(0.0, 0.0, []) is False
    assert zonegeo.point_in_polygon(0.0, 0.0, None) is False
    assert zonegeo.point_in_polygon(0.0, 0.0, 5) is False          # 不可迭代入参
    assert zonegeo.in_bbox(0.0, 0.0, 3.5) is False                 # 非列表入参（无 isinstance 守卫的旧实现会在 for 处抛 TypeError）
    assert zonegeo.point_in_polygon(0.0, 0.0, object()) is False   # 真值但不是可迭代序列
    big = "9" * 401                                                    # 合法 JSON number → Python 大整数 → float() 溢出
    assert zonegeo.zone_hit({"polygon_json": f"[[{big},0],[1,0],[1,1]]"}, 0.5, 0.1) is False


def test_broken_points_are_dropped_not_fatal():
    poly = [[0.0, 0.0], ["x", "y"], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]
    assert zonegeo.point_in_polygon(1.0, 1.0, poly) is True


def test_rect_shape_uses_bbox():
    zone = {"shape": "rect", "polygon": [[0.0, 0.0], [4.0, 0.0], [4.0, 2.0], [0.0, 2.0]]}
    assert zonegeo.zone_hit(zone, 3.9, 1.9) is True
    assert zonegeo.zone_hit(zone, 5.0, 1.0) is False
    # 同一 shape 换成 polygon_json 字符串形态（缓存表里存的就是字符串）
    zone_json = {"shape": "rect", "polygon_json": "[[0.0, 0.0], [4.0, 0.0], [4.0, 2.0], [0.0, 2.0]]"}
    assert zonegeo.zone_hit(zone_json, 3.9, 1.9) is True
    l_shape = {"shape": "rect",
               "polygon": [[0.0, 0.0], [4.0, 0.0], [4.0, 1.0], [1.0, 1.0], [1.0, 4.0], [0.0, 4.0]]}
    assert zonegeo.zone_hit(l_shape, 2.0, 2.0) is True       # 在 bbox 内、在多边形外 → rect 语义取 True
    assert zonegeo.point_in_polygon(2.0, 2.0, l_shape["polygon"]) is False   # 同一形状按多边形语义是 False


def test_zone_hit_accepts_cache_row_with_polygon_json_string():
    """db.list_zones 会给 polygon（已解析），但表里存的是字符串；两种都得能吃。"""
    zone = {"shape": "polygon", "polygon_json": "[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]"}
    assert zonegeo.zone_hit(zone, 1.0, 1.0) is True
    assert zonegeo.zone_hit({"shape": "polygon", "polygon_json": "坏了"}, 1.0, 1.0) is False
    assert zonegeo.zone_hit(None, 1.0, 1.0) is False
    # shape 缺失 + 已解析 polygon 形态 → 默认按 polygon 处理
    zone_parsed = {"polygon": [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]}
    assert zonegeo.zone_hit(zone_parsed, 1.0, 1.0) is True
