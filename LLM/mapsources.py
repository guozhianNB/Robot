# -*- coding: utf-8 -*-
r"""
地图源注册表（2026-09-14）——把「地图文件在哪」从**环境变量**提升为**具名、可在界面上切换**的一等公民。

为什么需要它（用户原话）：「我希望不管是划线还是编辑地图，都指定同一个"地图源"，也就是划线界面
"地图"那一栏，这样方便本机调试，也做到了模块化」。

## 与 ROS 端的边界（**硬约束，别越界**）
ROS 那侧的地图路径由板卡本地的 `~/tools/nav_screen.sh` + `$WS/maps/` 决定，**不读这个注册表**。
本模块只回答"编辑器读写的是哪一份地图文件"，**绝不启停 Nav2 / SLAM、绝不碰正在跑的那张图**
（用户 2026-09-14 明确：「那 ros 端的地图与地图编辑器分开，不管 ros 端，这样编辑其他地图时
也不会使得 slam 崩溃」）。因此：
  * 换源 = 换编辑器看的那份文件，对 ROS 无任何副作用；
  * 改完地图仍需人工重启导航才生效（后端只给命令提示，不去执行）。

## 存储
`LLM/data/maps_sources.json`（运行时数据，不入 git）。首次运行由 `conf` 的环境变量种入两个内置源：
  * `board` —— ssh，host 取自 `ROBOT_IP`（`.env` 可配，改一处即同时改板卡地址与 rosbridge）
  * `pc`    —— local，指向 `conf.MAPS_DIR`（开发期板卡不可达时用）
此后以文件为准。**界面只允许"选源"，不允许手填路径**（用户 2026-09-14 定：路径是配置项、视为可信，
一旦允许手填就等于把它变成用户输入，会新开一个命令注入面）。
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

from . import conf

VERSION = 1
KINDS = ("local", "ssh")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_lock = threading.RLock()
_cache: dict | None = None
_cache_sig: tuple | None = None


class SourceError(ValueError):
    """源配置/引用非法（id 不合法 / 字段缺失 / 删默认源 / 源不存在）。

    注意：``mapstore`` 与路由层捕的是 ``mapstore.MapStoreError``。本异常**不继承**它
    （mapsources 是被 mapstore 延迟导入的下层，反向 import 会成环），所以凡是
    "按 source 取 store" 的地方都必须**同时**捕 ``(MapStoreError, SourceError)``——
    只捕前者会让"未知源"漏成 HTTP 500（实测踩过：``?source=nope`` → 500）。
    """


# ---------------------------------------------------------------------------
# 字段与校验
# ---------------------------------------------------------------------------
def _seed_ssh() -> dict:
    return {
        "id": "board",
        "label": conf.MAPS_SOURCE_LABEL or f"板卡 {conf.MAPS_SSH_HOST}",
        "kind": "ssh",
        "host": conf.MAPS_SSH_HOST,
        "user": conf.MAPS_SSH_USER,
        "port": conf.MAPS_SSH_PORT,
        "root": conf.MAPS_SSH_ROOT,
        "key": conf.MAPS_SSH_KEY,
    }


def _seed_local() -> dict:
    return {"id": "pc", "label": "本机 仓库副本（调试）", "kind": "local",
            "root": str(conf.MAPS_DIR)}


def default_doc() -> dict:
    """首次运行写入的初始注册表：内置 board(=ROBOT_IP) 与 pc(仓库副本)。

    `default` 取 `conf.MAPS_IO` 对应的那个 —— 于是「不配注册表」时行为与今天完全一致。
    """
    return {"version": VERSION,
            "default": "pc" if conf.MAPS_IO == "local" else "board",
            "items": [_seed_ssh(), _seed_local()]}


def normalize_source(raw: dict, existing_ids=()) -> dict:
    """把一条源规范成完整结构，非法即抛 ``SourceError``（消息直接给用户看）。"""
    if not isinstance(raw, dict):
        raise SourceError("源必须是对象")
    sid = str(raw.get("id") or "").strip()
    if not _SOURCE_ID_RE.match(sid):
        raise SourceError(f"源 id 不合法：{sid!r}（只允许字母/数字/下划线/连字符，1~32 字符）")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise SourceError(f"源 {sid} 的 kind 只接受 {'/'.join(KINDS)}，收到 {kind!r}")
    label = str(raw.get("label") or "").strip() or sid
    out = {"id": sid, "label": label, "kind": kind}
    if kind == "local":
        root = str(raw.get("root") or "").strip()
        if not root:
            raise SourceError(f"源 {sid}（local）缺少 root")
        out["root"] = root
    else:
        host = str(raw.get("host") or "").strip()
        if not host:
            raise SourceError(f"源 {sid}（ssh）缺少 host")
        out.update({
            "host": host,
            "user": str(raw.get("user") or conf.MAPS_SSH_USER).strip(),
            "port": int(raw.get("port") or conf.MAPS_SSH_PORT),
            "root": str(raw.get("root") or conf.MAPS_SSH_ROOT).strip(),
            "key": str(raw.get("key") or "").strip(),
        })
    note = str(raw.get("note") or "").strip()
    if note:
        out["note"] = note
    return out


def _normalize_doc(raw: dict) -> tuple[dict, list[str]]:
    warn: list[str] = []
    if not isinstance(raw, dict):
        warn.append("maps_sources.json 顶层不是对象，已重置为内置源")
        raw = {}
    items: list[dict] = []
    seen = set()
    for it in (raw.get("items") or []):
        try:
            s = normalize_source(it)
        except SourceError as e:
            warn.append(str(e) + "（已跳过）")
            continue
        if s["id"] in seen:
            warn.append(f"源 id 重复：{s['id']}（已跳过）")
            continue
        seen.add(s["id"])
        items.append(s)
    if not items:
        warn.append("注册表里没有可用源，已重置为内置源")
        items = default_doc()["items"]
    default = str(raw.get("default") or "").strip()
    if default not in seen:
        if default:
            warn.append(f"默认源 {default!r} 不存在，已回退到 {items[0]['id']}")
        default = items[0]["id"]
    return {"version": VERSION, "default": default, "items": items}, warn


# ---------------------------------------------------------------------------
# 读 / 写
# ---------------------------------------------------------------------------
def _path() -> Path:
    return Path(conf.MAPS_SOURCES_FILE)


def _file_sig() -> tuple:
    p = _path()
    try:
        st = p.stat()
        return (str(p), st.st_mtime, st.st_size)
    except OSError:
        return (str(p), 0.0, 0)


def load(force: bool = False) -> dict:
    """读注册表（按文件 mtime+size 缓存；文件不存在则种入内置源并落盘）。"""
    global _cache, _cache_sig
    with _lock:
        sig = _file_sig()
        if not force and _cache is not None and _cache_sig == sig:
            return copy.deepcopy(_cache)
        p = _path()
        raw: dict = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:      # noqa: BLE001  坏文件 → 回退内置源，不崩
                raw = {}
                doc, warn = _normalize_doc({})
                doc["warnings"] = [f"maps_sources.json 解析失败（{e}），已用内置源"] + warn
                _cache, _cache_sig = doc, _file_sig()
                return copy.deepcopy(doc)
        else:
            raw = default_doc()
            try:
                _write_doc(raw)
            except OSError:
                pass
        doc, warn = _normalize_doc(raw)
        doc["warnings"] = warn
        doc["path"] = str(p)
        _cache, _cache_sig = doc, _file_sig()
        return copy.deepcopy(doc)


def _write_doc(doc: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": VERSION, "default": doc.get("default"),
               "items": doc.get("items") or []}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)                       # 原子替换


def save(doc: dict) -> dict:
    """整份落盘并清缓存。``doc`` 需已过 :func:`normalize_source`。"""
    global _cache, _cache_sig
    with _lock:
        _write_doc(doc)
        _cache, _cache_sig = None, None
    return load(force=True)


def upsert(raw: dict) -> dict:
    """新增或修改一条源（id 已存在则覆盖其字段）。"""
    src = normalize_source(raw)
    doc = load(force=True)
    items = [s for s in doc["items"] if s["id"] != src["id"]]
    items.append(src)
    items.sort(key=lambda s: s["id"])
    doc["items"] = items
    return save(doc)


def remove(source_id: str) -> dict:
    """删一条源。默认源不允许删（会留下悬空默认）；最后一个源也不允许删。"""
    sid = str(source_id or "").strip()
    with _lock:
        doc = load(force=True)
        if sid == doc["default"]:
            raise SourceError(f"{sid} 是当前默认源，不能删除；请先把默认源改成别的")
        items = [s for s in doc["items"] if s["id"] != sid]
        if len(items) == len(doc["items"]):
            raise SourceError(f"源不存在：{sid}")
        if not items:
            raise SourceError("至少要保留一个源")
        doc["items"] = items
        return save(doc)


def set_default(source_id: str) -> dict:
    sid = str(source_id or "").strip()
    with _lock:
        doc = load(force=True)
        if not any(s["id"] == sid for s in doc["items"]):
            raise SourceError(f"源不存在：{sid}")
        doc["default"] = sid
        return save(doc)


def get(source_id: str = "") -> dict:
    """取一条源；``source_id`` 空则取默认源。不存在即抛 ``SourceError``。"""
    doc = load()
    sid = str(source_id or "").strip() or doc["default"]
    for s in doc["items"]:
        if s["id"] == sid:
            return copy.deepcopy(s)
    raise SourceError(f"地图源不存在：{sid}（可用：{'、'.join(s['id'] for s in doc['items'])}）")


def default_id() -> str:
    return load()["default"]


def ids() -> list[str]:
    return [s["id"] for s in load()["items"]]


def fingerprint_of(source: dict) -> dict:
    """交给 ``MapCache`` 当键用的"身份指纹" —— 源一旦改了路径/主机，缓存不该继续命中。"""
    if source["kind"] == "local":
        return {"io_mode": "local", "root": source.get("root", "")}
    return {"io_mode": "ssh", "root": source.get("root", ""),
            "host": source.get("host", ""), "user": source.get("user", ""),
            "port": source.get("port")}


# ---------------------------------------------------------------------------
# 给接口用的视图（不泄露密码等敏感项；路径只读展示）
# ---------------------------------------------------------------------------
def _local_root_exists(root: str) -> bool:
    try:
        return Path(root).exists()
    except OSError:
        return False


def view_list(store_map=None) -> dict:
    """``GET /api/map/sources`` 的载荷：只读源清单 + 默认源 + 每源可用性。

    ``store_map`` 可选：``{id: MapStore}``，用于把可用性一并算出来（避免前端再打一轮）。
    """
    doc = load()
    items = []
    for s in doc["items"]:
        v = {k: s[k] for k in ("id", "label", "kind") if k in s}
        v["note"] = s.get("note", "")
        if s["kind"] == "local":
            v["root"] = s.get("root", "")
            v["target"] = s.get("root", "")
            v["path_ok"] = _local_root_exists(s.get("root", ""))
        else:
            v["root"] = s.get("root", "")
            v["host"] = s.get("host", "")
            v["user"] = s.get("user", "")
            v["port"] = s.get("port")
            v["target"] = f"{s.get('user')}@{s.get('host')}:{s.get('root')}"
            v["path_ok"] = None            # 远程路径无法廉价判断，交给 /test
        v["is_default"] = (s["id"] == doc["default"])
        if store_map and s["id"] in store_map:
            ok, why = store_map[s["id"]].available()
            v["available"] = ok
            v["reason"] = why
        items.append(v)
    return {"ok": True, "default": doc["default"], "items": items,
            "path": doc.get("path", str(_path())), "warnings": doc.get("warnings") or [],
            "env_robot_ip": os.environ.get("ROBOT_IP", ""),
            "readonly_note": "源列表由后端配置维护（maps_sources.json / 环境变量），界面只做选择、不手填路径"}
