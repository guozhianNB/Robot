# -*- coding: utf-8 -*-
r"""Deterministic single-executor scheduler for persisted Plans.

All MCP calls run on this module's daemon thread.  A tool response is never
treated as an action result until both the MCP envelope and the JSON payload
have been validated, and an accepted action is bound to its exact car task id.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import threading
import time

from .. import conf
from ..core import log as audit
from ..store import db
from . import action_gate, tools
from . import plan as plan_ops


_ACTIVE_STATES = frozenset({"dispatching", "running", "interrupting"})
_TERMINAL_STATES = frozenset({
    "succeeded", "failed", "skipped", "cancelled", "interrupted",
})
_ACTION_NAMES = frozenset({"robot_goto_point", "robot_move", "robot_turn"})

_stop_evt = threading.Event()
_start_lock = threading.Lock()
_tick_lock = threading.Lock()
_thread: threading.Thread | None = None

# Monotonic clocks deliberately stay outside SQLite.  A process restart sends
# all unfinished attempts to needs_review before this state can be consulted.
_attempt_started: dict[int, float] = {}
_unavailable_since: dict[int, float] = {}
_cancel_started: dict[int, float] = {}


@dataclass(frozen=True)
class ToolResult:
    kind: str
    payload: dict | None = None
    message: str = ""

    @property
    def task_id(self) -> int | None:
        if not isinstance(self.payload, dict):
            return None
        value = self.payload.get("task_id")
        return value if type(value) is int and value > 0 else None


def parse_stop_result(raw: dict) -> tuple[bool, str]:
    """Validate robot_stop's MCP envelope and inner car response."""
    if not isinstance(raw, dict):
        return False, "MCP 急停调用返回无效结果"
    if raw.get("ok") is not True:
        return False, str(raw.get("error") or raw.get("message") or "MCP 急停调用失败")
    body = raw.get("result")
    if not isinstance(body, str):
        return False, "急停结果不是 JSON 字符串"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False, "急停结果不是合法 JSON"
    if not isinstance(payload, dict):
        return False, "车控急停结果必须是对象"
    if payload.get("ok") is not True:
        return False, str(payload.get("error") or payload.get("message") or "车控急停失败")
    if payload.get("status") in {"error", "rejected", "uncertain", "unavailable", "stale"}:
        return False, str(payload.get("error") or payload.get("status"))
    return True, ""


def parse_tool_result(raw: dict) -> ToolResult:
    """Validate the transport envelope and the car server's JSON payload."""
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        message = raw.get("message") or raw.get("error") if isinstance(raw, dict) else ""
        return ToolResult("transport_error", message=str(message or "MCP 调用失败"))
    body = raw.get("result")
    if not isinstance(body, str):
        return ToolResult("malformed", message="MCP result 不是 JSON 字符串")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return ToolResult("malformed", message="MCP result 不是合法 JSON")
    if not isinstance(payload, dict):
        return ToolResult("malformed", message="车控结果必须是对象")

    status = payload.get("status")
    if status == "unavailable" and payload.get("ok") is True:
        return ToolResult("unavailable", payload, str(payload.get("reason") or ""))
    if payload.get("ok") is not True:
        return ToolResult(
            "action_error", payload,
            str(payload.get("error") or payload.get("message") or "车控动作失败"),
        )
    if status == "started":
        task_id = payload.get("task_id")
        if type(task_id) is not int or task_id <= 0:
            return ToolResult("malformed", payload, "started 缺少合法 task_id")
        return ToolResult("started", payload)
    if status == "uncertain":
        return ToolResult("uncertain", payload, str(payload.get("reason") or "结果不确定"))
    if status in {"error", "rejected", "stale"}:
        return ToolResult("action_error", payload, str(payload.get("error") or status))
    # A robot_status snapshot normally has no `status` at all.  It is useful
    # only when it carries the task ownership fields used by reconciliation.
    if any(key in payload for key in ("current", "last", "exec_state", "state_fresh")):
        return ToolResult("status", payload)
    return ToolResult("malformed", payload, "车控结果缺少可判定字段")


def _attempt_for(step: dict) -> dict:
    attempt = step.get("attempt")
    if isinstance(attempt, dict):
        return attempt
    attempts = step.get("attempts")
    if isinstance(attempts, list) and attempts:
        rows = [item for item in attempts if isinstance(item, dict)]
        if rows:
            return max(rows, key=lambda item: (int(item.get("attempt_no") or 0),
                                               int(item.get("id") or 0)))
    return {}


