# -*- coding: utf-8 -*-
r"""识图 MCP 模块包。

- vision_client : 业务核心（抓帧 / 读图 / 调云端视觉模型 / see_what 编排），**不含 MCP 协议**
- see_server    : MCP 2.0 服务端（stdio），把 see_what 暴露给 LLM 工具循环

设计取向与 LLM/car_mcp 一致：业务与协议分层，业务层可脱离 MCP 框架直接单测。
"""
