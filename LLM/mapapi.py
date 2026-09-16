# -*- coding: utf-8 -*-
r"""地图编辑器后端（独立服务）—— 从 ``server.py`` 原样搬来的编辑器专属路由。

为什么单独一个模块（规格 docs/superpowers/specs/2026-09-15-map-editor-on-demand-service-design.md）：
编辑器**默认不跑**，由主后端按需以独立进程拉起（``LLM.mapeditor_server:app``，:8010）；
主后端只保留陪护需要的 ``locator`` / ``maptags``（病房位置自动切换、记录病房区域），
不再背着这 900 行。

红线（规格 §4）：标记的唯一真相是地图文件夹里的 <图名>.tags.json，brain.db 只是只读索引缓存；
             所有写接口都是「改文件 → maptags.sync_map() → 审计」，绝不"只改库不改文件"。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import conf, db
from . import log as audit
from .conf import BASE_DIR

router = APIRouter()


# ---------------------------------------------------------------- 地图编辑器模型
class MapMetaIn(BaseModel):
    resolution: float | None = None
    origin: list[float] | None = None
    negate: int | None = None
    occupied_thresh: float | None = None
    free_thresh: float | None = None
    confirm: bool = False


class MapNameIn(BaseModel):
    new_name: str
    confirm: bool = False


class DestinationIn(BaseModel):
    map_name: str
    name: str = ""
    aliases: list[str] | str = []
    x: float | None = None
    y: float | None = None
    yaw_deg: float | None = None
    risk: str = "low"
    elder_allowed: int = 1
    note: str = ""
    learned_by: str = "editor"


class LearnIn(BaseModel):
    map_name: str
    name: str = ""
    aliases: list[str] | str = []
    risk: str = "low"
    elder_allowed: int = 1
    note: str = ""


class ZoneIn(BaseModel):
    map_name: str
    name: str = ""
    kind: str = "room"
    shape: str = "polygon"
    polygon: list[list[float]] = []
    parent: str = ""
    note: str = ""


class ValidateIn(BaseModel):
    map_name: str
    x: float
    y: float
    margin_m: float | None = None


class MapTagsIn(BaseModel):
    version: int = 1
    map: str = ""
    resolution: float | None = None
    origin: list[float] | None = None
    destinations: list[dict] = []
    zones: list[dict] = []


class MapSaveIn(BaseModel):
    pgm_b64: str = ""
    yaml_text: str = ""
    mode: str = "saveas"        # saveas | overwrite
    new_name: str = ""
    confirm: bool = False


class PoseInjectIn(BaseModel):
    x: float | None = None
    y: float | None = None
    yaw: float | None = None
    width: int | None = None
    height: int | None = None
    resolution: float | None = None
    origin: list[float] | None = None


# ---------------------------------------------------------------------------
# 地图编辑器（第三个前端 /mapeditor）
# 规格：docs/superpowers/specs/2026-09-14-map-editor-design.md
#   红线（§4）：标记的唯一真相是地图文件夹里的 <图名>.tags.json，brain.db 只是只读索引缓存；
#               所有写接口都是「改文件 → maptags.sync_map() → 审计」，绝不"只改库不改文件"。
# ---------------------------------------------------------------------------
from . import mapserver, maptags, mapstore, locator   # noqa: E402  （放此处便于阅读，import 无副作用）
from fastapi.responses import Response                 # noqa: E402

_MAP_EXTS = ("yaml", "pgm", "tags")


def _store(source: str = ""):
    """「地图源」依赖：``source`` 空 = 用默认源（``mapsources.default``）。

    **源未知一律转 HTTP 400**（附可用源清单）——这是用户可纠正的输入错误，不能变 500
    （实测：未知源原来会抛 500）。作为 FastAPI 依赖使用时（``store=Depends(_store)``）
    异常自动变成 400 响应；直接调用时抛 ``HTTPException``，路由无需各自 try/except。
    """
    from fastapi import HTTPException
    from . import mapsources
    try:
        return mapstore.get_store(source)
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _store_or_err(source: str = ""):
    """``(store, None)`` 或 ``(None, 400 响应)``：所有带 ``?source=`` 的路由统一用它。

    未知源是用户可纠正的输入错误，必须 400 + 可用源清单（实测直接抛会变 500）。
    """
    from . import mapsources
    try:
        return mapstore.get_store(source), None
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        return None, _err(str(e))


def _err(msg: str, code: int = 400, **extra):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=code, content={"ok": False, "error": msg, **extra})


def _name_of(path_name: str) -> str:
    """路径参数 → 合法地图名；不合法直接抛（由各路由统一转 400）。"""
    return mapstore.check_name(path_name)


def _tag_counts(names: list[str]) -> dict[str, dict]:
    """批量取各图的标记数量（读缓存，缺失即 0，不触发远程读）。"""
    out: dict[str, dict] = {}
    for n in names:
        try:
            out[n] = db.count_map_tags(n)
        except Exception:      # noqa: BLE001
            out[n] = {"destinations": 0, "zones": 0}
    return out


# ---------------------------------------------------------------- 地图源（sources）
@router.get("/api/map/sources")
async def map_sources_list(test: bool = Query(False)):
    """列出全部地图源（只读；界面只做"选"、不手填路径）。

    ``test=true`` 时对每个源真连一次（等价于 ``/test``），慢（ssh 每个源一次往返）；
    默认只给配置态，连通性由前端按需点"自检"或对当前源调用 ``/test``。
    """
    from . import mapsources
    doc = mapsources.load()
    store_map = {}
    if test:
        for s in doc["items"]:
            try:
                store_map[s["id"]] = mapstore.get_store(s["id"])
            except Exception:      # noqa: BLE001  单个源造不出来不影响列表
                pass
    return mapsources.view_list(store_map or None)


@router.post("/api/map/sources")
async def map_sources_upsert(body: dict = None):
    """新增/修改一条源。**这是配置入口，不是给界面自由填路径用的**：界面只做选择。"""
    from . import log as audit, mapsources
    body = body or {}
    src = body.get("source") if isinstance(body.get("source"), dict) else body
    try:
        doc = mapsources.upsert(src)
    except mapsources.SourceError as e:
        return _err(str(e))
    mapstore.reset_store()                 # 源变了 → 丢掉缓存实例，下次按新配置重建
    audit.log("map_source", action="upsert", source=src.get("id"), kind=src.get("kind"))
    return {"ok": True, **mapsources.view_list(), "default": doc["default"]}


@router.post("/api/map/sources/{sid}/default")
async def map_sources_set_default(sid: str):
    """把某源设为默认源（不传 ``?source=`` 时用它）。"""
    from . import log as audit, mapsources
    try:
        doc = mapsources.set_default(sid)
    except mapsources.SourceError as e:
        return _err(str(e), 404)
    mapstore.reset_store()
    audit.log("map_source", action="set_default", source=sid)
    return {"ok": True, "default": doc["default"], **mapsources.view_list()}


@router.post("/api/map/sources/{sid}/test")
async def map_sources_test(sid: str):
    """对某个源做连通性自检（``list()`` 一次，**不改任何文件**）。"""
    from . import mapsources
    try:
        mapstore.resolve_source(sid)
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        return _err(str(e), 404)
    return await asyncio.to_thread(mapstore.io_test, sid)


@router.delete("/api/map/sources/{sid}")
async def map_sources_delete(sid: str):
    """删一条源（默认源不允许删，见 mapsources.remove 的说明）。"""
    from . import log as audit, mapsources
    try:
        mapsources.remove(sid)
    except mapsources.SourceError as e:
        return _err(str(e))
    mapstore.reset_store()
    audit.log("map_source", action="delete", source=sid)
    return {"ok": True, **mapsources.view_list()}


@router.get("/api/map/list")
async def map_list(source: str = Query("", alias="source")):
    """列地图（**必须排除 .backup/**，规格 §〇 第 4 条）。带尺寸/元数据/未知率/标记数/残缺态。"""
    return await asyncio.to_thread(_map_list_sync, source)


def _map_list_sync(source: str = ""):
    """同步地图扫描在线程池执行，避免 SSH/PGM 工作阻塞 ASGI 事件循环。"""
    from . import log as audit
    from . import mapsources
    try:
        store = mapstore.get_store(source)
    except (mapstore.MapStoreError, mapsources.SourceError) as e:
        # 未知源 = 用户可纠正的输入错误 → 明确 400（含可用源），不要 500
        return _err(str(e))
    ok, why = store.available()
    if not ok:
        return {"ok": True, "status": "unavailable", "reason": why, "maps": [],
                "source": mapstore.current_source().get("id", ""),
                "mode": conf.MAPS_IO, "root": store.root}
    try:
        entries = store.list()
    except mapstore.MapStoreError as e:
        return {"ok": True, "status": "unavailable", "reason": str(e), "maps": [],
                "source": mapstore.current_source().get("id", ""),
                "mode": conf.MAPS_IO, "root": store.root}
    counts = _tag_counts([e["name"] for e in entries])
    maps = []
    for e in entries:
        item = dict(e)
        item["counts"] = counts.get(e["name"], {"destinations": 0, "zones": 0})
        item["status"] = ("残缺：有 pgm 没 yaml" if e["has_pgm"] and not e["has_yaml"]
                          else "残缺：有 yaml 没 pgm" if e["has_yaml"] and not e["has_pgm"]
                          else "ok")
        item["meta_ok"] = False
        item["width"] = item["height"] = None
        item["resolution"] = item["origin"] = None
        item["unknown_ratio"] = None
        if e["has_yaml"]:
            try:
                info = mapserver.map_info(e["name"], store)
                item.update({"width": info.get("width"), "height": info.get("height"),
                             "resolution": info.get("resolution"), "origin": info.get("origin"),
                             "unknown_ratio": info.get("unknown_ratio"),
                             "meta_ok": bool(info.get("meta_ok")),
                             "problems": info.get("problems") or [],
                             "stale": info.get("stale"), "cached_at": info.get("cached_at")})
            except mapstore.MapStoreError as ex:
                item["problems"] = [str(ex)]
        item["current"] = (db.get_settings().get("current_map") == e["name"])
        maps.append(item)
    audit.log("map_change", action="list", count=len(maps), mode=conf.MAPS_IO)
    return {"ok": True, "maps": maps, "mode": conf.MAPS_IO, "root": store.root,
            "current_map": db.get_settings().get("current_map")}


@router.get("/api/map/current")
async def map_current(source: str = Query("", alias="source"), store=Depends(_store)):
    """车此刻在跑哪张图（**不靠人工声明**，指纹反查，规格 §5.3）。"""
    return await asyncio.to_thread(locator.current_map, store)


@router.get("/api/map/{name}/meta")
async def map_meta(name: str):
    return await asyncio.to_thread(_map_meta_sync, name)


def _map_meta_sync(name: str, store):
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    try:
        info = mapserver.map_info(n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    counts = db.count_map_tags(n)
    got = maptags.resolve(n, store)
    return {"ok": True, "meta": info, "counts": counts,
            "tags_exists": got["exists"], "tags_warnings": got.get("warnings") or [],
            "tags_path": maptags.tags_path(n, store),
            "fingerprint": got.get("fingerprint"),
            "tags_mtime": maptags.file_mtime_iso(n, store)}


@router.post("/api/map/{name}/meta")
def map_meta_set(name: str, body: MapMetaIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """改 resolution/origin 等元数据：**先返回将失效的标记数并要求 confirm=true**（规格 §7.2）。"""
    from . import log as audit
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    try:
        info = mapserver.map_info(n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    counts = db.count_map_tags(n)
    affects = counts["destinations"] + counts["zones"]
    patch: dict = {}
    for field in ("resolution", "origin", "negate", "occupied_thresh", "free_thresh"):
        v = getattr(body, field)
        if v is not None:
            patch[field] = v
    if not patch:
        return _err("没有要修改的字段", 400)
    if body.resolution is not None and body.resolution <= 0:
        return _err("resolution 必须为正数", 400)
    if body.origin is not None and len(body.origin) < 2:
        return _err("origin 至少要 2 个分量", 400)
    if body.origin is not None and len(body.origin) > 2 and abs(float(body.origin[2])) > 1e-9:
        return _err("origin 的 yaw 必须为 0（本项目不支持旋转地图）", 400)
    if affects and not body.confirm:
        return _err(f"本图有 {counts['destinations']} 个地点、{counts['zones']} 个区域，"
                    f"改 resolution/origin 会让它们的坐标含义改变", 409,
                    need_confirm=True, affects=counts)
    try:
        text = store.read(n, "yaml").decode("utf-8", "replace")
        y = mapserver.parse_yaml_flat(text)
        changes = []
        for k, v in patch.items():
            old = y.get(k)
            if k == "origin":
                v = [float(x) for x in v]
            y[k] = v
            changes.append(f"{k}: {old} → {v}")
        new_text = _rewrite_yaml_fields(text, patch)
        # 改元数据 = 覆盖语义 → 强制备份
        backup = store.backup(n)
        store.write(n, "yaml", new_text.encode("utf-8"))
        mapserver.clear_cache()
        locator.clear_current_map_cache()
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    # §7.2 第 3 条：把"已确认改元数据"这件事也记录进 tags.json（用**新**的指纹覆写）
    tags_updated = False
    if affects:
        try:
            got = maptags.resolve(n, store)
            if got["ok"] and got["exists"]:
                tags = got["tags"]
                tags["resolution"] = float(y.get("resolution")) if y.get("resolution") else None
                org = y.get("origin")
                if isinstance(org, list) and len(org) >= 2:
                    tags["origin"] = [float(org[0]), float(org[1]),
                                      float(org[2]) if len(org) > 2 else 0.0]
                maptags.save(n, tags, store, action="meta_fingerprint_confirm",
                             changes="; ".join(changes))
                tags_updated = True
        except mapstore.MapStoreError as e:
            return _err(f"元数据已改，但刷新 tags.json 指纹失败：{e}", 500)
    maptags.sync_map(n, store, force=True)
    audit.log("map_change", action="meta_update", map=n, changes=changes,
              backup=",".join(backup), affected=affects, tags_updated=tags_updated)
    return {"ok": True, "changed": changes, "backup": backup, "affected": affects,
            "tags_updated": tags_updated, "meta": mapserver.map_info(n, store)}


def _rewrite_yaml_fields(text: str, patch: dict) -> str:
    """在 yaml 原文上逐字段替换（保留注释、缩进与键顺序）。缺字段则追加。"""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if any(ln.endswith("\r\n") for ln in lines) else "\n"
    if not lines:
        lines = []
    done = set()
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip()
        if key not in patch:
            continue
        val = patch[key]
        if isinstance(val, list):
            val = "[" + ", ".join(str(v) for v in val) + "]"
        indent = ln[: len(ln) - len(ln.lstrip())]
        tail = newline if ln.endswith(("\n", "\r")) else ""
        lines[i] = f"{indent}{key}: {val}{tail}"
        done.add(key)
    for key, val in patch.items():
        if key in done:
            continue
        if isinstance(val, list):
            val = "[" + ", ".join(str(v) for v in val) + "]"
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] = lines[-1] + newline
        lines.append(f"{key}: {val}{newline}")
    return "".join(lines)


@router.post("/api/map/{name}/rename")
def map_rename(name: str, body: MapNameIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """重命名成对文件（.pgm/.yaml/.tags.json）并同步改 yaml 的 image: 与 tags 的 map:。"""
    from . import log as audit
    try:
        n, new = _name_of(name), mapstore.check_name(body.new_name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    if n == new:
        return _err("新名字与原图相同")
    if store.exists(new, "yaml") or store.exists(new, "pgm"):
        return _err(f"目标已存在：{new}", 409)
    try:
        store.rename(n, new)
        # yaml 里的 image: 必须跟着走，否则 map_server 找不到 pgm
        try:
            text = store.read(new, "yaml").decode("utf-8", "replace")
            store.write(new, "yaml", mapserver.update_yaml_image(text, f"{new}.pgm").encode("utf-8"))
        except mapstore.MapStoreError:
            pass
        # tags.json 的 map 字段与文件名对齐（指纹不变）
        if store.exists(new, "tags"):
            got = maptags.resolve(new, store)
            if got["ok"] and got["exists"]:
                tags = got["tags"]
                tags["map"] = new
                tags["updated_at"] = db.now_iso()
                import json as _json
                store.write(new, "tags",
                         _json.dumps(tags, ensure_ascii=False, indent=2).encode("utf-8"))
        mapserver.clear_cache()
        locator.clear_current_map_cache()
        db.drop_map_tags(n)
        maptags.sync_map(new, store, force=True)
        if db.get_settings().get("current_map") == n:
            db.set_settings({"current_map": new})
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    audit.log("map_change", action="rename", old=n, new=new)
    return {"ok": True, "old": n, "new": new}


@router.post("/api/map/{name}/copy")
def map_copy(name: str, body: MapNameIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """复制成对文件 → **标记随行**（指纹一致才复制，规格 §B5.2 第 6 步 / §B九 坑 13）。"""
    from . import log as audit
    try:
        n, new = _name_of(name), mapstore.check_name(body.new_name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    if n == new:
        return _err("新名字与原图相同")
    warnings: list[str] = []
    try:
        got = maptags.resolve(n, store)
    except mapstore.MapStoreError as e:
        return _err(f"读取原图标记失败：{e}", 500)
    try:
        store.copy(n, new)                 # 会连带复制 tags.json（若存在）
    except mapstore.MapStoreError as e:
        # 目标已存在 / 源不存在 → 都是"用户可纠正"的冲突，给 409 而不是 500
        code = 409 if "已存在" in str(e) else 404
        return _err(str(e), code)
    try:
        try:
            text = store.read(new, "yaml").decode("utf-8", "replace")
            store.write(new, "yaml", mapserver.update_yaml_image(text, f"{new}.pgm").encode("utf-8"))
        except mapstore.MapStoreError:
            pass
        # 指纹比对：一致则保留随行的标记，不一致就删掉新图的 tags（绝不静默错配）
        copied_tags = False
        if got["ok"] and got["exists"] and store.exists(new, "tags"):
            try:
                info = mapserver.map_info(new, store)
                fp = maptags.fingerprint_check(got["tags"], info)
                if fp.get("changed"):
                    warnings.append(f"该图元数据与标记指纹不一致（{'; '.join(fp.get('reasons') or [])}），"
                                    f"标记未随行")
                    store.remove(new, "tags")
                else:
                    tags = got["tags"]
                    tags["map"] = new
                    tags["updated_at"] = db.now_iso()
                    import json as _json
                    store.write(new, "tags",
                             _json.dumps(tags, ensure_ascii=False, indent=2).encode("utf-8"))
                    copied_tags = True
            except mapstore.MapStoreError as e:
                warnings.append(f"标记随行失败：{e}")
        mapserver.clear_cache()
        locator.clear_current_map_cache()
        maptags.sync_map(new, store, force=True)
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    if not copied_tags:
        db.drop_map_tags(new)
    audit.log("map_change", action="copy", src=n, dst=new, tags_copied=copied_tags,
              warnings="; ".join(warnings))
    return {"ok": True, "src": n, "dst": new, "tags_copied": copied_tags, "warnings": warnings}


@router.delete("/api/map/{name}")
def map_delete(name: str, confirm: bool = Query(False), source: str = Query("", alias="source"), store=Depends(_store)):
    """删除成对文件（含 .tags.json）。本图有标记时必须 ``confirm=true``（规格 §5.1）。"""
    from . import log as audit
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    counts = db.count_map_tags(n)
    affects = counts["destinations"] + counts["zones"]
    if affects and not confirm:
        return _err(f"本图有 {affects} 个标记（{counts['destinations']} 地点 / {counts['zones']} 区域），"
                    f"删除会一并移除", 409, need_confirm=True, affects=counts)
    removed = []
    try:
        for ext in _MAP_EXTS:
            if store.exists(n, ext):
                store.remove(n, ext)
                removed.append(ext)
    except mapstore.MapStoreError as e:
        return _err(str(e), 500)
    if not removed:
        return _err(f"地图不存在：{n}", 404)
    mapserver.clear_cache()
    locator.clear_current_map_cache()
    db.drop_map_tags(n)
    audit.log("map_change", action="delete", map=n, removed=",".join(removed), affected=affects)
    return {"ok": True, "removed": removed, "affected": affects}


@router.get("/api/map/{name}/download")
def map_download(name: str, file: str = Query("yaml"), source: str = Query("", alias="source"), store=Depends(_store)):
    """下载原始文件；``file`` **只接受 yaml/pgm/tags**（其他值报错，避免变成任意文件读取）。"""
    if file not in _MAP_EXTS:
        return _err(f"file 只接受 {'/'.join(_MAP_EXTS)}，收到 {file!r}")
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    try:
        data, stale, at = store.read_with_meta(n, file)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    media = {"yaml": "text/yaml; charset=utf-8", "pgm": "image/x-portable-graymap",
             "tags": "application/json; charset=utf-8"}[file]
    fn = {"yaml": f"{n}.yaml", "pgm": f"{n}.pgm", "tags": f"{n}.tags.json"}[file]
    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    if stale:
        # 断连时回落到本地缓存 → 前端显示「当前离线，显示缓存（时间）」（规格 §B4.2）
        headers["X-Map-Stale"] = "1"
        if at:
            headers["X-Map-Cached-At"] = str(at)
    return Response(content=data, media_type=media, headers=headers)


@router.get("/api/map/{name}/image.png")
async def map_image(name: str, source: str = Query("", alias="source"), store=Depends(_store)):
    """**后端把 PGM 转成灰度 PNG**（前端按阈值着色）；按 mtime+size 缓存（规格 §7.4）。"""
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    try:
        png, stale, at = await asyncio.to_thread(mapserver.image_png, n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e), 404)
    headers = {"Cache-Control": "no-cache"}
    if stale:
        headers["X-Map-Stale"] = "1"
        if at:
            headers["X-Map-Cached-At"] = str(at)
    return Response(content=png, media_type="image/png", headers=headers)


# ---------------------------------------------------------------- 地点与区域（§5.2）
@router.get("/api/destinations")
async def destinations_list(map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        rows = await asyncio.to_thread(maptags.get_destinations, n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, "map": n, "destinations": rows}


@router.post("/api/destinations")
def destinations_add(d: DestinationIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """新增地点：服务端完整校验（名称唯一、坐标在地图内、障碍检查）并返回 ``warnings[]``。"""
    from . import log as audit
    try:
        n = _name_of(d.map_name)
        out = maptags.upsert_destination(n, d.model_dump(), store=store)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", action="destination_add", map=d.map_name, error=str(e))
        return _err(str(e))
    return {"ok": True, "uid": out["uid"], "warnings": out["warnings"], "save": out["save"]}


@router.post("/api/destinations/validate")
def destinations_validate(v: ValidateIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """标点即校验（§7.6）：越界 / 障碍 / 未知 / 距障碍余量 —— **只警告不阻止**。

    ⚠️ 本路由必须**定义在** ``POST /api/destinations/{uid}`` **之前**：否则 FastAPI 会先匹配
    到 ``{uid}``，把 "validate" 当成一个地点 uid（实测就是 "地点不存在：validate"）。
    """
    try:
        n = _name_of(v.map_name)
        out = mapserver.validate_point(n, v.x, v.y, store, v.margin_m)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return out


@router.post("/api/destinations/{uid}")
def destinations_update(uid: str, d: DestinationIn, source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    try:
        n = _name_of(d.map_name)
        out = maptags.upsert_destination(n, d.model_dump(), uid=uid, store=store)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", action="destination_update", map=d.map_name,
                  uid=uid, error=str(e))
        return _err(str(e))
    return {"ok": True, "uid": out["uid"], "warnings": out["warnings"], "save": out["save"]}


@router.delete("/api/destinations/{uid}")
def destinations_delete(uid: str, map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        maptags.delete_destination(n, uid, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="destination_delete", map=n, uid=uid)
    return {"ok": True, "uid": uid}


@router.post("/api/destinations/learn")
def destinations_learn(body: LearnIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """取**当前位姿**写入该图 tags.json（位姿不可用 → 明确失败并提示"可改为在图上点选"）。"""
    from . import log as audit
    try:
        n = _name_of(body.map_name)
        pose = locator.get_pose()
        out = maptags.learn_here(n, body.model_dump(), pose, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="destination_learn", map=n, uid=out["uid"],
              pose=out.get("pose"))
    return {"ok": True, "uid": out["uid"], "warnings": out["warnings"], "pose": out.get("pose"),
            "save": out["save"]}


@router.get("/api/zones")
async def zones_list(map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        rows = await asyncio.to_thread(maptags.get_zones, n, store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, "map": n, "zones": rows}


@router.post("/api/zones")
def zones_add(z: ZoneIn, source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    try:
        n = _name_of(z.map_name)
        out = maptags.upsert_zone(n, z.model_dump(), store=store)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", action="zone_add", map=z.map_name, error=str(e))
        return _err(str(e))
    return {"ok": True, "uid": out["uid"]}


@router.post("/api/zones/{uid}")
def zones_update(uid: str, z: ZoneIn, source: str = Query("", alias="source"), store=Depends(_store)):
    try:
        n = _name_of(z.map_name)
        out = maptags.upsert_zone(n, z.model_dump(), uid=uid, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, "uid": out["uid"]}


@router.delete("/api/zones/{uid}")
def zones_delete(uid: str, map: str = Query("", alias="map"), source: str = Query("", alias="source"), store=Depends(_store)):
    from . import log as audit
    if not map:
        return _err("缺少 map 参数")
    try:
        n = _name_of(map)
        out = maptags.delete_zone(n, uid, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="zone_delete", map=n, uid=uid, orphaned=out.get("orphaned"))
    return {"ok": True, "uid": uid, "orphaned": out.get("orphaned") or []}


@router.get("/api/map/{name}/tags")
def map_tags_get(name: str, source: str = Query("", alias="source"), store=Depends(_store)):
    """**直读该图 .tags.json 原文**（调试 / 迁移 / 人工核对用）。"""
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    store = _store(source)
    got = maptags.resolve(n, store)
    return {"ok": got["ok"], "map": n, "exists": got["exists"], "tags": got["tags"],
            "warnings": got.get("warnings") or [], "stale": got.get("stale"),
            "cached_at": got.get("cached_at"), "error": got.get("error") or "",
            "fingerprint": got.get("fingerprint"), "path": maptags.tags_path(n, store)}


@router.put("/api/map/{name}/tags")
def map_tags_put(name: str, body: MapTagsIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """整份替换标记文件（高级用途，做 schema 校验）；写文件 + sync_map()。"""
    try:
        n = _name_of(name)
        out = maptags.replace_all(n, body.model_dump(), store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return out


@router.post("/api/map/{name}/tags/reindex")
def map_tags_reindex(name: str, source: str = Query("", alias="source"), store=Depends(_store)):
    """强制重建该图的索引缓存（缓存丢失或怀疑不一致时用）。"""
    try:
        n = _name_of(name)
        out = maptags.reindex(n, store=store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    return {"ok": True, **out}


@router.post("/api/map/reindex-all/tags")
def map_tags_reindex_all(source: str = Query("", alias="source"), store=Depends(_store)):
    """重建**所有**图的索引缓存（缓存整个丢了时的恢复入口，验收红线 3 用）。

    路径特意避开 ``/api/map/{name}/...`` 前缀，免得和 ``{name}`` 参数路由抢匹配。
    """
    from . import log as audit
    try:
        out = maptags.reindex_all(store)
    except mapstore.MapStoreError as e:
        return _err(str(e))
    audit.log("map_change", action="tags_reindex_all", maps=len(out))
    return {"ok": True, "results": out}


# ---------------------------------------------------------------- 位姿 / 当前地图 / IO 状态
@router.get("/api/robot/pose")
async def robot_pose():
    """位姿（只读，rosbridge）。降级时 ``{"ok": True, "status": "unavailable"}``。"""
    return await asyncio.to_thread(lambda: locator.pose_payload(db.get_settings()))


@router.get("/api/mapeditor/status")
async def mapeditor_status(source: str = Query("", alias="source")):
    """编辑器顶部状态条的一份汇总：位姿 + rosbridge + 当前地图 + IO 模式。"""
    store = _store(source)
    io, loc = await asyncio.gather(
        asyncio.to_thread(mapstore.io_status, "", source),
        asyncio.to_thread(locator.status, store),
    )
    return {"ok": True, "io": io, "locator": loc}


@router.get("/api/mapeditor/io")
async def mapeditor_io(name: str = Query(""), source: str = Query("", alias="source")):
    return {"ok": True, **(await asyncio.to_thread(mapstore.io_status, name, source))}


@router.post("/api/mapeditor/io/test")
async def mapeditor_io_test(source: str = Query("", alias="source")):
    """主动连通性自检（``list()`` 一次），返回耗时与错误原因；**不改任何文件**。"""
    return await asyncio.to_thread(mapstore.io_test, source)


@router.post("/api/mapeditor/pose/inject")
def mapeditor_pose_inject(body: PoseInjectIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """注入假位姿 / 假地图元数据（**无 ROS 环境开发与测试用**，规格 §5.4 / §nine 4）。"""
    if body.x is None and body.width is None:
        locator.clear_injection()
        return {"ok": True, "cleared": True}
    if body.x is not None:
        locator.set_pose_for_test(body.x, body.y, body.yaw)
    if body.width is not None:
        locator.set_map_for_test(body.width, body.height, body.resolution, body.origin)
    return {"ok": True, "pose": locator.pose_payload(), "current_map": locator.current_map(store)}


# ---------------------------------------------------------------- 像素修图保存（B 篇 §B5.2）
@router.post("/api/map/{name}/save")
def map_save(name: str, body: MapSaveIn, source: str = Query("", alias="source"), store=Depends(_store)):
    """保存像素改动：白名单 → 体积 → yaml 白名单校验 → 备份 → 标记随行 → 原子写 → 审计。"""
    from . import log as audit
    import base64
    try:
        n = _name_of(name)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=name, error=str(e), stage="name")
        return _err(str(e))
    store = _store(source)

    # 2) 体积校验
    try:
        pgm = base64.b64decode(body.pgm_b64 or "", validate=False)
    except Exception as e:      # noqa: BLE001
        audit.log("map_edit_reject", map=n, error=f"pgm base64 解码失败：{e}", stage="decode")
        return _err(f"pgm base64 解码失败：{e}")
    if not pgm:
        return _err("pgm_b64 为空（编辑器没有回传像素数据）")
    if len(pgm) > conf.MAPS_MAX_PGM_BYTES:
        return _err(f"pgm 过大：{len(pgm)} B > {conf.MAPS_MAX_PGM_BYTES} B", 413)
    yaml_in = body.yaml_text or ""
    if len(yaml_in.encode("utf-8")) > conf.MAPS_MAX_YAML_BYTES:
        return _err(f"yaml 过大：>{conf.MAPS_MAX_YAML_BYTES} B", 413)
    if pgm[:2] not in (b"P5", b"P2"):
        return _err(f"pgm 魔数不被接受：{pgm[:2]!r}（只认 P2/P5，与上游 parsePGM 一致）")

    # 1) 白名单校验（红线 §B7.1）—— 必须**最早**做：它是 ssh 子进程模式唯一的一道命令注入防线，
    #    也是唯一一处"连试都不该试"的输入。放在体积校验之前，避免"超大 body + 非法名字"绕过。
    if body.mode not in ("saveas", "overwrite"):
        return _err(f"mode 只接受 saveas/overwrite，收到 {body.mode!r}")
    try:
        target = (mapstore.check_name(body.new_name or f"{n}_edited")
                  if body.mode == "saveas" else mapstore.check_name(n))
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=n, error=str(e), stage="name")
        return _err(str(e))
    if body.mode == "overwrite" and not body.confirm:
        return _err("覆盖原图必须带 confirm=true", 409, need_confirm=True)

    # 3) yaml 白名单校验：除 image 外任何字段与磁盘原值不一致 → 409
    try:
        disk_yaml = store.read(n, "yaml").decode("utf-8", "replace")
    except mapstore.MapStoreError as e:
        return _err(f"读磁盘原 yaml 失败：{e}", 404)
    from_disk = mapserver.parse_yaml_flat(disk_yaml)
    from_up = mapserver.parse_yaml_flat(yaml_in) if yaml_in.strip() else {}
    diffs = []
    for k, v in from_up.items():
        if k == "image":
            continue
        if k not in from_disk:
            diffs.append(f"{k}（磁盘上没有该字段，回传值 {v!r}）")
        elif _same_value(from_disk[k], v) is False:
            diffs.append(f"{k}（磁盘 {from_disk[k]!r} vs 回传 {v!r}）")
    if diffs:
        audit.log("map_edit_reject", map=n, mode=body.mode, diffs="; ".join(diffs), stage="yaml")
        return _err("回传 yaml 与磁盘原值不一致（只允许改 image 字段）：" + "; ".join(diffs), 409,
                    diffs=diffs)

    # 5) 备份（覆盖：强制；另存：目标已存在时也备份）
    backup: list[str] = []
    warnings: list[str] = []
    try:
        if body.mode == "overwrite":
            backup = store.backup(target)
        elif store.exists(target, "yaml") or store.exists(target, "pgm"):
            backup = store.backup(target)
    except mapstore.MapStoreError as e:
        if body.mode == "overwrite":
            # 备份是覆盖的前置条件，不允许"备份失败但继续"（规格 §B八）
            audit.log("map_edit_reject", map=n, error=str(e), stage="backup")
            return _err(f"备份失败，已拒绝覆盖保存：{e}", 500)
        warnings.append(f"目标已存在但备份失败：{e}")

    # 6) 标记随行
    tags_copied = False
    if body.mode == "overwrite":
        try:
            got = maptags.resolve(n, store)
            if got["ok"] and got["exists"]:
                got["tags"]["updated_at"] = db.now_iso()
                import json as _json
                store.write(n, "tags",
                         _json.dumps(got["tags"], ensure_ascii=False, indent=2).encode("utf-8"))
                tags_copied = True
        except mapstore.MapStoreError as e:
            warnings.append(f"标记指纹更新失败：{e}")
    else:
        try:
            got = maptags.resolve(n, store)
            if got["ok"] and got["exists"]:
                # 用**本次要写出的** pgm 元数据与原名指纹比对（像素改不了 resolution/origin）
                info = mapserver.map_info(n, store)
                fp = maptags.fingerprint_check(got["tags"], info)
                if fp.get("changed"):
                    warnings.append(f"该图元数据已变（{'; '.join(fp.get('reasons') or [])}），标记未随行")
                else:
                    tags = got["tags"]
                    tags["map"] = target
                    tags["updated_at"] = db.now_iso()
                    import json as _json
                    store.write(target, "tags",
                             _json.dumps(tags, ensure_ascii=False, indent=2).encode("utf-8"))
                    tags_copied = True
        except mapstore.MapStoreError as e:
            warnings.append(f"标记随行失败：{e}")

    # 7) 写 pgm（原子）
    try:
        store.write(target, "pgm", pgm)
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=n, target=target, error=str(e), stage="pgm")
        return _err(f"写 pgm 失败：{e}", 500)

    # 8) 写 yaml：以磁盘原文为本，只替换 image: 一行
    try:
        out_yaml = mapserver.update_yaml_image(disk_yaml, f"{target}.pgm")
        store.write(target, "yaml", out_yaml.encode("utf-8"))
    except mapstore.MapStoreError as e:
        audit.log("map_edit_reject", map=n, target=target, error=str(e), stage="yaml_write")
        return _err(f"写 yaml 失败：{e}", 500)

    mapserver.clear_cache()
    locator.clear_current_map_cache()
    if body.mode == "overwrite":
        try:
            maptags.sync_map(target, store, force=True)
        except mapstore.MapStoreError as e:
            warnings.append(f"刷新索引缓存失败：{e}")
    else:
        try:
            maptags.sync_map(target, store, force=True)
        except mapstore.MapStoreError:
            pass

    wrote = [f"{target}.pgm", f"{target}.yaml"]
    if tags_copied:
        wrote.append(f"{target}.tags.json")
    audit.log("map_edit_save", map=n, target=target, mode=body.mode,
              pgm_bytes=len(pgm), backup=",".join(backup), tags_copied=tags_copied,
              warnings="; ".join(warnings))
    return {"ok": True, "wrote": wrote, "backup": backup, "target": target,
            "tags_copied": tags_copied, "warnings": warnings,
            "restart_hint": f"~/tools/nav_screen.sh nav {target}",
            "note": "map_server 启动时一次性读入地图，改完必须重启导航才生效"}


def _same_value(a, b) -> bool:
    """yaml 字段值比较：数字容忍 int/float 差异，其余按字符串比。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b))
    return str(a) == str(b)




# ---------------------------------------------------------------- 静态托管 /mapeditor
_MAPEDITOR_DIST = BASE_DIR / "frontend" / "packages" / "mapeditor" / "dist"
_MAPEDITOR_PUBLIC = BASE_DIR / "frontend" / "packages" / "mapeditor" / "public"


def _mount(app, dist: Path, path: str) -> None:
    """挂一个端的构建产物；SPA 回退到 index.html（本编辑器无 router，用不到回退，防御性）。"""
    app.mount(path, StaticFiles(directory=str(dist), html=True), name=path.strip("/"))


def mount_editor(app) -> None:
    """挂 `/mapeditor`：判据是**入口页 index.html 存在**，不是目录存在。

    为什么这么判：`frontend/packages/mapeditor/dist/` 会被 dev 构建留下来，但如果它里面没有
    `index.html`（例如只把 `public/` 拷过去、或 dist 只装了 `pixel-editor.html`），
    直接挂 dist 会让 `/mapeditor/` 变 404 —— 此时必须回退到 `public/`。
    两个候选都没有 `index.html` 时，退一步挂 `public/`（至少 `pixel-editor.html` 能打开），
    并在启动日志里说清楚「前端未构建」，别让人对着 404 猜。
    """
    for cand, label in ((_MAPEDITOR_DIST, "dist（构建产物）"),
                        (_MAPEDITOR_PUBLIC, "public（未构建回退）")):
        if (cand / "index.html").exists():
            _mount(app, cand, "/mapeditor")
            print("[mapeditor] 挂载 {}：{}".format(label, cand))
            return
    if _MAPEDITOR_PUBLIC.exists():
        _mount(app, _MAPEDITOR_PUBLIC, "/mapeditor")
        print("[WARN] [mapeditor] 没有 index.html —— 只挂了 public/ 使 pixel-editor.html 可用："
              "{}\n        要打开地图编辑器主界面请先构建："
              "cd frontend && pnpm --filter mapeditor build".format(_MAPEDITOR_PUBLIC))
