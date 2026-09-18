# -*- coding: utf-8 -*-
r"""导航目标解析与 fail-closed 安全校验。"""
from __future__ import annotations

import copy
import math
from typing import Callable

from ..agent import session as _session
from ..maps import maptags as _maptags
from ..maps import mapserver as _mapserver
from ..core import zonegeo as _zonegeo


class CarNav:
    def __init__(self, running_map_name: Callable | None = None,
                 resolve: Callable | None = None, map_info: Callable | None = None,
                 validate_point: Callable | None = None,
                 fingerprint_check: Callable | None = None,
                 zone_hit: Callable | None = None):
        self.running_map_name = running_map_name or _session.running_map_name
        self.resolve_tags = resolve or _maptags.resolve
        self.map_info = map_info or _mapserver.map_info
        self.validate_point = validate_point or _mapserver.validate_point
        self.fingerprint_check = fingerprint_check or _maptags.fingerprint_check
        self.zone_hit = zone_hit or _zonegeo.zone_hit

    @staticmethod
    def _bad(error: str, hint: str = "") -> dict:
        return {"ok": False, "status": "rejected", "error": str(error),
                "hint": hint, "warnings": []}

    @staticmethod
    def _finite(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))

    def _context(self):
        try:
            running = self.running_map_name()
            if not isinstance(running, (tuple, list)) or not running:
                return None, self._bad("当前地图未知", "请确认导航正在运行并重试")
            name = str(running[0] or "").strip()
            reason = running[1] if len(running) > 1 else ""
            if not name or reason:
                return None, self._bad("当前地图未知", str(reason or "请确认导航正在运行并重试"))
            got = self.resolve_tags(name)
            if not isinstance(got, dict) or not got.get("ok") or not got.get("exists"):
                return None, self._bad(str((got or {}).get("error") or "地图标记不存在"), "请在地图编辑器标记地点和区域")
            if got.get("stale"):
                return None, self._bad("地图标记缓存已过期", "请恢复地图连接后重试")
            tags = got.get("tags") or {}
            info = self.map_info(name)
            if (not isinstance(info, dict) or info.get("meta_ok") is False or
                    info.get("resolution") is None or not info.get("origin")):
                return None, self._bad("yaml 元数据不可用", "请检查当前地图文件")
            if info.get("stale"):
                return None, self._bad("地图元数据缓存已过期", "请恢复地图连接后重试")
            fp = self.fingerprint_check(tags, info)
            if not isinstance(fp, dict) or not fp.get("ok") or fp.get("changed"):
                return None, self._bad("地图指纹已变化，拒绝使用旧标记", "请在地图编辑器重新校准标记")
            warnings = list(got.get("warnings") or []) + list(fp.get("reasons") or [])
            if any("无指纹" in str(item) for item in warnings):
                warnings.append("tags 无指纹")
            return (name, tags, warnings), None
        except Exception as exc:  # fail closed; no dependency errors leak
            return None, self._bad("地图解析失败", "请检查地图与导航状态")

    def _safe_point(self, name, x, y, yaw, source, warnings):
        try:
            checked = self.validate_point(name, float(x), float(y))
        except Exception:
            return self._bad("地图无法校验", "请检查地图文件")
        reasons = list(checked.get("reasons") or []) if isinstance(checked, dict) else []
        reject = (not isinstance(checked, dict) or not checked.get("ok") or
                  checked.get("stale") or
                  not checked.get("in_bounds", True) or checked.get("on_obstacle") or
                  checked.get("on_unknown"))
        for key in ("edge_margin_m", "clearance_m"):
            val = checked.get(key) if isinstance(checked, dict) else None
            if self._finite(val) and float(val) < 0.3:
                reject = True
        if reject:
            return self._bad("目标点安全校验未通过：" + ("；".join(map(str, reasons)) or "地图无法校验"), "请选择地图内的可通行停靠点")
        return {"ok": True, "map": name,
                "target": {"x": float(x), "y": float(y), "yaw_deg": float(yaw), "goal_source": source},
                "warnings": warnings, "summary_target": f"{source} ({float(x):.2f}, {float(y):.2f})"}

    @staticmethod
    def _zone_area(zone) -> float:
        points = zone.get("polygon") or []
        try:
            coords = [(float(point[0]), float(point[1])) for point in points
                      if isinstance(point, (list, tuple)) and len(point) >= 2]
            if len(coords) < 3:
                return math.inf
            return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2)
                           in zip(coords, coords[1:] + coords[:1]))) / 2.0
        except (TypeError, ValueError, OverflowError):
            return math.inf

    def status_context(self, pose) -> dict:
        if not isinstance(pose, dict) or not self._finite(pose.get("x")) or not self._finite(pose.get("y")):
            return {"map": None, "zone": None, "reason": "位姿不可用", "warnings": []}
        ctx, err = self._context()
        if err:
            return {"map": None, "zone": None, "reason": err["error"],
                    "warnings": list(err.get("warnings") or [])}
        name, tags, warnings = ctx
        matches = [zone for zone in (tags.get("zones") or [])
                   if isinstance(zone, dict) and self.zone_hit(zone, float(pose["x"]), float(pose["y"]))]
        zone = min(matches, key=lambda item: (self._zone_area(item), str(item.get("uid", "")))) if matches else None
        return {"map": name, "zone": copy.deepcopy(zone) if zone else None,
                "reason": "" if zone else "当前位置不在已标区域", "warnings": warnings}

    def resolve_point(self, x, y, yaw_deg=0.0) -> dict:
        if not all(self._finite(v) for v in (x, y, yaw_deg)):
            return self._bad("坐标和 yaw 必须是有限数字", "请传入数值坐标")
        ctx, err = self._context()
        if err: return err
        name, _tags, warnings = ctx
        return self._safe_point(name, x, y, yaw_deg, "point", warnings)

    def resolve_place(self, place: str) -> dict:
        try:
            return self._resolve_place(place)
        except Exception:
            return self._bad("地点解析失败", "请检查地图标记")

    def _resolve_place(self, place: str) -> dict:
        if not isinstance(place, str) or not place.strip():
            return self._bad("地点名称不能为空", "请提供地点名称")
        ctx, err = self._context()
        if err: return err
        name, tags, warnings = ctx
        needle = place.strip()
        matches = [d for d in tags.get("destinations", [])
                   if needle == str(d.get("name", "")).strip() or needle in [str(a).strip() for a in (d.get("aliases") or [])]]
        if not matches:
            avail = ", ".join(str(d.get("name")) for d in tags.get("destinations", []))
            return self._bad("未找到地点", f"当前图可用地点：{avail or '无'}")
        if len(matches) > 1:
            return self._bad("地点名称不唯一", "请使用精确地点名")
        d = matches[0]
        if not all(self._finite(d.get(k)) for k in ("x", "y", "yaw_deg")):
            return self._bad("地点坐标无效", "请在地图编辑器修正该地点")
        return self._safe_point(name, d["x"], d["y"], d["yaw_deg"], "destination", warnings)

    def resolve_zone(self, zone: str) -> dict:
        try:
            return self._resolve_zone(zone)
        except Exception:
            return self._bad("区域解析失败", "请检查地图区域与停靠点")

    def _resolve_zone(self, zone: str) -> dict:
        if not isinstance(zone, str) or not zone.strip():
            return self._bad("区域名称不能为空", "请提供区域名称")
        ctx, err = self._context()
        if err: return err
        name, tags, warnings = ctx
        matches = [z for z in tags.get("zones", []) if zone.strip() == str(z.get("name", "")).strip()]
        if not matches: return self._bad("未找到区域", "当前图可用区域：" + ", ".join(str(z.get("name")) for z in tags.get("zones", [])))
        if len(matches) > 1: return self._bad("区域名称不唯一", "请使用精确区域名")
        z = matches[0]
        goal = z.get("goal")
        if isinstance(goal, dict) and all(self._finite(goal.get(k)) for k in ("x", "y")):
            yaw = goal.get("yaw_deg", 0.0)
            if not self._finite(yaw) or not self.zone_hit(z, float(goal["x"]), float(goal["y"])):
                return self._bad("区域显式目标不在区域内", "请在地图编辑器修正区域停靠点")
            return self._safe_point(name, goal["x"], goal["y"], yaw, "explicit", warnings)
        candidates = [d for d in tags.get("destinations", [])
                      if self._finite(d.get("x")) and self._finite(d.get("y")) and
                      self._finite(d.get("yaw_deg")) and self.zone_hit(z, d["x"], d["y"])]
        if not candidates: return self._bad("区域没有可用停靠点", "请去地图编辑器标停靠点")
        poly = z.get("polygon") or []
        cx = sum(float(p[0]) for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2) / max(1, len(poly))
        cy = sum(float(p[1]) for p in poly if isinstance(p, (list, tuple)) and len(p) >= 2) / max(1, len(poly))
        d = min(candidates, key=lambda q: (q["x"]-cx)**2 + (q["y"]-cy)**2)
        out = self._safe_point(name, d["x"], d["y"], d.get("yaw_deg", 0.0), "destination", warnings + [f"区域无显式目标，回退地点：{d.get('name')}" ] )
        return out
