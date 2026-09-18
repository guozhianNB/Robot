#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""基于 CarLink/CarNav 的七工具车控 MCP 服务端。"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__:
    from .car_link import CarLink
    from .car_nav import CarNav
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from LLM.car_mcp.car_link import CarLink
    from LLM.car_mcp.car_nav import CarNav

try:
    from mcp.server.mcpserver import MCPServer
    from pydantic import SkipValidation
except Exception as exc:
    MCPServer = None
    _MCP_IMPORT_ERROR = str(exc)
else:
    _MCP_IMPORT_ERROR = ""

# Keep the numeric JSON schema but let handlers reject raw bool/string values.
# Pydantic's usual float coercion would otherwise turn True into a valid metre.
RawNumber = SkipValidation[float] if MCPServer is not None else float

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.environ.get("CAR_MCP_LOG", os.path.join(_HERE, "car_mcp.log"))
_DIRECTIONS = {"forward", "back", "left", "right"}
_START_LOCK = threading.Lock()
_STARTED_LINKS = weakref.WeakSet()


def _log(message: str) -> None:
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def _json(payload: dict) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"结果序列化失败: {exc}"}, ensure_ascii=False)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _error(message: str, **extra) -> str:
    return _json({"ok": False, "status": "rejected", "error": str(message), **extra})


def _ready(link, *, require_nav: bool = False):
    try:
        _start_state(link)
        result = link.readiness()
    except Exception as exc:
        return None, _error(f"读取车体就绪状态失败: {exc}", status="error")
    if not isinstance(result, dict) or result.get("ok") is not True:
        detail = result.get("error") if isinstance(result, dict) else "返回格式无效"
        return None, _error(f"车体未就绪: {detail}", status="error")
    if result.get("ready") is not True or result.get("exec_state") != "idle":
        return None, _error("车体未就绪或执行器忙", exec_state=result.get("exec_state"))
    if require_nav and result.get("nav_available") is not True:
        return None, _error("导航服务不可用", status="error", hint="请确认导航已启动并重试")
    return result, None


def _start_state(link):
    # Connections start on use, keeping tools registered while the board is offline.
    # Never re-enter start during an action: it acquires the long-lived ctrl I/O lock.
    # After initialization each channel, including the state loop, reconnects itself.
    starter = getattr(link, "start", None)
    if starter is not None:
        with _START_LOCK:
            if link in _STARTED_LINKS or getattr(link, "_state_thread", None) is not None:
                return None
            result = starter()
            _STARTED_LINKS.add(link)
            return result


def _worker(link, action: str, request: dict, task_id: int) -> None:
    try:
        result = link.call_action(action, request, task_id=task_id)
        ok = isinstance(result, dict) and result.get("ok") is True
        uncertain = isinstance(result, dict) and result.get("uncertain") is True
        detail = result if isinstance(result, dict) else str(result)
    except Exception as exc:
        ok, uncertain, detail = False, True, str(exc)
    try:
        if ok:
            link.finish_task(task_id, True, detail)
        elif uncertain:
            link.mark_uncertain(task_id, detail)
        else:
            link.finish_task(task_id, False, detail)
    except Exception as exc:
        _log(f"finish_task failed task_id={task_id}: {exc}")


def _start(link, executor, action: str, target: dict, request: dict, expected_state: str,
           summary: str, warnings=None, *, require_nav: bool = False,
           transport_action: str | None = None) -> str:
    _, failure = _ready(link, require_nav=require_nav)
    if failure:
        return failure
    try:
        task = link.begin_task(action, target, expected_state)
    except Exception as exc:
        return _error(f"登记车控任务失败: {exc}", status="error")
    if not isinstance(task, dict) or task.get("ok") is not True:
        return _json(task if isinstance(task, dict) else {"ok": False, "error": "任务被拒绝"})
    task_id = task.get("task_id")
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        return _error("车控任务标识无效")
    try:
        executor.submit(_worker, link, transport_action or action, request, task_id)
    except Exception as exc:
        try:
            link.finish_task(task_id, False, str(exc))
        except Exception:
            pass
        return _error(f"后台执行器不可用: {exc}", status="error")
    return _json({"ok": True, "status": "started", "action": action, "summary": summary,
                  "exec_state": expected_state, "target": target, "warnings": list(warnings or [])})


