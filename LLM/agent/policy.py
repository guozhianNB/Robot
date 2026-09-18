# -*- coding: utf-8 -*-
r"""
角色策略包（分层用户体系）：每个层级 = 一套策略 = 提示词片段 + 工具白名单 + 数据可见范围。

规格：docs/superpowers/specs/2026-09-14-layered-user-roles-design.md §3.2/§3.3
红线：R1 前端 role 不可信（role 只由 session.derive_role 推导）
      R2 fail-closed（未知角色一律按集体层最小能力）
      R5 层级上下文单向（集体层对同病房老人可读；反向与跨病房不可读）

设计取舍：本模块**只放纯数据 + 纯函数**，不做任何 IO（会话状态在 session.py）。
P1 的 check_action()（动作风险分级/二次确认）依赖 car MCP，**不在这里留空壳**。
"""
from pathlib import Path

from ..conf import BASE_DIR

PROMPT_DIR = BASE_DIR / "LLM" / "agent" / "prompt"

POLICY_DEFAULTS: dict[str, dict] = {
    # ---- 集体层：一屋子人。读得到本病房公开对话，读不到任何个人档案 ----
    "ward": {
        "prompt_file": PROMPT_DIR / "ward.md",
        # 只接"安全 + 只读"两类：状态播报与急停。
        # 依据规格 §3.3 能力矩阵：`车·状态查询（位姿/电量）✅ 只读播报`、`车·急停/呼救 ✅ 永远允许（R3）`。
        # 未识别的说话人也会回落到这一层 —— 急停必须可用（空列表 = 连急停都做不了，违反 R3）。
        "allowed_tools": ["robot_status", "robot_stop"],
        "data_scope": "none",
        "ward_context": True,
    },
    # ---- 老人层：本人档案 + 本病房集体上下文（只读、单向）----
    "elder": {
        "prompt_file": PROMPT_DIR / "elder.md",
        # R3：急停/呼救类工具永远在列（当前仅有 robot_stop 是安全动作）
        "allowed_tools": ["robot_status", "robot_stop", "see_what"],
        "data_scope": "self",
        "ward_context": True,
    },
    # ---- 管理层：全部能力 ----
    "admin": {
        "prompt_file": PROMPT_DIR / "admin.md",
        "allowed_tools": None,          # None = 不按角色裁剪（仍受全局 per-tool 开关约束）
        "data_scope": "all",
        "ward_context": False,
    },
}


def role_policy(role: str | None) -> dict:
    """取角色策略；未知/None/空 → **集体层**（R2 fail-closed）。

    返回**浅拷贝**（`allowed_tools` 也是新 list）：策略表是模块级共享常量，调用方一旦原地
    `append` 就会污染全局白名单 —— 往低权限角色的白名单里长出一条危险工具，比少一条危险得多。
    """
    p = POLICY_DEFAULTS.get(role or "", POLICY_DEFAULTS["ward"])
    return {**p, "allowed_tools": None if p["allowed_tools"] is None else list(p["allowed_tools"])}
