# -*- coding: utf-8 -*-
r"""MCP 客户端桥（可选能力）：把外部 MCP 服务器工具并入 OpenAI function-calling 工具循环。

【本模块的职责】
  养老陪护机器人后端（FastAPI）需要调用外部 MCP 服务器提供的工具（天气查询、新闻
  获取、RSS 抓取等）。MCP 服务器是独立子进程、通过 stdio 与客户端通信。本模块充当
  "客户端桥"，承担两件事：
    - 启动阶段 start()：拉起 conf.MCP_SERVERS 中 enabled 的服务器子进程，握手、
      list_tools，把每个 MCP 工具转换成 OpenAI function-calling schema 注册进 _tools；
    - 运行阶段 call_tool()：被 tools.py 的 run_tool 分发调用，把工具调用投递到 MCP
      子进程并同步等待返回，返回值格式与本地工具一致。

【线程模型（为什么需要"后台线程 + 专属事件循环"）】
  MCP 官方 SDK 是基于 asyncio 的，而 LLM 后端的工具执行路径（chat.py → tools.py →
  run_tool）是同步代码（跑在 FastAPI 的线程池里），无法直接 await。解决办法：
    - 开一个独立后台守护线程 + 专属 asyncio 事件循环，MCP stdio 会话常驻该线程；
    - 所有异步操作（连接、list_tools、call_tool）经 asyncio.run_coroutine_threadsafe()
      投递进该循环，再用 fut.result(timeout=...) 同步等待结果。
  效果：调用方（同步的 run_tool）无感，像调普通函数；MCP 子进程与主事件循环互不阻塞。

【降级策略（遵循 AGENTS.md「系统稳健性」：可选能力必须降级运行）】
  - mcp SDK 依赖缺失 → _MCP_AVAILABLE=False，start() 记审计后直接返回 None；
  - 总开关 mcp_enabled 关闭 / conf.MCP_SERVERS 无启用服务器 → 同上，不拉起任何进程；
  - 单台服务器连接失败 → 只记入 _errors，其余服务器照常连接；
  - 上述情况一律"只记录 + 置标志，绝不崩主程序"，对话照常（只是没有 MCP 工具）。

【扩展指引】
  新增 MCP 服务器：只需在 conf.py 的 MCP_SERVERS 里加一条配置即可，本模块无需改动。
"""
import asyncio       # 跨线程桥接：run_coroutine_threadsafe / wait_for / new_event_loop / run_forever
import threading     # 后台守护线程，承载 MCP 专属 asyncio 事件循环

from . import log as audit                 # 审计日志：所有状态变更落 JSONL（事件类型 mcp / mcp_degraded）
from .conf import (
    MCP_SERVERS,        # dict[str, dict]：服务器名 -> {"command","args","env","enabled"}，MCP 服务器清单
    MCP_TOOL_TIMEOUT,   # int：单次工具调用超时（秒），防 MCP 子进程卡死拖死对话
    MCP_CONNECT_TIMEOUT # int：单台服务器连接/握手超时（秒），防坏配置阻塞启动
)

# ---------------------------------------------------------------------------
# 可选能力降级：外部依赖逐个尝试引入，收集缺失项。
#  - _MCP_AVAILABLE = False 时，mcp 相关名字（ClientSession 等）在 try 块外不可用，
#    所有对外函数必须先判断 _MCP_AVAILABLE 再使用，否则 NameError；
#  - _MISSING_DEPS 逐个收集缺失原因（缺多个时别只报第一个），供审计/健康接口展示。
# 这是 AGENTS.md「系统稳健性」要求的统一降级模式，与 LLM/voice_api.py 保持一致。
# ---------------------------------------------------------------------------
_MCP_AVAILABLE = True       # 依赖层可用性标志：True = mcp SDK 可 import
_MISSING_DEPS = []          # 缺失依赖原因列表（每个异常一条），为空 = 依赖齐全

try:
    # 仅把真正需要的名字引入模块命名空间；import 失败（没装 / 版本不兼容）都算缺失。
    from mcp import ClientSession, StdioServerParameters   # 会话对象 + stdio 服务器参数
    from mcp.client.stdio import stdio_client              # stdio 客户端上下文管理器
except Exception as _exc:   # 注意：用 Exception 而非 ImportError，版本不兼容（初始化阶段抛错）也覆盖
    _MCP_AVAILABLE = False
    _MISSING_DEPS.append(str(_exc))   # 记录真实异常信息，便于排查为何不可用

