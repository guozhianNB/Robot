# @zhangzwd/mcp-gateway

一个轻量级 MCP 网关，将多个 MCP 服务聚合成一个统一的 stdio 接口。

每个子服务的工具会自动加上 `{服务名}_` 前缀，避免工具名冲突。

## 安装

```bash
npx @zhangzwd/mcp-gateway
```

无需全局安装，`npx` 会自动下载运行。

## 配置文件

默认读取 `~/.mcp/gateway.config.json`，可通过环境变量 `MCP_GATEWAY_CONFIG` 覆盖路径。

```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp", "--api-key", "你的API_KEY"]
    },
    "另一个服务名": {
      "command": "node",
      "args": ["path/to/server.js"]
    }
  }
}
```

`mcpServers` 中的每个 key 就是服务名，会作为工具前缀。例如 `context7` 服务的 `resolve-library-id` 工具对外暴露为 `context7_resolve-library-id`。

## 在 AI 工具中使用

### opencode.json

```json
{
  "mcp": {
    "gateway": {
      "type": "local",
      "command": ["npx", "-y", "@zhangzwd/mcp-gateway"]
    }
  }
}
```

### codex config.toml

```toml
[mcp_servers.gateway]
command = "npx"
args = ["-y", "@zhangzwd/mcp-gateway"]
```

### reasonix config.json

```json
{
  "mcpServers": {
    "gateway": {
      "command": "npx",
      "args": ["-y", "@zhangzwd/mcp-gateway"]
    }
  }
}
```

## 项目结构

```
~/.mcp/gateway.config.json   # 配置文件（你编辑这个）
~/.mcp-gateway/
  gateway.js                  # 主程序（已发布到 npm）
  package.json
  README.md
```

## 工作原理

1. 读取 `gateway.config.json`，获取子 MCP 服务列表
2. 为每个子服务启动一个子进程（stdio 通信）
3. 调用 `listTools` 获取工具列表，工具名加 `{服务名}_` 前缀
4. 将自己注册为一个 MCP Server，暴露合并后的工具列表
5. 收到 `tools/call` 请求时，根据工具名前缀路由到对应的子服务
