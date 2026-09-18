# -*- coding: utf-8 -*-
r"""
地图标记（地点 / 区域）的**唯一真相**：``<地图名>.tags.json``（规格 §4.1）。

三条红线（违反就重演"两套真相"事故）：
  1. **唯一真相 = ``<地图名>.tags.json``**（与 ``.pgm``/``.yaml`` 同级同前缀，随地图文件夹整体迁移）。
  2. **SQLite 只做索引缓存，严格单向（文件 → SQLite），永不反向写。** 所有写接口都是
     「改文件 → ``sync_map()`` 刷缓存 → 审计」，绝不允许"只改库不改文件"。
  3. **缓存可丢弃、可完整重建**：清空两张表不影响任何数据；读前比 ``mtime+size+sha1``，
     不一致就整图重建。

格式选 JSON 不选 YAML：本项目依赖清单里没有 pyyaml，JSON 由 stdlib 直接支持、前端也直接吃。

``uid`` 是稳定身份：``destinations`` 用 ``d<N>``、``zones`` 用 ``z<N>``，生成后**永不改变**
（改名不改 uid）；外部引用（如 ``profiles`` 的病房↔区域关联）一律按 uid 走。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time

from ..store import db
from ..core import log as audit
from ..core import zonegeo
from . import mapserver
from .mapstore import MapStoreError, check_name, get_store

VERSION = 1

# 允许的取值（与 db 的缓存表口径一致）
RISKS = ("low", "high")
ZONE_KINDS = ("room", "ward", "bed", "other")
SHAPES = ("polygon", "rect")


# ---------------------------------------------------------------------------
# 基础：读 / 写 / schema
# ---------------------------------------------------------------------------
def empty_tags(map_name: str, resolution=None, origin=None) -> dict:
    return {"version": VERSION, "map": map_name, "resolution": resolution,
            "origin": origin, "destinations": [], "zones": [],
            "created_at": db.now_iso(), "updated_at": db.now_iso()}


def _parse(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as e:      # noqa: BLE001
        raise MapStoreError(f"{'tags.json'} 解析失败：{e}") from e
    if not isinstance(obj, dict):
        raise MapStoreError("tags.json 顶层必须是对象")
    return obj


def _norm_tags(obj: dict, map_name: str) -> tuple[dict, list[str]]:
    """把任意 tags 对象规范成完整 schema；返回 ``(tags, warnings)``。

    不做"猜错就丢数据"的处理：能补的补、能归一化的归一化，改不动的进 warnings 并保留原值。
    """
    warn: list[str] = []
    out = empty_tags(map_name)
    out["created_at"] = obj.get("created_at") or out["created_at"]
    out["updated_at"] = obj.get("updated_at") or out["updated_at"]
    ver = obj.get("version", VERSION)
    if ver != VERSION:
        warn.append(f"tags.json version={ver!r}（当前支持 {VERSION}）")
    out["version"] = VERSION
    file_map = obj.get("map")
    if file_map and file_map != map_name:
        warn.append(f"tags.json 里的 map={file_map!r} 与文件名 {map_name!r} 不一致（按文件名归属，已纠正）")
    out["map"] = map_name
    out["resolution"] = _num_or_none(obj.get("resolution"))
    origin = obj.get("origin")
    if isinstance(origin, list) and len(origin) >= 2:
        try:
            out["origin"] = [float(origin[0]), float(origin[1]),
                             float(origin[2]) if len(origin) > 2 else 0.0]
        except (TypeError, ValueError):
            warn.append(f"tags.json origin 非法：{origin!r}")
            out["origin"] = None
    elif origin is not None:
        warn.append(f"tags.json origin 非法：{origin!r}")

    seen_uid, seen_name = set(), set()
    for d in _as_list(obj.get("destinations"), "destinations", warn):
        dd = dict(d)
        uid = str(dd.get("uid") or "").strip()
        name = str(dd.get("name") or "").strip()
        if not uid or not name:
            warn.append(f"地点缺少 uid/name，已跳过：{d!r}")
            continue
        if uid in seen_uid:
            warn.append(f"地点 uid 重复，已跳过：{uid}")
            continue
        if name in seen_name:
            warn.append(f"地点名重复，已跳过：{name}")
            continue
        seen_uid.add(uid)
        seen_name.add(name)
        aliases = dd.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a for a in aliases.split(",") if a.strip()]
        out["destinations"].append({
            "uid": uid, "name": name,
            "aliases": [str(a).strip() for a in aliases if str(a).strip()],
            "x": _num_or_zero(dd.get("x")), "y": _num_or_zero(dd.get("y")),
            "yaw_deg": _num_or_zero(dd.get("yaw_deg")),
            "risk": dd.get("risk") if dd.get("risk") in RISKS else "low",
            "elder_allowed": 0 if str(dd.get("elder_allowed", 1)) in ("0", "False", "false") else 1,
            "note": str(dd.get("note") or ""),
            "learned_by": str(dd.get("learned_by") or ""),
            "created_at": str(dd.get("created_at") or db.now_iso()),
            "updated_at": str(dd.get("updated_at") or db.now_iso()),
        })

    seen_uid, seen_name = set(), set()
    for z in _as_list(obj.get("zones"), "zones", warn):
        zz = dict(z)
        uid = str(zz.get("uid") or "").strip()
        name = str(zz.get("name") or "").strip()
        if not uid or not name:
            warn.append(f"区域缺少 uid/name，已跳过：{z!r}")
            continue
        if uid in seen_uid or name in seen_name:
            warn.append(f"区域 uid/名重复，已跳过：{uid}/{name}")
            continue
        seen_uid.add(uid)
        seen_name.add(name)
        shape = zz.get("shape") if zz.get("shape") in SHAPES else "polygon"
        poly = []
        for pt in (zz.get("polygon") or []):
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                try:
                    poly.append([float(pt[0]), float(pt[1])])
                except (TypeError, ValueError):
                    continue
        if shape == "polygon" and len(poly) < 3:
            warn.append(f"区域 {name} 的多边形少于 3 个点，已跳过")
            continue
        normalized = {
            "uid": uid, "name": name,
            "kind": zz.get("kind") if zz.get("kind") in ZONE_KINDS else "room",
            "shape": shape, "polygon": poly,
            "parent": str(zz.get("parent") or ""),
            "note": str(zz.get("note") or ""),
            "created_at": str(zz.get("created_at") or db.now_iso()),
            "updated_at": str(zz.get("updated_at") or db.now_iso()),
        }
        if "goal" in zz:
            goal, reason = _norm_goal(zz.get("goal"))
            if goal is None:
                warn.append(f"区域 {name} goal 非法，已丢弃：{reason}")
            else:
                normalized["goal"] = goal
        out["zones"].append(normalized)
    return out, warn


def _as_list(val, key: str, warn: list[str]) -> list:
    if val is None:
        return []
    if not isinstance(val, list):
        warn.append(f"tags.json 的 {key} 不是数组，已按空处理")
        return []
    return [x for x in val if isinstance(x, dict)]


def _num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num_or_zero(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _norm_goal(value) -> tuple[dict | None, str | None]:
    """规范区域停靠点；拒绝 bool、NaN、Infinity 和缺失坐标。"""
    if not isinstance(value, dict):
        return None, "必须是对象"
    values = {}
    for key in ("x", "y"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, f"{key} 必须是 JSON number"
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None, f"{key} 不是数值"
        if not math.isfinite(number):
            return None, f"{key} 必须是有限数值"
        values[key] = number
    raw_yaw = value.get("yaw_deg", 0.0)
    if isinstance(raw_yaw, bool) or not isinstance(raw_yaw, (int, float)):
        return None, "yaw_deg 必须是 JSON number"
    try:
        yaw = float(raw_yaw)
    except (TypeError, ValueError, OverflowError):
        return None, "yaw_deg 不是数值"
    if not math.isfinite(yaw):
        return None, "yaw_deg 必须是有限数值"
    values["yaw_deg"] = yaw
    return values, None


# ---------------------------------------------------------------------------
# 读：tags.json → 内存（唯一真相）
# ---------------------------------------------------------------------------
def resolve(map_name: str, store=None) -> dict:
    """读该图 tags.json 的完整内容。

    返回 ``{ok, map, tags, exists, warnings, stale, cached_at, error}``。
    文件不存在**不是错误**（返回空结构 + ``exists=False``）—— 与"残缺态"区分开。
    """
    st = store or get_store()
    name = check_name(map_name)
    out = {"ok": True, "map": name, "tags": empty_tags(name), "exists": False,
           "warnings": [], "stale": False, "cached_at": None, "error": ""}
    try:
        raw, stale, at = st.read_with_meta(name, "tags")
    except MapStoreError as e:
        msg = str(e)
        if "不存在" in msg or "No such file" in msg:
            out["warnings"].append("本图还没有 .tags.json（还没有标过任何地点/区域）")
            return out
        out.update({"ok": False, "error": msg})
        return out
    out["exists"] = True
    out["stale"] = stale
    out["cached_at"] = at
    try:
        obj = _parse(raw)
    except MapStoreError as e:
        out.update({"ok": False, "error": str(e)})
        return out
    tags, warn = _norm_tags(obj, name)
    out["tags"] = tags
    out["warnings"] = warn
    # 指纹比对（§7.2）：tags 里的 resolution/origin 与当前 yaml 实际值不一致 → 明确告知
    try:
        info = mapserver.map_info(name, st)
        out["fingerprint"] = fingerprint_check(tags, info)
    except MapStoreError as e:
        out["fingerprint"] = {"ok": False, "error": str(e), "changed": False, "reasons": []}
    return out


def fingerprint_check(tags: dict, info: dict) -> dict:
    """比 ``tags.json`` 的指纹与当前 yaml 实际值（规格 §7.2：让"元数据被改过"第一次可检测）。"""
    reasons: list[str] = []
    if tags.get("resolution") is None or not tags.get("origin"):
        return {"ok": True, "changed": False, "reasons": ["该 tags.json 无指纹（早期或手工创建）"],
                "tags": {"resolution": tags.get("resolution"), "origin": tags.get("origin")},
                "yaml": {"resolution": info.get("resolution"), "origin": info.get("origin")}}
    res_now, org_now = info.get("resolution"), info.get("origin")
    if res_now is None or not org_now:
        return {"ok": False, "changed": False, "reasons": [f"yaml 元数据不可用：{info.get('problems')}"]}
    changed = False
    if abs(float(tags["resolution"]) - float(res_now)) > 1e-9:
        changed = True
        reasons.append(f"resolution 已变：tags.json={tags['resolution']} vs yaml={res_now}")
    t_org = list(tags["origin"])[:2]
    y_org = list(org_now)[:2]
    if any(abs(float(a) - float(b)) > 1e-9 for a, b in zip(t_org, y_org)):
        changed = True
        reasons.append(f"origin 已变：tags.json={t_org} vs yaml={y_org}")
    if changed:
        n = len(tags.get("destinations") or [])
        m = len(tags.get("zones") or [])
        reasons.append(f"本图 {n} 个地点 / {m} 个区域可能整体失准")
    return {"ok": True, "changed": changed, "reasons": reasons,
            "tags": {"resolution": tags.get("resolution"), "origin": tags.get("origin")},
            "yaml": {"resolution": res_now, "origin": y_org}}


# ---------------------------------------------------------------------------
# 写：内存 → tags.json（原子写，唯一写入口）
# ---------------------------------------------------------------------------
def save(map_name: str, tags: dict, store=None, event: str = "map_change",
         action: str = "tags_save", **audit_fields) -> dict:
    """把完整 tags 结构写回 ``<图名>.tags.json``（原子写）→ 刷缓存 → 审计。

    **这是本模块唯一的落盘出口**：所有地点/区域的增删改都必须走它（红线 2）。
    """
    st = store or get_store()
    name = check_name(map_name)
    tags = copy.deepcopy(tags)
    tags["version"] = VERSION
    tags["map"] = name
    tags["updated_at"] = db.now_iso()
    tags.setdefault("created_at", tags["updated_at"])
    _refresh_fingerprint(name, tags, st)
    payload = json.dumps(tags, ensure_ascii=False, indent=2).encode("utf-8")
    st.write(name, "tags", payload)
    stt = None
    try:
        stt = st.stat(name, "tags")
    except Exception:      # noqa: BLE001
        pass
    man = sync_map(name, store=st, force=True)
    audit.log(event, action=action, map=name,
              destinations=len(tags.get("destinations") or []),
              zones=len(tags.get("zones") or []),
              bytes=len(payload), **audit_fields)
    return {"ok": True, "map": name, "bytes": len(payload), "stat": stt,
            "cache": {"destinations": man.get("destinations", 0),
                      "zones": man.get("zones", 0),
                      "warnings": man.get("warnings", [])}}


def _refresh_fingerprint(name: str, tags: dict, st) -> None:
    """写盘前用**当前 yaml 的实际值**刷新指纹（规格 §7.2 第 3 条：让"已确认改元数据"被记录）。"""
    try:
        info = mapserver.map_info(name, st)
    except MapStoreError:
        return
    if info.get("resolution") is not None:
        tags["resolution"] = float(info["resolution"])
    if info.get("origin"):
        tags["origin"] = [float(v) for v in info["origin"][:3]]


# ---------------------------------------------------------------------------
# 索引缓存同步（单向：文件 → SQLite）
# ---------------------------------------------------------------------------
def sync_map(map_name: str, store=None, force: bool = False) -> dict:
    """按需把 ``<图名>.tags.json`` 同步进索引缓存；返回缓存计数与告警。

    **幂等**：``mtime+size+sha1`` 三项都没变则直接返回现有清单（一次 stat 的代价）。
    缓存可丢弃：任何异常都只影响缓存，读路径仍可回退到直接读文件。
    """
    st = store or get_store()
    name = check_name(map_name)
    stt = None
    try:
        stt = st.stat(name, "tags")
    except Exception:      # noqa: BLE001
        stt = None
    man_db = db.get_map_tags_manifest(name)
    if stt is None:
        # 文件不在了 → 缓存也清掉（缓存只是索引，不能比真相活得久）
        if man_db is not None:
            db.drop_map_tags(name)
        return {"map": name, "exists": False, "destinations": 0, "zones": 0,
                "warnings": ["本图没有 .tags.json"], "rebuilt": man_db is not None}
    if not force and man_db is not None:
        same_stat = (abs(float(man_db.get("file_mtime") or 0) - stt[0]) < 1e-6
                     and int(man_db.get("file_size") or -1) == stt[1])
        if same_stat:
            return {"map": name, "exists": True, "rebuilt": False,
                    "destinations": db.count_map_tags(name)["destinations"],
                    "zones": db.count_map_tags(name)["zones"],
                    "warnings": man_db.get("warnings") or [],
                    "fingerprint": {"resolution": man_db.get("resolution"),
                                    "origin": man_db.get("origin")}}
    got = resolve(name, st)
    if not got["ok"]:
        raise MapStoreError(got.get("error") or "读取 tags.json 失败")
    tags = got["tags"]
    warn = list(got.get("warnings") or [])
    fp = got.get("fingerprint") or {}
    if fp.get("changed"):
        warn = warn + list(fp.get("reasons") or [])
    raw = json.dumps(tags, ensure_ascii=False, sort_keys=True).encode("utf-8")
    sha = hashlib.sha1(raw).hexdigest()          # noqa: S324 非安全用途，仅做变更指纹
    dests = [{
        "uid": d["uid"], "name": d["name"], "aliases": ",".join(d.get("aliases") or []),
        "x": d["x"], "y": d["y"], "yaw_deg": d["yaw_deg"], "risk": d["risk"],
        "elder_allowed": d["elder_allowed"], "note": d["note"],
        "learned_by": d["learned_by"], "created_at": d["created_at"],
        "updated_at": d["updated_at"],
    } for d in tags["destinations"]]
    zones = [{
        "uid": z["uid"], "name": z["name"], "kind": z["kind"], "shape": z["shape"],
        "polygon_json": json.dumps(z["polygon"]), "parent": z["parent"],
        "note": z["note"], "created_at": z["created_at"], "updated_at": z["updated_at"],
    } for z in tags["zones"]]
    db.replace_map_tags(name, {
        "file_mtime": stt[0], "file_size": stt[1], "sha1": sha,
        "resolution": tags.get("resolution"), "origin": tags.get("origin"),
        "warnings": warn,
    }, dests, zones)
    return {"map": name, "exists": True, "rebuilt": True,
            "destinations": len(dests), "zones": len(zones), "warnings": warn,
            "fingerprint": {"resolution": tags.get("resolution"), "origin": tags.get("origin")}}


# ---------------------------------------------------------------------------
# 读路径：地点 / 区域（缓存优先，stale 自动重建）
# ---------------------------------------------------------------------------
def get_destinations(map_name: str, store=None) -> list[dict]:
    name = check_name(map_name)
    _ensure_fresh(name, store)
    rows = db.list_destinations(name)
    for r in rows:
        r["aliases_list"] = [a for a in (r.get("aliases") or "").split(",") if a]
    return rows


def get_zones(map_name: str, store=None) -> list[dict]:
    name = check_name(map_name)
    _ensure_fresh(name, store)
    return db.list_zones(name)


def get_zone(map_name: str, uid: str, store=None) -> dict | None:
    for z in get_zones(map_name, store):
        if z["uid"] == uid:
            return z
    return None


def get_destination(map_name: str, uid: str, store=None) -> dict | None:
    for d in get_destinations(map_name, store):
        if d["uid"] == uid:
            return d
    return None


def _ensure_fresh(map_name: str, store=None) -> None:
    """读前比 mtime+size；不一致就整图重建。**任何失败都降级为"缓存照用"**。"""
    st = store or get_store()
    try:
        sync_map(map_name, st)
    except Exception as e:      # noqa: BLE001  板卡不可达/文件坏 → 缓存照用（§B4.2 口径）
        audit.log("map_change", action="cache_stale", map=map_name, error=str(e))


# ---------------------------------------------------------------------------
# 增删改（一律走「读文件 → 改内存 → 写文件」）
# ---------------------------------------------------------------------------
def _next_uid(items: list[dict], prefix: str) -> str:
    used = set()
    for it in items:
        u = str(it.get("uid") or "")
        if u.startswith(prefix) and u[len(prefix):].isdigit():
            used.add(int(u[len(prefix):]))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n}"


def _load_or_empty(map_name: str, store) -> dict:
    got = resolve(map_name, store)
    if not got["ok"]:
        raise MapStoreError(got.get("error") or "读取 tags.json 失败")
    return got["tags"]


def upsert_destination(map_name: str, data: dict, uid: str = "", store=None) -> dict:
    """新增（``uid=""``）或修改某个地点；返回 ``(uid, warnings)`` 摘要。"""
    st = store or get_store()
    name = check_name(map_name)
    tags = _load_or_empty(name, st)
    now = db.now_iso()
    warnings: list[str] = []

    other = [d for d in tags["destinations"] if d["uid"] != uid]
    dup = [d for d in other if d["name"] == (data.get("name") or "").strip()]
    if dup:
        raise MapStoreError(f"地点名已存在：{data.get('name')!r}")

    if uid:
        cur = next((d for d in tags["destinations"] if d["uid"] == uid), None)
        if cur is None:
            raise MapStoreError(f"地点不存在：{uid}")
        target = cur
    else:
        target = {"uid": _next_uid(tags["destinations"], "d"), "created_at": now,
                  "learned_by": "editor"}
        tags["destinations"].append(target)
    for k in ("name", "note", "risk", "learned_by"):
        if data.get(k) is not None:
            target[k] = str(data[k]).strip() if k != "risk" else (
                data[k] if data[k] in RISKS else "low")
    if data.get("aliases") is not None:
        al = data["aliases"]
        if isinstance(al, str):
            al = [a.strip() for a in al.replace("、", ",").split(",")]
        target["aliases"] = [str(a).strip() for a in al if str(a).strip()]
    for k in ("x", "y", "yaw_deg"):
        if data.get(k) is not None:
            target[k] = _num_or_zero(data[k])
    if data.get("elder_allowed") is not None:
        target["elder_allowed"] = 0 if str(data["elder_allowed"]) in ("0", "False", "false") else 1
    target.setdefault("aliases", [])
    target.setdefault("risk", "low")
    target.setdefault("elder_allowed", 1)
    target.setdefault("note", "")
    target.setdefault("learned_by", "editor")
    target["updated_at"] = now
    if not str(target.get("name") or "").strip():
        raise MapStoreError("地点必须有名字")

    # 即时校验（§7.6）：越界/障碍/未知只警告不阻止，但必须在 UI 上留痕
    if target.get("x") is not None and target.get("y") is not None:
        try:
            v = mapserver.validate_point(name, float(target["x"]), float(target["y"]), st)
            warnings += v.get("reasons") or []
            target["_last_check"] = {"pixel_kind": v.get("pixel_kind"),
                                     "clearance_m": v.get("clearance_m"),
                                     "edge_margin_m": v.get("edge_margin_m")}
        except MapStoreError as e:
            warnings.append(f"校验不可用：{e}")
    target.pop("_last_check", None)

    res = save(name, tags, st, action="destination_upsert" if uid else "destination_add",
               uid=target["uid"], place_name=target["name"])
    return {"uid": target["uid"], "warnings": warnings, "save": res}


def delete_destination(map_name: str, uid: str, store=None) -> dict:
    st = store or get_store()
    name = check_name(map_name)
    tags = _load_or_empty(name, st)
    before = len(tags["destinations"])
    tags["destinations"] = [d for d in tags["destinations"] if d["uid"] != uid]
    if len(tags["destinations"]) == before:
        raise MapStoreError(f"地点不存在：{uid}")
    save(name, tags, st, action="destination_delete", uid=uid)
    return {"ok": True, "uid": uid}


def upsert_zone(map_name: str, data: dict, uid: str = "", store=None) -> dict:
    st = store or get_store()
    name = check_name(map_name)
    tags = _load_or_empty(name, st)
    now = db.now_iso()
    warnings: list[str] = []
    other = [z for z in tags["zones"] if z["uid"] != uid]
    if any(z["name"] == (data.get("name") or "").strip() for z in other):
        raise MapStoreError(f"区域名已存在：{data.get('name')!r}")

    if uid:
        cur = next((z for z in tags["zones"] if z["uid"] == uid), None)
        if cur is None:
            raise MapStoreError(f"区域不存在：{uid}")
        target = cur
    else:
        target = {"uid": _next_uid(tags["zones"], "z"), "created_at": now}
        tags["zones"].append(target)
    if data.get("name") is not None:
        target["name"] = str(data["name"]).strip()
    if data.get("kind") is not None:
        target["kind"] = data["kind"] if data["kind"] in ZONE_KINDS else "room"
    if data.get("shape") is not None:
        target["shape"] = data["shape"] if data["shape"] in SHAPES else "polygon"
    if data.get("polygon") is not None:
        poly = []
        for pt in data["polygon"]:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                poly.append([_num_or_zero(pt[0]), _num_or_zero(pt[1])])
        target["polygon"] = poly
    if data.get("parent") is not None:
        target["parent"] = str(data["parent"]).strip()
    if data.get("note") is not None:
        target["note"] = str(data["note"])
    # ``goal`` is special: absent means "leave it unchanged" on update, while
    # explicit None means clear it.  API callers preserve this distinction.
    if "goal" in data:
        if data["goal"] is None:
            target.pop("goal", None)
        else:
            goal, reason = _norm_goal(data["goal"])
            target.pop("goal", None)
            if goal is None:
                warnings.append(f"区域 {target.get('name') or uid} goal 非法，已丢弃：{reason}")
            else:
                target["goal"] = goal
    target.setdefault("kind", "room")
    target.setdefault("shape", "polygon")
    target.setdefault("polygon", [])
    target.setdefault("parent", "")
    target.setdefault("note", "")
    target["updated_at"] = now
    if not str(target.get("name") or "").strip():
        raise MapStoreError("区域必须有名字")
    if target["shape"] == "polygon" and len(target["polygon"]) < 3:
        raise MapStoreError("多边形区域至少需要 3 个点")
    if target.get("parent"):
        p = next((z for z in tags["zones"] if z["uid"] == target["parent"]), None)
        if p is None:
            raise MapStoreError(f"父区域不存在：{target['parent']}")
        if p["uid"] == target["uid"]:
            raise MapStoreError("父区域不能是自己")
    goal_validation = None
    if target.get("goal") is not None:
        goal = target["goal"]
        reasons: list[str] = []
        try:
            checked = mapserver.validate_point(name, goal["x"], goal["y"], st)
            reasons.extend(checked.get("reasons") or [])
            if checked.get("ok") is False and not reasons:
                reasons.append("停靠点无法校验")
        except Exception as e:  # noqa: BLE001  校验不可用不应阻断编辑保存
            reasons.append(f"停靠点无法校验：{e}")
        try:
            if not zonegeo.zone_hit(target, goal["x"], goal["y"]):
                reasons.append("停靠点在区域外")
        except Exception as e:  # noqa: BLE001  坏几何只产生告警
            reasons.append(f"停靠点区域归属无法校验：{e}")
        goal_validation = {"ok": not reasons, "reasons": reasons}
    save(name, tags, st, action="zone_upsert" if uid else "zone_add",
         uid=target["uid"], zone_name=target["name"])
    return {"ok": True, "uid": target["uid"], "warnings": warnings,
            "goal_validation": goal_validation}


def delete_zone(map_name: str, uid: str, store=None) -> dict:
    st = store or get_store()
    name = check_name(map_name)
    tags = _load_or_empty(name, st)
    before = len(tags["zones"])
    tags["zones"] = [z for z in tags["zones"] if z["uid"] != uid]
    if len(tags["zones"]) == before:
        raise MapStoreError(f"区域不存在：{uid}")
    # 断开挂在它下面的子区域（不级联删除，避免静默丢数据）
    children = [z["uid"] for z in tags["zones"] if z.get("parent") == uid]
    for z in tags["zones"]:
        if z.get("parent") == uid:
            z["parent"] = ""
    save(name, tags, st, action="zone_delete", uid=uid, orphaned=children)
    return {"ok": True, "uid": uid, "orphaned": children}


def replace_all(map_name: str, obj: dict, store=None) -> dict:
    """整份替换（``PUT /api/map/{name}/tags``，高级用途）：走 schema 规范化后再落盘。"""
    st = store or get_store()
    name = check_name(map_name)
    tags, warn = _norm_tags(obj if isinstance(obj, dict) else {}, name)
    res = save(name, tags, st, action="tags_replace")
    res["warnings"] = warn
    return res


# ---------------------------------------------------------------------------
# 记录当前位置（"记录当前位姿成地点"）
# ---------------------------------------------------------------------------
def learn_here(map_name: str, data: dict, pose: dict | None, store=None) -> dict:
    """把**当前位姿**存成地点（规格 §5.2 ``/api/destinations/learn``）。位姿不可用则明确失败。"""
    if not pose or pose.get("x") is None or pose.get("y") is None:
        raise MapStoreError("当前位姿不可用（导航未运行 / rosbridge 未启动）；可改为在图上点选")
    payload = dict(data or {})
    payload["x"] = pose["x"]
    payload["y"] = pose["y"]
    if payload.get("yaw_deg") is None and pose.get("yaw") is not None:
        import math
        payload["yaw_deg"] = math.degrees(float(pose["yaw"]))
    payload.setdefault("learned_by", "learn_button")
    out = upsert_destination(map_name, payload, store=store)
    out["pose"] = {"x": pose["x"], "y": pose["y"], "yaw": pose.get("yaw"),
                   "source": pose.get("source")}
    return out


# ---------------------------------------------------------------------------
# 快捷入口：把"当前房间"记成病房区域（16 边形近似圆，供分层体系的 D17 用）
# ---------------------------------------------------------------------------
def record_room_polygon(map_name: str, name: str, x: float, y: float, radius_m: float = 2.0,
                        kind: str = "ward", note: str = "", store=None,
                        segments: int = 16) -> dict:
    """以 ``(x, y)`` 为圆心采样 ``segments`` 边形写入区域（规格「便捷入口」条）。"""
    import math
    poly = []
    for i in range(max(8, int(segments))):
        a = 2 * math.pi * i / max(8, int(segments))
        poly.append([x + radius_m * math.cos(a), y + radius_m * math.sin(a)])
    return upsert_zone(map_name, {"name": name, "kind": kind, "shape": "polygon",
                                  "polygon": poly, "note": note}, store=store)


def reindex(map_name: str, store=None) -> dict:
    """强制重建某图的索引缓存（``POST /api/map/{name}/tags/reindex``）。"""
    name = check_name(map_name)
    out = sync_map(name, store=store, force=True)
    audit.log("map_change", action="tags_reindex", map=name, **{
        k: out.get(k) for k in ("destinations", "zones", "rebuilt")})
    return out


def reindex_all(store=None) -> list[dict]:
    """重建所有图的缓存（缓存整个丢了时的恢复入口）。"""
    st = store or get_store()
    out = []
    for meta in st.list():
        try:
            out.append(sync_map(meta["name"], st, force=True))
        except Exception as e:      # noqa: BLE001  单张图坏不影响其它
            out.append({"map": meta["name"], "error": str(e)})
    return out


def file_stat(map_name: str, store=None) -> tuple[float, int] | None:
    st = store or get_store()
    return st.stat(check_name(map_name), "tags")


def file_mtime_iso(map_name: str, store=None) -> str:
    stt = file_stat(map_name, store)
    if not stt:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stt[0]))


def tags_path(map_name: str, store=None) -> str:
    """给前端/日志显示的路径（local 是绝对路径，ssh 是远端路径）。"""
    st = store or get_store()
    name = check_name(map_name)
    root = str(st.root).rstrip("/\\")
    sep = os.sep if getattr(st, "io_mode", "") == "local" else "/"
    return f"{root}{sep}{name}.tags.json"
