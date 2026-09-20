# -*- coding: utf-8 -*-
"""Plan persistence tests use a temporary SQLite database only."""
import sqlite3
import threading

import pytest

from LLM.store import db


@pytest.fixture()
def plan_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "plan.db"))
    db.init_db()
    return db


def _plan(**changes):
    row = {
        "kind": "care",
        "title": "去 A 点",
        "priority": "P2",
        "preemption": "queue",
        "status": "queued",
        "owner_uid": "elder-1",
        "source_kind": "manual",
        "source_id": "source-1",
        "creator_uid": "admin",
        "creator_role": "admin",
        "creator_surface": "admin",
        "wake_at": None,
        "deadline_at": "2026-09-22 09:00:00",
    }
    row.update(changes)
    return row


def _step(seq=1, **changes):
    row = {
        "seq": seq,
        "step_type": "action",
        "action": "robot_goto_point",
        "label": "前往 A 点",
        "args_json": {"x": 1.0, "y": 2.0, "yaw_deg": 0.0},
        "status": "pending",
        "wait_kind": None,
        "wake_at": None,
        "map_name": "ward-map",
        "target_json": {"source_action": "robot_goto_place", "name": "A 点"},
        "tags_fingerprint": "sha1:abc",
        "timeout_sec": 60,
        "retry_policy": "safe_goto_only",
        "max_attempts": 2,
        "started_at": None,
        "finished_at": None,
        "last_progress_at": None,
        "last_error": "",
    }
    row.update(changes)
    return row


def test_schema_contains_all_plan_fields_constraints_and_indexes(plan_db):
    expected = {
        "plans": {
            "id", "display_no", "kind", "title", "priority", "preemption", "status",
            "owner_uid", "source_kind", "source_id", "creator_uid", "creator_role",
            "creator_surface", "current_step_id", "wake_at", "deadline_at", "version",
            "created_at", "updated_at",
        },
        "plan_steps": {
            "id", "plan_id", "seq", "step_type", "action", "label", "args_json", "status",
            "wait_kind", "wake_at", "map_name", "target_json", "tags_fingerprint",
            "timeout_sec", "retry_policy", "max_attempts", "started_at", "finished_at",
            "last_progress_at", "last_error",
        },
        "plan_step_attempts": {
            "id", "plan_id", "step_id", "attempt_no", "idempotency_key", "dispatch_state",
            "car_task_id", "request_json", "accept_json", "result_json", "started_at",
            "last_checked_at", "finished_at", "outcome",
        },
    }
    conn = plan_db._conn()
    try:
        for table, fields in expected.items():
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert fields <= columns

        plan_indexes = {
            tuple(part["name"] for part in conn.execute(f"PRAGMA index_info({idx['name']})"))
            for idx in conn.execute("PRAGMA index_list(plans)")
        }
        step_indexes = {
            tuple(part["name"] for part in conn.execute(f"PRAGMA index_info({idx['name']})"))
            for idx in conn.execute("PRAGMA index_list(plan_steps)")
        }
        attempt_indexes = {
            tuple(part["name"] for part in conn.execute(f"PRAGMA index_info({idx['name']})"))
            for idx in conn.execute("PRAGMA index_list(plan_step_attempts)")
        }
        assert ("status", "priority", "created_at") in plan_indexes
        assert ("plan_id", "seq") in step_indexes
        assert ("car_task_id",) in attempt_indexes

        foreign_keys = conn.execute("PRAGMA foreign_key_list(plan_steps)").fetchall()
        assert any(row["table"] == "plans" and row["on_delete"] == "CASCADE"
                   for row in foreign_keys)
    finally:
        conn.close()


def test_create_plan_roundtrip(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])

    got = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)

    assert got["display_no"] == f"PL-{made['id']:04d}"
    assert got["version"] == 1
    assert got["deadline_at"] == "2026-09-22 09:00:00"
    assert got["steps"][0]["args_json"]["x"] == 1.0
    assert got["steps"][0]["target_json"]["name"] == "A 点"
    assert got["steps"][0]["attempts"] == []


def test_create_plan_rolls_back_if_second_step_fails(plan_db):
    with pytest.raises(sqlite3.IntegrityError):
        plan_db.create_plan(_plan(), [_step(seq=1), _step(seq=1, label="重复顺序")])

    assert plan_db.list_plans() == []


