# -*- coding: utf-8 -*-
r"""识图业务核心（不含 MCP）：抓一帧画面 → 连同文字一起送云端视觉大模型 → 取回文字回答。

【职责边界】
  本模块只管"业务"，不碰 MCP 协议、不碰 stdio：
    - grab_camera_jpeg()  ：从摄像头共享服务取**当前帧**，用纯 stdlib 编码器编成 JPEG
    - load_image_file()   ：读一张本机图片（**限白名单目录内**），原样作载荷
    - ask_qwen()          ：把（图 + 文字）发给阿里百炼的视觉模型，返回回答文本
    - see_what()          ：上面三步的编排，返回统一 dict（**永不抛异常**）
  MCP 注册在 see_server.py 里 —— 业务层因此可以脱离 MCP 框架直接单测。

【为什么复用 vision/ 的东西，而不是绕后端 HTTP】
  vision/ 是摄像头共享子系统：摄像头同一时刻只能被一个进程独占，故由
  `camera_server` 守护进程唯一持有、按通道向多客户端分发"最新一帧"。它自带的
  `webbridge.nv12_to_jpeg()` 是**纯 stdlib** 的 NV12→JPEG 编码器，所以这里直接
  复用，不引 opencv / Pillow（AGENTS.md：能用 stdlib 就绝不引外部依赖）。
  **刻意不走**后端 `/api/vision/snapshot`：那会形成"后端拉起本子进程 → 子进程再
  回调后端"的自环 —— 后端还没监听、或后端重启期间，识图就必挂。

【两条取图路径的取舍（重要）】
  `webbridge.get_jpeg()` 会**优先**走服务端硬件 JPEG（需 `camera_server --enable-jpeg`），
  但那条路返回**全分辨率 1920x1080**（按官方公式 h*w/(32*32)+2 ≈ 2000 token/张，
  又慢又贵）；软件路径又固定压到 640 宽。故此处不走 get_jpeg，而是自己取 NV12 再
  按 VISION_SEE_MAX_WIDTH 编码，尺寸口径可控且**两条来源（硬件/软件）行为一致**。

【降级（AGENTS.md「系统稳健性」）】
  外部依赖（openai / python-dotenv）与项目内包（vision）都在顶层逐个 try/except
  引入，失败逐条记进 _MISSING；`cloud_available()` 汇总"能不能做识图"。
  - see_server 启动时若云端不可用 → 降级退出（不污染后端，见该文件）；
  - **摄像头不可用不算启动失败**（服务可能稍后才起）→ 留到每次调用时返回 ok:false；
  - 所有对外函数都不抛异常，失败一律 {"ok": False, "error": "人话原因"}。
"""
import base64
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 仓库根入 sys.path：本文件是 LLM/vision_mcp/xxx.py，故 parents[2] = 仓库根。
# 目的：脚本方式启动（python LLM/vision_mcp/see_server.py）时也能 import vision / LLM。
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# 可选依赖逐个引入（缺哪个记哪条，别只报第一个）
# ---------------------------------------------------------------------------
_MISSING: list[str] = []

try:
    from openai import OpenAI
except Exception as _exc:            # 用 Exception 而非 ImportError：版本不兼容也覆盖
    OpenAI = None
    _MISSING.append("openai: " + str(_exc))

try:
    from dotenv import load_dotenv
except Exception as _exc:
    load_dotenv = None
    # dotenv 缺失不算致命：只要环境里已有 DASHSCOPE_API_KEY 照样能用

try:
    from vision.camera_client import (CameraClient, CameraNotRunning,
                                      CameraServerError, CameraTimeout)
    from vision.webbridge import nv12_to_jpeg
except Exception as _exc:
    CameraClient = None
    nv12_to_jpeg = None
    _MISSING.append("vision（摄像头共享子系统）: " + str(_exc))

# .env 只为"单独起本 server 调试"兜底：正常由后端拉起时，mcp_client 已把
# DASHSCOPE_API_KEY 继承进子进程环境（conf.MCP_SERVERS 的 env 空串机制）。
if load_dotenv is not None:
    try:
        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 运行参数（环境变量优先，均可留空取默认；不新增 conf.py 常量，保持独立）