def _task_matches(expected, actual) -> bool:
    """Compare the DB TEXT representation with the MCP integer id exactly."""
    if type(actual) is int:
        if actual <= 0:
            return False
        return (type(expected) is int and expected == actual) or (
            isinstance(expected, str) and expected == str(actual))
    if type(expected) is int:
        return isinstance(actual, str) and actual == str(expected)
    return isinstance(expected, str) and isinstance(actual, str) and expected == actual


def reconcile_step(step: dict, status_payload: dict, now: float) -> str:
    """Return the next semantic state without mutating the supplied rows."""
    if not isinstance(step, dict) or not isinstance(status_payload, dict):
        return "needs_review"
    attempt = _attempt_for(step)
    expected = attempt.get("car_task_id")
    if not ((type(expected) is int and expected > 0) or
            (isinstance(expected, str) and expected.isdigit() and int(expected) > 0)):
        return "needs_review"

    if status_payload.get("status") == "unavailable":
        since = attempt.get("unavailable_since")
        if not isinstance(since, (int, float)) or isinstance(since, bool):
            return "waiting"
        return ("needs_review" if now - float(since) > conf.PLAN_STATUS_GRACE_S
                else "waiting")

    last = status_payload.get("last")
    if isinstance(last, dict) and _task_matches(expected, last.get("task_id")):
        if last.get("status") == "uncertain":
            return "needs_review"
        if last.get("ok") is True:
            return "succeeded"
        if last.get("ok") is False:
            return "failed"
        return "needs_review"

    started = attempt.get("started_at_mono")
    timeout = step.get("timeout_sec")
    if (isinstance(started, (int, float)) and not isinstance(started, bool)
            and isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            and float(timeout) > 0 and now - float(started) > float(timeout)):
        return "needs_review"

    current = status_payload.get("current")
    if isinstance(current, dict) and _task_matches(expected, current.get("task_id")):
        return "running"
    # Other task ids and idle without a matching terminal result are not proof
    # of completion.  Keep waiting until the bounded timeout above.
    return "waiting"


def _all_plans() -> list[dict]:
    plans = []
    # ``None`` deliberately requests an unbounded scan.  A 200-row UI page is
    # not a safe source for execution ownership or multi-active detection.
    for row in db.list_plans(limit=None):
        full = db.get_plan(row["id"], include_steps=True, include_attempts=True)
        if full is not None:
            for step in full.get("steps", []):
                step["_plan_version"] = full.get("version")
            plans.append(full)
    return plans


def _set_plan_status(plan_id: int, status: str, expected_version: int | None = None) -> bool:
    current = db.get_plan(plan_id)
    if current is None or current.get("status") == status:
        return current is not None
    if current.get("status") in {"cancelling", "cancelled", "needs_review"}:
        return False
    if db.transition_plan_execution(plan_id, expected_version=expected_version,
                                    plan_changes={"status": status}) is None:
        return False
    return True


def _mark_review(step: dict, reason: str, attempt: dict | None = None) -> None:
    attempt_changes = None
    attempt_id = None
    if attempt and attempt.get("id"):
        attempt_id = attempt["id"]
        attempt_changes = {
            "dispatch_state": "uncertain", "outcome": "uncertain",
            "result_json": {"reason": reason}, "last_checked_at": db.now_iso(),
        }
    updated = db.transition_plan_execution(
        step["plan_id"], expected_version=step.get("_plan_version"), step_id=step["id"],
        step_changes={"status": "needs_review", "last_error": reason},
        attempt_id=attempt_id, attempt_changes=attempt_changes,
        plan_changes={"status": "needs_review"},
    )
    audit.log("plan", action="needs_review", plan_id=step["plan_id"],
              step_id=step["id"], reason=reason)
    if updated:
        plan_ops._notify(updated, "plan_needs_review", level="critical")


def _finish(step: dict, attempt: dict, outcome: str, payload: dict) -> None:
    final_step = "succeeded" if outcome == "succeeded" else "failed"
    now_iso = db.now_iso()
    plan = db.get_plan(step["plan_id"], include_steps=True)
    if plan is None:
        return
    plan_status = "failed"
    if outcome == "succeeded":
        statuses = ["succeeded" if item.get("id") == step["id"] else item.get("status")
                    for item in plan.get("steps", [])]
        plan_status = "succeeded" if all(item == "succeeded" for item in statuses) else "running"
    updated = db.transition_plan_execution(
        step["plan_id"], expected_version=step.get("_plan_version"), step_id=step["id"],
        step_changes={"status": final_step, "finished_at": now_iso,
                      "last_progress_at": now_iso,
                      "last_error": "" if outcome == "succeeded" else str(
                          payload.get("detail") or "动作失败")},
        attempt_id=attempt["id"], attempt_changes={
            "dispatch_state": "finished", "outcome": outcome,
            "result_json": payload, "last_checked_at": now_iso,
            "finished_at": now_iso,
        },
        plan_changes={"status": plan_status},
    )
    _attempt_started.pop(step["id"], None)
    _unavailable_since.pop(step["id"], None)
    if updated:
        plan_ops._notify(updated, "plan_done" if plan_status == "succeeded" else "plan_failed",
                         level="info" if plan_status == "succeeded" else "warning")


