# -*- coding: utf-8 -*-
r"""区域停靠点（zone goal）数据契约测试。"""
import asyncio
import json

import time

import pytest
from pydantic import ValidationError

from LLM.maps import mapapi, maptags, mapserver
from LLM.maps.mapstore import MapStoreError


class _MemoryStore:
    io_mode = "memory"
    root = "memory"

    def __init__(self):
        self._files = {}

    def write(self, name, ext, data):
        self._files[(name, ext)] = bytes(data)

    def read(self, name, ext):
        try:
            return self._files[(name, ext)]
        except KeyError as e:
            raise MapStoreError("文件不存在") from e

    def read_with_meta(self, name, ext):
        data = self.read(name, ext)
        return data, False, None

    def stat(self, name, ext):
        data = self._files.get((name, ext))
        return (time.time(), len(data)) if data is not None else None


def _zone(**extra):
    zone = {
        "uid": "z1",
        "name": "101",
        "kind": "room",
        "shape": "polygon",
        "polygon": [[0, 0], [4, 0], [4, 4], [0, 4]],
    }
    zone.update(extra)
    return zone


def test_norm_tags_keeps_legacy_zone_without_goal_and_normalizes_valid_goal():
    tags, warnings = maptags._norm_tags(
        {"zones": [_zone(), _zone(uid="z2", name="102", goal={"x": 1, "y": 2})]},
        "ward",
    )

    assert "goal" not in tags["zones"][0]
    assert tags["zones"][1]["goal"] == {"x": 1.0, "y": 2.0, "yaw_deg": 0.0}
    assert warnings == []


@pytest.mark.parametrize(
    "goal",
    [
        {"x": True, "y": 2},
        {"x": "1", "y": 2},
        {"x": 1, "y": float("nan")},
        {"x": 1, "y": float("inf")},
        {"x": 1},
    ],
)
def test_norm_tags_drops_invalid_goal_with_warning(goal):
    tags, warnings = maptags._norm_tags({"zones": [_zone(goal=goal)]}, "ward")

    assert "goal" not in tags["zones"][0]
    assert any("goal" in warning for warning in warnings)


def test_norm_tags_drops_explicit_null_goal_with_warning_but_missing_goal_is_silent():
    tags, warnings = maptags._norm_tags(
        {"zones": [_zone(goal=None), _zone(uid="z2", name="102")]}, "ward"
    )

    assert "goal" not in tags["zones"][0]
    assert "goal" not in tags["zones"][1]
    assert sum("goal" in warning for warning in warnings) == 1


def test_zone_list_reads_goal_from_tags_truth_not_sqlite(monkeypatch):
    expected = {"x": 1.0, "y": 2.0, "yaw_deg": 0.0}
    monkeypatch.setattr(
        mapapi.maptags,
        "resolve",
        lambda name, store: {"ok": True, "tags": {"zones": [_zone(goal=expected)]}},
    )
    monkeypatch.setattr(
        mapapi.maptags,
        "get_zones",
        lambda *args, **kwargs: [{**_zone(), "goal": None}],
    )

    result = asyncio.run(mapapi.zones_list(map="ward", store=_MemoryStore()))

    assert result["ok"] is True
    assert result["zones"][0]["goal"] == expected


@pytest.mark.parametrize("value", ["1", True, float("nan"), float("inf")])
def test_zone_goal_api_rejects_non_json_finite_numbers(value):
    with pytest.raises(ValidationError):
        mapapi.ZoneGoalIn(x=value, y=2)