def _move(link, executor, direction, distance_m) -> str:
    try:
        if not isinstance(direction, str) or direction not in _DIRECTIONS:
            return _error("direction 必须是 forward/back/left/right")
        if not _number(distance_m) or not 0 < float(distance_m) <= 5:
            return _error("distance_m 必须是有限数字且满足 0 < distance_m <= 5")
        distance = float(distance_m)
        target = {"direction": direction, "distance_m": distance}
        return _start(link, executor, f"move_{direction}", target, target, "moving", f"移动 {direction} {distance:g} 米",
                      transport_action="move")
    except Exception as exc:
        return _error(f"robot_move 异常: {exc}")


def _turn(link, executor, angle_deg) -> str:
    try:
        if not _number(angle_deg) or not 0 < abs(float(angle_deg)) <= 360:
            return _error("angle_deg 必须是有限数字且满足 0 < abs(angle_deg) <= 360")
        angle = float(angle_deg)
        target = {"angle_deg": angle}
        return _start(link, executor, "turn", target, target, "moving", f"旋转 {angle:g} 度")
    except Exception as exc:
        return _error(f"robot_turn 异常: {exc}")


def _goto(link, nav, executor, label: str, *args) -> str:
    try:
        resolver = getattr(nav, f"resolve_{label}")
        resolved = resolver(*args)
        if not isinstance(resolved, dict) or resolved.get("ok") is not True:
            return _json(resolved if isinstance(resolved, dict) else {"ok": False, "error": "目标解析失败"})
        target = resolved.get("target")
        if not isinstance(target, dict):
            return _error("目标解析结果缺少坐标")
        x, y, yaw = target.get("x"), target.get("y"), target.get("yaw_deg", 0)
        if not all(_number(value) for value in (x, y, yaw)):
            return _error("目标坐标不是有限数字")
        request = {"place": "", "x": float(x), "y": float(y), "theta": float(yaw)}
        return _start(link, executor, f"goto_{label}", target, request, "navigating",
                      f"前往 {resolved.get('summary_target') or label}", resolved.get("warnings"),
                      require_nav=True, transport_action="navigate_to")
    except Exception as exc:
        return _error(f"robot_goto_{label} 异常: {exc}")


def _goto_point(link, nav, executor, x, y, yaw_deg=0) -> str:
    return _goto(link, nav, executor, "point", x, y, yaw_deg)


def _goto_zone(link, nav, executor, zone) -> str:
    return _goto(link, nav, executor, "zone", zone)


def _goto_place(link, nav, executor, place) -> str:
    return _goto(link, nav, executor, "place", place)


def _stop(link) -> str:
    try:
        result = link.stop()
        return _json(result if isinstance(result, dict) else {"ok": bool(result), "result": result})
    except Exception as exc:
        return _error(f"robot_stop 异常: {exc}")


def _status(link, nav=None) -> str:
    try:
        started = _start_state(link)
        result = link.snapshot()
        if isinstance(result, dict) and (result.get("available") is False or
                                        result.get("state_fresh") is False):
            reason = result.get("last_error") or (started or {}).get("error") or "执行状态未知或已过期，请检查车体连接"
            result = {**result, "ok": True, "status": "unavailable", "reason": reason}
        if not isinstance(result, dict):
            result = {"ok": True, "status": "unavailable", "reason": "状态格式无效"}
        if nav is not None:
            try:
                context = nav.status_context(result.get("pose"))
                map_reason = context.get("reason") if isinstance(context, dict) else "地图状态格式无效"
                if result.get("reason") and map_reason:
                    context = {**context, "map_reason": map_reason, "reason": result["reason"]}
                result = {**result, **context}
            except Exception as exc:
                reason = str(exc)
                result = {**result, "map": None, "zone": None,
                          "reason": result.get("reason") or reason,
                          "map_reason": reason,
                          "warnings": list(result.get("warnings") or [])}
        return _json(result)
    except Exception as exc:
        return _json({"ok": True, "status": "unavailable", "reason": str(exc)})


