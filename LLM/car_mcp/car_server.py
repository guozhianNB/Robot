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


def _explain(detail: str, url: str = "") -> tuple[str, str]:
    """把裸异常翻译成"下一步该干什么"（车控规格 §十 的降级表）。返回 ``(message, hint)``。

    为什么要有它：原来把 `[Errno 111] Connection refused` 原样当"车体未就绪"报给模型，
    模型只能如实复述一句通信错误，人和模型都不知道该去查什么（2026-09-19 现场连踩两次）。
    分不清的错误一律原样返回 —— 绝不编造原因。
    """
    text = str(detail or "")
    low = text.lower()
    # 注意别把「缺少可选依赖 websocket-client」也算成连不上 —— 那句本身就说清了原因。
    if any(mark in low for mark in ("connection refused", "errno 111", "timed out", "timeout",
                                    "getaddrinfo", "connection reset", "disconnected")):
        return (f"连不上板卡 rosbridge（{url or '未配置 ROSBRIDGE_URL'}）：{text}",
                "车控桥大概没起 —— 板卡上执行 ~/tools/nav_screen.sh lat2"
                "（nav_screen.sh nav 会自动带起它）")
    if "does not exist" in low or "not found" in low:
        return (f"车控服务没起来：{text}",
                "确认小车处于导航模式（板卡上 robot_actions 节点是否在跑）")
    return text, ""


def _ready(link, *, require_nav: bool = False):
    try:
        _start_state(link)
        result = link.readiness()
    except Exception as exc:
        message, hint = _explain(f"读取车体就绪状态失败: {exc}", getattr(link, "url", ""))
        return None, _error(message, status="error", **({"hint": hint} if hint else {}))
    if not isinstance(result, dict) or result.get("ok") is not True:
        detail = result.get("error") if isinstance(result, dict) else "返回格式无效"
        message, hint = _explain(f"车体未就绪: {detail}", getattr(link, "url", ""))
        return None, _error(message, status="error", **({"hint": hint} if hint else {}))
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
        # 每次都探一次 readiness：状态查询要的是**此刻**的执行态与导航可用性，不是"上一次动作时"的。
        # 不探的话——后端刚起、模型又只问状态不问动作时，nav_available 会一直停在冷启动的 false，
        # 模型据此说"导航没起"（与 available 同类的假警报，2026-09-19）。readiness 只读、不动车。
        try:
            link.readiness()
        except Exception:
            pass
        result = link.snapshot()
        # 降级只看 state_fresh，**不看 available**：available 曾经是"四通道 socket 全在"，
        # 而后端比 rosbridge 早起时那一条 stop 通道永不重连 → 好链路被永久报成 unavailable
        # （2026-09-19 实测：模型据此说"链路没恢复"，其实 mobility 一路正常）。state_fresh 才
        # 真正回答"我现在还能不能看到车"。
        if isinstance(result, dict) and result.get("state_fresh") is False:
            reason = result.get("last_error") or (started or {}).get("error") or "执行状态未知或已过期，请检查车体连接"
            message, hint = _explain(reason, getattr(link, "url", ""))
            result = {**result, "ok": True, "status": "unavailable", "reason": message,
                      **({"hint": hint} if hint else {})}
        if not isinstance(result, dict):
            result = {"ok": True, "status": "unavailable", "reason": "状态格式无效"}
        if nav is not None:
            try:
                context = nav.status_context(result.get("pose"))
                map_reason = context.get("reason") if isinstance(context, dict) else "地图状态格式无效"
                if result.get("reason") and map_reason:
                    context = {**context, "map_reason": map_reason, "reason": result["reason"]}
                result = {**result, **context}
                pose = result.get("pose") or {}
                # 区域是拿"最后一次收到的位姿"算的：位姿过期或可疑（AMCL 未收敛的那种 (0,0,0)）时
                # 必须挑明，否则模型会把"你在平台A"当事实讲出来。
                if context.get("zone") and (result.get("pose_stale") or pose.get("suspect")):
                    why = "位姿还没收敛（疑似初始位姿未对齐）" if pose.get("suspect") else "位姿已过期"
                    result = {**result, "zone_from_stale_pose": True,
                              "warnings": list(result.get("warnings") or []) + [
                                  f"区域「{context['zone'].get('name')}」是用{why}的位姿算的，"
                                  "不是实时位置（AMCL 静止时不发位姿）"]}
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
        """查询执行状态、缓存位姿、当前与上一次任务结果。判断"到没到"看 last.ok，别看 arrived。

        返回 JSON 字符串，要点：`pose` 是**最后一次收到**的位姿，配合 `pose_stale`（true = 不能当
        实时位置用：没收到/过期/新鲜度未知）与 `pose.suspect`（疑似 AMCL 未收敛，如恰好停在原点）
        判断可信度 —— 两者为真时**不能**把坐标或区域当实时位置告诉用户；`zone_from_stale_pose`
        表示区域就是这么算出来的。判断"到没到"看 `last.ok`，别看 `arrived`（只在变化时发布）。
        连接或状态不可用时 `ok:true`/`status:unavailable`，不代表车已就绪。
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
