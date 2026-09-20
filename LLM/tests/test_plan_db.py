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
        "report_json": {"notify": True, "speak_if_present": False},
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
            "creator_surface", "report_json", "current_step_id", "wake_at", "deadline_at", "version",
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
        unfinished = next(
            idx for idx in conn.execute("PRAGMA index_list(plan_step_attempts)")
            if idx["name"] == "idx_plan_attempts_one_unfinished")
        assert unfinished["unique"] == 1
        assert unfinished["partial"] == 1

        step_foreign_keys = conn.execute("PRAGMA foreign_key_list(plan_steps)").fetchall()
        assert any(row["table"] == "plans" and row["on_delete"] == "CASCADE"
                   for row in step_foreign_keys)
        attempt_foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(plan_step_attempts)").fetchall()
        ownership = [row for row in attempt_foreign_keys if row["table"] == "plan_steps"]
        assert {(row["from"], row["to"]) for row in ownership} == {
            ("plan_id", "plan_id"), ("step_id", "id")}
        assert {row["id"] for row in ownership} == {ownership[0]["id"]}
        assert all(row["on_delete"] == "CASCADE" for row in ownership)
    finally:
        conn.close()


def test_create_plan_roundtrip(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])

    got = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)

    assert got["display_no"] == f"PL-{made['id']:04d}"
    assert got["version"] == 1
    assert got["deadline_at"] == "2026-09-22 09:00:00"
    assert got["report_json"] == {"notify": True, "speak_if_present": False}
    assert got["steps"][0]["args_json"]["x"] == 1.0
    assert got["steps"][0]["target_json"]["name"] == "A 点"
    assert got["steps"][0]["attempts"] == []


def test_init_db_migrates_report_json_into_existing_plans(tmp_path, monkeypatch):
    path = tmp_path / "legacy-plan.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE plans (
           id INTEGER PRIMARY KEY AUTOINCREMENT, display_no TEXT NOT NULL DEFAULT '',
           kind TEXT NOT NULL, title TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'P2',
           preemption TEXT NOT NULL DEFAULT 'queue', status TEXT NOT NULL DEFAULT 'draft',
           owner_uid TEXT, source_kind TEXT NOT NULL DEFAULT 'manual', source_id TEXT,
           creator_uid TEXT, creator_role TEXT, creator_surface TEXT, current_step_id INTEGER,
           wake_at TEXT, deadline_at TEXT, version INTEGER NOT NULL DEFAULT 1,
           created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        """INSERT INTO plans
           (display_no,kind,title,priority,preemption,status,source_kind,version,
            created_at,updated_at)
           VALUES ('PL-0001','care','旧计划','P2','queue','draft','manual',1,
                   '2026-09-21 10:00:00','2026-09-21 10:00:00')"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", str(path))

    db.init_db()

    conn = db._conn()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plans)")}
    finally:
        conn.close()
    assert "report_json" in columns
    migrated = db.get_plan(1)
    assert migrated["title"] == "旧计划"
    assert migrated["report_json"] == {}


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
    version = plan_db.get_plan(made["id"])["version"]
    with pytest.raises(ValueError):
        plan_db.prepare_plan_attempt(step_id, 1, {"action": "robot_goto_point"})
    with pytest.raises(ValueError):
        plan_db.prepare_plan_attempt(step_id, 2, {"action": "robot_goto_point"})
    got = plan_db.get_plan(made["id"], include_attempts=True)
    assert got["version"] == version
    assert len(got["attempts"]) == 1


def test_pending_step_with_unfinished_attempt_cannot_prepare_again(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    plan_db.prepare_plan_attempt(step["id"], 1, {})
    conn = plan_db._conn()
    try:
        conn.execute("UPDATE plan_steps SET status='pending' WHERE id=?", (step["id"],))
        conn.commit()
    finally:
        conn.close()
    version = plan_db.get_plan(made["id"])["version"]

    with pytest.raises(ValueError):
        plan_db.prepare_plan_attempt(step["id"], 2, {})

    got = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert got["version"] == version
    assert got["steps"][0]["status"] == "pending"
    assert len(got["attempts"]) == 1


def test_partial_unique_index_rejects_two_unfinished_attempts(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    plan_db.prepare_plan_attempt(step["id"], 1, {})

    conn = plan_db._conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO plan_step_attempts
                   (plan_id,step_id,attempt_no,idempotency_key,dispatch_state,request_json)
                   VALUES (?,?,?,?,?,?)""",
                (made["id"], step["id"], 2, "second-unfinished", "dispatched", "{}"),
            )
    finally:
        conn.close()


