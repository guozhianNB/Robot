# -*- coding: utf-8 -*-
r"""LLM-facing entry point for submitting validated Plan candidates."""
from ..agent.tools import tool


@tool(
    "create_plan",
    "创建需要多个动作、等待、跨轮次继续或完成后回报的候选计划；不能用于 P0 安全告警。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "计划标题"},
            "priority": {"type": "string", "enum": ["P1", "P2", "P3"], "default": "P2"},
            "owner_uid": {"type": "string", "default": ""},
            "steps": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["action", "wait", "manual"]},
                        "action": {"type": "string"},
                        "args": {"type": "object"},
                        "label": {"type": "string"},
                        "wait_kind": {"type": "string", "enum": ["time", "device", "external", "manual"]},
                        "wake_at": {"type": "string"},
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            },
            "report": {
                "type": "object",
                "properties": {
                    "notify": {"type": "boolean"},
                    "speak_if_present": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "required": ["title", "steps"],
        "additionalProperties": False,
    },
    roles={"ward", "elder", "admin"},
)
def create_plan(title, steps, priority="P2", owner_uid="", report=None) -> dict:
    from ..agent import plan
    return plan.create_from_tool(title, steps, priority, owner_uid, report)
