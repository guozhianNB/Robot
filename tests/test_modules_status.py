# -*- coding: utf-8 -*-
"""GET /api/modules/status 聚合接口测试。

外加一条**红线回归**：进/出主 app 的完整 lifespan 时，退出收尾（`LLM/server.py` 里
`yield` 之后的 `mapctl.stop()`）不得打到本机真实的编辑器服务 —— 详见文件末尾的
`test_lifespan_shutdown_does_not_hit_real_editor_service` 与
`.superpowers/sdd/msvc-fix4-report.md`。
"""
import os
from fastapi.testclient import TestClient

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test")   # 顶层 OpenAI 构造需 key 非空

from LLM.server import app   # noqa: E402

_STOP_PATH = "/api/mapeditor/service/stop"


class _OkResp:
    """假的 urlopen 响应：status 200（探活与自停都当"成功"）。"""

    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_modules_status_shape(monkeypatch):
    # 本用例进的是**主 app 的完整 lifespan**（`with` 会跑启动 + 收尾），收尾第一行是
    # `mapctl.stop()`：测试进程里没有句柄，不桩就落到 external 分支去 POST 真实
    # 127.0.0.1:8010 的自停接口。这里显式再桩一次（`tests/conftest.py` 的
    # `no_real_mapeditor_stop` 已全目录兜底，这一条是就地可见的意图声明）。
    from LLM.maps import mapctl
    monkeypatch.setattr(mapctl, "stop", lambda: {"ok": True, "running": False,
                                                 "source": "none", "pid": None,
                                                 "port": 8010, "uptime_s": None})
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


def test_lifespan_shutdown_does_not_hit_real_editor_service(monkeypatch):
    """红线回归：退出主 app 的 lifespan 不得打到本机真实的编辑器服务。

    RED（修复前）：本用例在 `with TestClient(app)` 里进出完整 lifespan，收尾第一行是裸的
    `mapctl.stop()`；测试进程里 `_proc is None` 且 `port_alive()` 为真（本机真有编辑器在
    跑，开发时的常态）⇒ 落到 external 分支，向 127.0.0.1:8010 真发
    `POST /api/mapeditor/service/stop`：**那台服务真的退出**（浏览器里未保存的像素修图随之
    丢失），随后还要白等最多 5 秒。
    GREEN（修复后）：`tests/conftest.py::no_real_mapeditor_stop` 护栏让收尾不碰真实世界，
    `urlopen` 一次都不该落在自停接口上。

    本用例**刻意不自己桩 `mapctl.stop`**：自己桩等于把要验证的东西假设掉。它验证的是
    "本目录进 lifespan 的用例都已被护栏罩住"这件事的可观测后果 —— 摘掉 conftest 那条
    fixture，本用例立刻回红。`port_alive` 恒 True 也是刻意的：模拟"本机真有编辑器在听"。
    """
    from LLM.maps import mapctl

    seen = []

    def _urlopen(req, timeout=None):
        url = getattr(req, "full_url", None) or str(req)
        seen.append(url)
        return _OkResp()

    monkeypatch.setattr(mapctl, "port_alive", lambda port=None, timeout=0.5: True)
    monkeypatch.setattr(mapctl.urllib.request, "urlopen", _urlopen)

    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200

    hit = [u for u in seen if u.endswith(_STOP_PATH)]
    assert not hit, "退出主 app lifespan 时打到了真实编辑器自停接口：{}".format(hit)
