#!/usr/bin/env node

// ============================================================
// MCP Gateway - 把多个 MCP 服务聚合成一个 stdio 接口
// 工作原理：
//   1. 读取 gateway.config.json，列出所有子 MCP 服务
//   2. 每个子服务启动一个子进程（stdio 通信）
//   3. 收集所有子服务的工具列表，加前缀防冲突
//   4. 把自己暴露为一个 MCP Server
//   5. AI 工具调用 gateway → gateway 转发给对应的子 MCP
// ============================================================

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { readFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";

console.error("[gateway] 启动中...");

// --- 读取配置 ---
// 优先级：
//   1. 环境变量 MCP_GATEWAY_CONFIG（可覆盖）
//   2. 默认 ~/.mcp/gateway.config.json（npx 模式下自动找到）
const defaultConfigPath = join(homedir(), ".mcp", "gateway.config.json");
const configPath = process.env.MCP_GATEWAY_CONFIG || defaultConfigPath;
console.error(`[gateway] 配置文件: ${configPath}`);

const config = JSON.parse(readFileSync(configPath, "utf-8"));
const serverConfigs = config.mcpServers || {};
const serverNames = Object.keys(serverConfigs);
console.error(`[gateway] 发现 ${serverNames.length} 个 MCP 服务: ${serverNames.join(", ")}`);

// --- 连接所有子 MCP 服务 ---
// 对配置中的每一项：
//   1. 创建 StdioClientTransport（启动子进程）
//   2. 创建 Client（MCP 客户端）
//   3. 调用 connect() 完成握手（initialize）
//   4. 调用 listTools() 获取工具列表
//   5. 工具名加上 "${服务名}_" 前缀防止冲突

const children = {};

for (const [name, cfg] of Object.entries(serverConfigs)) {
  console.error(`[gateway]   连接: ${name}`);

  const transport = new StdioClientTransport({
    command: cfg.command,
    args: cfg.args || [],
  });

  const client = new Client({
    name: `gateway-${name}`,
    version: "1.0.0",
  });

  await client.connect(transport);
  const result = await client.listTools();

  // 给工具名加前缀：serverName_toolName
  const tools = result.tools.map((tool) => ({
    ...tool,
    name: `${name}_${tool.name}`,
  }));

  children[name] = { client, tools };
  console.error(`[gateway]   ✅ ${name}: ${tools.length} 个工具`);
}

console.error(`[gateway] 共 ${Object.values(children).reduce((s, c) => s + c.tools.length, 0)} 个工具`);

// --- 把自己注册为一个 MCP Server ---
// 用 Server 类处理两种请求：
//   tools/list - 返回所有子 MCP 的工具（已合并）
//   tools/call - 根据工具名前缀路由到对应的子 MCP

const server = new Server(
  { name: "mcp-gateway", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  const allTools = Object.values(children).flatMap((c) => c.tools);
  return { tools: allTools };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  // 遍历所有子 MCP，找匹配的（工具名前缀 = 服务名）
  for (const [serverName, child] of Object.entries(children)) {
    const match = child.tools.find((t) => t.name === name);
    if (match) {
      const originalName = name.slice(serverName.length + 1);
      console.error(`[gateway]   ➡️ ${serverName}.${originalName}`);
      return await child.client.callTool({
        name: originalName,
        arguments: args,
      });
    }
  }

  throw new Error(`[gateway] 未知工具: ${name}`);
});

// --- 连接父进程（AI 工具）---
// StdioServerTransport 使用 stdin/stdout 通信
const transport = new StdioServerTransport();
await server.connect(transport);

console.error("[gateway] ✅ 就绪，等待请求...");
