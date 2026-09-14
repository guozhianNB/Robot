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


def tool(name: str, description: str, parameters: dict, enabled: bool = True,
         roles: set[str] | None = None):
    """注册一个工具。`roles=None` = 不限角色（现有工具保持兼容）；空集 = 谁都不给。

    角色白名单只是**闸门 2**（与全局 per-tool 开关取交集）；动作风险分级属 P1 的
    `policy.check_action()`（闸门 3），本函数不做。
    """
    def deco(fn):
        _TOOL_REGISTRY[name] = {
            "schema": {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            },
            "fn": fn,
            "enabled": enabled,
            "roles": None if roles is None else set(roles),
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


def _mcp_tools_for(settings: dict, role: str) -> list[dict]:
    """MCP 工具按 `conf.MCP_SERVERS[server]["roles"]` 过滤；未声明视为 {"admin"}（从严）。"""
    from .conf import MCP_SERVERS
    out = []
    for name, entry in mcp_client.tools().items():
        if name in _TOOL_REGISTRY:
            continue                        # 与本地重名时本地优先（沿用旧口径）
        roles = MCP_SERVERS.get(entry.get("server", ""), {}).get("roles") or {"admin"}
        if role in roles:
            out.append(entry["schema"])
    return out


def effective_tools(settings: dict, principal: dict | None = None) -> list[dict]:
    """闸门 2：per-tool 开关 ∩ 角色白名单。`principal` 缺省 → 集体层（R2 fail-closed）。"""
    from .policy import role_policy
    role = (principal or {}).get("role")
    allow = role_policy(role)["allowed_tools"]      # None = 不按角色裁剪；[] = 一个都不给
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        if not settings.get(f"{name}_enabled", reg["enabled"]):
            continue
        if allow is not None and name not in allow:
            continue
        if reg.get("roles") is not None and (role or "") not in reg["roles"]:
            continue
        out.append(reg["schema"])
    if settings.get("mcp_enabled"):
        out += _mcp_tools_for(settings, role or "")
    return out


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
def run_tool(name: str, args: dict, principal: dict | None = None) -> dict:
    """统一分发。**执行前再校验一次角色白名单**（闸门 2 第二道）。"""
    from .policy import role_policy
    from . import log as audit
    p = principal or {}
    allow = role_policy(p.get("role"))["allowed_tools"]
    reg = _TOOL_REGISTRY.get(name)
    if allow is not None and name not in allow:
        audit.log("policy_deny", tool=name, role=p.get("role"), uid=p.get("uid"),
                  slot=p.get("slot"), reason="out_of_role_whitelist")
        return {"ok": False, "error": f"当前身份不允许调用工具 {name}"}
    if reg is not None and reg.get("roles") is not None and (p.get("role") or "") not in reg["roles"]:
        audit.log("policy_deny", tool=name, role=p.get("role"), uid=p.get("uid"),
                  slot=p.get("slot"), reason="tool_roles_mismatch")
        return {"ok": False, "error": f"当前身份不允许调用工具 {name}"}
    if not reg:
        # 不在本地注册表 → 尝试 MCP 工具（未注册/未连接时 mcp_client 返回 ok=False）
        if name in mcp_client.tools():
            return mcp_client.call_tool(name, args or {})
        return {"ok": False, "message": f"未知工具 {name}"}
    try:
        return _run_fn(reg["fn"], args or {})
    except Exception as e:
        return {"ok": False, "message": f"工具执行失败: {e}"}
