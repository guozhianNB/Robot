# -*- coding: utf-8 -*-
r"""智能体层：大模型端"大脑与嘴"的业务实现。

- :mod:`LLM.agent.chat`       对话编排（SSE 流式 + 思考路由 + RAG 注入 + 工具循环）
- :mod:`LLM.agent.memory`     RAG 长期记忆 + 半自动沉淀（红线：医疗关键词拒写）
- :mod:`LLM.agent.tools`      工具注册中心 + 分发（本地 ``LLM/tool/`` + MCP）
- :mod:`LLM.agent.mcp_client` MCP 客户端桥（可选能力，缺失只降级）
- :mod:`LLM.agent.reminder`   定时提醒调度（独立线程，15s tick）
- :mod:`LLM.agent.session`    分层用户体系的会话层（角色/主体/当前病房）
- :mod:`LLM.agent.policy`     三角色策略包（纯数据 + 纯函数，不做 IO）
- :mod:`LLM.agent.prompt/`    提示词片段：``base.md``（共用人设+红线）
                              + ``{ward,elder,admin}.md``（角色片段）
"""
