import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 定义如何启动 fetch server
server_params = StdioServerParameters(
    command="npx",
    args=["-y", "@tokenizin/mcp-npx-fetch"],
    env=None  # 继承当前环境变量；如需指定 Node 路径可设 {"PATH": "..."}
)

async def main():
    # 启动子进程并建立 MCP 会话
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # ⚠️ 必须初始化握手，否则无法调用工具
            await session.initialize()

            # 列出可用工具（验证连接成功）
            tools = await session.list_tools()
            print("可用工具:", [t.name for t in tools.tools])

            # 调用 fetch_html
            result = await session.call_tool(
                "fetch_html",
                arguments={"url": "https://example.com"}
            )
            print("抓取结果:", result.content[0].text[:500])

if __name__ == "__main__":
    asyncio.run(main())