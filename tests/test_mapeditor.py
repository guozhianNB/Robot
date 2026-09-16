# -*- coding: utf-8 -*-
r"""地图编辑器后端测试（规格 §九 / §B11.1）——**完全不依赖板卡**。

覆盖：
  * LocalMapStore 契约（CRUD / rename / stat / list 排除 .backup）
  * 地图名白名单（路径穿越 / 命令注入 / keepout / 超长）且**断言没有任何文件被触碰**
  * PGM 解析（P5/P2/坏魔数）+ PNG 编码 + 未知率三分口径
  * 像素↔米 往返（含 y 轴翻转）
  * maptags：schema 规范化、写文件+刷缓存、外部改文件后缓存自动重建、清空索引表可重建、指纹告警
  * 接口层：地点/区域增删改、标点校验、保存流程（另存/覆盖/备份/白名单 409）、备份保留
"""
from __future__ import annotations

import base64
import json
import struct
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from LLM import conf, db, locator, log as audit_log, mapserver, mapsources, mapstore, maptags  # noqa: E402

SAMPLE_YAML = """image: my_map.pgm
mode: trinary
resolution: 0.05
origin: [-4.6, -1.91, 0]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.25
"""


def make_pgm(width=8, height=6, fill=205, spots=None):
    """造一张 P5 地图；``spots`` = {(x, y): value}。"""
    px = bytearray([fill]) * (width * height)
    for (x, y), v in (spots or {}).items():
        px[y * width + x] = v
    head = f"P5\n{width} {height}\n255\n".encode("ascii")
    return head + bytes(px)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把地图根目录与数据库都指向临时目录，全程离线。"""
    maps = tmp_path / "maps"
    maps.mkdir()
    monkeypatch.setattr(conf, "MAPS_IO", "local")
    monkeypatch.setattr(conf, "MAPS_DIR", maps)
    monkeypatch.setattr(conf, "MAPS_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(conf, "ROSBRIDGE_MOCK_POSE", "")
    # 「地图源」注册表也必须隔离（2026-09-14 新增）：真实那份 maps_sources.json 指向板卡/仓库路径，
    # 不隔离的话测试会打真实路径（实测会多出约 30 个失败）。这里用一个只有临时目录的单源注册表。
    monkeypatch.setattr(conf, "MAPS_SOURCES_FILE", tmp_path / "maps_sources.json")
    (tmp_path / "maps_sources.json").write_text(
        json.dumps({"version": 1, "default": "test",
                    "items": [{"id": "test", "label": "测试", "kind": "local", "root": str(maps)}]},
                   ensure_ascii=False), encoding="utf-8")
    mapsources._cache, mapsources._cache_sig = None, None
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "brain.db"))
    # log.py 在 import 期就绑定了 AUDIT_LOG，所以必须改它模块里的那一份
    monkeypatch.setattr(conf, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_log, "AUDIT_LOG", tmp_path / "audit.jsonl")
    mapstore.reset_store()
    mapserver.clear_cache()
    locator.clear_injection()
    db.init_db()
    (maps / "my_map.yaml").write_text(SAMPLE_YAML, encoding="utf-8", newline="")
    (maps / "my_map.pgm").write_bytes(make_pgm(8, 6, 205, {(0, 0): 0, (7, 5): 254}))
    yield {"maps": maps}
    mapstore.reset_store()
    mapserver.clear_cache()
    locator.clear_injection()


def _store():
    return mapstore.get_store()


# ---------------------------------------------------------------------------
# MapStore 契约
# ---------------------------------------------------------------------------
def test_local_store_contract(env):
    maps = env["maps"]
    st = _store()
    assert st.io_mode == "local"
    ok, why = st.available()
    assert ok, why
    names = [e["name"] for e in st.list()]
    assert names == ["my_map"]
    assert st.read("my_map", "yaml").decode("utf-8") == SAMPLE_YAML
    assert st.stat("my_map", "pgm")[1] == len(make_pgm(8, 6, 205, {(0, 0): 0, (7, 5): 254}))
    # 写（原子）
    st.write("my_map", "tags", b'{"version":1}')
    assert (maps / "my_map.tags.json").read_bytes() == b'{"version":1}'
    # 复制 / 重命名成全套
    st.copy("my_map", "copy1")
    assert (maps / "copy1.pgm").exists() and (maps / "copy1.tags.json").exists()
    st.rename("copy1", "copy2")
    assert (maps / "copy2.yaml").exists() and not (maps / "copy1.yaml").exists()
    # 删除
    st.remove("copy2", "yaml")
    assert not (maps / "copy2.yaml").exists()
    # stat 缺失 → None
    assert st.stat("nope", "yaml") is None


def test_list_excludes_backup_dir(env):
    maps = env["maps"]
    st = _store()
    st.backup("my_map")
    bdir = maps / conf.MAPS_BACKUP_DIRNAME
    assert bdir.is_dir() and list(bdir.iterdir())
    assert [e["name"] for e in st.list()] == ["my_map"]


def test_name_whitelist_rejects_and_touches_nothing(env):
    maps = env["maps"]
    bad = ["../etc/passwd", "a/b", "a b", 'x"y', "含keepout名", "k" * 65, "..", "a;rm -rf /",
           "a`id`", "$(id)", "a\nb", ".", ""]
    for name in bad:
        with pytest.raises(mapstore.MapStoreError):
            mapstore.check_name(name)
    st = _store()
    for name in bad:
        with pytest.raises(mapstore.MapStoreError):
            st.read(name, "yaml")
        with pytest.raises(mapstore.MapStoreError):
            st.write(name, "yaml", b"x")
    # 目录内容没被碰过：原始两个文件 + .backup 目录之外没有任何新东西
    assert sorted(p.name for p in maps.iterdir()) == ["my_map.pgm", "my_map.yaml"]
    # 归一化：允许带后缀
    assert mapstore.check_name("my_map.yaml") == "my_map"
    assert mapstore.check_name("my_map.pgm") == "my_map"


def test_unavailable_store_degrades(tmp_path, monkeypatch):
    """本机没有地图目录（源指向不存在的路径）→ available=False 且不抛（AGENTS 降级红线）。

    2026-09-14「地图源」之后：默认源来自 maps_sources.json，所以要连注册表一起指向不存在的目录。
    """
    missing = tmp_path / "not-there"
    monkeypatch.setattr(conf, "MAPS_IO", "local")
    monkeypatch.setattr(conf, "MAPS_DIR", missing)
    monkeypatch.setattr(conf, "MAPS_SOURCES_FILE", tmp_path / "maps_sources.json")
    (tmp_path / "maps_sources.json").write_text(
        json.dumps({"version": 1, "default": "gone",
                    "items": [{"id": "gone", "label": "不存在", "kind": "local",
                               "root": str(missing)}]}, ensure_ascii=False), encoding="utf-8")
    mapsources._cache, mapsources._cache_sig = None, None
    mapstore.reset_store()
    st = mapstore.get_store()
    ok, why = st.available()
    assert not ok and "MAPS_DIR" in why
    with pytest.raises(mapstore.MapStoreError):
        st.list()
    mapstore.reset_store()


def test_ssh_store_without_transport_is_unavailable(monkeypatch, tmp_path):
    """paramiko 缺失 + ssh 客户端不可用 → available=False，**后端照常启动**（红线）。"""
    monkeypatch.setattr(conf, "MAPS_IO", "ssh")
    monkeypatch.setattr(conf, "MAPS_SSH_TRANSPORT", "paramiko")
    monkeypatch.setattr(mapstore, "_paramiko_available", lambda: (False, "未安装 paramiko"))
    st = mapstore.SshMapStore(cache=mapstore.MapCache(tmp_path / "c"))
    ok, why = st.available()
    assert not ok and "paramiko" in why
    with pytest.raises(mapstore.MapStoreError):
        st.read("x", "yaml")


# ---------------------------------------------------------------------------
# PGM / PNG / 未知率
# ---------------------------------------------------------------------------
def test_parse_p5_and_p2():
    data = make_pgm(3, 2, 0, {(1, 1): 255})
    p = mapserver.parse_pgm(data)
    assert (p.magic, p.width, p.height, p.maxval) == ("P5", 3, 2, 255)
    assert p.pixels[4] == 255
    text = b"P2\n# comment\n3 2\n255\n" + b" ".join(str(v).encode() for v in
                                                    [0, 1, 2, 3, 4, 5]) + b"\n"
    p2 = mapserver.parse_pgm(text)
    assert p2.magic == "P2" and list(p2.pixels) == [0, 1, 2, 3, 4, 5]


def test_parse_pgm_bad_magic():
    with pytest.raises(mapstore.MapStoreError, match="魔数"):
        mapserver.parse_pgm(b"P6\n1 1\n255\n\x00")


def test_parse_pgm_truncated():
    with pytest.raises(mapstore.MapStoreError, match="数据不足"):
        mapserver.parse_pgm(b"P5\n4 4\n255\n\x00\x01")


def test_pgm_roundtrip_16bit():
    px = [0, 300, 65535, 12]
    pgm = mapserver.PGM("P5", 4, 1, 65535, px)
    out = mapserver.encode_pgm(pgm)
    assert out[:2] == b"P5"
    back = mapserver.parse_pgm(out)
    assert list(back.pixels) == px and back.maxval == 65535


def test_png_structure():
    png = mapserver.encode_gray_png(2, 2, bytes([0, 1, 2, 3]))
    assert png.startswith(mapserver.PNG_MAGIC)
    assert png[12:16] == b"IHDR" and png[-8:-4] == b"IEND"
    w, h, bits, color = struct.unpack(">IIBB", png[16:26])
    assert (w, h, bits, color) == (2, 2, 8, 0)


def test_unknown_ratio_ros_convention():
    """像素 0=占用 / 205=未知灰但按阈值判 free / 254=free（与 ROS map_server 口径一致）。"""
    pgm = mapserver.PGM("P5", 4, 1, 255, [0, 205, 254, 255])
    meta = mapserver.meta_from_yaml(SAMPLE_YAML)
    c = mapserver.classify_counts(pgm, meta)
    assert c["occupied"] == 1
    assert c["free"] == 3
    assert c["unknown"] == 0
    # 造真正落在阈值带里的值：occ 在 (0.25, 0.65] → unknown
    pgm2 = mapserver.PGM("P5", 3, 1, 255, [0, 128, 255])
    c2 = mapserver.classify_counts(pgm2, meta)
    assert c2["unknown"] == 1 and c2["occupied"] == 1 and c2["free"] == 1
    assert c2["unknown_ratio"] == pytest.approx(1 / 3)
    # negate=1 时黑白翻转
    meta_n = dict(meta, negate=1)
    c3 = mapserver.classify_counts(pgm2, meta_n)
    assert c3["occupied"] == 1 and c3["free"] == 1 and c3["unknown"] == 1


def test_meta_missing_fields_marks_not_ok():
    meta = mapserver.meta_from_yaml("image: x.pgm\nmode: trinary\n")
    assert not meta["meta_ok"] and meta["resolution"] is None
    assert any("resolution" in p for p in meta["problems"])


def test_meta_rejects_rotated_origin():
    meta = mapserver.meta_from_yaml("resolution: 0.05\norigin: [0, 0, 0.3]\n")
    assert not meta["meta_ok"]
    assert any("yaw" in p for p in meta["problems"])


def test_update_yaml_image_preserves_rest():
    text = "# 手写注释\nimage: old.pgm\nmode: trinary\nresolution: 0.05\n"
    out = mapserver.update_yaml_image(text, "new.pgm")
    assert "# 手写注释" in out
    assert "image: new.pgm" in out and "old.pgm" not in out
    assert out.count("resolution: 0.05") == 1
    # 缺 image 行 → 追加
    out2 = mapserver.update_yaml_image("resolution: 0.05\n", "x.pgm")
    assert out2.endswith("image: x.pgm\n")


def test_coords_roundtrip_and_y_flip():
    """像素↔米往返误差 < 半个像素，且 y 轴确实翻转（§7.1 最高频错误）。"""
    res, origin, height = 0.05, [-4.6, -1.91, 0.0], 111
    for x, y in [(-4.6, -1.91), (0.0, 0.0), (2.1, -1.0), (-4.55, 3.5)]:
        px, py = mapserver.meters_to_pixel(x, y, res, origin, height)
        bx, by = mapserver.pixel_to_meters(px, py, res, origin, height)
        assert abs(bx - x) < res / 2 and abs(by - y) < res / 2
    # 米的 min_y 对应图像**最后一行**
    _px, py_bottom = mapserver.meters_to_pixel(-4.6, -1.91, res, origin, height)
    assert py_bottom == height - 1
    _px2, py_top = mapserver.meters_to_pixel(-4.6, -1.91 + (height - 1) * res, res, origin, height)
    assert py_top == 0


# ---------------------------------------------------------------------------
# 标点即校验
# ---------------------------------------------------------------------------
def test_validate_point_warns(env):
    st = _store()
    # 障碍像素 (0,0) → 米坐标：x=origin_x, y=origin_y+(H-1)*res
    v = mapserver.validate_point("my_map", -4.6, -1.91 + 5 * 0.05, st)
    assert v["on_obstacle"] is True and v["reasons"]
    assert v["pixel"] == [0, 0]
    assert v["edge_margin_m"] is not None and v["edge_margin_m"] < 0.3
    # 地图外
    out = mapserver.validate_point("my_map", 100.0, 100.0, st)
    assert out["in_bounds"] is False and out["reasons"]
    # 正常点
    mid = mapserver.validate_point("my_map", -4.6 + 4 * 0.05, -1.91 + 3 * 0.05, st)
    assert mid["in_bounds"] is True and mid["pixel_kind"] == "free"
    assert mid["clearance_m"] is not None and mid["clearance_m"] > 0


# ---------------------------------------------------------------------------
# maptags：唯一真相 + 单向索引缓存
# ---------------------------------------------------------------------------
def test_tags_empty_when_file_absent(env):
    got = maptags.resolve("my_map")
    assert got["ok"] and got["exists"] is False
    assert got["tags"]["destinations"] == [] and got["tags"]["zones"] == []
    assert got["warnings"]


def test_destination_crud_writes_file_then_cache(env):
    maps = env["maps"]
    out = maptags.upsert_destination("my_map", {"name": "护士办公室", "aliases": ["护士站", "办公室"],
                                                "x": -4.0, "y": -1.0, "yaw_deg": 90,
                                                "risk": "low", "elder_allowed": 1})
    assert out["uid"] == "d1"
    f = maps / "my_map.tags.json"
    assert f.exists(), "唯一真相必须是地图文件夹里的 tags.json"
    obj = json.loads(f.read_text(encoding="utf-8"))
    assert obj["map"] == "my_map" and obj["resolution"] == 0.05
    assert obj["origin"] == [-4.6, -1.91, 0.0]
    assert obj["destinations"][0]["name"] == "护士办公室"
    assert obj["destinations"][0]["aliases"] == ["护士站", "办公室"]
    # 索引缓存已刷（只读镜像）
    rows = db.list_destinations("my_map")
    assert len(rows) == 1 and rows[0]["uid"] == "d1" and rows[0]["aliases"] == "护士站,办公室"
    # uid 稳定：改名不改 uid
    maptags.upsert_destination("my_map", {"name": "护士站"}, uid="d1")
    obj = json.loads(f.read_text(encoding="utf-8"))
    assert obj["destinations"][0]["uid"] == "d1" and obj["destinations"][0]["name"] == "护士站"
    # 重名拒绝
    with pytest.raises(mapstore.MapStoreError, match="已存在"):
        maptags.upsert_destination("my_map", {"name": "护士站", "x": 0, "y": 0})
    # uid 不复用（删除后新增仍递增）
    maptags.upsert_destination("my_map", {"name": "二号地点", "x": -4.0, "y": -1.5})
    maptags.delete_destination("my_map", "d1")
    out2 = maptags.upsert_destination("my_map", {"name": "三号", "x": -4.0, "y": -1.2})
    assert out2["uid"] == "d1"      # 空缺最小可用号（稳定身份，不复用历史 id 语义）
    obj = json.loads(f.read_text(encoding="utf-8"))
    assert {d["name"] for d in obj["destinations"]} == {"二号地点", "三号"}


def test_zone_crud_and_hierarchy(env):
    maps = env["maps"]
    out = maptags.upsert_zone("my_map", {"name": "101", "kind": "ward", "shape": "rect",
                                         "polygon": [[-4.0, -1.0], [-3.0, -1.0],
                                                     [-3.0, -0.5], [-4.0, -0.5]]})
    assert out["uid"] == "z1"
    z2 = maptags.upsert_zone("my_map", {"name": "1 号床", "kind": "bed", "shape": "polygon",
                                        "polygon": [[-4.0, -1.0], [-3.8, -1.0], [-3.8, -0.8]],
                                        "parent": "z1"})
    assert z2["uid"] == "z2"
    zones = maptags.get_zones("my_map")
    by_uid = {z["uid"]: z for z in zones}
    assert set(by_uid) == {"z1", "z2"}
    assert by_uid["z2"]["parent"] == "z1"
    assert by_uid["z1"]["polygon"][0] == [-4.0, -1.0]
    # 多边形点数不足
    with pytest.raises(mapstore.MapStoreError, match="3 个点"):
        maptags.upsert_zone("my_map", {"name": "坏", "polygon": [[0, 0], [1, 1]]})
    # 父区域不存在
    with pytest.raises(mapstore.MapStoreError, match="父区域不存在"):
        maptags.upsert_zone("my_map", {"name": "孤儿", "parent": "z9",
                                       "polygon": [[0, 0], [1, 0], [0, 1]]})
    # 删父区域 → 子区域只剩孤儿标记，不级联删
    res = maptags.delete_zone("my_map", "z1")
    assert res["orphaned"] == ["z2"]
    assert [z["uid"] for z in maptags.get_zones("my_map")] == ["z2"]
    assert maptags.get_zones("my_map")[0]["parent"] == ""
    obj = json.loads((maps / "my_map.tags.json").read_text(encoding="utf-8"))
    assert len(obj["zones"]) == 1


def test_external_file_edit_triggers_cache_rebuild(env):
    """外部直接改 tags.json → 缓存自动重建（红线 2/3 的核心验收）。"""
    maps = env["maps"]
    maptags.upsert_destination("my_map", {"name": "A", "x": -4.0, "y": -1.0})
    assert len(db.list_destinations("my_map")) == 1
    obj = json.loads((maps / "my_map.tags.json").read_text(encoding="utf-8"))
    obj["destinations"].append({"uid": "d7", "name": "外部加的", "aliases": [],
                                "x": -3.0, "y": -1.0, "yaw_deg": 0, "risk": "low",
                                "elder_allowed": 1, "note": "", "learned_by": "manual",
                                "created_at": "2026-09-14 00:00:00",
                                "updated_at": "2026-09-14 00:00:00"})
    (maps / "my_map.tags.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8", newline="")
    rows = maptags.get_destinations("my_map")
    assert {r["uid"] for r in rows} == {"d1", "d7"}


def test_cache_can_be_dropped_and_rebuilt(env):
    """红线 3：清空索引缓存不影响任何数据，可完整重建。"""
    env["maps"]
    maptags.upsert_destination("my_map", {"name": "A", "x": -4.0, "y": -1.0})
    maptags.upsert_zone("my_map", {"name": "Z", "polygon": [[-4, -1], [-3, -1], [-3, -0.5]]})
    n = db.clear_all_map_tags()
    assert n == 3                      # 1 地点 + 1 区域 + 1 清单
    assert db.list_destinations("my_map") == [] and db.list_zones("my_map") == []
    out = maptags.reindex("my_map")
    assert out["destinations"] == 1 and out["zones"] == 1
    assert len(db.list_destinations("my_map")) == 1
    assert len(db.list_zones("my_map")) == 1


def test_old_cache_schema_is_migrated(env):
    """旧库（uid 单列主键）→ 新库（(map_name, uid) 复合主键），且不丢缓存行。

    这是**实测踩过的坑**：旧主键下 my_map 复制成 my_map2 后再刷缓存必然
    `UNIQUE constraint failed: destinations.uid`。
    注意：旧表定义上就装不下"两张图各有 d1"，所以这里的旧数据只能是"一张图一行"。
    """
    import sqlite3
    con = sqlite3.connect(db.DB_PATH)
    try:
        con.executescript("""
        DROP TABLE IF EXISTS destinations; DROP TABLE IF EXISTS zones;
        CREATE TABLE destinations (
          uid TEXT PRIMARY KEY, map_name TEXT NOT NULL, name TEXT NOT NULL,
          aliases TEXT DEFAULT '', x REAL DEFAULT 0, y REAL DEFAULT 0, yaw_deg REAL DEFAULT 0,
          risk TEXT DEFAULT 'low', elder_allowed INTEGER DEFAULT 1, note TEXT DEFAULT '',
          learned_by TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
          UNIQUE(map_name, name));
        CREATE TABLE zones (
          uid TEXT PRIMARY KEY, map_name TEXT NOT NULL, name TEXT NOT NULL,
          kind TEXT DEFAULT 'room', shape TEXT DEFAULT 'polygon',
          polygon_json TEXT DEFAULT '[]', parent TEXT DEFAULT '', note TEXT DEFAULT '',
          created_at TEXT, updated_at TEXT, UNIQUE(map_name, name));
        INSERT INTO destinations (uid,map_name,name,x,y,updated_at)
          VALUES ('d1','my_map','旧地点',1,2,'2026-09-14 10:00:00');
        INSERT INTO zones (uid,map_name,name,polygon_json,updated_at)
          VALUES ('z1','my_map','旧区域','[[0,0],[1,0],[1,1]]','2026-09-14 10:00:00');
        """)
        con.commit()
    finally:
        con.close()
    db.init_db()                      # 触发迁移
    con = sqlite3.connect(db.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        info = con.execute("PRAGMA table_info(destinations)").fetchall()
        pk = [r["name"] for r in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])]
        assert pk == ["map_name", "uid"], f"主键没迁成功：{pk}"
        zinfo = con.execute("PRAGMA table_info(zones)").fetchall()
        zpk = [r["name"] for r in sorted((r for r in zinfo if r["pk"]), key=lambda r: r["pk"])]
        assert zpk == ["map_name", "uid"]
        # 旧数据搬过来了
        rows = [dict(r) for r in con.execute("SELECT * FROM destinations").fetchall()]
        assert len(rows) == 1 and rows[0]["name"] == "旧地点" and rows[0]["uid"] == "d1"
        assert len(con.execute("SELECT * FROM zones").fetchall()) == 1
    finally:
        con.close()
    # 迁完之后"同一 uid 出现在两张图"不再炸（这正是迁移要解决的问题）
    assert [r["name"] for r in db.list_destinations("my_map")] == ["旧地点"]
    maptags.upsert_destination("my_map2", {"name": "新图地点", "x": -4.0, "y": -1.0},
                               store=mapstore.get_store())
    rows = db.list_destinations("my_map2")
    # 关键点：my_map2 是**另一张图**，它的 uid 从 d1 重新开始 —— 与 my_map 的 d1 并存不冲突，
    # 这正是必须用复合主键的原因。旧主键下这一步必然 IntegrityError。
    assert len(rows) == 1 and rows[0]["uid"] == "d1"
    assert rows[0]["name"] == "新图地点"
    assert [r["uid"] for r in db.list_destinations("my_map")] == ["d1"]
    assert [r["name"] for r in db.list_destinations("my_map")] == ["旧地点"]
    # 幂等：再跑一次 init_db 不应有任何变化
    db.init_db()
    assert len(db.list_destinations("my_map")) == 1
    assert len(db.list_destinations("my_map2")) == 1


def test_fingerprint_change_is_detected(env):
    """元数据被改过 → 明确告警，而不是静默按旧坐标用（§7.2）。"""
    maps = env["maps"]
    maptags.upsert_destination("my_map", {"name": "A", "x": -4.0, "y": -1.0})
    yml = maps / "my_map.yaml"
    yml.write_text(SAMPLE_YAML.replace("resolution: 0.05", "resolution: 0.10"), encoding="utf-8", newline="")
    mapserver.clear_cache()
    got = maptags.resolve("my_map")
    assert got["fingerprint"]["changed"] is True
    assert any("resolution" in r for r in got["fingerprint"]["reasons"])
    assert any("可能整体失准" in r for r in got["fingerprint"]["reasons"])
    # 缓存同步也会带上这条告警
    man = db.get_map_tags_manifest("my_map")
    maptags.sync_map("my_map", force=True)
    man = db.get_map_tags_manifest("my_map")
    assert any("resolution" in w for w in man["warnings"])


def test_tags_schema_normalizes_and_warns(env):
    maps = env["maps"]
    bad = {
        "version": 99, "map": "wrong_name",
        "destinations": [
            {"uid": "d1", "name": "好点", "x": 1, "y": 2, "aliases": "a,b"},
            {"uid": "d1", "name": "重复 uid"},
            {"name": "缺 uid"},
        ],
        "zones": [{"uid": "z1", "name": "Z", "polygon": [[0, 0], [1, 0], [0, 1]], "kind": "bogus"}],
    }
    (maps / "my_map.tags.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8", newline="")
    got = maptags.resolve("my_map")
    assert got["ok"]
    assert got["tags"]["map"] == "my_map"          # 按文件名归属并纠正
    assert got["tags"]["version"] == 1
    assert len(got["tags"]["destinations"]) == 1
    assert got["tags"]["destinations"][0]["aliases"] == ["a", "b"]
    assert got["tags"]["zones"][0]["kind"] == "room"   # 非法 kind 回退
    joined = " ".join(got["warnings"])
    assert "version" in joined and "不一致" in joined and "重复" in joined and "缺少 uid" in joined


def test_learn_here_uses_pose_and_degrades(env):
    locator.set_pose_for_test(-3.5, -1.2, 0.0)
    out = maptags.learn_here("my_map", {"name": "记录点"}, locator.get_pose())
    assert out["uid"] == "d1"
    d = maptags.get_destination("my_map", "d1")
    assert d["x"] == pytest.approx(-3.5) and d["y"] == pytest.approx(-1.2)
    assert d["learned_by"] == "learn_button"
    # 位姿不可用 → 明确失败
    locator.clear_injection()
    with pytest.raises(mapstore.MapStoreError, match="位姿不可用"):
        maptags.learn_here("my_map", {"name": "X"}, None)


def test_replace_all_and_backup_bytes(env):
    """整份替换可用；备份内容 == 保存前的字节（恢复的底线）。"""
    maps = env["maps"]
    out = maptags.replace_all("my_map", {"destinations": [
        {"uid": "d1", "name": "P", "x": 0, "y": 0}]})
    assert out["ok"] and (maps / "my_map.tags.json").exists()
    before = (maps / "my_map.pgm").read_bytes()
    st = _store()
    made = st.backup("my_map")
    assert len(made) == 2 and all(m.startswith(".backup/") for m in made)
    st.write("my_map", "pgm", b"P5\n1 1\n255\n\x00")
    bdir = maps / ".backup"
    backups = sorted(bdir.glob("my_map.*.pgm"))
    assert backups and backups[-1].read_bytes() == before
    assert (maps / "my_map.pgm").read_bytes() != before


def test_backup_keep_prunes_oldest(env):
    """备份保留 N 组、删的是最旧的（规格 §B11.1 第 5 条）。

    直接调 ``_prune_backups`` 而不是 mock ``time.strftime``：后者是**全局**函数，Python 的
    logging 格式化时间戳时也会调它，计数器会被第三方调用抢走（实测踩过）。
    """
    maps = env["maps"]
    st = _store()
    bdir = maps / conf.MAPS_BACKUP_DIRNAME
    bdir.mkdir(parents=True, exist_ok=True)
    stamps = [f"202603{i:02d}-000000" for i in range(1, 13)]   # 12 个互不相同的时间戳
    for ts in stamps:
        for ext in ("pgm", "yaml"):
            (bdir / f"my_map.{ts}.{ext}").write_bytes(b"x")
    # 别的图与别的文件名不能被误删（§B5.2 第 5 步"只按本模块自己的命名规则删"）
    (bdir / "other_map.20260301-000000.pgm").write_bytes(b"x")
    (bdir / "my_map.notes.txt").write_bytes(b"x")
    st._prune_backups("my_map", keep=10)
    left = sorted({p.name.split(".")[-2] for p in bdir.iterdir()
                   if p.name.startswith("my_map.") and p.suffix in (".pgm", ".yaml")})
    # 注意：`my_map.notes.txt` 这种旁路文件**不得**被算入组数，否则会多删一组（已修）
    assert left == stamps[-10:]
    assert "20260301-000000" not in left and "20260302-000000" not in left
    assert (bdir / "other_map.20260301-000000.pgm").exists()
    assert (bdir / "my_map.notes.txt").exists()


# ---------------------------------------------------------------------------
# 位姿 / 当前地图识别
# ---------------------------------------------------------------------------
def test_pose_injection_and_degrade(env):
    assert locator.pose_payload()["status"] == "unavailable"


def test_rosbridge_connection_failure_has_retry_backoff(monkeypatch):
    from LLM import roslink

    calls = 0

    def fail_connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("offline")

    roslink.reset_for_test()
    monkeypatch.setattr(roslink, "_WS_AVAILABLE", True)
    monkeypatch.setattr(roslink.websocket, "create_connection", fail_connect)

    assert roslink._connect() is None
    assert roslink._connect() is None
    assert calls == 1
    locator.set_pose_for_test(1.25, -0.5, 0.0)
    p = locator.pose_payload()
    assert p["status"] == "ok" and p["x"] == 1.25 and p["y"] == -0.5
    locator.clear_injection()
    assert locator.pose_payload()["status"] == "unavailable"


def test_current_map_by_fingerprint(env):
    """唯一命中 / 零命中 / 多命中（§5.3）。"""
    maps = env["maps"]
    locator.set_map_for_test(8, 6, 0.05, [-4.6, -1.91, 0.0])
    out = locator.current_map(_store())
    assert out["source"] == "map_topic" and out["name"] == "my_map"
    locator.set_map_for_test(99, 99, 0.05, [0, 0, 0])
    out = locator.current_map(_store())
    assert out["source"] == "unknown" and "不匹配" in out["detail"]
    # 多命中：复制一张完全同尺寸同元数据的图
    (maps / "twin.yaml").write_text(SAMPLE_YAML, encoding="utf-8", newline="")
    (maps / "twin.pgm").write_bytes(make_pgm(8, 6, 205, {(0, 0): 0, (7, 5): 254}))
    mapserver.clear_cache()
    locator.set_map_for_test(8, 6, 0.05, [-4.6, -1.91, 0.0])
    out = locator.current_map(_store())
    assert out["source"] == "unknown" and out.get("ambiguous")
    locator.clear_injection()


def test_current_map_unknown_when_no_ros(env):
    locator.clear_injection()
    out = locator.current_map(_store())
    assert out["ok"] and out["source"] == "unknown" and out["detail"]


def test_current_map_reuses_scan_until_cache_is_cleared(monkeypatch):
    calls = 0

    class CountingStore:
        def list(self):
            nonlocal calls
            calls += 1
            return [{"name": "my_map"}]

    topic = {"width": 8, "height": 6, "resolution": 0.05,
             "origin": [-4.6, -1.91, 0.0]}
    info = {**topic, "meta_ok": True}
    monkeypatch.setattr(locator, "_topic_map_meta", lambda: topic)
    monkeypatch.setattr(mapserver, "map_info", lambda _name, _store: info)

    locator.clear_current_map_cache()
    assert locator.current_map(CountingStore())["name"] == "my_map"
    assert locator.current_map(CountingStore())["name"] == "my_map"
    assert calls == 1

    locator.clear_current_map_cache()
    assert locator.current_map(CountingStore())["name"] == "my_map"
    assert calls == 2


def test_current_map_cache_ttl_starts_after_slow_scan(monkeypatch):
    calls = 0
    clock = iter([100.0, 104.0, 104.1])

    class SlowStore:
        def list(self):
            nonlocal calls
            calls += 1
            return [{"name": "my_map"}]

    topic = {"width": 8, "height": 6, "resolution": 0.05,
             "origin": [-4.6, -1.91, 0.0]}
    monkeypatch.setattr(locator, "_topic_map_meta", lambda: topic)
    monkeypatch.setattr(locator.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(mapserver, "map_info", lambda _name, _store: {
        **topic, "meta_ok": True,
    })

    locator.clear_current_map_cache()
    assert locator.current_map(SlowStore())["name"] == "my_map"
    assert locator.current_map(SlowStore())["name"] == "my_map"
    assert calls == 1


# ---------------------------------------------------------------------------
# 接口层（TestClient，无需启动 uvicorn）
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(env):
    """接口层测试用 TestClient。

    **刻意不进入 context manager**：那会触发 `lifespan`（拉起语音 worker / MCP / 提醒线程），
    在无音频设备的开发机上会长时间挂住；本文件测的是路由与数据层，不需要那些副作用
    （`db.init_db()` 已由 `env` 调过）。

    被测 app：编辑器路由已拆到独立服务 `LLM.mapeditor_server`（规格 2026-09-15）。
    该模块由「任务 2」创建，在它落地之前退回到用 `mapapi.router` 现搭一个等价 app
    （与 `mapeditor_server.app` 的路由表一致）；任务 2 落地后应删掉这段回退。
    """
    from fastapi.testclient import TestClient
    try:
        from LLM.mapeditor_server import app
    except ModuleNotFoundError:          # 任务 2 尚未落地（本拆分任务 1 的中间态）
        from fastapi import FastAPI
        from LLM import mapapi
        app = FastAPI()
        app.include_router(mapapi.router)
    return TestClient(app)


def test_api_map_list_excludes_backup(client, env):
    _store().backup("my_map")
    r = client.get("/api/map/list")
    assert r.status_code == 200
    d = r.json()
    assert [m["name"] for m in d["maps"]] == ["my_map"]
    assert d["maps"][0]["width"] == 8 and d["maps"][0]["meta_ok"] is True


def test_api_map_image_png(client):
    r = client.get("/api/map/my_map/image.png")
    assert r.status_code == 200 and r.content.startswith(mapserver.PNG_MAGIC)
    assert r.headers["content-type"] == "image/png"


def test_api_download_only_allows_known_files(client):
    assert client.get("/api/map/my_map/download?file=yaml").status_code == 200
    assert client.get("/api/map/my_map/download?file=pgm").status_code == 200
    bad = client.get("/api/map/my_map/download?file=../../.env")
    assert bad.status_code == 400 and "只接受" in bad.json()["error"]
    assert client.get("/api/map/my_map/download?file=tags").status_code == 404


def test_api_destinations_roundtrip_and_validate(client):
    r = client.post("/api/destinations", json={"map_name": "my_map", "name": "护士办公室",
                                               "aliases": ["护士站"], "x": -4.0, "y": -1.0,
                                               "yaw_deg": 90})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["uid"] == "d1"
    assert isinstance(body["warnings"], list)
    lst = client.get("/api/destinations?map=my_map").json()
    assert [d["name"] for d in lst["destinations"]] == ["护士办公室"]
    # 更新
    up = client.post("/api/destinations/d1", json={"map_name": "my_map", "name": "护士站",
                                                   "x": -3.9, "y": -1.0})
    assert up.status_code == 200 and up.json()["uid"] == "d1"
    # 校验接口
    v = client.post("/api/destinations/validate",
                    json={"map_name": "my_map", "x": -4.6, "y": -1.66}).json()
    assert v["on_obstacle"] is True
    # 删除
    assert client.delete("/api/destinations/d1?map=my_map").status_code == 200
    assert client.get("/api/destinations?map=my_map").json()["destinations"] == []


def test_api_destinations_reject_bad_map_name(client):
    r = client.post("/api/destinations", json={"map_name": "../evil", "name": "x", "x": 0, "y": 0})
    assert r.status_code == 400 and "不合法" in r.json()["error"]


def test_api_zones_and_tags_raw(client):
    z = client.post("/api/zones", json={"map_name": "my_map", "name": "101", "kind": "ward",
                                        "polygon": [[-4, -1], [-3, -1], [-3, -0.5], [-4, -0.5]]})
    assert z.status_code == 200 and z.json()["uid"] == "z1"
    assert client.get("/api/zones?map=my_map").json()["zones"][0]["name"] == "101"
    raw = client.get("/api/map/my_map/tags").json()
    assert raw["exists"] is True and raw["tags"]["zones"][0]["uid"] == "z1"
    assert "my_map.tags.json" in raw["path"]
    # PUT 整份替换
    put = client.put("/api/map/my_map/tags", json={"destinations": [
        {"uid": "d1", "name": "整份替换点", "x": 0.0, "y": 0.0}]})
    assert put.status_code == 200 and put.json()["ok"]
    assert client.get("/api/destinations?map=my_map").json()["destinations"][0]["name"] == "整份替换点"
    # reindex
    assert client.post("/api/map/my_map/tags/reindex").json()["destinations"] == 1
    assert client.post("/api/map/reindex-all/tags").json()["results"]


def test_api_rename_and_copy_handle_tags(client, env):
    maps = env["maps"]
    client.post("/api/destinations", json={"map_name": "my_map", "name": "A", "x": -4.0, "y": -1.0})
    r = client.post("/api/map/my_map/copy", json={"new_name": "my_map2"})
    assert r.status_code == 200 and r.json()["tags_copied"] is True
    assert (maps / "my_map2.tags.json").exists()
    assert json.loads((maps / "my_map2.tags.json").read_text(encoding="utf-8"))["map"] == "my_map2"
    assert "image: my_map2.pgm" in (maps / "my_map2.yaml").read_text(encoding="utf-8")
    assert client.get("/api/destinations?map=my_map2").json()["destinations"][0]["name"] == "A"
    # 重命名
    r = client.post("/api/map/my_map2/rename", json={"new_name": "my_map3"})
    assert r.status_code == 200
    assert (maps / "my_map3.yaml").exists() and not (maps / "my_map2.yaml").exists()
    assert "image: my_map3.pgm" in (maps / "my_map3.yaml").read_text(encoding="utf-8")
    tags = json.loads((maps / "my_map3.tags.json").read_text(encoding="utf-8"))
    assert tags["map"] == "my_map3"
    assert client.get("/api/map/list").json()["maps"]
    # 目标已存在 → 409
    assert client.post("/api/map/my_map/copy", json={"new_name": "my_map3"}).status_code == 409


def test_api_delete_requires_confirm_when_tagged(client, env):
    maps = env["maps"]
    client.post("/api/destinations", json={"map_name": "my_map", "name": "A", "x": -4.0, "y": -1.0})
    r = client.delete("/api/map/my_map")
    assert r.status_code == 409 and r.json()["need_confirm"] is True
    assert (maps / "my_map.yaml").exists()
    r = client.delete("/api/map/my_map?confirm=true")
    assert r.status_code == 200
    assert not (maps / "my_map.yaml").exists() and not (maps / "my_map.tags.json").exists()
    assert db.list_destinations("my_map") == []


def test_api_meta_change_requires_confirm(client, env):
    client.post("/api/destinations", json={"map_name": "my_map", "name": "A", "x": -4.0, "y": -1.0})
    r = client.post("/api/map/my_map/meta", json={"resolution": 0.1})
    assert r.status_code == 409 and r.json()["need_confirm"] is True
    assert r.json()["affects"]["destinations"] == 1
    r = client.post("/api/map/my_map/meta", json={"resolution": 0.1, "confirm": True})
    assert r.status_code == 200, r.text
    yml = (env["maps"] / "my_map.yaml").read_text(encoding="utf-8")
    assert "resolution: 0.1" in yml
    # 备份存在
    assert list((env["maps"] / ".backup").glob("my_map.*.yaml"))
    # tags 指纹被"已确认"地覆写
    obj = json.loads((env["maps"] / "my_map.tags.json").read_text(encoding="utf-8"))
    assert obj["resolution"] == pytest.approx(0.1)
    # 非法值
    assert client.post("/api/map/my_map/meta",
                       json={"resolution": -1, "confirm": True}).status_code == 400
    assert client.post("/api/map/my_map/meta",
                       json={"origin": [0, 0, 0.5], "confirm": True}).status_code == 400


def test_api_save_saveas_then_overwrite(client, env):
    maps = env["maps"]
    yaml_disk = (maps / "my_map.yaml").read_text(encoding="utf-8")
    # 上游回传的 yaml 是"重新序列化"过的：键值相同但顺序/格式不同 → 必须通过校验
    upstream_yaml = ("image: my_map_edited.pgm\nmode: trinary\norigin: [-4.6, -1.91, 0]\n"
                     "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.25\nresolution: 0.05\n")
    pgm = make_pgm(8, 6, 205, {(1, 1): 0})
    body = {"pgm_b64": base64.b64encode(pgm).decode(), "yaml_text": upstream_yaml,
            "mode": "saveas"}
    r = client.post("/api/map/my_map/save", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["target"] == "my_map_edited"
    assert sorted(d["wrote"]) == ["my_map_edited.pgm", "my_map_edited.yaml"]
    assert "nav_screen.sh nav my_map_edited" in d["restart_hint"]
    # yaml 以磁盘原文为本，只改 image 一行
    new_yaml = (maps / "my_map_edited.yaml").read_text(encoding="utf-8")
    assert new_yaml == mapserver.update_yaml_image(yaml_disk, "my_map_edited.pgm")
    assert (maps / "my_map_edited.pgm").read_bytes() == pgm
    # 覆盖保存 → 强制备份 + confirm
    n_before = len(list((maps / ".backup").glob("my_map.*.pgm")))
    r2 = client.post("/api/map/my_map/save", json={**body, "mode": "overwrite"})
    assert r2.status_code == 409 and r2.json()["need_confirm"] is True
    r2 = client.post("/api/map/my_map/save", json={**body, "mode": "overwrite", "confirm": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["backup"]
    assert len(list((maps / ".backup").glob("my_map.*.pgm"))) > n_before
    assert (maps / "my_map.pgm").read_bytes() == pgm


def test_api_save_rejects_yaml_metadata_change(client, env):
    """除 image 外任何字段不一致 → 409 并列出差异键（规格 §B一 验收 6）。"""
    yaml_bad = SAMPLE_YAML.replace("resolution: 0.05", "resolution: 0.10")
    r = client.post("/api/map/my_map/save", json={
        "pgm_b64": base64.b64encode(make_pgm(2, 2)).decode(), "yaml_text": yaml_bad,
        "mode": "saveas"})
    assert r.status_code == 409
    assert any("resolution" in d for d in r.json()["diffs"])
    assert not (env["maps"] / "my_map_edited.pgm").exists()


def test_api_save_rejects_bad_name_and_bad_magic(client, env):
    body = {"pgm_b64": base64.b64encode(make_pgm(2, 2)).decode(), "yaml_text": SAMPLE_YAML,
            "mode": "saveas", "new_name": "../../evil"}
    r = client.post("/api/map/my_map/save", json=body)
    assert r.status_code == 400 and "不合法" in r.json()["error"]
    body["new_name"] = "ok_name"
    body["pgm_b64"] = base64.b64encode(b"P6\n1 1\n255\n\x00\x00\x00").decode()
    r = client.post("/api/map/my_map/save", json=body)
    assert r.status_code == 400 and "魔数" in r.json()["error"]
    assert not (env["maps"] / "ok_name.pgm").exists()


def test_api_save_size_limit(client, env):
    big = b"P5\n3000 3000\n255\n" + b"\x00" * 9  # 声明很大但实际小 → 走"体积"分支前先看真实长度
    monkey = conf.MAPS_MAX_PGM_BYTES
    try:
        conf.MAPS_MAX_PGM_BYTES = 10
        r = client.post("/api/map/my_map/save", json={
            "pgm_b64": base64.b64encode(make_pgm(8, 6)).decode(), "yaml_text": SAMPLE_YAML,
            "mode": "saveas"})
        assert r.status_code == 413
    finally:
        conf.MAPS_MAX_PGM_BYTES = monkey
    assert big


def test_api_save_marks_travel_with_tags(client, env):
    maps = env["maps"]
    client.post("/api/destinations", json={"map_name": "my_map", "name": "A", "x": -4.0, "y": -1.0})
    r = client.post("/api/map/my_map/save", json={
        "pgm_b64": base64.b64encode(make_pgm(8, 6, 205, {(2, 2): 0})).decode(),
        "yaml_text": SAMPLE_YAML, "mode": "saveas", "new_name": "my_map_x"})
    assert r.status_code == 200, r.text
    assert r.json()["tags_copied"] is True
    t = json.loads((maps / "my_map_x.tags.json").read_text(encoding="utf-8"))
    assert t["map"] == "my_map_x" and t["destinations"][0]["name"] == "A"


def test_api_io_and_pose_endpoints(client):
    io = client.get("/api/mapeditor/io").json()
    assert io["ok"] and io["mode"] == "local" and io["available"] is True
    t = client.post("/api/mapeditor/io/test").json()
    assert t["ok"] is True and t["count"] == 1 and "elapsed_ms" in t
    assert client.get("/api/robot/pose").json()["status"] == "unavailable"
    inj = client.post("/api/mapeditor/pose/inject", json={"x": 0.5, "y": -0.5, "yaw": 0.0}).json()
    assert inj["pose"]["status"] == "ok"
    assert client.get("/api/robot/pose").json()["x"] == 0.5
    client.post("/api/mapeditor/pose/inject", json={})
    assert client.get("/api/robot/pose").json()["status"] == "unavailable"
    st = client.get("/api/mapeditor/status").json()
    assert st["ok"] and "locator" in st


def test_api_audit_records_map_events(client, env):
    client.post("/api/destinations", json={"map_name": "my_map", "name": "A", "x": -4.0, "y": -1.0})
    # 名字非法（含空格）→ 必须写 map_edit_reject
    client.post("/api/map/my_map/save", json={
        "pgm_b64": base64.b64encode(make_pgm(8, 6)).decode(), "yaml_text": SAMPLE_YAML,
        "mode": "saveas", "new_name": "bad name"})
    text = audit_log.AUDIT_LOG.read_text(encoding="utf-8")
    assert "map_change" in text and "destination_add" in text
    assert "map_edit_reject" in text
    # yaml 不一致 → 再一次 map_edit_reject
    before = text.count("map_edit_reject")
    client.post("/api/map/my_map/save", json={
        "pgm_b64": base64.b64encode(make_pgm(8, 6)).decode(),
        "yaml_text": SAMPLE_YAML.replace("0.05", "0.09"), "mode": "saveas"})
    assert audit_log.AUDIT_LOG.read_text(encoding="utf-8").count("map_edit_reject") > before


def test_map_list_does_not_block_event_loop(monkeypatch):
    import asyncio
    import time

    from LLM import mapapi as server   # map_list/_store 已随编辑器路由搬到 mapapi（任务 1）

    class SlowStore:
        root = "slow-test"

        def available(self):
            return True, ""

        def list(self):
            time.sleep(0.15)
            return []

    # 2026-09-14「地图源」之后 _store 带一个 source 形参（默认源时不传），lambda 要能收下它；
    # map_list 也显式传 source=""，免得直接拿到 Query 默认对象。
    monkeypatch.setattr(server, "_store", lambda source="": SlowStore())
    monkeypatch.setattr(server.db, "get_settings", lambda: {"current_map": ""})

    async def exercise():
        started = time.perf_counter()
        request = asyncio.create_task(server.map_list(""))
        await asyncio.sleep(0.01)
        heartbeat_elapsed = time.perf_counter() - started
        await request
        return heartbeat_elapsed

    assert asyncio.run(exercise()) < 0.08