# ---------------------------------------------------------------------------
# 运行时状态（模块级单例，跨线程共享）
# 线程安全说明：_loop / _thread / _started 只在 start()/stop()（lifespan 单线程阶段）写入；
# _sessions / _tools / _errors 的写入都在后台事件循环里进行，读取（call_tool / status）
# 在其它线程。读取方看到的是"最近一次写"的结果，对本模块的用法（启动完才调用工具、
# 连接失败只置错误不删条目）而言是安全的，无需额外加锁。
# ---------------------------------------------------------------------------
_loop = None                 # 后台事件循环（asyncio.AbstractEventLoop），承载所有 MCP 异步操作
_thread = None               # 后台线程对象（daemon=True），run_forever 跑事件循环
_sessions: dict[str, tuple] = {}   # 服务器名 -> (stdio_client 上下文 ctx, ClientSession)，已连接会话表
_tools: dict[str, dict] = {}       # 工具名 -> {"server": 所属服务器名, "schema": OpenAI function schema}
_errors: dict[str, str] = {}       # 服务器名 -> 连接失败原因（字符串），供 status() 展示
_started = False                   # 是否至少有一台服务器成功连接并注册了工具


def available() -> bool:
    """mcp SDK 是否可用（依赖层）。

    仅反映"依赖能不能 import"，与服务器连接成功与否无关。
    调用方可用它判断是否值得调用 start() / call_tool()。
    """
    return _MCP_AVAILABLE


def is_started() -> bool:
    """是否有 MCP 服务器已成功连接并注册了工具。

    等价于"本次运行有没有 MCP 工具可用"。start() 结束后（无论成败）
    都会刷新该标志；stop() 会清零。
    """
    return _started


def tools() -> dict[str, dict]:
    """已注册的 MCP 工具表：工具名 -> {"server": 服务器名, "schema": OpenAI schema}。

    返回的是内部 dict 的引用，调用方应只读；修改请通过连接/断开流程。
    """
    return _tools


def schemas() -> list[dict]:
    """全部 MCP 工具的 OpenAI function-calling schema 列表。

    供 chat 引擎（build_system / 工具循环）合并进 tools 参数，
    让大模型"看到"这些外部工具并决定是否调用。
    """
    return [e["schema"] for e in _tools.values()]


def status() -> dict:
    """汇总 MCP 模块运行状态，供 /api/health 与审计日志使用。

    返回结构：
      - available   : 依赖层是否可用（bool）
      - missing_deps: 缺失依赖原因列表（list[str]）
      - started     : 是否有工具已注册（bool）
      - servers     : 每个服务器当前状态（"connected" / "error"）
      - errors      : 连接失败原因（dict[str, str]）
      - tools       : 已注册工具名（排序后的 list[str]）
    """
    return {
        "available": _MCP_AVAILABLE,                 # 依赖层可用性
        "missing_deps": list(_MISSING_DEPS),         # 缺失依赖原因（拷一份，避免外部改到内部表）
        "started": _started,                         # 是否已有可用工具
        # 遍历 配置里的服务器 ∪ 报错过的服务器：在配置里且没报错 = connected，否则 error。
        # 用并集是为了把"连上了但后来被移除配置"的残留也如实展示出来。
        "servers": {n: ("error" if n in _errors else "connected")
                    for n in (set(MCP_SERVERS) | set(_errors))},
        "errors": dict(_errors),                     # 各服务器连接失败原因
        "tools": sorted(_tools),                     # 已注册工具名（排序便于阅读）
    }


# ---------------------------------------------------------------------------
# 生命周期：start() / stop() 由 server.py 的 lifespan 钩子调用。
#   lifespan 启动 → ... → mcp_client.start(settings)，注册的 schema 并入工具循环；
#   lifespan 退出 → mcp_client.stop()，关闭全部子进程并停线程。
# ---------------------------------------------------------------------------
def _loop_runner():
    """后台线程入口：把本线程的 asyncio 事件循环设为 _loop 并永久运行。

    只干两件事：
      1. asyncio.set_event_loop(_loop) —— 让"本线程内"的 asyncio 默认循环就是 _loop；
      2. _loop.run_forever() —— 阻塞运行，直到外部调 stop() 触发 _loop.stop()。
    该函数在独立线程（target=_loop_runner）中执行，不会阻塞主程序。
    """
    asyncio.set_event_loop(_loop)   # 绑定专属事件循环到当前（后台）线程
    _loop.run_forever()             # 永久阻塞运行循环，直到被 stop() 叫停


