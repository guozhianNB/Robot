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


def _mcp_roles(server: str) -> set[str]:
    """MCP 服务器声明的角色集。**未声明 → {"admin"}**（不受控外部能力，默认从严）。

    注意区分"未声明"与"显式声明为空"：`.get("roles") is None` = 未声明（给 admin）；
    `roles=[]` = 作者明确表示"谁都不给"，就真的给谁都不给。
    """
    from .conf import MCP_SERVERS
    declared = MCP_SERVERS.get(server, {}).get("roles")
    return {"admin"} if declared is None else set(declared)


def _mcp_tools_for(settings: dict, resolved_role: str, allow) -> list[dict]:
    """MCP 工具按「角色白名单 ∩ 服务器 roles」过滤 —— 与本地工具同一把尺子。

    `allow` 是 `role_policy(role)["allowed_tools"]`（None=不裁剪）。**白名单是天花板**：
    服务器声明 `roles=["elder"]` 也不能让一个不在 elder 白名单里的 MCP 工具对 elder 可见。
    """
    out = []
    for name, entry in mcp_client.tools().items():
        if name in _TOOL_REGISTRY:
            continue                        # 与本地重名时本地优先（沿用旧口径）
        if allow is not None and name not in allow:
            continue
        if resolved_role not in _mcp_roles(entry.get("server", "")):
            continue
        out.append(entry["schema"])
    return out


def effective_tools(settings: dict, principal: dict | None = None) -> list[dict]:
    """闸门 2：per-tool 开关 ∩ 角色白名单 ∩ 工具自身 roles。`principal` 缺省 → 集体层（R2）。"""
    from .policy import POLICY_DEFAULTS, role_policy
    role = (principal or {}).get("role")
    resolved = role if role in POLICY_DEFAULTS else "ward"     # 解析一次，白名单与 roles 用同一个
    allow = role_policy(role)["allowed_tools"]                 # None = 不裁剪；[] = 一个都不给
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        if not settings.get(f"{name}_enabled", reg["enabled"]):
            continue
        if allow is not None and name not in allow:
            continue
        if reg.get("roles") is not None and resolved not in reg["roles"]:
            continue
        out.append(reg["schema"])
    if settings.get("mcp_enabled"):
        out += _mcp_tools_for(settings, resolved, allow)
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
    from .policy import POLICY_DEFAULTS, role_policy
    from . import log as audit
    p = principal or {}
    role = p.get("role")
    resolved = role if role in POLICY_DEFAULTS else "ward"
    allow = role_policy(role)["allowed_tools"]
    reg = _TOOL_REGISTRY.get(name)
    is_local = reg is not None
    if not is_local and name not in mcp_client.tools():
        # 未知工具（模型幻觉/拼错）：走未知分支，**不落越权审计**（别污染越权统计）
        return {"ok": False, "message": f"未知工具 {name}"}
    allow_ok = allow is None or name in allow
    if is_local:
        roles_ok = reg.get("roles") is None or resolved in reg["roles"]
    else:
        roles_ok = resolved in _mcp_roles(mcp_client.tools()[name].get("server", ""))
    if not (allow_ok and roles_ok):
        audit.log("policy_deny", tool=name, role=role, uid=p.get("uid"), slot=p.get("slot"),
                  decision="deny", args=args or {},
                  reason="out_of_role_whitelist" if not allow_ok else "tool_roles_mismatch")
        return {"ok": False, "error": f"当前身份不允许调用工具 {name}"}
    if not is_local:
        return mcp_client.call_tool(name, args or {})
    try:
        return _run_fn(reg["fn"], args or {})
    except Exception as e:
        return {"ok": False, "message": f"工具执行失败: {e}"}
