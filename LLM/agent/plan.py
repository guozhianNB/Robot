# -*- coding: utf-8 -*-
r"""Plan candidate validation and deterministic navigation-target compilation."""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime

from .. import conf
from ..core import log as audit
from ..store import db
from . import notify


PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
STEP_TYPES = frozenset({"action", "wait", "manual"})
PLAN_ACTIONS = frozenset({
    "robot_move", "robot_turn", "robot_goto_point",
    "robot_goto_place", "robot_goto_zone",
})
# 需要把「地点/区域/坐标」解析成绝对目标、因而可能暂时解析不了的动作。
# 创建期解析不了时按设计 §4.2 挂 waiting（保留原动作与参数），
# 由 `plan_scheduler` 定期重解析后才编译成 `robot_goto_point`。
DEFERRED_NAV_ACTIONS = frozenset({
    "robot_goto_point", "robot_goto_place", "robot_goto_zone",
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


def _compile_target_result(action: str, args: dict, resolver) -> dict:
    """Resolve one navigation action into a frozen point, never fabricating coordinates.

    Returns ``{"ok": True, "point": ..., "fields": ...}`` or, when the target simply
    cannot be resolved right now (导航没跑、地点没标、缓存过期…),
    ``{"ok": False, "error": ...}``.  Malformed arguments are rejected earlier by
    ``_normalize_action_args`` and therefore never reach this deferrable path.
    """
    try:
        point, fields = _compile_target(action, args, resolver)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "point": point, "fields": fields}


def defer_reason(action: str, error: str) -> str:
    """The audit/UI-facing reason stored on a step whose target is not frozen yet."""
    return f"目标尚未解析（{action}）：{error or '未知错误'}"


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
            if action in DEFERRED_NAV_ACTIONS:
                built = _compile_target_result(action, args, resolver)
                if not built["ok"]:
                    # 设计 §4.2：暂时无法解析时步骤进入 waiting，禁止编造坐标。
                    # 原动作与原参数原样留在行里，调度器据此重解析。
                    out.update({"status": "waiting",
                                "last_error": defer_reason(action, built["error"])})
                else:
                    out.update(built["fields"])
                    out.update({"action": "robot_goto_point", "args_json": built["point"]})
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
    deferred = [step for step in steps if step.get("status") == "waiting"]
    principal = dict(creator or {})
    plan_row = {
        "kind": "care", "title": title.strip(), "priority": priority,
        "preemption": "queue",
        # 首步就要等目标解析时 Plan 也如实标 waiting（与 _handle_wait 口径一致）。
        "status": "waiting" if deferred else "queued",
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
    if deferred:
        summary += f"，其中 {len(deferred)} 步等待目标解析"
    try:
        audit.log("plan", action="create", plan_id=stored["id"],
                  display_no=stored["display_no"], priority=priority,
                  creator_uid=plan_row["creator_uid"], creator_role=plan_row["creator_role"],
                  deferred_targets=len(deferred))
    except Exception as exc:  # committed state must not be reported as a failed tool call
        print(f"[WARN] Plan {stored['display_no']} 已创建，但审计写入失败：{exc}")
    # Broadcast only after the database commit; an SSE failure must never undo it.
    _publish(stored, kind="plan_created")
    return {"ok": True, "plan": stored, "summary": summary}


def resolve_step_target(step: dict, resolver=None) -> dict:
    """Re-resolve a navigation step that creation had to defer (design §4.2).

    Pure: no database writes, no dispatch.  Returns ``{"ok": True, "point", "fields"}``
    when the target is finally resolvable, or ``{"ok": False, "error"}`` while it is
    still not (navigation down, tags missing, fingerprint stale…).  Stored arguments
    that do not even validate raise ``ValueError``: that is corrupted persisted data,
    not a temporary map condition, and the caller must fail closed.
    """
    action = step.get("action")
    if action not in DEFERRED_NAV_ACTIONS:
        raise ValueError(f"动作 {action!r} 不需要重解析导航目标")
    args = _normalize_action_args(action, dict(step.get("args_json") or {}))
    return _compile_target_result(action, args, resolver)


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


def create_from_reminder(reminder: dict) -> dict:
    """Create the one manual Plan associated with a due reminder."""
    rid = reminder.get("id")
    title = str(reminder.get("title") or "").strip()
    content = str(reminder.get("content") or "").strip()
    if rid in (None, "") or not (title or content):
        audit.log("plan", action="create_from_reminder_skipped", reminder_id=rid,
                  reason="reminder has no legal manual step")
        return {"ok": True, "created": False, "skipped": True}

    steps = compile_steps([{"type": "manual", "label": content or title}], None)
    plan_row = {
        "kind": "reminder", "title": title or content, "priority": "P1",
        "preemption": "queue", "status": "queued",
        "owner_uid": str(reminder.get("uid") or "").strip() or None,
        "source_kind": "reminder", "source_id": str(rid),
        "creator_uid": str(reminder.get("created_by") or "").strip() or None,
        "creator_role": None, "creator_surface": "system",
        "report_json": {"notify": True, "speak_if_present": False},
    }
    try:
        made = db.create_plan(plan_row, steps)
    except sqlite3.IntegrityError as exc:
        if "plans.source_kind, plans.source_id" not in str(exc):
            raise
        return {"ok": True, "created": False, "duplicate": True}
    audit.log("plan", action="create_from_reminder", reminder_id=rid,
              plan_id=made["id"], display_no=made["display_no"])
    _publish(made, kind="plan_created")
    return {"ok": True, "created": True, "plan": made}


# ---------------------------------------------------------------- 人工状态迁移
_PLAN_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired"})


def _actor_name(actor: dict | None) -> str:
    actor = actor or {}
    return str(actor.get("uid") or actor.get("role") or "unknown")[:128]


def _actor_fields(actor: dict | None) -> dict:
    actor = actor or {}
    return {"actor_uid": str(actor.get("uid") or "")[:128],
            "actor_role": str(actor.get("role") or "")[:64],
            "actor_surface": str(actor.get("slot") or actor.get("surface") or "")[:64]}


def _conflict(current: dict | None, reason: str = "conflict") -> dict:
    return {"ok": False, "error": reason, "plan": current} if current is not None else {
        "ok": False, "error": "not_found"}


def _read_plan(plan_id: int, *, attempts: bool = True) -> dict | None:
    return db.get_plan(plan_id, include_steps=True, include_attempts=attempts)


def _attempt_for(step: dict) -> dict:
    attempts = [item for item in (step.get("attempts") or []) if isinstance(item, dict)]
    return max(attempts, key=lambda item: (int(item.get("attempt_no") or 0),
                                            int(item.get("id") or 0)), default={})


def _publish(plan: dict, *, step: dict | None = None, kind: str = "plan_updated") -> None:
    if kind == "plan_updated":
        if plan.get("status") == "needs_review":
            kind = "plan_needs_review"
        elif step is not None:
            kind = "plan_step_changed"
    try:
        from ..core import bus
        bus.publish(kind, plan_id=plan.get("id"), version=plan.get("version"),
                    plan={"id": plan.get("id"), "display_no": plan.get("display_no"),
                          "status": plan.get("status"), "priority": plan.get("priority")},
                    step=(step or {}).copy() if step else None)
    except Exception as exc:
        audit.log("plan", action="publish_failed", plan_id=plan.get("id"), error=str(exc))


def _notify(plan: dict, event_type: str, *, level: str = "info") -> None:
    """Notifications are best effort and never undo a committed transition."""
    try:
        notify.ingest("plan", event_type, level=level, uid=plan.get("owner_uid") or "",
                      body=f"{plan.get('display_no', 'Plan')}：{plan.get('title', '')}",
                      ref=f"plan:{plan.get('id')}")
    except Exception as exc:
        audit.log("plan", action="notify_failed", plan_id=plan.get("id"),
                  type=event_type, error=str(exc))


def _committed(plan: dict, *, actor: dict | None, action: str,
               step: dict | None = None, notify_type: str | None = None,
               level: str = "info") -> dict:
    audit.log("plan", action=action, plan_id=plan.get("id"),
              step_id=(step or {}).get("id"), actor=_actor_name(actor),
              version=plan.get("version"), **_actor_fields(actor))
    _publish(plan, step=step)
    if notify_type:
        _notify(plan, notify_type, level=level)
    return {"ok": True, "plan": plan}


def change_priority(plan_id: int, version: int, priority: str, actor: dict | None = None) -> dict:
    if priority not in PRIORITIES:
        return {"ok": False, "error": "invalid_priority"}
    current = _read_plan(plan_id)
    if current is None:
        return _conflict(None, "not_found")
    if current.get("version") != version:
        return _conflict(current, "version_conflict")
    if current.get("status") in _PLAN_TERMINAL or current.get("status") == "cancelling":
        return _conflict(current, "invalid_state")
    updated = db.transition_plan_execution(plan_id, expected_version=version,
                                           plan_changes={"priority": priority})
    if updated is None:
        return _conflict(_read_plan(plan_id), "version_conflict")
    return _committed(updated, actor=actor, action="priority", level="info")


def cancel_plan(plan_id: int, version: int, reason: str, actor: dict | None = None) -> dict:
    reason = str(reason or "").strip()
    if not reason:
        return {"ok": False, "error": "reason_required"}
    current = _read_plan(plan_id)
    if current is None:
        return _conflict(None, "not_found")
    if current.get("version") != version:
        return _conflict(current, "version_conflict")
    status = current.get("status")
    if status in {"cancelled", "cancelling"}:
        # Cancellation is idempotent.  In particular, never emit robot_stop twice.
        return {"ok": True, "plan": current}
    if status in _PLAN_TERMINAL:
        return _conflict(current, "invalid_state")
    steps = current.get("steps") or []
    active = next((s for s in steps if s.get("status") in {"dispatching", "running", "interrupting"}), None)
    if active is None:
        updated = db.transition_plan_execution(
            plan_id, expected_version=version, all_step_changes={"status": "cancelled"},
            all_step_exclude_statuses=("succeeded", "failed", "skipped", "cancelled", "interrupted"),
            plan_changes={"status": "cancelled"})
        if updated is None:
            return _conflict(_read_plan(plan_id), "version_conflict")
        return _committed(updated, actor=actor, action="cancel",
                          notify_type="plan_done", level="warning")

    # Reserve the cancellation atomically before touching the car.  A duplicate
    # request now observes cancelling and cannot issue a second stop command.
    now = db.now_iso()
    updated = db.transition_plan_execution(
        plan_id, expected_version=version, step_id=active["id"],
        step_changes={"status": "interrupting", "last_error": reason,
                      "last_progress_at": now},
        plan_changes={"status": "cancelling"})
    if updated is None:
        return _conflict(_read_plan(plan_id), "version_conflict")
    try:
        from . import tools
        stop = tools.run_plan_tool("robot_stop", {})
        from .plan_scheduler import parse_stop_result
        ok, message = parse_stop_result(stop)
        if not ok:
            raise RuntimeError(message)
    except Exception as exc:
        latest = _read_plan(plan_id)
        if latest:
            step = next((s for s in latest.get("steps", []) if s.get("id") == active["id"]), active)
            attempt = _attempt_for(step)
            reviewed = db.transition_plan_execution(
                plan_id, expected_version=latest.get("version"), step_id=step["id"],
                step_changes={"status": "needs_review", "last_error": f"急停失败：{exc}"},
                attempt_id=attempt.get("id"),
                attempt_changes=({"dispatch_state": "uncertain", "outcome": "uncertain",
                                  "result_json": {"reason": str(exc)},
                                  "last_checked_at": db.now_iso()} if attempt.get("id") else None),
                plan_changes={"status": "needs_review"})
            if reviewed is None:
                return _conflict(_read_plan(plan_id), "version_conflict")
            latest = reviewed
            if latest:
                _committed(latest, actor=actor, action="cancel_stop_failed",
                           step=step, notify_type="plan_needs_review", level="critical")
                return {"ok": False, "error": "stop_failed", "plan": latest}
    latest = _read_plan(plan_id)
    return _committed(latest or updated, actor=actor, action="cancel",
                      step=active) if latest else {"ok": True, "plan": updated}


def _plan_status_after_step(plan: dict, step_id: int, terminal: str) -> str:
    statuses = [terminal if s.get("id") == step_id else s.get("status")
                for s in plan.get("steps", [])]
    if terminal == "failed":
        return "failed"
    if any(status == "needs_review" for status in statuses):
        return "needs_review"
    if all(s == "succeeded" for s in statuses):
        return "succeeded"
    return "running"


def confirm_step(plan_id: int, version: int, step_id: int, decision: str,
                 note: str, actor: dict | None = None) -> dict:
    note = str(note or "").strip()
    current = _read_plan(plan_id)
    if current is None:
        return _conflict(None, "not_found")
    if current.get("version") != version:
        return _conflict(current, "version_conflict")
    step = next((s for s in current.get("steps", []) if s.get("id") == step_id), None)
    if step is None:
        return _conflict(current, "step_not_found")
    if step.get("status") in {"running", "dispatching", "interrupting"}:
        return _conflict(current, "invalid_state")
    status = step.get("status")
    if status == "needs_review":
        if decision not in {"mark_succeeded", "mark_failed", "retry"} or not note:
            return {"ok": False, "error": "needs_review_decision_requires_note", "plan": current}
        if decision == "retry":
            return retry_step(plan_id, version, step_id, note, actor)
        terminal = "succeeded" if decision == "mark_succeeded" else "failed"
    else:
        if decision != "complete" or step.get("step_type") not in {"manual", "wait"}:
            return {"ok": False, "error": "invalid_decision", "plan": current}
        if step.get("step_type") == "wait" and step.get("wait_kind") != "manual":
            return {"ok": False, "error": "time_wait_cannot_be_confirmed", "plan": current}
        if status not in {"pending", "waiting", "paused"}:
            return _conflict(current, "invalid_state")
        terminal = "succeeded"
    now = db.now_iso()
    plan_status = _plan_status_after_step(current, step_id, terminal)
    updated = db.transition_plan_execution(
        plan_id, expected_version=version, step_id=step_id,
        step_changes={"status": terminal, "finished_at": now,
                      "last_error": "" if terminal == "succeeded" else note},
        plan_changes={"status": plan_status})
    if updated is None:
        return _conflict(_read_plan(plan_id), "version_conflict")
    return _committed(updated, actor=actor, action=f"confirm_{decision}", step=step,
                      notify_type=("plan_failed" if terminal == "failed" else
                                   "plan_done" if plan_status == "succeeded" else None),
                      level="warning" if terminal == "failed" else "info")


def retry_step(plan_id: int, version: int, step_id: int, note: str,
               actor: dict | None = None) -> dict:
    note = str(note or "").strip()
    current = _read_plan(plan_id)
    if current is None:
        return _conflict(None, "not_found")
    if current.get("version") != version:
        return _conflict(current, "version_conflict")
    step = next((s for s in current.get("steps", []) if s.get("id") == step_id), None)
    if step is None:
        return _conflict(current, "step_not_found")
    if step.get("status") != "needs_review" or not note:
        return {"ok": False, "error": "retry_requires_needs_review_and_note", "plan": current}
    if step.get("step_type") != "action" or step.get("retry_policy") != "safe_goto_only":
        return {"ok": False, "error": "retry_not_allowed", "plan": current}
    # Relative actions (move/turn) never retry after an uncertain outcome.
    if step.get("action") != "robot_goto_point":
        return {"ok": False, "error": "retry_not_allowed", "plan": current}
    attempts = [a for a in (step.get("attempts") or []) if isinstance(a, dict)]
    attempt_no = max((int(a.get("attempt_no") or 0) for a in attempts), default=0) + 1
    request = {"action": step.get("action"), "args": dict(step.get("args_json") or {})}
    updated = db.transition_plan_execution(
        plan_id, expected_version=version, step_id=step_id,
        step_changes={"status": "pending", "last_error": note,
                      "finished_at": None},
        new_attempt={"attempt_no": attempt_no, "request": request},
        plan_changes={"status": "queued"})
    if updated is None:
        return _conflict(_read_plan(plan_id), "version_conflict")
    return _committed(updated, actor=actor, action="retry", step=step)
