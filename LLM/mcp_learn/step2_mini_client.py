# -*- coding: utf-8 -*-
r"""第 2 步教材：最小 MCP 客户端（约 30 行）。

目的：亲手跑通"客户端 → MCP 服务器 → 搜索引擎"的完整链路，
亲眼看到 MCP 协议的三步握手。运行命令：

    .\.venv\Scripts\python.exe LLM\mcp_learn\step2_mini_client.py

运行后你会看到（按顺序）：
    1. npx 拉起 duckduckgo-mcp-server 子进程（服务器打印 running on stdio）
    2. 客户端打印服务器提供的工具清单（tools/list 的结果）
    3. 客户端调用 duckduckgo_web_search 搜索"养老陪护机器人"（tools/call 的结果）
"""
import asyncio
import sys

# Windows 控制台默认 GBK，强制用 UTF-8 输出，避免中文乱码（对功能无影响）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mcp import ClientSession, StdioServerParameters          # 会话对象 + 服务器启动参数
from mcp.client.stdio import stdio_client                     # stdio 客户端（拉起子进程并读写管道）


async def main():
    # ① 启动参数：告诉客户端"我要连的 MCP 服务器怎么启动"
    #    Windows 上 npx 是 npx.cmd，Linux 写 npx；-y 表示自动下载包
    params = StdioServerParameters(command="npx.cmd", args=["-y", "duckduckgo-mcp-server"])

    # ② 进入 stdio 上下文：真正拉起子进程，拿到读/写两条流
    async with stdio_client(params) as (read_stream, write_stream):
        # ③ 创建会话：基于两条流建立 JSON-RPC 通道
        async with ClientSession(read_stream, write_stream) as session:
            # ④ 握手：客户端和服务器协商协议版本/能力（MCP 协议第一步）
            await session.initialize()

            # ⑤ tools/list：问服务器"你会什么工具"，返回工具清单
            tools = await session.list_tools()
            print(f"\n服务器提供了 {len(tools.tools)} 个工具：")
            for t in tools.tools:
                print(f"  · {t.name} — {t.description[:60]}...")

            # ⑥ tools/call：调用工具，真正执行一次网页搜索
            print("\n调用 duckduckgo_web_search 搜索『养老陪护机器人』...\n")
            result = await session.call_tool(
                "duckduckgo_web_search",
                arguments={"query": "养老陪护机器人", "count": 5, "safeSearch": "moderate"},
            )
            # 服务器返回的是若干 ContentBlock，这里取出所有文本块拼起来打印
            for c in result.content:
                text = getattr(c, "text", "")
                if text:
                    print(text)
            print("\n✅ 完整链路已跑通！")


if __name__ == "__main__":
    asyncio.run(main())