async def _connect_one(name: str, params: dict):
    """拉起一台 stdio MCP 服务器：建子进程 → 握手 → 取工具清单 → 注册 schema。

    注意：本函数在后台事件循环里执行（由 _connect_all → run_coroutine_threadsafe 调度），
    调用方通过 fut.result() 同步等待其完成或抛异常。

    参数：
      name   : 服务器名（conf.MCP_SERVERS 的键），用于索引 _sessions/_errors/审计
      params : 该服务器的连接参数 {"command","args","env",...}，来自 conf.MCP_SERVERS[name]

    副作用：
      成功 → 在 _sessions 记录会话、在 _tools 注册全部工具；
      失败 → 抛出异常（由 _connect_all 捕获并记入 _errors，不会影响其它服务器）。
    """
    # 1) 组装 stdio 子进程启动参数：可执行命令 + 命令行参数 + 可选环境变量。
    server_params = StdioServerParameters(
        command=params["command"],              # 启动 MCP 服务器的可执行程序
        args=params.get("args") or [],          # 传给子进程的参数（可缺省，缺省给空表）
        env=params.get("env"),                  # 可选的子进程环境变量覆盖
    )
    # 2) 进入 stdio 客户端上下文：真正拉起子进程，拿到它的 stdout/stdin 读写流。
    #    注意：这里是手动调用 __aenter__()（而非 async with），因为返回的 ctx 需要
    #    存进 _sessions，供 stop() 时 __aexit__ 优雅关闭。
    ctx = stdio_client(server_params)
    read_stream, write_stream = await ctx.__aenter__()
    # 3) 用读写流创建会话并建立连接。
    session = ClientSession(read_stream, write_stream)
    await session.__aenter__()                  # 打开会话（内部建立 JSON-RPC 通道）
    await session.initialize()                  # 与服务器握手（能力协商、协议版本对齐）
    # 4) 拉取服务器支持的工具清单（MCP 协议：tools/list）。
    listed = await session.list_tools()
    # 5) 把每个 MCP 工具转成 OpenAI function-calling 的 schema，注册进全局 _tools。
    for t in listed.tools:
        # mcp SDK 不同版本字段名不统一：新版本用 input_schema（snake_case），
        # 旧版本用 inputSchema（camelCase），两个都兼容。
        input_schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
        _tools[t.name] = {
            "server": name,                     # 记录所属服务器，call_tool 时据此找到会话
            "schema": {                         # OpenAI function-calling 格式的完整工具描述
                "type": "function",
                "function": {
                    "name": t.name,             # 工具名（即大模型/run_tool 用到的名字）
                    "description": t.description or "",  # 描述（可缺省，缺省给空串）
                    "parameters": input_schema or {"type": "object", "properties": {}},
                    # ↑ 参数 JSON Schema；拿不到就退回"空对象"占位，保证结构合法
                },
            },
        }
    # 6) 记录已连接会话 + 审计 + 控制台提示，供调试和状态查询。
    _sessions[name] = (ctx, session)
    audit.log("mcp", action="connected", server=name, tools=len(listed.tools))
    print(f"[MCP] 服务器 {name} 已连接，注册 {len(listed.tools)} 个工具: "
          f"{[t.name for t in listed.tools]}")


async def _connect_all(enabled: dict):
    """逐个连接所有启用的 MCP 服务器（后台事件循环里执行）。

    逐台 try/except：单台失败只记入 _errors 并打印告警，不影响其它服务器连接。
    """
    for name, params in enabled.items():
        try:
            # 给单台服务器加超时兜底：配置错误/子进程卡死时快速失败，不阻塞启动。
            await asyncio.wait_for(_connect_one(name, params), timeout=MCP_CONNECT_TIMEOUT)
        except Exception as e:
            _errors[name] = str(e)              # 记录失败原因，status() 可查
            audit.log("mcp", action="connect_error", server=name, error=str(e))
            print(f"[WARN] MCP 服务器 {name} 连接失败: {e}")
            # 不 re-raise：其余服务器继续尝试，单台失败不拖垮整体。


