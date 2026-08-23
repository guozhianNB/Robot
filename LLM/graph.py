# -*- coding: utf-8 -*-
r"""
Kuzu 知识图谱封装：实体/关系 upsert（去重）+ 一跳关系查询。
可选依赖缺失 → _AVAILABLE=False，全部接口降级为空操作，不阻断对话。
"""
import threading

from .conf import DATA_DIR
from . import log as audit

_AVAILABLE = True
_DB_PATH = str(DATA_DIR / "graph")
_db = None
_conn = None
_MISSING = []
_lock = threading.Lock()

try:
    import kuzu
except ImportError as _exc:
    kuzu = None
    _AVAILABLE = False
    _MISSING.append("kuzu: " + str(_exc))


def _init():
    """初始化连接与 schema（kuzu 缺失时静默返回；测试可重设 _DB_PATH 后调用）。"""
    global _db, _conn, _AVAILABLE
    if kuzu is None:
        return
    try:
        _db = kuzu.Database(_DB_PATH)
        _conn = kuzu.Connection(_db)
        with _lock:
            _conn.execute("CREATE NODE TABLE IF NOT EXISTS Entity(id STRING PRIMARY KEY, uid STRING, name STRING, type STRING)")
            _conn.execute("CREATE REL TABLE IF NOT EXISTS Relation(FROM Entity TO Entity, type STRING, uid STRING, ts STRING)")
        _AVAILABLE = True
    except Exception as _exc:
        _AVAILABLE = False
        _MISSING.append(str(_exc))


def upsert_entity(uid: str, eid: str, name: str, etype: str) -> None:
    if not _AVAILABLE:
        return
    try:
        with _lock:
            _conn.execute(
                "MERGE (e:Entity {id: $id}) ON CREATE SET e.uid=$uid, e.name=$name, e.type=$type",
                {"id": eid, "uid": uid, "name": name, "type": etype})
    except Exception as e:  # noqa: BLE001
        audit.log("graph", action="upsert_entity_error", uid=uid, eid=eid, error=str(e))


def upsert_relation(uid: str, src_id: str, dst_id: str, rtype: str) -> None:
    if not _AVAILABLE:
        return
    try:
        with _lock:
            _conn.execute(
                "MATCH (a:Entity {id: $src}), (b:Entity {id: $dst}) "
                "MERGE (a)-[r:Relation {type: $type}]->(b) "
                "ON CREATE SET r.uid=$uid, r.ts=$ts",
                {"src": src_id, "dst": dst_id, "type": rtype, "uid": uid, "ts": ""})
    except Exception as e:  # noqa: BLE001
        audit.log("graph", action="upsert_relation_error", uid=uid, error=str(e))


def one_hop(eid: str) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "MATCH (a:Entity {id: $id})-[r:Relation]->(b:Entity) RETURN b.name AS target, r.type AS type",
                {"id": eid})
        out = []
        while rows.has_next():
            rec = rows.get_next()
            out.append({"target": rec[0], "type": rec[1]})
        return out
    except Exception:  # noqa: BLE001
        return []


def entities_by_name(uid: str, name: str) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "MATCH (e:Entity) WHERE e.uid=$uid AND e.name=$name RETURN e.id, e.type",
                {"uid": uid, "name": name})
        out = []
        while rows.has_next():
            rec = rows.get_next()
            out.append({"id": rec[0], "type": rec[1]})
        return out
    except Exception:  # noqa: BLE001
        return []


def list_entities(uid: str) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "MATCH (e:Entity) WHERE e.uid=$uid RETURN e.id, e.name, e.type",
                {"uid": uid})
        out = []
        while rows.has_next():
            rec = rows.get_next()
            out.append({"id": rec[0], "name": rec[1], "type": rec[2]})
        return out
    except Exception:  # noqa: BLE001
        return []


def list_relations(uid: str) -> list[dict]:
    if not _AVAILABLE:
        return []
    try:
        with _lock:
            rows = _conn.execute(
                "MATCH (a:Entity)-[r:Relation]->(b:Entity) WHERE r.uid=$uid "
                "RETURN a.name AS src, r.type AS type, b.name AS dst",
                {"uid": uid})
        out = []
        while rows.has_next():
            rec = rows.get_next()
            out.append({"src": rec[0], "type": rec[1], "dst": rec[2]})
        return out
    except Exception:  # noqa: BLE001
        return []


def status() -> dict:
    return {"available": _AVAILABLE, "missing": _MISSING}


_init()