def test_zone_update_distinguishes_omitted_goal_from_explicit_null(monkeypatch):
    store = _MemoryStore()
    monkeypatch.setattr(maptags, "sync_map", lambda *args, **kwargs: {})
    monkeypatch.setattr(mapserver, "validate_point", lambda *args, **kwargs: {"reasons": []})
    monkeypatch.setattr(maptags.zonegeo, "zone_hit", lambda *args, **kwargs: True)

    created = maptags.upsert_zone(
        "ward",
        {"name": "101", "polygon": [[0, 0], [4, 0], [4, 4]],
         "goal": {"x": 1, "y": 2}},
        store=store,
    )
    uid = created["uid"]
    maptags.upsert_zone("ward", {"name": "101-renamed"}, uid=uid, store=store)
    kept = maptags.resolve("ward", store)["tags"]["zones"][0]
    assert kept["goal"] == {"x": 1.0, "y": 2.0, "yaw_deg": 0.0}

    maptags.upsert_zone("ward", {"goal": None}, uid=uid, store=store)
    cleared = maptags.resolve("ward", store)["tags"]["zones"][0]
    assert "goal" not in cleared


def test_zone_api_update_uses_model_fields_set_for_goal_clear(monkeypatch):
    store = _MemoryStore()
    calls = []

    def fake_upsert(map_name, data, uid="", store=None):
        calls.append(data)
        return {"ok": True, "uid": uid or "z1", "goal_validation": None}

    monkeypatch.setattr(maptags, "upsert_zone", fake_upsert)
    omitted = mapapi.ZoneIn(map_name="ward", name="101")
    mapapi.zones_update("z1", omitted, store=store)
    assert "goal" not in calls[-1]

    explicit_null = mapapi.ZoneIn(map_name="ward", name="101", goal=None)
    mapapi.zones_update("z1", explicit_null, store=store)
    assert calls[-1]["goal"] is None


def test_zone_goal_validation_warns_but_saves_for_outside_obstacle_and_unknown(monkeypatch):
    store = _MemoryStore()
    monkeypatch.setattr(maptags, "sync_map", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        mapserver,
        "validate_point",
        lambda *args, **kwargs: {"reasons": ["该点在障碍像素上", "该处未扫到（unknown）"]},
    )
    monkeypatch.setattr(maptags.zonegeo, "zone_hit", lambda *args, **kwargs: False)

    result = maptags.upsert_zone(
        "ward",
        {"name": "101", "polygon": [[0, 0], [4, 0], [4, 4]],
         "goal": {"x": 9, "y": 9}},
        store=store,
    )

    assert result["ok"] is True
    assert any("障碍" in reason for reason in result["goal_validation"]["reasons"])
    assert any("unknown" in reason for reason in result["goal_validation"]["reasons"])
    assert any("区域外" in reason for reason in result["goal_validation"]["reasons"])


def test_zone_goal_validation_reports_unavailable_map_without_blocking_save(monkeypatch):
    store = _MemoryStore()
    monkeypatch.setattr(maptags, "sync_map", lambda *args, **kwargs: {})

    def unavailable(*args, **kwargs):
        raise MapStoreError("PGM 不存在")

    monkeypatch.setattr(mapserver, "validate_point", unavailable)
    monkeypatch.setattr(maptags.zonegeo, "zone_hit", lambda *args, **kwargs: True)

    result = maptags.upsert_zone(
        "ward",
        {"name": "101", "polygon": [[0, 0], [4, 0], [4, 4]],
         "goal": {"x": 1, "y": 2}},
        store=store,
    )

    assert result["ok"] is True
    assert any("无法校验" in reason for reason in result["goal_validation"]["reasons"])


def test_sync_map_excludes_goal_from_sqlite_cache(monkeypatch):
    store = _MemoryStore()
    store.write(
        "ward",
        "tags",
        json.dumps({"zones": [_zone(goal={"x": 1, "y": 2, "yaw_deg": 3})]}).encode(),
    )
    captured = {}
    monkeypatch.setattr(maptags.db, "get_map_tags_manifest", lambda name: None)
    monkeypatch.setattr(maptags.db, "replace_map_tags", lambda name, manifest, dests, zones: captured.update(zones=zones))
    monkeypatch.setattr(mapserver, "map_info", lambda *args, **kwargs: {"resolution": None, "origin": None})

    maptags.sync_map("ward", store=store, force=True)

    assert "goal" not in captured["zones"][0]
