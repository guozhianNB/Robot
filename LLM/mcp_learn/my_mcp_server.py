# -*- coding: utf-8 -*-
r"""最小 MCP 测试服务器（mcp 2.0 语法）：注册一个 say_hello 工具。教学用，用后即删。

mcp 2.0 服务端写法（注意！不是老教程里的 FastMCP）：
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("名字")
    @server.tool()
    def 工具名(参数: 类型) -> str: ...
    server.run()   # 默认 stdio 传输
"""
from mcp.server.mcpserver import MCPServer

server = MCPServer("tiny-server")


@server.tool()
def say_hello(name: str) -> str:
    """对 name 打招呼。"""
    return f"你好，{name}！来自 tiny-server。"


if __name__ == "__main__":
    server.run()   # transport="stdio" 是默认值