def _poll(step: dict, now: float) -> None:
    attempt = _attempt_for(step)
    if not attempt or attempt.get("dispatch_state") != "dispatched":
        _mark_review(step, "运行步骤缺少已派发 attempt", attempt or None)
        return
    parsed = parse_tool_result(tools.run_plan_tool("robot_status", {}))
    if parsed.kind in {"transport_error", "malformed", "uncertain"}:
        _mark_review(step, parsed.message or "车况无法可靠解析", attempt)
        return
    if parsed.kind == "action_error":
        _mark_review(step, parsed.message or "车况查询失败", attempt)
        return

    payload = parsed.payload or {}
    if parsed.kind == "unavailable":
        since = _unavailable_since.setdefault(step["id"], now)
    else:
        _unavailable_since.pop(step["id"], None)
        since = None
    probe = dict(attempt)
    probe["started_at_mono"] = _attempt_started.setdefault(step["id"], now)
    if since is not None:
        probe["unavailable_since"] = since
    decision = reconcile_step({**step, "attempt": probe}, payload, now)
    checked_at = db.now_iso()
    if decision in {"running", "waiting"}:
        # A snapshot carrying another task id is a late/foreign observation;
        # keep it out of this attempt's result evidence.  Only a matching
        # current task (or a matching terminal last result) may be persisted.
        expected = attempt.get("car_task_id")
        current = payload.get("current") if isinstance(payload, dict) else None
        last = payload.get("last") if isinstance(payload, dict) else None
        matching = ((isinstance(current, dict) and
                     _task_matches(expected, current.get("task_id"))) or
                    (isinstance(last, dict) and
                     _task_matches(expected, last.get("task_id"))))
        changes = {"last_checked_at": checked_at}
        if matching:
            changes["result_json"] = payload
        db.transition_plan_execution(
            step["plan_id"], expected_version=step.get("_plan_version"), step_id=step["id"],
            step_changes={"last_progress_at": checked_at} if decision == "running" else None,
            attempt_id=attempt["id"], attempt_changes=changes,
        )
        if decision == "running":
            pass
        return
    if decision == "needs_review":
        reason = ("车控状态不可用超过恢复宽限期" if parsed.kind == "unavailable"
                  else "未获得与当前 task_id 匹配的可靠终态")
        _mark_review(step, reason, attempt)
        return
    _finish(step, attempt, decision, payload)


def _poll_cancel(step: dict, now: float) -> None:
    """Confirm a cancellation without claiming that robot_stop stopped the car.

    The stop command only writes a websocket message.  Cancellation is final
    only after a fresh status snapshot shows no active task; unavailable or
    stale status is held for the configured grace period and then reviewed.
    """
    started = _cancel_started.setdefault(step["id"], now)
    parsed = parse_tool_result(tools.run_plan_tool("robot_status", {}))
    payload = parsed.payload if isinstance(parsed.payload, dict) else {}
    fresh = parsed.kind == "status" and payload.get("state_fresh") is True
    if parsed.kind == "unavailable" or not fresh:
        if now - started >= conf.PLAN_STATUS_GRACE_S:
            attempt = _attempt_for(step)
            _mark_review(step, "急停后 15 秒未获得新鲜车况", attempt or None)
            _cancel_started.pop(step["id"], None)
        return
    current = payload.get("current")
    exec_state = str(payload.get("exec_state") or payload.get("state") or "").lower()
    active = isinstance(current, dict) and current.get("task_id") not in (None, "", 0)
    if active or exec_state in {"moving", "running", "executing", "busy"}:
        if now - started >= conf.PLAN_STATUS_GRACE_S:
            _mark_review(step, "急停后仍检测到活动车控任务", _attempt_for(step) or None)
            _cancel_started.pop(step["id"], None)
        return
    plan = db.get_plan(step["plan_id"], include_steps=True, include_attempts=True)
    if plan is None or plan.get("status") != "cancelling":
        _cancel_started.pop(step["id"], None)
        return
    attempt = _attempt_for(step)
    now_iso = db.now_iso()
    updated = db.transition_plan_execution(
        step["plan_id"], expected_version=step.get("_plan_version"), step_id=step["id"],
        step_changes={"status": "interrupted", "finished_at": now_iso,
                      "last_progress_at": now_iso},
        attempt_id=attempt.get("id"),
        attempt_changes=({"dispatch_state": "finished", "outcome": "uncertain",
                          "result_json": payload, "finished_at": now_iso,
                          "last_checked_at": now_iso} if attempt.get("id") else None),
        plan_changes={"status": "cancelled"},
    )
    if updated:
        plan_ops._publish(updated, step=step)
        plan_ops._notify(updated, "plan_done", level="info")
    _cancel_started.pop(step["id"], None)