def test_versioned_update_succeeds_then_rejects_stale_version(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])

    state, updated = plan_db.update_plan_versioned(
        made["id"], 1, status="running", priority="P1"
    )
    stale_state, stale = plan_db.update_plan_versioned(
        made["id"], 1, status="cancelled"
    )
    missing_state, missing = plan_db.update_plan_versioned(999_999, 1, status="running")

    assert state == "updated"
    assert updated["version"] == 2
    assert updated["status"] == "running"
    assert updated["priority"] == "P1"
    assert stale_state == "conflict"
    assert stale["version"] == 2
    assert stale["status"] == "running"
    assert (missing_state, missing) == ("missing", None)


def test_attempt_number_and_idempotency_key_are_unique(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step_id = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]["id"]

    attempt = plan_db.prepare_plan_attempt(step_id, 1, {"action": "robot_goto_point"})

    assert attempt["request_json"] == {"action": "robot_goto_point"}
    assert attempt["idempotency_key"] == f"{made['id']}:{step_id}:1"
    with pytest.raises(sqlite3.IntegrityError):
        plan_db.prepare_plan_attempt(step_id, 1, {"action": "robot_goto_point"})


def test_plan_delete_cascades_to_steps_and_attempts(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step_id = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]["id"]
    plan_db.prepare_plan_attempt(step_id, 1, {"action": "robot_goto_point"})

    conn = plan_db._conn()
    try:
        conn.execute("DELETE FROM plans WHERE id=?", (made["id"],))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM plan_steps").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM plan_step_attempts").fetchone()[0] == 0
    finally:
        conn.close()


def test_step_and_attempt_updates_keep_json_inside_db_layer(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    attempt = plan_db.prepare_plan_attempt(step["id"], 1, {"command": {"x": 1.0}})

    updated_step = plan_db.update_step(
        step["id"], status="running", args_json={"x": 3.0},
        target_json={"source_action": "robot_goto_point"},
    )
    updated_attempt = plan_db.update_plan_attempt(
        attempt["id"], dispatch_state="dispatched", car_task_id="task-7",
        accept_json={"ok": True}, result_json={"status": "running"},
    )

    assert updated_step["args_json"] == {"x": 3.0}
    assert updated_step["target_json"] == {"source_action": "robot_goto_point"}
    assert updated_attempt["request_json"] == {"command": {"x": 1.0}}
    assert updated_attempt["accept_json"] == {"ok": True}
    assert updated_attempt["result_json"] == {"status": "running"}
    assert plan_db.update_step(999_999, status="running") is None
    assert plan_db.update_plan_attempt(999_999, outcome="failed") is None


def test_list_counts_and_before_id(plan_db):
    first = plan_db.create_plan(_plan(title="第一个", status="queued"), [_step()])
    second = plan_db.create_plan(_plan(title="第二个", status="waiting"), [_step()])
    plan_db.create_plan(_plan(title="第三个", status="queued"), [_step()])

    assert [row["title"] for row in plan_db.list_plans(states=("queued",))] == ["第三个", "第一个"]
    assert [row["id"] for row in plan_db.list_plans(limit=5, before_id=second["id"])] == [first["id"]]
    assert plan_db.plan_counts() == {"queued": 2, "waiting": 1}


def test_concurrent_versioned_updates_allow_one_writer(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    barrier = threading.Barrier(3)
    results = []

    def write(status):
        barrier.wait()
        results.append(plan_db.update_plan_versioned(made["id"], 1, status=status)[0])

    threads = [
        threading.Thread(target=write, args=("running",)),
        threading.Thread(target=write, args=("cancelled",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["conflict", "updated"]
    assert plan_db.get_plan(made["id"])["version"] == 2


def test_active_step_and_recovery_move_uncertain_work_to_review(plan_db):
    made = plan_db.create_plan(_plan(status="running"), [_step(status="running")])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    plan_db.update_plan_versioned(made["id"], 1, current_step_id=step["id"])

    active = plan_db.active_plan_step()
    recovered = plan_db.recover_running_plan_steps()
    got = plan_db.get_plan(made["id"], include_steps=True)

    assert active["id"] == step["id"]
    assert active["plan"]["id"] == made["id"]
    assert recovered == 1
    assert got["status"] == "needs_review"
    assert got["version"] == 3
    assert got["steps"][0]["status"] == "needs_review"
    assert plan_db.active_plan_step() is None
