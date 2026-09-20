# -*- coding: utf-8 -*-
r"""Plan candidate validation and deterministic navigation-target compilation."""
from __future__ import annotations

import math
import re
from datetime import datetime

from .. import conf
from ..core import log as audit
from ..store import db


PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
STEP_TYPES = frozenset({"action", "wait", "manual"})
PLAN_ACTIONS = frozenset({
    "robot_move", "robot_turn", "robot_goto_point",
    "robot_goto_place", "robot_goto_zone",
})

_TOP_LEVEL_FIELDS = frozenset({"title", "priority", "owner_uid", "steps", "report"})
_STEP_FIELDS = {
    "action": frozenset({"type", "action", "args", "label"}),
    "wait": frozenset({"type", "wait_kind", "wake_at", "label"}),
    "manual": frozenset({"type", "label"}),
}
_WAIT_KINDS = frozenset({"time", "device", "external"})
_DIRECTIONS = frozenset({"forward", "back", "left", "right"})
_SHA1_FINGERPRINT = re.compile(r"^sha1:[0-9a-f]{40}$")


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _require_fields(args: dict, required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = required - set(args)
    extra = set(args) - required - optional
    if missing:
        raise ValueError(f"动作参数缺少字段：{', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"动作参数包含不允许字段：{', '.join(sorted(extra))}")


def _normalize_action_args(action: str, raw_args) -> dict:
    if not isinstance(raw_args, dict):
        raise ValueError(f"{action} 的 args 必须是对象")
    args = dict(raw_args)
    if action == "robot_move":
        _require_fields(args, {"direction", "distance_m"})
        if args["direction"] not in _DIRECTIONS:
            raise ValueError("robot_move.direction 必须是 forward/back/left/right")
        if not _finite(args["distance_m"]) or not 0 < float(args["distance_m"]) <= 5:
            raise ValueError("robot_move.distance_m 必须是有限数字且满足 0 < distance_m <= 5")
        return {"direction": args["direction"], "distance_m": float(args["distance_m"])}
    if action == "robot_turn":
        _require_fields(args, {"angle_deg"})
        if not _finite(args["angle_deg"]) or not 0 < abs(float(args["angle_deg"])) <= 360:
            raise ValueError("robot_turn.angle_deg 必须是有限数字且满足 0 < abs(angle_deg) <= 360")
        return {"angle_deg": float(args["angle_deg"])}
    if action == "robot_goto_point":
        _require_fields(args, {"x", "y"}, {"yaw_deg"})
        yaw = args.get("yaw_deg", 0.0)
        if not all(_finite(value) for value in (args["x"], args["y"], yaw)):
            raise ValueError("robot_goto_point 坐标和 yaw_deg 必须是有限数字")
        return {"x": float(args["x"]), "y": float(args["y"]), "yaw_deg": float(yaw)}
    if action == "robot_goto_place":
        _require_fields(args, {"place"})
        if not isinstance(args["place"], str) or not args["place"].strip():
            raise ValueError("robot_goto_place.place 不能为空")
        return {"place": args["place"].strip()}
    if action == "robot_goto_zone":
        _require_fields(args, {"zone"})
        if not isinstance(args["zone"], str) or not args["zone"].strip():
            raise ValueError("robot_goto_zone.zone 不能为空")
        return {"zone": args["zone"].strip()}
    raise ValueError(f"动作 {action} 不在 Plan 自动执行白名单")


def _resolver_or_default(resolver):
    if resolver is not None:
        return resolver
    from ..car_mcp.car_nav import CarNav
    return CarNav()


def _action_defaults(action: str) -> tuple[int, str, int]:
    if action == "robot_move":
        return conf.PLAN_MOVE_TIMEOUT_S, "none", 1
    if action == "robot_turn":
        return conf.PLAN_TURN_TIMEOUT_S, "none", 1
    return conf.PLAN_GOTO_TIMEOUT_S, "safe_goto_only", 2


def _compile_target(action: str, args: dict, resolver) -> tuple[dict, dict]:
    nav = _resolver_or_default(resolver)
    if action == "robot_goto_place":
        name = args["place"]
        resolved = nav.resolve_place(name)
    elif action == "robot_goto_zone":
        name = args["zone"]
        resolved = nav.resolve_zone(name)
    else:
        name = None
        resolved = nav.resolve_point(args["x"], args["y"], args["yaw_deg"])
    if not isinstance(resolved, dict) or not resolved.get("ok"):
        error = resolved.get("error") if isinstance(resolved, dict) else "目标解析返回无效"
        raise ValueError(f"{action} 目标解析失败：{error or '未知错误'}")
    target = resolved.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"{action} 目标解析失败：缺少 target")
    yaw = target.get("yaw_deg", 0.0)
    if not all(_finite(value) for value in (target.get("x"), target.get("y"), yaw)):
        raise ValueError(f"{action} 目标解析失败：坐标无效")
    point = {"x": float(target["x"]), "y": float(target["y"]), "yaw_deg": float(yaw)}
    map_name = str(resolved.get("map") or "").strip()
    if not map_name:
        raise ValueError(f"{action} 目标解析失败：缺少地图名")
    tags_fingerprint = resolved.get("tags_fingerprint")
    if not isinstance(tags_fingerprint, str) or not _SHA1_FINGERPRINT.fullmatch(
            tags_fingerprint):
        raise ValueError(f"{action} 目标解析失败：缺少合法 tags 指纹")
    snapshot = {
        "source_action": action,
        "x": point["x"], "y": point["y"], "yaw_deg": point["yaw_deg"],
        "goal_source": target.get("goal_source") or "point",
    }
    if name is not None:
        snapshot["name"] = name
    return point, {
        "map_name": map_name,
        "target_json": snapshot,
        "tags_fingerprint": tags_fingerprint,
    }


def _base_step(seq: int, step_type: str, label: str) -> dict:
    return {
        "seq": seq, "step_type": step_type, "action": None, "label": label,
        "args_json": {}, "status": "pending", "wait_kind": None, "wake_at": None,
        "map_name": None, "target_json": None, "tags_fingerprint": None,
        "timeout_sec": 0, "retry_policy": "none", "max_attempts": 1,
    }


def compile_steps(raw_steps: list[dict], resolver) -> list[dict]:
    """Validate all model-authored steps and compile navigation names to safe points."""
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("steps 必须是非空数组")
    compiled = []
    for seq, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"步骤 {seq} 必须是对象")
        step_type = raw.get("type")
        if step_type not in STEP_TYPES:
            raise ValueError(f"步骤 {seq} type 必须是 action/wait/manual")
        extra = set(raw) - _STEP_FIELDS[step_type]
        if extra:
            raise ValueError(f"步骤 {seq} {step_type} 包含不允许字段：{', '.join(sorted(extra))}")
        label = raw.get("label", "")
        if not isinstance(label, str):
            raise ValueError(f"步骤 {seq} label 必须是字符串")
        out = _base_step(seq, step_type, label.strip())

        if step_type == "action":
            action = raw.get("action")
            if action not in PLAN_ACTIONS:
                raise ValueError(f"动作 {action!r} 不在 Plan 自动执行白名单")
            args = _normalize_action_args(action, raw.get("args", {}))
            timeout, retry, attempts = _action_defaults(action)
            out.update({"action": action, "args_json": args, "timeout_sec": timeout,
                        "retry_policy": retry, "max_attempts": attempts})
            if action.startswith("robot_goto_"):
                point, target_fields = _compile_target(action, args, resolver)
                out.update(target_fields)
                out.update({"action": "robot_goto_point", "args_json": point})
        elif step_type == "wait":
            wait_kind = raw.get("wait_kind")
            if wait_kind not in _WAIT_KINDS:
                raise ValueError(f"步骤 {seq} wait_kind 必须是 time/device/external")
            wake_at = raw.get("wake_at")
            if wait_kind == "time":
                if not isinstance(wake_at, str) or not wake_at.strip():
                    raise ValueError(f"步骤 {seq} 的 time wait 必须有 wake_at")
                try:
                    datetime.fromisoformat(wake_at.strip())
                except ValueError as exc:
                    raise ValueError(f"步骤 {seq} 的 wake_at 必须是 ISO 日期时间") from exc
                out["wake_at"] = wake_at.strip()
            elif wake_at not in (None, ""):
                raise ValueError(f"步骤 {seq} 只有 time wait 可以设置 wake_at")
            out["wait_kind"] = wait_kind
        else:
            out["wait_kind"] = "manual"
        compiled.append(out)
    return compiled