def test_attempt_plan_and_step_must_belong_to_same_plan(plan_db):
    first = plan_db.create_plan(_plan(title="第一个"), [_step()])
    second = plan_db.create_plan(_plan(title="第二个"), [_step()])
    other_step = plan_db.get_plan(second["id"], include_steps=True)["steps"][0]

    conn = plan_db._conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO plan_step_attempts
                   (plan_id,step_id,attempt_no,idempotency_key,dispatch_state,request_json)
                   VALUES (?,?,?,?,?,?)""",
                (first["id"], other_step["id"], 1, "cross-plan", "prepared", "{}"),
            )
    finally:
        conn.close()


def test_init_db_migrates_legacy_attempt_foreign_keys_without_losing_valid_rows(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    attempt = plan_db.prepare_plan_attempt(step["id"], 1, {"action": "robot_goto_point"})
    legacy_sql = plan_db._PLAN_ATTEMPTS_TABLE_SQL.replace(
        "FOREIGN KEY(plan_id, step_id) REFERENCES plan_steps(plan_id, id) ON DELETE CASCADE",
        "FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE,\n"
        "  FOREIGN KEY(step_id) REFERENCES plan_steps(id) ON DELETE CASCADE",
    )
    columns = (
        "id,plan_id,step_id,attempt_no,idempotency_key,dispatch_state,car_task_id,"
        "request_json,accept_json,result_json,started_at,last_checked_at,finished_at,outcome"
    )
    conn = plan_db._conn()
    try:
        conn.execute("UPDATE plan_steps SET status='pending' WHERE id=?", (step["id"],))
        conn.execute("ALTER TABLE plan_step_attempts RENAME TO plan_step_attempts__valid")
        conn.execute("DROP INDEX idx_plan_attempts_car_task_id")
        conn.execute(legacy_sql)
        conn.execute(
            f"INSERT INTO plan_step_attempts ({columns}) "
            f"SELECT {columns} FROM plan_step_attempts__valid"
        )
        conn.execute("DROP TABLE plan_step_attempts__valid")
        conn.commit()
    finally:
        conn.close()

    plan_db.init_db()

    got = plan_db.get_plan(made["id"], include_attempts=True)
    assert [row["id"] for row in got["attempts"]] == [attempt["id"]]
    assert plan_db.recover_running_plan_steps() == 1
    recovered = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert recovered["version"] == 3
    assert recovered["status"] == "needs_review"
    assert recovered["steps"][0]["status"] == "needs_review"
    assert recovered["attempts"][0]["dispatch_state"] == "uncertain"
    conn = plan_db._conn()
    try:
        ownership = [row for row in conn.execute(
            "PRAGMA foreign_key_list(plan_step_attempts)") if row["table"] == "plan_steps"]
        assert {(row["from"], row["to"]) for row in ownership} == {
            ("plan_id", "plan_id"), ("step_id", "id")}
    finally:
        conn.close()


def test_init_db_refuses_cross_owned_legacy_attempt_without_deleting_evidence(plan_db):
    first = plan_db.create_plan(_plan(title="第一个"), [_step()])
    second = plan_db.create_plan(_plan(title="第二个"), [_step()])
    other_step = plan_db.get_plan(second["id"], include_steps=True)["steps"][0]
    legacy_sql = plan_db._PLAN_ATTEMPTS_TABLE_SQL.replace(
        "FOREIGN KEY(plan_id, step_id) REFERENCES plan_steps(plan_id, id) ON DELETE CASCADE",
        "FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE,\n"
        "  FOREIGN KEY(step_id) REFERENCES plan_steps(id) ON DELETE CASCADE",
    )
    conn = plan_db._conn()
    try:
        conn.execute("DROP TABLE plan_step_attempts")
        conn.execute(legacy_sql)
        conn.execute(
            """INSERT INTO plan_step_attempts
               (plan_id,step_id,attempt_no,idempotency_key,dispatch_state,request_json)
               VALUES (?,?,?,?,?,?)""",
            (first["id"], other_step["id"], 1, "cross-owned-legacy", "prepared", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="cross-owned"):
        plan_db.init_db()

    conn = plan_db._conn()
    try:
        kept = conn.execute(
            "SELECT * FROM plan_step_attempts WHERE idempotency_key='cross-owned-legacy'"
        ).fetchone()
        old_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='plan_step_attempts__old'"
        ).fetchone()
        assert kept is not None
        assert kept["plan_id"] == first["id"]
        assert kept["step_id"] == other_step["id"]
        assert old_table is None
    finally:
        conn.close()


def test_prepare_attempt_dispatches_step_and_bumps_plan_once(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]

    plan_db.prepare_plan_attempt(step["id"], 1, {"action": "robot_goto_point"})

    got = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert got["version"] == 2
    assert got["steps"][0]["status"] == "dispatching"
    assert len(got["attempts"]) == 1


def test_prepare_attempt_rolls_back_step_and_version_if_insert_fails(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    attempt = plan_db.prepare_plan_attempt(step["id"], 1, {"action": "robot_goto_point"})
    plan_db.update_plan_attempt(
        attempt["id"], dispatch_state="finished", outcome="failed")
    plan_db.update_step(step["id"], status="pending")
    before = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)

    with pytest.raises(sqlite3.IntegrityError):
        plan_db.prepare_plan_attempt(step["id"], 1, {"action": "duplicate"})

    after = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert after["version"] == before["version"]
    assert after["steps"][0]["status"] == "pending"
    assert len(after["attempts"]) == 1


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


def test_step_and_attempt_changes_invalidate_stale_plan_version(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]

    plan_db.update_step(step["id"], label="调度器已修改")
    stale_state, stale = plan_db.update_plan_versioned(
        made["id"], made["version"], title="护士台旧快照写入")
    after_step = plan_db.get_plan(made["id"])
    attempt = plan_db.prepare_plan_attempt(step["id"], 1, {"action": "robot_goto_point"})
    before_attempt_update = plan_db.get_plan(made["id"])
    plan_db.update_plan_attempt(attempt["id"], dispatch_state="dispatched")
    after_attempt_update = plan_db.get_plan(made["id"])

    assert stale_state == "conflict"
    assert stale["version"] == 2
    assert after_step["version"] == 2
    assert before_attempt_update["version"] == 3
    assert after_attempt_update["version"] == 4


def test_json_fields_fail_closed_on_invalid_values(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]

    with pytest.raises((TypeError, ValueError)):
        plan_db.update_step(step["id"], args_json=[])
    conn = plan_db._conn()
    try:
        conn.execute("UPDATE plan_steps SET args_json='not-json' WHERE id=?", (step["id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.DataError):
        plan_db.get_plan(made["id"], include_steps=True)
    conn = plan_db._conn()
    try:
        conn.execute("UPDATE plan_steps SET args_json='[]' WHERE id=?", (step["id"],))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.DataError):
        plan_db.get_plan(made["id"], include_steps=True)


def test_update_field_whitelists_reject_ownership_and_identity_changes(plan_db):
    made = plan_db.create_plan(_plan(), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    attempt = plan_db.prepare_plan_attempt(step["id"], 1, {})

    with pytest.raises(ValueError):
        plan_db.update_plan_versioned(made["id"], 2, id=77)
    with pytest.raises(ValueError):
        plan_db.update_step(step["id"], plan_id=77)
    with pytest.raises(ValueError):
        plan_db.update_plan_attempt(attempt["id"], step_id=77)


def test_list_counts_and_before_id(plan_db):
    first = plan_db.create_plan(_plan(title="第一个", status="queued"), [_step()])
    second = plan_db.create_plan(_plan(title="第二个", status="waiting"), [_step()])
    plan_db.create_plan(_plan(title="第三个", status="queued"), [_step()])

    assert [row["title"] for row in plan_db.list_plans(states=("queued",))] == ["第三个", "第一个"]
    assert [row["id"] for row in plan_db.list_plans(limit=5, before_id=second["id"])] == [first["id"]]
    assert plan_db.plan_counts() == {"queued": 2, "waiting": 1}


def test_concurrent_versioned_updates_allow_one_writer(plan_db, monkeypatch):
    made = plan_db.create_plan(_plan(), [_step()])
    barrier = threading.Barrier(3)
    results = []
    errors = []

    class NoopLock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(plan_db, "_lock", NoopLock())

    def write(status):
        barrier.wait()
        try:
            results.append(plan_db.update_plan_versioned(made["id"], 1, status=status)[0])
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=("running",)),
        threading.Thread(target=write, args=("cancelled",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
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


def test_recovery_marks_unfinished_attempt_uncertain(plan_db):
    made = plan_db.create_plan(_plan(status="running"), [_step()])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    attempt = plan_db.prepare_plan_attempt(step["id"], 1, {"action": "robot_goto_point"})
    plan_db.update_plan_attempt(attempt["id"], dispatch_state="dispatched")

    assert plan_db.recover_running_plan_steps() == 1

    got = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert got["attempts"][0]["dispatch_state"] == "uncertain"
    assert got["attempts"][0]["outcome"] == "uncertain"


def test_recovery_finds_legacy_pending_step_from_unfinished_attempt(plan_db):
    made = plan_db.create_plan(_plan(status="queued"), [_step(status="pending")])
    step = plan_db.get_plan(made["id"], include_steps=True)["steps"][0]
    conn = plan_db._conn()
    try:
        conn.execute(
            """INSERT INTO plan_step_attempts
               (plan_id,step_id,attempt_no,idempotency_key,dispatch_state,request_json)
               VALUES (?,?,?,?,?,?)""",
            (made["id"], step["id"], 1, "legacy-half-state", "prepared", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    assert plan_db.recover_running_plan_steps() == 1

    got = plan_db.get_plan(made["id"], include_steps=True, include_attempts=True)
    assert got["version"] == 2
    assert got["status"] == "needs_review"
    assert got["steps"][0]["status"] == "needs_review"
    assert got["attempts"][0]["dispatch_state"] == "uncertain"
    assert got["attempts"][0]["outcome"] == "uncertain"