def _dispatch(step: dict, now: float) -> None:
    if step.get("action") not in _ACTION_NAMES:
        _mark_review(step, f"动作不在自动调度清单: {step.get('action')}")
        return
    token = action_gate.claim("plan", f"plan:{step['plan_id']}:step:{step['id']}")
    if token is None:
        return
    try:
        attempts = [item for item in step.get("attempts", []) if isinstance(item, dict)]
        attempt_no = max((int(item.get("attempt_no") or 0) for item in attempts), default=0) + 1
        request = {"action": step["action"], "args": dict(step.get("args_json") or {})}
        prepared = next((item for item in attempts
                         if item.get("dispatch_state") == "prepared"), None)
        attempt = prepared or db.prepare_plan_attempt(
            step["id"], attempt_no, request, expected_version=step.get("_plan_version"))
        _attempt_started[step["id"]] = now
        prepared_plan = db.get_plan(step["plan_id"])
        if prepared_plan is None or not _set_plan_status(
                step["plan_id"], "running", prepared_plan.get("version")):
            return
        dispatch_plan = db.get_plan(step["plan_id"])
        if dispatch_plan is None:
            return
        dispatch_version = dispatch_plan.get("version")
        parsed = parse_tool_result(tools.run_plan_tool(step["action"], request["args"]))
        if parsed.kind == "started":
            accepted = parsed.payload or {}
            started_at = db.now_iso()
            db.transition_plan_execution(
                step["plan_id"], expected_version=dispatch_version, step_id=step["id"],
                step_changes={"status": "running", "started_at": started_at,
                              "last_progress_at": started_at},
                attempt_id=attempt["id"], attempt_changes={
                    "dispatch_state": "dispatched", "car_task_id": parsed.task_id,
                    "accept_json": accepted, "last_checked_at": started_at,
                },
                plan_changes={"status": "running"},
            )
            return
        if parsed.kind == "action_error":
            finished_at = db.now_iso()
            updated = db.transition_plan_execution(
                step["plan_id"], expected_version=dispatch_version, step_id=step["id"],
                step_changes={"status": "failed", "finished_at": finished_at,
                              "last_error": parsed.message},
                attempt_id=attempt["id"], attempt_changes={
                    "dispatch_state": "finished", "outcome": "rejected",
                    "accept_json": parsed.payload, "result_json": parsed.payload,
                    "last_checked_at": finished_at, "finished_at": finished_at,
                },
                plan_changes={"status": "failed"},
            )
            if updated:
                plan_ops._notify(updated, "plan_failed", level="warning")
            return
        _mark_review(step, parsed.message or f"动作受理结果不确定: {parsed.kind}", attempt)
    except (ValueError, RuntimeError) as exc:
        # A concurrent/manual state change is not permission to dispatch twice.
        latest = db.get_plan(step["plan_id"], include_steps=True, include_attempts=True)
        current = next((item for item in (latest or {}).get("steps", [])
                        if item.get("id") == step.get("id")), None)
        if current is not None:
            current["_plan_version"] = latest.get("version")
            # The Attempt CAS failed because a human/API writer advanced the
            # aggregate.  This tick is stale; never turn the newer decision
            # (priority change, manual completion, etc.) into needs_review.
            if (step.get("_plan_version") is not None and
                    latest.get("version") != step.get("_plan_version")):
                return
            if latest.get("status") in {"cancelling", "cancelled", "needs_review"}:
                return
            # Includes the legacy half-state (pending + prepared attempt).  It
            # is evidence of an ambiguous dispatch and must not spin/retry.
            _mark_review(current, f"派发状态冲突: {exc}", _attempt_for(current) or None)
    finally:
        action_gate.release(token)


