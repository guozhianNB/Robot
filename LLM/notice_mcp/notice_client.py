# -*- coding: utf-8 -*-
r"""护士后台传达业务核心（不含 MCP）：把一条"要传达给护士的话"投递进后端通知中心。

【职责边界】
  本模块只管"业务"，不碰 MCP 协议、不碰 stdio：
    - push()          ：归一化 → POST 后端投递口 → 返回统一 dict（**永不抛异常**）
    - backend_url()   ：当前后端地址（调试用）
    - endpoint()      ：完整投递 URL（调试用）
  MCP 注册在 notice_server.py 里 —— 业务层因此可以脱离 MCP 框架直接单测。

【落在哪条通道上（重要）】
  投递口 = `POST {NOTICE_BACKEND_URL}/api/notifications`（规格
  docs/superpowers/specs/2026-09-18-nurse-console-design.md D4：**有意免鉴权**，任何模块/
  小车都能上报）。**刻意不直连 SQLite、也不 import LLM.agent.notify**：
    - 直写库会绕过 `notify.ingest()` 的去重合并 / 审计 / `bus.publish` 广播 —— 护士台就
      收不到实时事件（只能等 30s 轮询兜底），且违反"notify 是唯一写入口"；
    - 直调 notify 也广播不动：SSE 总线是**后端进程内**的，子进程 publish 没有订阅者。
  走 HTTP 投递口反而是唯一既不丢实时性、又不新开写入口的路。

【依赖】
  只用标准库（urllib）——AGENTS.md：能用 stdlib 就绝不引外部依赖；也因此本模块可以整包
  拷到另一台机器（如小车）上，只改 NOTICE_BACKEND_URL 就能用。

【降级】
  连不上 / 超时 / 非 2xx / 响应不是 JSON —— 一律 `{"ok": False, "error": "人话原因"}`，
  绝不抛异常、绝不阻塞调用方（超时默认 5s，远小于 mcp_client 的 30s 工具超时）。

【环境变量】
  NOTICE_BACKEND_URL  后端根地址，默认 http://127.0.0.1:8000（跨机部署改这一个即可）
  NOTICE_TIMEOUT_S    单次投递超时秒数，默认 5
"""
import json
import os
import socket
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 运行参数（环境变量优先；不 import LLM.conf —— 本模块保持可独立部署）
# ---------------------------------------------------------------------------
_DEFAULT_BACKEND = "http://127.0.0.1:8000"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except Exception:                     # 配错环境变量不许炸掉整个工具
        return default


def _resolve_backend(raw) -> str:
    """把环境变量里的地址归一成"无尾斜杠、绝不为空"的后端根地址。"""
    return ((raw or "").strip() or _DEFAULT_BACKEND).rstrip("/") or _DEFAULT_BACKEND


BACKEND_URL = _resolve_backend(os.environ.get("NOTICE_BACKEND_URL"))
TIMEOUT = _env_float("NOTICE_TIMEOUT_S", 5.0)

LEVELS = ("info", "warning", "critical")
DEFAULT_LEVEL = "info"
# 级别容忍表：模型不保证吐标准词。**只降不升是危险的**（把"跌倒"写成 Critical/high 就成了最低档、
# 护士台不置顶不蜂鸣），故常见近义词往上归：unknown → info，medium → warning，high/severe → critical。
_LEVEL_ALIASES = {
    "info": "info", "information": "info", "informational": "info", "low": "info",
    "normal": "info", "notice": "info", "verbose": "info",
    "warning": "warning", "warn": "warning", "medium": "warning",
    "moderate": "warning", "caution": "warning",
    "critical": "critical", "crit": "critical", "high": "critical",
    "severe": "critical", "emergency": "critical", "urgent": "critical", "fatal": "critical",
}
SOURCE = "cart"                # 与护士台「小车」筛选页签一致（frontend/packages/nurse/src/App.vue）
NOTICE_TYPE = "message"        # 缺省标题 = notify.TITLES["message"] =「通知」
MESSAGE_MAX = 500              # 与 conf.NOTIFY_BODY_MAX 同值（服务端还会再截一次，两层不冲突）


def normalize_level(level) -> str:
    """把模型给的 level 归一到三档（大小写/空白不敏感，常见近义词按上表映射，其余 → info）。"""
    key = str(level or "").strip().lower()
    return _LEVEL_ALIASES.get(key, DEFAULT_LEVEL)