def start(settings: dict):
    """拉起所有启用的 MCP 服务器；任何失败都只记录不抛异常。

    这是 lifespan 启动阶段调用的入口。逐级降级检查（依赖 → 开关 → 配置 → 连接），
    每一步失败都"记审计 + 打印 WARN + 返回 None"，保证后端照常启动。

    参数：
      settings : 来自 DB 的运行时设置 dict，至少含 mcp_enabled 总开关

    返回：
      - 成功：全部 MCP 工具的 OpenAI schema 列表（供工具循环合并）；
      - 任何降级/失败情况：None。
    """
    global _loop, _thread, _started
    # —— 降级检查 1：mcp SDK 依赖不可用（没装 / 版本不兼容）——
    if not _MCP_AVAILABLE:
        reason = "缺少依赖：" + "; ".join(_MISSING_DEPS)   # 缺多个时全列出
        audit.log("mcp_degraded", reason=reason)
        print("[WARN] MCP 不可用（缺少依赖：" + "; ".join(_MISSING_DEPS) + "）")
        return None
    # —— 降级检查 2：总开关关闭（用户在设置里关掉了 MCP）——
    if not settings.get("mcp_enabled"):
        audit.log("mcp_degraded", reason="总开关 mcp_enabled 关闭")
        return None
    # —— 过滤出"启用"的服务器：conf.MCP_SERVERS 里 enabled 缺省视为启用 ——
    enabled = {n: p for n, p in MCP_SERVERS.items() if p.get("enabled", True)} # 过滤掉未启用的服务器
    # —— 降级检查 3：没有任何启用的服务器配置 ——
    if not enabled:
        audit.log("mcp_degraded", reason="conf.MCP_SERVERS 未配置启用的服务器")
        return None

    # 1) 创建专属事件循环 + 后台守护线程，让 MCP 异步操作与主程序隔离运行。
    _loop = asyncio.new_event_loop()                                   # 新建一个独立事件循环
    _thread = threading.Thread(target=_loop_runner, name="mcp-client", daemon=True)
    _thread.start()                                                    # 线程内 run_forever
    # 2) 把"连接全部服务器"的协程投递进后台循环，并同步阻塞等待完成。
    #    fut.result(timeout=...) 同时承担"总超时兜底"：即便某台服务器被 wait_for
    #    放过后仍僵住，这里也能兜住整体启动时间。
    fut = asyncio.run_coroutine_threadsafe(_connect_all(enabled), _loop)
    try:
        # 总超时 = 单台超时 × 服务器数（各台串行） + 15s 余量，给启动留足时间。
        fut.result(timeout=MCP_CONNECT_TIMEOUT * max(1, len(enabled)) + 15)
    except Exception as e:
        audit.log("mcp_degraded", reason=f"连接阶段异常: {e}")
        print(f"[WARN] MCP 连接阶段异常: {e}")
    # 3) 收尾：只要注册到 ≥1 个工具就算"启动成功"；一台都没有则整体降级提示。
    _started = bool(_tools)
    if not _started:
        audit.log("mcp_degraded", reason="所有 MCP 服务器连接失败")
        print("[WARN] MCP 所有服务器连接失败，本次运行无 MCP 工具")
    return schemas()


async def _close_all():
    """关闭全部已连接的 MCP 会话（后台事件循环里执行）。

    逐个调用 session/ctx 的 __aexit__ 优雅关闭：先关会话（结束 JSON-RPC 通道），
    再关 stdio 上下文（终止子进程）。单台关闭失败只吞掉异常，继续关剩下的。
    """
    for name, (ctx, session) in list(_sessions.items()):
        # 用 list() 拷贝遍历：关闭过程不修改 _sessions，此处拷贝仅为保险。
        try:
            await session.__aexit__(None, None, None)   # 关闭会话通道
            await ctx.__aexit__(None, None, None)       # 关闭 stdio 子进程
        except Exception:
            pass        # 关闭失败不致命，直接跳过；子进程是 daemon，进程退出也会被回收
    _sessions.clear()   # 清空会话表


def stop():
    """关闭全部 MCP 会话并停掉后台线程（幂等，lifespan 退出时调用）。

    分三个阶段，每段都有 try/except 兜底，保证任何异常都不阻止收尾：
      1. 优雅关闭所有会话（投递 _close_all 到后台循环并同步等待）；
      2. 通知事件循环停止（call_soon_threadsafe(_loop.stop)）；
      3. 等待后台线程退出（join）并清空全部运行时状态。
    幂等性：无论 start() 是否真正拉起过线程，调用多次/在异常路径调用都安全。
    """
    global _loop, _thread, _started, _tools, _errors
    # 阶段 1：有事件循环且有会话 → 投递关闭协程，最多等 10s。
    if _loop is not None and _sessions:
        try:
            fut = asyncio.run_coroutine_threadsafe(_close_all(), _loop)
            fut.result(timeout=10)   # 若子进程迟迟不退出，10s 后放弃（不阻塞进程退出）
        except Exception:
            pass
    # 阶段 2：通知后台事件循环退出 run_forever。
    #   call_soon_threadsafe 是跨线程安全地往循环里塞"停止"回调的标准做法。
    if _loop is not None:
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except Exception:
            pass
    # 阶段 3：等后台线程真正结束，再复位全部状态，避免僵尸线程/残留引用。
    if _thread is not None:
        _thread.join(timeout=10)
    _loop = _thread = None
    _sessions.clear()
    _tools.clear()
    _errors.clear()
    _started = False