# ---------------------------------------------------------------------------
MODEL = (os.environ.get("VISION_SEE_MODEL") or "qwen-vl-plus").strip() or "qwen-vl-plus"   # 正式视觉模型；可由环境变量覆盖
BASE_URL = (os.environ.get("VISION_SEE_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
if not BASE_URL:
    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TIMEOUT = _env_float("VISION_SEE_TIMEOUT", 20.0)      # 云端单次调用超时（秒）
MAX_TOKENS = _env_int("VISION_SEE_MAX_TOKENS", 512)   # 回答长度上限：回答要念给老人听，别放任长文
MAX_WIDTH = _env_int("VISION_SEE_MAX_WIDTH", 1024)    # 送云端前的最大宽度（控 token 与时延）
QUALITY = _env_int("VISION_SEE_QUALITY", 85)          # JPEG 质量
GRAB_TIMEOUT = _env_float("VISION_SEE_GRAB_TIMEOUT", 5.0)   # 取帧超时（秒）
CONNECT_TIMEOUT = _env_float("VISION_SEE_CONNECT_TIMEOUT", 2.0)
CHANNEL = _env_int("VISION_SEE_CHANNEL", 1)           # 摄像头通道（1=全分辨率，2=512x512 小图）
MAX_IMAGE_BYTES = _env_int("VISION_SEE_MAX_BYTES", 3 * 1024 * 1024)   # 单图上限，防超长 payload

# 允许读取的图片目录（os.pathsep 分隔多目录）。**默认只允许 inbox 一个目录**：
# 否则"模型可指定任意本机路径"就等于给对话链开了个本机文件读取能力，是新的攻击面。
_ALLOWED_DIRS = [Path(p).expanduser()
                 for p in (os.environ.get("VISION_SEE_IMAGE_DIRS") or "").split(os.pathsep)
                 if p.strip()] or [REPO_ROOT / "LLM" / "data" / "vision_inbox"]

_SUPPORTED_MIME = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}


def missing() -> list[str]:
    """缺失依赖原因列表（拷一份，避免调用方改到内部状态）。"""
    return list(_MISSING)


def cloud_available() -> bool:
    """云端识图是否可用（openai SDK 在 + 有 DASHSCOPE_API_KEY）。

    只看"能不能发请求"，不碰网络 —— 真正的连通性由调用时的异常暴露。
    """
    if OpenAI is None:
        return False
    return bool((os.getenv("DASHSCOPE_API_KEY") or "").strip())


def vision_target() -> tuple[str, int]:
    """摄像头共享服务地址：优先跟后端配置（LLM.conf 的 VISION_HOST/PORT）保持一致。

    「后端在 PC、摄像头在板卡」时后端会设 VISION_HOST=板卡地址；本子进程继承同一
    环境变量，故取值天然一致。conf 不可 import（如被并发改动写坏）时退回环境变量默认。
    """
    host = os.environ.get("VISION_HOST") or "127.0.0.1"
    port = _env_int("VISION_PORT", 9540)
    try:
        from LLM import conf as _conf
        host = getattr(_conf, "VISION_HOST", None) or host
        port = int(getattr(_conf, "VISION_PORT", None) or port)
    except Exception:
        pass
    return host, port


class SeeError(Exception):
    """业务异常：消息是**给人/模型看的人话**，会进返回体的 error 字段。"""


def _brief(exc) -> str:
    """异常信息截断：云端报错原文很长时不要刷屏（也不要把 key 之类带到对话里）。"""
    text = str(exc).strip() or exc.__class__.__name__
    return text[:200]


# ---------------------------------------------------------------------------
# 图片编码计划
# ---------------------------------------------------------------------------
def _pick_max_width(width: int, target: int) -> int:
    """算出应传给 nv12_to_jpeg 的 max_width，使输出宽度 ≤ target。

    为什么要绕这一下：`nv12_to_jpeg` 的 max_width 语义是"**再除一次 2 还不小于它**
    就继续除"，所以直接传目标宽度会得到反直觉结果 —— 1920 传 1024 时 1920//2=960
    < 1024，一步都不缩，仍旧输出 1920。正确做法是传 `width // step`（step = 让
    width/step ≤ target 的最小 2 的幂），编码器内部的除法次数才会正好等于 step。

    实测三例：1920→960、1280→640、640→640（不缩）。
    """
    step = 1
    while width // step > target and step < 16:
        step *= 2
    return width // step


def _scaled_dims(width: int, height: int, target: int) -> tuple[int, int]:
    """按 _pick_max_width 的同一口径，算出编码后的实际宽高（编码器会再裁到 8 的倍数）。"""
    step = 1
    while width // step > target and step < 16:
        step *= 2
    ow, oh = width // step, height // step
    return ow - ow % 8, oh - oh % 8


# ---------------------------------------------------------------------------
# 取图一：摄像头当前帧
# ---------------------------------------------------------------------------
def _new_camera_client():
    """构造 CameraClient（独立函数便于单测替换）。超时收紧：取不到帧要快速失败。"""
    host, port = vision_target()
    return CameraClient(host, port, connect_timeout=CONNECT_TIMEOUT,
                        io_timeout=GRAB_TIMEOUT, wait_timeout=GRAB_TIMEOUT)


def grab_camera_jpeg(channel: int = None) -> tuple[bytes, int, int]:
    """取摄像头**当前帧**并编成 JPEG，返回 (jpeg_bytes, width, height)。

    失败一律抛 SeeError（消息人话化），由 see_what 统一转成 ok:false。
    """
    if CameraClient is None or nv12_to_jpeg is None:
        raise SeeError("vision 子系统不可用（" + "; ".join(_MISSING) + "）")
    ch = CHANNEL if channel is None else channel
    host, port = vision_target()
    try:
        with _new_camera_client() as cam:
            frame = cam.get_frame(channel=ch)
    except CameraNotRunning as e:
        raise SeeError("摄像头服务未运行（%s:%s）：%s。启动：python3 -m vision.camera_server"
                       % (host, port, _brief(e)))
    except CameraTimeout as e:
        raise SeeError("摄像头取帧超时（%ss）：%s" % (GRAB_TIMEOUT, _brief(e)))
    except (CameraServerError, OSError) as e:
        raise SeeError("摄像头取帧失败：%s" % _brief(e))

    data, width, height = getattr(frame, "data", None), frame.width, frame.height
    if not data:
        raise SeeError("摄像头返回了空帧")
    try:
        jpg = nv12_to_jpeg(data, width, height, quality=QUALITY,
                           max_width=_pick_max_width(width, MAX_WIDTH))
    except Exception as e:
        raise SeeError("摄像头 JPEG 编码失败：%s" % _brief(e))
    if len(jpg) > MAX_IMAGE_BYTES:
        raise SeeError("摄像头图片太大（%.1fMB > %.1fMB）"
                       % (len(jpg) / 1048576, MAX_IMAGE_BYTES / 1048576))
    ow, oh = _scaled_dims(width, height, MAX_WIDTH)
    return jpg, ow, oh


# ---------------------------------------------------------------------------
# 取图二：本机图片文件（限白名单目录）
# ---------------------------------------------------------------------------
def allowed_dirs() -> list[Path]:
    """允许读取的图片目录（已 resolve；不存在的目录也保留，报错时便于说明）。"""
    out = []
    for d in _ALLOWED_DIRS:
        try:
            out.append(d.resolve())
        except Exception:
            out.append(d)
    return out


def load_image_file(path: str) -> tuple[bytes, str, Path]:
    """读一张本机图片，返回 (bytes, mime, 解析后的路径)。

    三层校验（缺一不可）：① resolve 后必须落在允许目录内（挡 `..` 与绝对路径越界）；
    ② 是普通文件且不超过 VISION_SEE_MAX_BYTES；③ 魔数必须是 JPEG、PNG 或 WebP
    （不靠扩展名）。
    """
    raw = (path or "").strip()
    if not raw:
        raise SeeError("图片路径为空")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = REPO_ROOT / p                      # 相对路径按仓库根解析，便于人工用短路径
    try:
        p = p.resolve()
    except Exception as e:
        raise SeeError("图片路径无法解析：%s" % _brief(e))

    roots = allowed_dirs()
    if not any(_is_within(p, r) for r in roots):
        raise SeeError("图片路径不在允许目录内（只允许读：%s）"
                       % ", ".join(str(r) for r in roots))
    if not p.is_file():
        raise SeeError("图片不存在或不是普通文件：%s" % p)
    size = p.stat().st_size
    if size <= 0:
        raise SeeError("图片是空文件：%s" % p)
    if size > MAX_IMAGE_BYTES:
        raise SeeError("图片太大（%.1fMB > %.1fMB）：%s"
                       % (size / 1048576, MAX_IMAGE_BYTES / 1048576, p))

    data = p.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise SeeError("图片太大（%.1fMB > %.1fMB）：%s"
                       % (len(data) / 1048576, MAX_IMAGE_BYTES / 1048576, p))
    mime = None
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        for magic, m in _SUPPORTED_MIME.items():
            if data.startswith(magic):
                mime = m
                break
    if mime is None:
        raise SeeError("只支持 JPEG/PNG/WEBP 图片（按文件内容判断，不看扩展名）")
    return data, mime, p


def _is_within(path: Path, root: Path) -> bool:
    """path 是否在 root 之内（含相等）。不靠字符串前缀，避免 `inbox2/` 被 `inbox` 放过。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 调云端视觉大模型
# ---------------------------------------------------------------------------
_client = None


def _ensure_client():
    """懒加载 OpenAI 客户端（指向阿里百炼 OpenAI 兼容端点）。不可用时抛 SeeError。"""
    global _client
    if _client is not None:
        return _client
    if OpenAI is None:
        raise SeeError("缺少 openai 依赖（" + "; ".join(_MISSING) + "）")
    key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        raise SeeError("缺少 DASHSCOPE_API_KEY（放仓库根 .env）")
    try:
        _client = OpenAI(api_key=key, base_url=BASE_URL, max_retries=0)
    except Exception as e:
        raise SeeError("云端客户端构造失败（%s）" % e.__class__.__name__)
    return _client


def _extract_text(resp) -> str:
    """从 chat.completions 响应里取文本（content 可能是 str，也可能是分块列表）。"""
    try:
        msg = resp.choices[0].message
    except Exception:
        raise SeeError("云端返回结构异常（没有 choices）")
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        parts = []
        for part in content:
            if isinstance(part, dict):
                part_text = part.get("text")
            else:
                part_text = getattr(part, "text", None)
            if isinstance(part_text, str):
                parts.append(part_text)
        return "".join(parts).strip()
    return ""


def ask_qwen(prompt: str, image_bytes: bytes, mime: str = "image/jpeg") -> str:
    """把（图 + 文字）发给视觉模型，返回回答文本。失败抛 SeeError。"""
    client = _ensure_client()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            # 阿里百炼兼容模式接受 data URL（base64 内联图），无需先上传拿 URL
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
        ],
    }]
    try:
        resp = client.chat.completions.create(model=MODEL, messages=messages,
                                             timeout=TIMEOUT, max_tokens=MAX_TOKENS)
    except Exception as e:
        raise SeeError("云端视觉模型调用失败（%s）" % e.__class__.__name__)
    text = _extract_text(resp)
    if not text:
        raise SeeError("云端视觉模型返回了空回答")
    return text


# ---------------------------------------------------------------------------
# 编排入口（MCP 工具直接调它）
# ---------------------------------------------------------------------------
def see_what(image: str, prompt: str, channel: int = 1) -> dict:
    """看图回答问题：图来自摄像头当前帧或指定图片文件。

    ``image`` 必须是 ``camera``（大小写不敏感，允许首尾空白）或白名单内的
    本机图片路径。摄像头通道由调用方显式指定，范围为 1..255。

    返回统一结构（**永不抛异常**）：
      成功 {"ok": True,  "answer": str, "model": str, "image": {...}, "ms": int}
      失败 {"ok": False, "error": str}
    """
    t0 = time.monotonic()
    image_info = {}
    try:
        image = image.strip() if isinstance(image, str) else ""
        if not image:
            raise SeeError("image（camera 或图片路径）不能为空")
        if not isinstance(prompt, str):
            raise SeeError("prompt 必须是字符串")
        if not prompt.strip():
            raise SeeError("prompt（要看什么/要问什么）不能为空")
        is_camera = image.casefold() == "camera"
        if is_camera and (isinstance(channel, bool) or not isinstance(channel, int)
                          or not 1 <= channel <= 255):
            raise SeeError("channel 必须是 1..255 的整数")
        if OpenAI is None:
            raise SeeError("缺少 openai 依赖（" + "; ".join(_MISSING) + "）")

        if is_camera:
            # 注意：grab_camera_jpeg 返回 (jpeg, 宽, 高) 三元组，mime 恒为 jpeg
            data, w, h = grab_camera_jpeg(channel)
            mime = "image/jpeg"
            image_info = {"source": "camera", "channel": channel, "mime": mime,
                          "width": w, "height": h, "bytes": len(data)}
        else:
            data, mime, p = load_image_file(image)
            image_info = {"source": "file", "path": str(p), "mime": mime,
                          "bytes": len(data)}

        answer = ask_qwen(prompt, data, mime)
        out = {"ok": True, "answer": answer, "model": MODEL,
               "image": image_info, "ms": int((time.monotonic() - t0) * 1000)}
        _audit(action="ok", model=MODEL, ms=out["ms"],
               source=image_info.get("source"), bytes=image_info.get("bytes"))
        return out
    except SeeError as e:
        _audit(action="error", model=MODEL,
               ms=int((time.monotonic() - t0) * 1000),
               source=image_info.get("source"), error=str(e))
        return {"ok": False, "error": str(e)}
    except Exception as e:                      # 兜底：绝不让异常穿到对话循环
        _audit(action="error", model=MODEL,
               ms=int((time.monotonic() - t0) * 1000), error=_brief(e))
        return {"ok": False, "error": "识图失败：%s" % _brief(e)}


def _audit(**fields):
    """落审计（事件 vision_see）。**只记元信息，不落图片本体、不落 base64。**

    审计是"可选增强"：log 模块不可用/写盘失败一律吞掉，不能因为审计挂掉识图。
    """
    try:
        from LLM.core import log as audit
        audit.log("vision_see", **fields)
    except Exception:
        pass
