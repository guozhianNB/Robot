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
        assert set(mods.keys()) == {"voice", "embed", "ragstore", "graph"}
        # voice：status 字段存在（running/stopped/unavailable 之一）
        assert mods["voice"]["status"] in ("running", "stopped", "unavailable")
        # 其余三个：available 布尔字段存在
        for k in ("embed", "ragstore", "graph"):
            assert "available" in mods[k]
            assert isinstance(mods[k].get("missing"), list)
