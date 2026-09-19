# -*- coding: utf-8 -*-
"""GET /api/modules/status 聚合接口测试。"""
import os
from fastapi.testclient import TestClient

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test")   # 顶层 OpenAI 构造需 key 非空

from LLM.server import app   # noqa: E402


def test_modules_status_shape():
    with TestClient(app) as c:
        r = c.get("/api/modules/status")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        mods = data["modules"]
        # 聚合维度：语音 / embedding / RAG 存储 / 知识图谱 / MCP 工具
        # `mcp` 是后加的聚合项 —— 旧断言只认前四个（2026-09-15 补齐）
        assert set(mods.keys()) == {"voice", "embed", "ragstore", "graph", "mcp"}
        # voice：status 字段存在。完整词表见 LLM/voice/worker.py 的 self.status
        # 注释：running / degraded / disabled / stopped —— `degraded` 是"依赖缺一点但
        # 仍在降级跑"的正常状态（旧断言只认前三种，2026-09-15 补齐）
        assert mods["voice"]["status"] in ("running", "degraded", "disabled", "stopped")
        # 其余三个：available 布尔字段存在
        for k in ("embed", "ragstore", "graph"):
            assert "available" in mods[k]
            assert isinstance(mods[k].get("missing"), list)
        # mcp 的字段名与其他模块不同（mcp_client.status() 的口径）：
        # available / missing_deps / started / servers / errors / tools
        assert isinstance(mods["mcp"]["available"], bool)
        assert isinstance(mods["mcp"].get("missing_deps"), list)
        assert isinstance(mods["mcp"].get("servers"), dict)