# ---------------------------------------------------------------------------
# 工具调用（同步桥）：供 tools.py 的 run_tool 分发，是外部唯一入口。
# 全程同步（阻塞等待），但实际执行在后台事件循环，调用方线程不被锁死。
# ---------------------------------------------------------------------------
async def _call(session, name: str, args: dict) -> dict:
    """在后台事件循环里真正执行一次 MCP 工具调用，并把结果整理成统一格式。

    参数：
      session : 目标服务器的 ClientSession（从 _sessions 里取）
      name    : 工具名
      args    : 工具参数 dict（可空）

    返回：
      {"ok": True,  "result": 文本结果}  —— 正常返回，result 截断到 4000 字符
      {"ok": False, "message": 错误文本}  —— MCP 服务器侧报了 isError
    """
    # 调用 MCP 协议方法 tools/call，传入参数（空 dict 兜底）。
    result = await session.call_tool(name, arguments=args or {})
    # MCP 返回内容是一组 ContentBlock，这里只取文本类块（getattr 兼容无 text 的块）。
    parts = [getattr(c, "text", None) for c in result.content]
    # 拼接所有文本块；全空则给占位提示，避免返回空串让上层误判。
    body = "\n".join(p for p in parts if p).strip() or "（MCP 工具无文本返回）"
    # 服务器显式标了 isError → 按"工具执行失败"处理（消息截断到 1000 字符防刷屏）。
    if getattr(result, "isError", False):
        return {"ok": False, "message": body[:1000]}
    # 正常结果截断到 4000 字符，防止超长结果撑爆对话上下文。
    return {"ok": True, "result": body[:4000]}


def call_tool(name: str, args: dict) -> dict:
    """线程安全地同步调用 MCP 工具，返回与本地工具一致的 {"ok", "result"|"message"}。

    这是 tools.py::run_tool 分发 MCP 工具的入口。全程同步等待，但任何失败（未知工具、
    未启动、服务器未连接、超时、异常）都返回 {"ok": False, "message": ...}，绝不抛异常，
    保证上层对话循环稳定。

    参数：
      name : 工具名（必须是 _tools 里已注册的名字）
      args : 工具参数 dict

    返回：
      {"ok": True,  "result": str} —— 成功；
      {"ok": False, "message": str} —— 失败及原因。
    """
    # 1) 查工具表：名字没注册说明大模型请求了不存在的工具。
    entry = _tools.get(name)
    if entry is None:
        return {"ok": False, "message": f"未知 MCP 工具 {name}"}
    # 2) 事件循环必须已在运行（start() 成功后才可能走到这）。
    if _loop is None:
        return {"ok": False, "message": "MCP 后台循环未启动"}
    # 3) 按工具所属服务器取会话：连接失败/被关掉的服务器取不到 → 明确报错。
    pair = _sessions.get(entry["server"])
    if pair is None:
        return {"ok": False, "message": f"MCP 服务器 {entry['server']} 未连接"}
    # 4) 把异步 _call 投递进后台循环，再同步阻塞等结果（真正的"桥接"）。
    fut = asyncio.run_coroutine_threadsafe(_call(pair[1], name, args or {}), _loop)
    try:
        return fut.result(timeout=MCP_TOOL_TIMEOUT)   # 等结果，超时由异常分支兜底
    except asyncio.TimeoutError:
        # 子进程卡死/响应慢：按超时失败返回，让对话继续而非挂死。
        return {"ok": False, "message": f"MCP 工具 {name} 调用超时（>{MCP_TOOL_TIMEOUT}s）"}
    except Exception as e:
        # 其它任何异常（网络断开、协议错误等）统一转成友好失败信息。
        return {"ok": False, "message": f"MCP 工具 {name} 调用失败: {e}"}