class _BackendError(Exception):
    """内部异常：只在 _post 内使用，push() 会把它转成 ok:false 的返回值。"""


def backend_url() -> str:
    """当前后端地址（调试/自检用，**不联网**）。"""
    return BACKEND_URL


def endpoint() -> str:
    """投递口的完整 URL。"""
    return BACKEND_URL + "/api/notifications"


def _post(payload: dict) -> dict:
    """POST 到后端投递口；成功返回后端 JSON，失败抛 `_BackendError`（人话原因）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint(), data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:          # 先于 URLError：HTTPError 是它的子类
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            detail = str(body.get("detail") or body.get("error") or "")
        except Exception:
            detail = ""
        finally:
            e.close()
        raise _BackendError(f"护士后台拒绝了这条通知（HTTP {e.code}）"
                            + (f"：{detail}" if detail else ""))
    except urllib.error.URLError as e:           # 连不上 / 超时（urllib 会把 OSError 包进来）
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            # urllib 把超时包成 URLError(reason=timeout)：这里拆开，好让错误话术说清是"慢"
            # 而不是"连不上"（两者的排查动作完全不同）。
            raise _BackendError(f"护士后台响应超时（>{TIMEOUT:g}s）")
        raise _BackendError(f"连不上护士后台（{BACKEND_URL}）：{reason}")
    except (socket.timeout, TimeoutError):       # 兜底：某些版本裸抛超时
        raise _BackendError(f"护士后台响应超时（>{TIMEOUT:g}s）")
    except Exception as e:                       # 理论上到不了这，但绝不许穿透
        # 带上异常文本（截断）：只留类名的话，线上只会看到"投递失败：ValueError"，
        # 无从判断是编码、超时还是别的（子进程侧还会把它记进 notice_mcp.log，见 notice_server）。
        raise _BackendError(f"投递失败（{e.__class__.__name__}）：{str(e)[:120]}")

    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise _BackendError("护士后台返回的不是 JSON（后端版本可能对不上）")
    if not isinstance(result, dict):
        raise _BackendError("护士后台返回了意外的响应格式")
    return result


def push(message: str, level: str = DEFAULT_LEVEL, uid: str = "",
         title: str = "", source: str = SOURCE, type: str = NOTICE_TYPE) -> dict:
    """把一条要传达的话推给护士台。返回统一 dict，**永不抛异常**。

    成功：`{"ok": True, "id": 12, "level": "critical", "deduped": False, "detail": "…"}`
          —— `deduped=True` 表示窗口内同一件事已有未处理通知，本次合并计数（护士只看到一条）。
    失败：`{"ok": False, "error": "人话原因"}`（调用方据此**不要**对老人说"已经通知护士了"）。
    """
    # 归一化：模型可能给出"奇怪类型"（数字/数组/None）。这里一律先 str 化再判断，
    # 宁可退回默认值，也绝不因为一个畸形参数把整条工具调用炸成通用异常。
    text = str(message or "").strip()
    if not text:
        return {"ok": False, "error": "要传达的内容不能为空：请把「谁、什么事」用一句话说清"}
    truncated = len(text) > MESSAGE_MAX
    if truncated:
        text = text[:MESSAGE_MAX]

    lvl = normalize_level(level)
    payload = {
        "source": str(source or "").strip() or SOURCE,
        "type": str(type or "").strip() or NOTICE_TYPE,
        "level": lvl,
        "uid": str(uid or "").strip(),
        "title": str(title or "").strip(),
        "message": text,
    }

    try:
        result = _post(payload)
    except _BackendError as e:
        return {"ok": False, "error": str(e)}

    # **只认明确的成功**：后端必须说 `{"ok": true}`。别的形状（代理页、空对象、老版本响应、
    # 只有 id 没有 ok）一律当没投出去 —— 这条工具的输出决定模型会不会对老人说"我已经通知护士了"。
    if result.get("ok") is not True:
        return {"ok": False,
                "error": str(result.get("error") or result.get("detail")
                             or "护士后台没有确认收到这条通知")}

    deduped = bool(result.get("deduped"))
    detail = "已通知护士" + ("（同一件事已合并为一条，护士台只显示一条）" if deduped else "")
    if truncated:
        detail += f"（内容超过 {MESSAGE_MAX} 字，已截断）"
    return {"ok": True, "id": result.get("id"), "level": lvl,
            "deduped": deduped, "detail": detail}
