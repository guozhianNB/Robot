# -*- coding: utf-8 -*-
r"""MCP 集成冒烟测试（独立脚本，下划线前缀 → 不会被 tools.py 自动加载）。

用法（项目根目录）：
    .venv\Scripts\python.exe -m LLM.tool._mcp_demo

它会：
  1. 临时向 conf.MCP_SERVERS 注册一个 fetch 服务器（npx @tokenizin/mcp-npx-fetch）；
  2. 走 mcp_client.start() 拉起并注册工具；
  3. 调用 fetch_html 抓取 example.com，打印结果；
  4. 清理（stop）。

真实部署请把服务器配置写进 conf.py 的 MCP_SERVERS（而不是这里临时注入）。
"""
from .. import conf
from .. import mcp_client

FETCH_URL = "https://example.com"


def main():
    # 1. 临时配置一台 MCP 服务器（仅本脚本进程内生效）
    #    注意：Windows 上 npx 是 .cmd 脚本，command 须写 npx.cmd（Linux 写 npx）
    conf.MCP_SERVERS["fetch_demo"] = {
        "command": "npx.cmd",
        "args": ["-y", "@tokenizin/mcp-npx-fetch"],
        "enabled": True,
    }

    # 2. 走正式入口启动（mcp_enabled=True）
    print("mcp 依赖可用:", mcp_client.available())
    schemas = mcp_client.start({"mcp_enabled": True})
    if not schemas:
        print("启动失败/无工具，状态:", mcp_client.status())
        return
    print("已注册 MCP 工具:", [s["function"]["name"] for s in schemas])

    # 3. 调用一个工具
    name = "fetch_html"
    print(f"\n调用 {name}(url={FETCH_URL}) ...")
    result = mcp_client.call_tool(name, {"url": FETCH_URL})
    print("ok =", result.get("ok"))
    print("结果前 300 字:", str(result.get("result", result.get("message")))[:300])

    # 4. 清理
    mcp_client.stop()
    print("\n已关闭 MCP 会话")


if __name__ == "__main__":
    main()