def create_candidate(payload: dict, creator: dict, resolver=None) -> dict:
    """Validate a complete LLM candidate before atomically persisting it."""
    if not isinstance(payload, dict):
        raise ValueError("Plan payload 必须是对象")
    extra = set(payload) - _TOP_LEVEL_FIELDS
    if extra:
        raise ValueError(f"Plan 包含不允许字段：{', '.join(sorted(extra))}")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title 不能为空")
    priority = payload.get("priority", "P2")
    if priority not in PRIORITIES:
        raise ValueError("priority 必须是 P0/P1/P2/P3")
    if priority == "P0":
        raise ValueError("LLM 不能创建 P0 Plan，请走既有告警链路")
    owner_uid = payload.get("owner_uid", "")
    if not isinstance(owner_uid, str):
        raise ValueError("owner_uid 必须是字符串")
    report = payload.get("report")
    normalized_report = {"notify": False, "speak_if_present": False}
    if report is not None:
        if not isinstance(report, dict) or set(report) - {"notify", "speak_if_present"}:
            raise ValueError("report 只允许 notify 和 speak_if_present")
        if any(not isinstance(value, bool) for value in report.values()):
            raise ValueError("report 字段必须是布尔值")
        normalized_report.update(report)

    steps = compile_steps(payload.get("steps"), resolver)
    principal = dict(creator or {})
    plan_row = {
        "kind": "care", "title": title.strip(), "priority": priority,
        "preemption": "queue", "status": "queued",
        "owner_uid": owner_uid.strip() or None,
        "source_kind": "chat", "source_id": None,
        "creator_uid": principal.get("uid") or None,
        "creator_role": principal.get("role") or None,
        "creator_surface": principal.get("slot") or None,
        "report_json": normalized_report,
    }
    made = db.create_plan(plan_row, steps)
    stored = made
    summary = f"{stored['display_no']} {stored['title']}（{len(steps)} 步）"
    try:
        audit.log("plan", action="create", plan_id=stored["id"],
                  display_no=stored["display_no"], priority=priority,
                  creator_uid=plan_row["creator_uid"], creator_role=plan_row["creator_role"])
    except Exception as exc:  # committed state must not be reported as a failed tool call
        print(f"[WARN] Plan {stored['display_no']} 已创建，但审计写入失败：{exc}")
    return {"ok": True, "plan": stored, "summary": summary}


def create_from_tool(title, steps, priority="P2", owner_uid="", report=None) -> dict:
    """Create a candidate with the principal bound by ``tools.run_tool``."""
    from . import tools
    try:
        out = create_candidate(
            {"title": title, "steps": steps, "priority": priority,
             "owner_uid": owner_uid, "report": report},
            creator=tools.current_principal(),
        )
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    made = out["plan"]
    return {"ok": True, "plan_id": made["id"], "display_no": made["display_no"],
            "status": made["status"], "summary": out["summary"]}