def _handle_wait(step: dict, wall_now: float) -> None:
    if step.get("wait_kind") != "time":
        if step.get("status") != "waiting":
            db.transition_plan_execution(
                step["plan_id"], expected_version=step.get("_plan_version"), step_id=step["id"],
                step_changes={"status": "waiting"},
                plan_changes={"status": "waiting"},
            )
        return
    try:
        wake = datetime.fromisoformat(str(step.get("wake_at"))).timestamp()
    except (TypeError, ValueError, OSError):
        _mark_review(step, "time wait 的 wake_at 无法解析")
        return
    if wall_now < wake:
        if step.get("status") != "waiting":
            db.transition_plan_execution(
                step["plan_id"], expected_version=step.get("_plan_version"), step_id=step["id"],
                step_changes={"status": "waiting"},
                plan_changes={"status": "waiting"},
            )
        return
    plan = db.get_plan(step["plan_id"], include_steps=True)
    if plan is None:
        return
    statuses = ["succeeded" if item.get("id") == step["id"] else item.get("status")
                for item in plan.get("steps", [])]
    plan_status = "succeeded" if all(status == "succeeded" for status in statuses) else "running"
    updated = db.transition_plan_execution(
        step["plan_id"], expected_version=step.get("_plan_version"), step_id=step["id"],
        step_changes={"status": "succeeded", "finished_at": db.now_iso()},
        plan_changes={"status": plan_status},
    )
    if updated and plan_status == "succeeded":
        plan_ops._notify(updated, "plan_done", level="info")


def _choose_pending(plans: list[dict]) -> dict | None:
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    eligible = []
    for plan in plans:
        if plan.get("status") not in {"queued", "running", "waiting"}:
            continue
        steps = sorted(plan.get("steps", []), key=lambda item: (item.get("seq", 0), item["id"]))
        for step in steps:
            if step.get("status") in _TERMINAL_STATES:
                continue
            if step.get("status") in {"pending", "waiting"}:
                eligible.append((rank.get(plan.get("priority"), 4), plan.get("created_at", ""),
                                 plan["id"], step.get("seq", 0), step))
            break
    return min(eligible, default=(None, None, None, None, None))[-1]


def tick_once(now: float | None = None) -> None:
    """Run at most one deterministic scheduler transition."""
    if not conf.PLAN_EXECUTOR_ENABLED or not _tick_lock.acquire(blocking=False):
        return
    try:
        mono_now = time.monotonic() if now is None else float(now)
        wall_now = time.time()
        plans = _all_plans()
        active = [step for plan in plans for step in plan.get("steps", [])
                  if step.get("status") in _ACTIVE_STATES]
        if len(active) > 1:
            for step in active:
                _mark_review(step, "检测到多个活动 Plan 步骤，调度器已关闭自动推进",
                             _attempt_for(step) or None)
            return
        if active:
            step = active[0]
            if step.get("status") == "running":
                _poll(step, mono_now)
            elif step.get("status") == "interrupting":
                _poll_cancel(step, mono_now)
            else:
                _mark_review(step, f"无法自动恢复 {step.get('status')} 步骤",
                             _attempt_for(step) or None)
            return

        step = _choose_pending(plans)
        if step is None:
            return
        if step.get("step_type") == "action" and step.get("status") == "pending":
            _dispatch(step, mono_now)
        elif step.get("step_type") in {"wait", "manual"}:
            _handle_wait(step, wall_now)
    except Exception as exc:
        audit.log("plan", action="scheduler_error", error=str(exc))
    finally:
        _tick_lock.release()


def _run() -> None:
    while not _stop_evt.is_set():
        tick_once()
        _stop_evt.wait(conf.PLAN_TICK_S)


def start() -> None:
    """Recover unfinished work and start the daemon scheduler once."""
    global _thread
    if not conf.PLAN_EXECUTOR_ENABLED:
        return
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        recovered = db.recover_running_plan_steps()
        if recovered:
            audit.log("plan", action="recover_needs_review", count=recovered)
        _stop_evt.clear()
        _thread = threading.Thread(target=_run, name="plan-scheduler", daemon=True)
        _thread.start()


def stop() -> None:
    """Request shutdown and reclaim the previous daemon before returning."""
    global _thread
    with _start_lock:
        thread = _thread
        _stop_evt.set()
        # Keep the lifecycle lock across join: a concurrent start must wait
        # for this decision, then either start a fresh thread or observe the
        # still-live reference.  Releasing before join silently dropped starts
        # in the stop/start race.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, float(conf.PLAN_TICK_S) * 2.0))
        if _thread is thread and (thread is None or not thread.is_alive()):
            _thread = None
