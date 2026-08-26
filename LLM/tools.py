# -*- coding: utf-8 -*-
r"""
联网工具集（模块 9）：
  - web_search(query)  通用搜索：有 BOCHA_API_KEY / SERPAPI_KEY 时走官方 API；
                        否则走 DuckDuckGo Instant Answer + 本地新闻语料关键词检索兜底。
  - get_news(category) 结构化新闻：抓取 RSS（人民日报/央视/新华社/BBC中文等，按分类）。
安全约束：结果只做摘要；健康类信息在返回里注明"仅供参考"；过滤惊悚关键词。

工具以 OpenAI Function Calling 格式暴露给大模型，由 chat 引擎调用。

【新增工具三步走】schema 与实现写在一起，装饰器自动注册：
    @tool("工具名", "何时用/怎么用的描述（模型靠它决定调用）", {参数 JSON Schema}, enabled=True)
    def 工具名(参数: str = "默认值") -> dict:
        ...
        return {"ok": True, "result": "..."}   # 或 {"ok": False, "message": "..."}
注册后 run_tool 自动分发、TOOLS 自动收录、per-tool 开关 <工具名>_enabled 自动生效，
前端工具页自动显示开关，无需再改 chat.py / server.py / db.py / conf.py。
"""
import importlib
import inspect
import pkgutil

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
    """按 per-tool 开关（`<工具名>_enabled`）过滤，返回要传给模型的 schema 列表。"""
    return [reg["schema"] for name, reg in _TOOL_REGISTRY.items()
            if settings.get(f"{name}_enabled", reg["enabled"])]


def tools_with_state(settings: dict) -> list[dict]:
    """给前端用：schema + enabled（当前开关状态）+ switch_key（设置项 key）。"""
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        item = dict(reg["schema"])
        item["enabled"] = bool(settings.get(f"{name}_enabled", reg["enabled"]))
        item["switch_key"] = f"{name}_enabled"
        out.append(item)
    return out


# ---------------------------------------------------------------- 调度入口
def run_tool(name: str, args: dict) -> dict:
    reg = _TOOL_REGISTRY.get(name)
    if not reg:
        return {"ok": False, "message": f"未知工具 {name}"}
    try:
        return _run_fn(reg["fn"], args or {})
    except Exception as e:
        return {"ok": False, "message": f"工具执行失败: {e}"}
