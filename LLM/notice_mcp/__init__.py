# -*- coding: utf-8 -*-
r"""护士传达 MCP 模块包。

- notice_client : 业务核心（把一条"要传达给护士的话"投递到后端通知中心），**不含 MCP 协议**
- notice_server : MCP 2.0 服务端（stdio），把 notify_nurse 暴露给 LLM 工具循环

设计取向与 LLM/vision_mcp、LLM/car_mcp 一致：业务与协议分层，业务层可脱离 MCP 框架直接单测。
业务层**只用标准库**（urllib），因此也可以整包拷到小车/另一台机器上单独跑。
"""
