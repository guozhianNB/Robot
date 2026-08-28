# -*- coding: utf-8 -*-
r"""
工具注册中心 + 分发入口（模块 9）：
  - 本地工具：LLM/tool/ 下的模块用 @tool 装饰器注册（OpenAI function-calling 格式），
    自动加载、自动分发、per-tool 开关自动生效，前端工具页自动显示；
  - MCP 工具：conf.MCP_SERVERS 配置的外部 MCP 服务器，mcp_client 启动时拉起并转 schema，
    与本地工具一样参与对话工具循环（重名时本地优先）。
  - 原 web_search / get_news 本地工具已于 2026-08-29 移除，联网能力改由 MCP 服务器提供。

【新增本地工具三步走】schema 与实现写在一起，装饰器自动注册：
    @tool("工具名", "何时用/怎么用的描述（模型靠它决定调用）", {参数 JSON Schema}, enabled=True)
    def 工具名(参数: str = "默认值") -> dict:
        ...
        return {"ok": True, "result": "..."}   # 或 {"ok": False, "message": "..."}
注册后 run_tool 自动分发、TOOLS 自动收录、per-tool 开关 <工具名>_enabled 自动生效，
前端工具页自动显示开关，无需再改 chat.py / server.py / db.py / conf.py。

【新增 MCP 服务器】只需在 conf.py 的 MCP_SERVERS 加一条配置，无需改任何代码。
"""
import importlib
import inspect
import pkgutil

from . import mcp_client   # MCP 桥（可选能力，内部自行降级，import 永远安全）

# ---------------------------------------------------------------- 注册表
# name -> {"schema": OpenAI function-calling 声明, "fn": 实现函数, "enabled": 默认开关}
_TOOL_REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict, enabled: bool = True):
    """注册一个工具：OpenAI function-calling schema 与实现写在一起，run_tool 自动分发。"""
    def deco(fn):
        _TOOL_REGISTRY[name] = {
            "schema": {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            },
            "fn": fn,
            "enabled": enabled,
        }
        return fn
    return deco


def _run_fn(fn, args: dict):
    """调用工具实现：按函数签名过滤模型传来的参数，缺省交给函数默认值兜底。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):   # 拿不到签名（如 C 函数）→ 直接展开
        return fn(**args)
    allowed = {p for p in sig.parameters if p not in ("self", "cls")}
    kwargs = {k: v for k, v in args.items() if k in allowed}
    return fn(**kwargs)











# ---------------------------------------------------------------- 自动加载工具实现
# 遍历 LLM/tool/ 子包，import 所有模块触发 @tool 装饰器注册（下划线开头为共享辅助，跳过）。
from . import tool as _tool_pkg

for _mod in pkgutil.iter_modules(_tool_pkg.__path__):
    if _mod.name.startswith("_"):
        continue
    importlib.import_module(f"{__package__}.tool.{_mod.name}")


# ---------------------------------------------------------------- 注册表导出
# 注意：必须在全部 @tool 注册之后生成（模块 import 时按顺序执行）
TOOLS = [reg["schema"] for reg in _TOOL_REGISTRY.values()]
TOOL_ENABLED_KEYS = [f"{n}_enabled" for n in _TOOL_REGISTRY]
TOOL_DEFAULTS = {f"{n}_enabled": reg["enabled"] for n, reg in _TOOL_REGISTRY.items()}


def effective_tools(settings: dict) -> list[dict]:
    """按 per-tool 开关（`<工具名>_enabled`）过滤，返回要传给模型的 schema 列表。
    MCP 工具在 `mcp_enabled` 开启时合并进来；与本地工具重名时本地优先。"""
    local_names = set(_TOOL_REGISTRY)
    tools = [reg["schema"] for name, reg in _TOOL_REGISTRY.items()
             if settings.get(f"{name}_enabled", reg["enabled"])]
    if settings.get("mcp_enabled"):
        tools += [s for name, s in ((n, e["schema"]) for n, e in mcp_client.tools().items())
                  if name not in local_names]
    return tools


def tools_with_state(settings: dict) -> list[dict]:
    """给前端用：schema + enabled（当前开关状态）+ switch_key（设置项 key）。
    MCP 工具挂到全局开关 mcp_enabled 下（服务器级，非 per-tool）。"""
    local_names = set(_TOOL_REGISTRY)
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        item = dict(reg["schema"])
        item["enabled"] = bool(settings.get(f"{name}_enabled", reg["enabled"]))
        item["switch_key"] = f"{name}_enabled"
        out.append(item)
    if settings.get("mcp_enabled"):
        for name, entry in mcp_client.tools().items():
            if name in local_names:
                continue
            item = dict(entry["schema"])
            item["enabled"] = True
            item["switch_key"] = "mcp_enabled"
            item["server"] = entry["server"]
            out.append(item)
    return out


# ---------------------------------------------------------------- 调度入口
def run_tool(name: str, args: dict) -> dict:
    reg = _TOOL_REGISTRY.get(name)
    if not reg:
        # 不在本地注册表 → 尝试 MCP 工具（未注册/未连接时 mcp_client 返回 ok=False）
        if name in mcp_client.tools():
            return mcp_client.call_tool(name, args or {})
        return {"ok": False, "message": f"未知工具 {name}"}
    try:
        return _run_fn(reg["fn"], args or {})
    except Exception as e:
        return {"ok": False, "message": f"工具执行失败: {e}"}
