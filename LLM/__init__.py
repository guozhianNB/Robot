# -*- coding: utf-8 -*-
r"""LLM 大模型端包。按功能分四层（依赖单向：core → store → agent → server）：

- ``LLM/server.py`` / ``LLM/mapeditor_server.py`` — 两个 FastAPI 入口（包根，启动命令不变）
  ``uvicorn LLM.server:app``（:8000，含 /admin /kiosk）／ ``LLM.mapeditor_server:app``（:8010，按需）
- ``LLM/conf.py``   — 集中配置（全包共享的顶层契约，改参数先来这里）
- ``LLM/core/``     — 基础设施：log(审计) / bus(SSE 广播) / vectors(轻量向量) / zonegeo(几何)
- ``LLM/store/``    — 持久化：db(SQLite) / ragstore(Chroma) / graph(Kuzu) / embed / migrate
- ``LLM/agent/``    — 智能体：chat(对话编排) / memory(RAG 记忆) / tools / mcp_client /
                      reminder / session / policy + ``prompt/``(提示词片段)
- ``LLM/maps/``     — 地图域：mapstore / mapsources / maptags / mapserver /
                      locator / roslink + mapapi(编辑器路由) / mapctl(编辑器进程管理)
- ``LLM/voice/``    — 语音链路：worker / asr / tts / kws / vad / speaker + voice_api(挂载逻辑)
- ``LLM/tool/``     — 本地工具实现（``@tool`` 装饰器注册，tools.py 自动加载）
- ``LLM/car_mcp/`` ／ ``LLM/vision_mcp/`` — 外部 MCP 服务器（独立子进程，非后端导入链）
"""
