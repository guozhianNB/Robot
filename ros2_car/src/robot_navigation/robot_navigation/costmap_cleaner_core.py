# -*- coding: utf-8 -*-
"""清代价地图的纯逻辑（零 ROS 依赖，便于离线单测）。

上游 ROS1 的 `costmap_cleaner` 只调一个 `/move_base/clear_costmaps`；
ROS2 Nav2 没有这个总服务，必须分别清 global 与 local 两个代价地图。
"""
__all__ = ["DEFAULT_COSTMAP_NAMES", "clear_service_names", "missing_services"]

# nav2_bringup 里这两个代价地图不是独立节点：local 挂在 controller_server、
# global 挂在 planner_server 内部（Costmap2DROS 子节点），命名空间即自身名字。
DEFAULT_COSTMAP_NAMES = ("global_costmap", "local_costmap")


def clear_service_names(costmap_names=DEFAULT_COSTMAP_NAMES):
    """把代价地图名映射成 Nav2 的"整张清空"服务名。

    服务名来自 nav2_costmap_2d/src/clear_costmap_service.cpp（humble 分支）::

        node->create_service<ClearEntirely>("clear_entirely_" + costmap_.getName(), ...)

    `getName()` 返回的就是 "global_costmap" / "local_costmap"（**本身已带 costmap**），
    服务又是相对名、解析到节点自己的命名空间，故完整名是::

        /<costmap 名>/clear_entirely_<costmap 名>

    ⚠️ 不是 `/global_costmap/clear_entirely_global_costmap_costmap`
    —— 多写一个 `_costmap` 是这里最经典的错法，有测试钉死。
    """
    return ["/%s/clear_entirely_%s" % (name, name) for name in costmap_names]


def missing_services(expected, available):
    """expected 里哪些不在 available 中（用于启动自检，把静默失败变成显式告警）。"""
    have = set(available)
    return [name for name in expected if name not in have]
