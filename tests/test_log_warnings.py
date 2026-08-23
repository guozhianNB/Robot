# -*- coding: utf-8 -*-
"""log.read_warnings 过滤逻辑测试。"""
import json

from LLM import log


def _write(tmp_path, records):
    p = tmp_path / "audit.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


def test_filters_errors_and_alarms(tmp_path, monkeypatch):
    records = [
        {"ts": "t1", "event": "voice_state", "status": "running"},          # 正常，应过滤
        {"ts": "t2", "event": "voice_error", "error": "设备故障"},           # error 事件 → 保留
        {"ts": "t3", "event": "reminder", "action": "tick_error", "error": "x"},  # action 含 error → 保留
        {"ts": "t4", "event": "alarm", "level": "warning"},                  # alarm → 保留
        {"ts": "t5", "event": "voice_degraded"},                             # voice_degraded → 保留
        {"ts": "t6", "event": "chat", "action": "turn"},                     # 正常 → 过滤
        {"ts": "t7", "event": "system", "action": "shutdown"},               # 正常 → 过滤
    ]
    monkeypatch.setattr(log, "AUDIT_LOG", _write(tmp_path, records))
    out = log.read_warnings(limit=50)
    assert [r["ts"] for r in out] == ["t2", "t3", "t4", "t5"]   # 按写入顺序保留


def test_limit_and_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(log, "AUDIT_LOG", str(tmp_path / "nonexistent.jsonl"))
    assert log.read_warnings(limit=10) == []                     # 文件不存在 → 空

    records = [{"ts": f"t{i}", "event": "alarm", "level": "warn"} for i in range(5)]
    monkeypatch.setattr(log, "AUDIT_LOG", _write(tmp_path, records))
    out = log.read_warnings(limit=3)                             # limit 生效
    assert [r["ts"] for r in out] == ["t2", "t3", "t4"]


def test_bad_json_skipped(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"ts": "t1", "event": "alarm", "level": "warn"}\n')
        f.write("not-json\n")                                     # 解析失败行 → 跳过
        f.write('{"ts": "t2", "event": "voice_error"}\n')
        f.write('123\n')                                          # 合法 JSON 但非对象 → 跳过
    monkeypatch.setattr(log, "AUDIT_LOG", str(p))
    out = log.read_warnings(limit=50)
    assert [r["ts"] for r in out] == ["t1", "t2"]
