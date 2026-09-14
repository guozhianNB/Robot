# -*- coding: utf-8 -*-
r"""
地图位图服务（规格 §7.4 / §7.1）——**纯 stdlib**：PGM 解析、灰度 PNG 编码、未知率统计、
像素↔米换算、标点即校验。

为什么不用 Pillow：项目依赖清单里没有它，板卡环境依赖不可信，AGENTS 要求「能用 stdlib
就绝不引外部依赖」。PNG 只用到 ``zlib`` + ``struct``，灰度高 8bit 足够前端按阈值着色。

未知率口径（规格 §7.4）：按 yaml 的 ``negate``/``occupied_thresh``/``free_thresh`` 把像素
三分。占用概率 ``occ`` 与 ROS ``map_server`` 一致：**先归一化再按 ``negate`` 取反** ——

    occ = pixel/255     （negate 为真）
    occ = 1 - pixel/255 （negate 为假，即默认：**黑=占用、白=空闲、灰=未知**）

然后 ``occ > occupied_thresh`` → occupied，``occ < free_thresh`` → free，其余 → unknown。
举例（``negate=0``，默认阈值 0.65/0.25）：像素 0 → occ=1.0 → occupied；255/254 → occ≈0.0 → free；
205（ROS 的"未知"灰） → occ≈0.196 → free。这与 ROS ``map_server``/``map_saver`` 的口径一致，
也和本仓既有工具的判定一致。
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

from . import conf
from .mapstore import MapStoreError, get_store

# 像素分类
OCCUPIED, FREE, UNKNOWN = "occupied", "free", "unknown"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# yaml（扁平 map_saver schema 的最小解析器，零依赖）
# ---------------------------------------------------------------------------
def parse_yaml_flat(text: str) -> dict:
    """解析 map_saver 生成的**扁平** ``key: value`` yaml（不含嵌套）。

    支持：``#`` 注释、空行、``[a, b, c]`` 行内数组、引号包裹的字符串、bool/int/float。
    不支持的语法（嵌套 map、多行块、锚点）会让该行被忽略 —— 我们的 yaml 只有 7 行标量。
    """
    out: dict = {}
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        out[key] = _scalar(val)
    return out


def _scalar(val: str):
    v = val.strip()
    if v == "":
        return ""
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in inner.split(",")]
    if (v.startswith('"') and v.endswith('"') and len(v) >= 2) or \
       (v.startswith("'") and v.endswith("'") and len(v) >= 2):
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def meta_from_yaml(text: str) -> dict:
    """从 yaml 文本抽地图元数据；缺字段用 ROS 默认值补齐，并标 ``meta_ok``。"""
    y = parse_yaml_flat(text)
    problems: list[str] = []
    res = y.get("resolution")
    try:
        res = float(res)
        if res <= 0:
            raise ValueError("resolution 必须为正")
    except (TypeError, ValueError):
        problems.append(f"resolution 缺失或非法（{res!r}）")
        res = None
    origin = y.get("origin")
    ok_origin = isinstance(origin, list) and len(origin) >= 2
    if ok_origin:
        try:
            origin = [float(origin[0]), float(origin[1]),
                      float(origin[2]) if len(origin) > 2 else 0.0]
        except (TypeError, ValueError):
            ok_origin = False
    if not ok_origin:
        problems.append(f"origin 缺失或非法（{y.get('origin')!r}）")
        origin = None
    elif abs(origin[2]) > 1e-9:
        problems.append(f"origin 的 yaw={origin[2]} 非 0（本项目约定全部为 0，不支持旋转地图）")

    def _num(key: str, default: float) -> float:
        try:
            return float(y[key])
        except (KeyError, TypeError, ValueError):
            return default

    return {
        "raw": y,
        "image": str(y.get("image") or ""),
        "mode": str(y.get("mode") or "trinary"),
        "resolution": res,
        "origin": origin,
        "negate": int(_num("negate", 0.0)),
        "occupied_thresh": _num("occupied_thresh", 0.65),
        "free_thresh": _num("free_thresh", 0.25),
        "meta_ok": not problems,
        "problems": problems,
    }


def update_yaml_image(text: str, image_name: str) -> str:
    """**以磁盘原文为本**，只替换 ``image:`` 那一行（无该行则追加）。

    规格 §B5.2 第 8 步 / §B9 坑 7：上游用 js-yaml ``dump()`` 重新序列化会丢掉注释与
    缩进风格，所以这边只在原文上改一行。
    """
    lines = (text or "").splitlines(keepends=True)
    if not lines or not text.endswith(("\n", "\r")):
        pass
    hit = -1
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.split(":", 1)[0].strip() == "image" and ":" in stripped:
            hit = i
            break
    newline = "\n"
    for ln in lines:
        if ln.endswith("\r\n"):
            newline = "\r\n"
            break
    if hit >= 0:
        indent = lines[hit][: len(lines[hit]) - len(lines[hit].lstrip())]
        tail = newline if lines[hit].endswith(("\n", "\r")) else ""
        lines[hit] = f"{indent}image: {image_name}{tail}"
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] = lines[-1] + newline
        lines.append(f"image: {image_name}{newline}")
    return "".join(lines)


# ---------------------------------------------------------------------------
# PGM
# ---------------------------------------------------------------------------
class PGM:
    """解析后的 PGM：``magic/width/height/maxval/pixels``（``pixels`` 无符号整数序列）。"""

    __slots__ = ("magic", "width", "height", "maxval", "pixels")

    def __init__(self, magic: str, width: int, height: int, maxval: int, pixels):
        self.magic = magic
        self.width = width
        self.height = height
        self.maxval = maxval
        self.pixels = pixels

    def __len__(self) -> int:
        return self.width * self.height


def parse_pgm(data: bytes) -> PGM:
    """解析 P2（ASCII）/ P5（二进制）PGM；其他魔数报明确错误（上游 ``parsePGM`` 同样只认这两种）。"""
    if not data:
        raise MapStoreError("PGM 为空")
    pos = 0
    n = len(data)

    def _token() -> bytes:
        nonlocal pos
        while pos < n:
            c = data[pos:pos + 1]
            if c in b" \t\r\n":
                pos += 1
                continue
            if c == b"#":
                while pos < n and data[pos:pos + 1] not in b"\r\n":
                    pos += 1
                continue
            break
        start = pos
        while pos < n and data[pos:pos + 1] not in b" \t\r\n":
            pos += 1
        return data[start:pos]

    magic = _token().decode("ascii", "replace")
    if magic not in ("P2", "P5"):
        raise MapStoreError(f"不支持的 PGM 魔数：{magic!r}（只认 P2/P5）")
    try:
        width = int(_token())
        height = int(_token())
        maxval = int(_token())
    except ValueError as e:
        raise MapStoreError(f"PGM 头解析失败（宽/高/maxval）：{e}") from e
    if width <= 0 or height <= 0:
        raise MapStoreError(f"PGM 尺寸非法：{width}x{height}")
    if not (0 < maxval <= 65535):
        raise MapStoreError(f"PGM maxval 非法：{maxval}")
    total = width * height

    if magic == "P2":
        vals = []
        while pos < n and len(vals) < total:
            tok = _token()
            if not tok:
                break
            vals.append(int(tok))
        if len(vals) < total:
            raise MapStoreError(f"P2 数据不足：期望 {total} 个像素，实际 {len(vals)}")
        pixels = vals
    else:
        pos += 1                      # 头部与二进制数据之间恰好一个空白字符
        bpp = 1 if maxval < 256 else 2
        need = total * bpp
        body = data[pos:pos + need]
        if len(body) < need:
            raise MapStoreError(f"P5 数据不足：期望 {need} 字节，实际 {len(body)}")
        if bpp == 1:
            pixels = list(body)
        else:
            pixels = list(struct.unpack(f">{total}H", body))
    return PGM(magic, width, height, maxval, pixels)


def encode_pgm(pgm: PGM) -> bytes:
    """编码成 P5（≤255）或 P5 双字节大端（>255），与上游 ``encodePGM`` 同口径。"""
    maxval = int(pgm.maxval or 255)
    head = f"P5\n{pgm.width} {pgm.height}\n{maxval}\n".encode("ascii")
    if maxval < 256:
        body = bytes(min(255, max(0, int(v))) for v in pgm.pixels)
    else:
        body = struct.pack(f">{len(pgm.pixels)}H", *[int(v) & 0xFFFF for v in pgm.pixels])
    return head + body


# ---------------------------------------------------------------------------
# PNG（灰度高 8bit，纯 stdlib）
# ---------------------------------------------------------------------------
def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def encode_gray_png(width: int, height: int, gray: bytes | bytearray) -> bytes:
    """把灰度像素（``width*height`` 字节，行优先、**行 0 在图像顶部**）编码成 PNG。"""
    if len(gray) != width * height:
        raise MapStoreError(f"灰度数据长度不符：{len(gray)} != {width}*{height}")
    raw = bytearray()
    stride = width
    for y in range(height):
        raw.append(0)                    # filter type 0（None）
        raw += gray[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)   # 8bit 灰度
    return (PNG_MAGIC + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# 元数据 / 图元缓存（键 = store 签名 + name + mtime + size）
# ---------------------------------------------------------------------------
_META_CACHE: dict[str, dict] = {}
_IMG_CACHE: dict[str, bytes] = {}
_CACHE_MAX = 8


def _cache_key(name: str, ext: str, st: tuple[float, int] | None) -> str:
    mt, sz = (st or (0.0, 0))
    return f"{conf.MAPS_IO}|{get_store().root}|{name}|{ext}|{mt:.3f}|{sz}"


def _remember(cache: dict, key: str, val):
    if len(cache) >= _CACHE_MAX and key not in cache:
        cache.pop(next(iter(cache)))
    cache[key] = val
    return val


def clear_cache() -> None:
    _META_CACHE.clear()
    _IMG_CACHE.clear()


def read_meta(name: str, store=None) -> dict:
    """读某图的 yaml 元数据（含 ``meta_ok``/``problems``）。文件缺失 → 抛 ``MapStoreError``。"""
    st = store or get_store()
    stt = st.stat(name, "yaml")
    key = _cache_key(name, "yaml", stt)
    if key in _META_CACHE:
        return _META_CACHE[key]
    try:
        text = st.read(name, "yaml").decode("utf-8", "replace")
    except MapStoreError:
        raise
    meta = meta_from_yaml(text)
    meta["yaml_text"] = text
    meta["yaml_stat"] = stt
    return _remember(_META_CACHE, key, meta)


def read_pgm(name: str, store=None) -> tuple[PGM, bool, float | None]:
    """读某图的 PGM；返回 ``(pgm, stale, cached_at)``（ssh 断连时可能来自缓存）。"""
    st = store or get_store()
    data, stale, at = st.read_with_meta(name, "pgm")
    return parse_pgm(data), stale, at


def occupancy(pixel: float, maxval: int, negate: bool) -> float:
    """单像素的占用概率（与 ROS map_server 一致）：``negate`` 为假时黑=占用。"""
    p = (float(pixel) / maxval) if maxval else 0.0
    return (1.0 - p) if not negate else p


def classify_counts(pgm: PGM, meta: dict) -> dict:
    """按 yaml 阈值三分像素，返回 ``{occupied, free, unknown, total, unknown_ratio}``。"""
    occ_t = float(meta.get("occupied_thresh") or 0.65)
    free_t = float(meta.get("free_thresh") or 0.25)
    negate = bool(meta.get("negate") or 0)
    maxv = pgm.maxval or 255
    occ = free = unk = 0
    for v in pgm.pixels:
        p = occupancy(v, maxv, negate)
        if p > occ_t:
            occ += 1
        elif p < free_t:
            free += 1
        else:
            unk += 1
    total = pgm.width * pgm.height
    return {"occupied": occ, "free": free, "unknown": unk, "total": total,
            "unknown_ratio": (unk / total) if total else 0.0}


def image_png(name: str, store=None) -> tuple[bytes, bool, float | None]:
    """生成灰度 PNG（按 ``mtime+size`` 缓存）。返回 ``(png_bytes, stale, cached_at)``。"""
    st = store or get_store()
    stt = st.stat(name, "pgm")
    key = _cache_key(name, "png", stt)
    cached = _IMG_CACHE.get(key)
    if cached is not None:
        return cached, False, None
    pgm, stale, at = read_pgm(name, st)
    gray = bytearray(len(pgm.pixels))
    if pgm.maxval == 255:
        gray[:] = bytes(min(255, max(0, int(v))) for v in pgm.pixels)
    else:
        maxv = pgm.maxval or 255
        for i, v in enumerate(pgm.pixels):
            gray[i] = int(round(v * 255.0 / maxv))
    png = encode_gray_png(pgm.width, pgm.height, gray)
    _remember(_IMG_CACHE, key, png)
    return png, stale, at


def map_info(name: str, store=None) -> dict:
    """地图完整信息：yaml 元数据 + 尺寸 + 未知率。供 ``/api/map/{name}/meta`` 与列表用。"""
    st = store or get_store()
    meta = read_meta(name, st)
    info = {
        "name": name, "source": conf.MAPS_IO,
        "image": meta.get("image"), "mode": meta.get("mode"),
        "resolution": meta.get("resolution"), "origin": meta.get("origin"),
        "negate": meta.get("negate"),
        "occupied_thresh": meta.get("occupied_thresh"),
        "free_thresh": meta.get("free_thresh"),
        "meta_ok": meta.get("meta_ok"), "problems": meta.get("problems") or [],
        "width": None, "height": None, "unknown_ratio": None,
        "counts": None, "stale": False, "cached_at": None,
    }
    try:
        pgm, stale, at = read_pgm(name, st)
        info.update({"width": pgm.width, "height": pgm.height,
                     "stale": stale, "cached_at": at})
        if meta.get("resolution") and meta.get("origin"):
            info["counts"] = classify_counts(pgm, meta)
            info["unknown_ratio"] = info["counts"]["unknown_ratio"]
            res, origin = float(meta["resolution"]), meta["origin"]
            info["bounds"] = {
                "min_x": origin[0], "max_x": origin[0] + (pgm.width - 1) * res,
                "min_y": origin[1], "max_y": origin[1] + (pgm.height - 1) * res,
            }
    except MapStoreError as e:
        info["problems"] = list(info["problems"]) + [f"PGM 不可用：{e}"]
    return info


# ---------------------------------------------------------------------------
# §7.1 像素 ↔ 米 换算（y 轴翻转是最高频错误）
# ---------------------------------------------------------------------------
def meters_to_pixel(x: float, y: float, resolution: float, origin, height: int) -> tuple[float, float]:
    """米 → 像素（浮点）。``py`` 是**图像行号（从上往下）**，故 y 轴要翻转。"""
    ox, oy = float(origin[0]), float(origin[1])
    px = (x - ox) / resolution
    py = (height - 1) - (y - oy) / resolution
    return px, py


def pixel_to_meters(px: float, py: float, resolution: float, origin, height: int) -> tuple[float, float]:
    """像素（浮点，``py`` 为行号）→ 米。"""
    ox, oy = float(origin[0]), float(origin[1])
    x = ox + px * resolution
    y = oy + (height - 1 - py) * resolution
    return x, y


def pixel_at(pgm: PGM, x: float, y: float, resolution: float, origin) -> tuple[int, int] | None:
    """米坐标 → 整数像素下标；越界返回 None。"""
    if resolution <= 0 or not origin:
        return None
    px, py = meters_to_pixel(x, y, resolution, origin, pgm.height)
    ix, iy = int(round(px)), int(round(py))
    if 0 <= ix < pgm.width and 0 <= iy < pgm.height:
        return ix, iy
    return None


def classify_pixel(pgm: PGM, ix: int, iy: int, meta: dict) -> str:
    """单个像素的分类（occupied/free/unknown）。"""
    v = pgm.pixels[iy * pgm.width + ix]
    p = occupancy(v, pgm.maxval or 255, bool(meta.get("negate")))
    if p > float(meta.get("occupied_thresh") or 0.65):
        return OCCUPIED
    if p < float(meta.get("free_thresh") or 0.25):
        return FREE
    return UNKNOWN


# ---------------------------------------------------------------------------
# §7.6 标点即校验
# ---------------------------------------------------------------------------
def _distance_transform(cls: list[str], width: int, height: int, resolution: float) -> list[float]:
    """到最近 occupied 像素的欧氏距离（米）。两趟扫描 + 平方距离，避免开方。

    用 ``inf`` 表示"没有障碍"。
    """
    inf = float("inf")
    d = [0.0 if c == OCCUPIED else inf for c in cls]
    # 第一趟：左上 → 右下
    for y in range(height):
        row = y * width
        for x in range(width):
            i = row + x
            best = d[i]
            if best == 0.0:
                continue
            if y > 0:
                best = min(best, d[i - width] + 1.0)
                if x > 0:
                    best = min(best, d[i - width - 1] + 2.0)
                if x + 1 < width:
                    best = min(best, d[i - width + 1] + 2.0)
            if x > 0:
                best = min(best, d[i - 1] + 1.0)
            d[i] = best
    # 第二趟：右下 → 左上
    for y in range(height - 1, -1, -1):
        row = y * width
        for x in range(width - 1, -1, -1):
            i = row + x
            best = d[i]
            if best == 0.0:
                continue
            if y + 1 < height:
                best = min(best, d[i + width] + 1.0)
                if x > 0:
                    best = min(best, d[i + width - 1] + 2.0)
                if x + 1 < width:
                    best = min(best, d[i + width + 1] + 2.0)
            if x + 1 < width:
                best = min(best, d[i + 1] + 1.0)
            d[i] = best
    return [x * resolution for x in d]


_DT_CACHE: dict[str, tuple[list[float], int, int]] = {}


def _clearance_grid(name: str, store=None):
    """(清距图, width, height)。键含 mtime+size，地图变了自动失效。"""
    st = store or get_store()
    stt = st.stat(name, "pgm")
    key = _cache_key(name, "dt", stt)
    got = _DT_CACHE.get(key)
    if got is not None:
        return got[0], got[1], got[2]
    pgm, _stale, _at = read_pgm(name, st)
    meta = read_meta(name, st)
    cls = [classify_pixel(pgm, i % pgm.width, i // pgm.width, meta)
           for i in range(pgm.width * pgm.height)]
    dt = _distance_transform(cls, pgm.width, pgm.height, float(meta.get("resolution") or 0.05))
    if len(_DT_CACHE) >= 4:
        _DT_CACHE.pop(next(iter(_DT_CACHE)))
    _DT_CACHE[key] = (dt, pgm.width, pgm.height)
    return dt, pgm.width, pgm.height


def validate_point(name: str, x: float, y: float, store=None,
                   margin_m: float | None = None) -> dict:
    """标点校验（规格 §7.6）：越界 / 障碍 / 未知 / 距障碍余量。**只警告不阻止**。"""
    st = store or get_store()
    margin = float(conf.DEFAULT_SETTINGS["map_boundary_margin_m"]
                   if margin_m is None else margin_m)
    meta = read_meta(name, st)
    res, origin = meta.get("resolution"), meta.get("origin")
    reasons: list[str] = []
    out = {"ok": True, "map_name": name, "x": x, "y": y,
           "on_obstacle": False, "on_unknown": False,
           "clearance_m": None, "in_bounds": True,
           "edge_margin_m": None, "reasons": reasons}
    if not res or not origin:
        reasons.append("该图 yaml 元数据不可用，无法校验")
        out["ok"] = False
        return out
    if abs(origin[2]) > 1e-9:
        reasons.append(f"该图 origin yaw={origin[2]} 非 0，不支持旋转地图")
        out["ok"] = False
        return out
    pgm, _stale, _at = read_pgm(name, st)
    ix, iy = None, None
    px, py = meters_to_pixel(x, y, res, origin, pgm.height)
    if 0 <= px <= pgm.width - 1 and 0 <= py <= pgm.height - 1:
        ix, iy = int(round(px)), int(round(py))
    if ix is None:
        out["in_bounds"] = False
        reasons.append(f"该点在地图有效范围外（图尺寸 {pgm.width}x{pgm.height}）")
        left = x - origin[0]
        right = (origin[0] + (pgm.width - 1) * res) - x
        bottom = y - origin[1]
        top = (origin[1] + (pgm.height - 1) * res) - y
        out["edge_margin_m"] = round(min(left, right, bottom, top), 3)
        return out
    kind = classify_pixel(pgm, ix, iy, meta)
    out["pixel"] = [ix, iy]
    out["pixel_kind"] = kind
    if kind == OCCUPIED:
        out["on_obstacle"] = True
        reasons.append("该点在障碍像素上，Nav2 可能拒绝该目标")
    elif kind == UNKNOWN:
        out["on_unknown"] = True
        reasons.append("该处未扫到（unknown），导航可能失败")
    # 边界余量（沿用 where_am_i.py 口径：各边缩进 margin）
    left = x - origin[0]
    right = (origin[0] + (pgm.width - 1) * res) - x
    bottom = y - origin[1]
    top = (origin[1] + (pgm.height - 1) * res) - y
    edge = min(left, right, bottom, top)
    out["edge_margin_m"] = round(edge, 3)
    if edge < margin:
        reasons.append(f"距地图边界仅 {edge:.2f} m（< {margin} m），越界 3cm 就会 ABORTED")
    try:
        dt, w, h = _clearance_grid(name, st)
        out["clearance_m"] = round(dt[iy * w + ix], 3)
        if math.isinf(out["clearance_m"]):
            out["clearance_m"] = None
    except MapStoreError:
        pass
    return out