def _close_resources(link, executor) -> None:
    try:
        link.close()
    except Exception as exc:
        _log(f"link.close failed: {exc}")
    try:
        executor.shutdown(wait=True)
    except Exception as exc:
        _log(f"executor.shutdown failed: {exc}")


def build_server(*, link=None, nav=None, executor=None):
    if MCPServer is None:
        return None
    link = link or CarLink()
    nav = nav or CarNav()
    executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="car-mcp")
    server = MCPServer("robot-car")

    @server.tool()
    def robot_move(direction: str, distance_m: RawNumber) -> str:
        """移动小车，到位自动停。direction 仅 forward/back/left/right；0 < distance_m <= 5。

        返回 JSON 字符串；started 仅表示受理，完成情况查 robot_status。失败必须如实说明，不能假装成功。
        """
        return _move(link, executor, direction, distance_m)

    @server.tool()
    def robot_turn(angle_deg: RawNumber) -> str:
        """原地旋转，正数左转、负数右转；0 < abs(angle_deg) <= 360，到位自动停。

        返回 JSON 字符串；started 仅表示受理，完成情况查 robot_status；失败不得假装成功。
        """
        return _turn(link, executor, angle_deg)

    @server.tool()
    def robot_goto_point(x: RawNumber, y: RawNumber, yaw_deg: RawNumber = 0) -> str:
        """前往明确的 map 系米坐标点，yaw_deg 为角度；禁止编造坐标，须通过地图与导航校验。

        返回 JSON 字符串；started 仅表示受理，warnings 须如实转述；失败不得假装成功。
        """
        return _goto_point(link, nav, executor, x, y, yaw_deg)

    @server.tool()
    def robot_goto_zone(zone: str) -> str:
        """前往唯一匹配区域的安全停靠点；无显式停靠点时仅回退区域内已标地点，禁止编造坐标。

        返回 JSON 字符串；started 仅表示受理，warnings 须如实转述；失败不得假装成功。
        """
        return _goto_zone(link, nav, executor, zone)

    @server.tool()
    def robot_goto_place(place: str) -> str:
        """按地点名或别名，前往地图中唯一匹配的安全地点；禁止编造名称或坐标。

        返回 JSON 字符串；started 仅表示受理，完成情况查 robot_status；失败不得假装成功。
        """
        return _goto_place(link, nav, executor, place)

    @server.tool()
    def robot_stop() -> str:
        """老人喊停或出现异常时立即急停，不受忙状态或地图、导航可用性影响。

        返回 JSON 字符串；成功仅表示已发出急停消息，不能声称板卡已确认停车；失败如实说明。
        """
        return _stop(link)

    @server.tool()
    def robot_status() -> str:
        """查询执行状态、缓存位姿、当前与上一次任务结果。位姿可能滞后，不能当实时厘米级定位。

        返回 JSON 字符串；连接或状态不可用时 ok:true/status:unavailable，不代表车已就绪。
        """
        return _status(link, nav)

    server._car_resources = (link, executor)
    return server


def main() -> None:
    if MCPServer is None:
        _log(f"python-mcp 不可用: {_MCP_IMPORT_ERROR}")
        sys.stderr.write(f"python-mcp 未安装，robot-car MCP 无法启动: {_MCP_IMPORT_ERROR}\n")
        raise SystemExit(2)
    link = CarLink()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="car-mcp")
    try:
        server = build_server(link=link, nav=CarNav(), executor=executor)
        if server is None:
            raise SystemExit(2)
        _log("robot-car MCP server 就绪")
        server.run(transport="stdio")
    finally:
        _close_resources(link, executor)
        _log("robot-car MCP server 退出")


if __name__ == "__main__":
    main()
