#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""识图 MCP 服务端（子进程, stdio 传输）。

它把"看图回答问题"的能力包成 MCP 工具 `see_what`，供 LLM 后端
（`LLM/agent/mcp_client.py`）在启动时以 stdio 子进程拉起，工具自动并入 OpenAI
function-calling 工具循环 —— 大模型据此可以在需要知道"现场是什么样"时看一眼。

工具一览（业务实现见 vision_client.py）：
  see_what(image, prompt, channel=1)   看摄像头当前画面或指定图片，回答关于画面内容的问题

用法（本地直接起，便于单独调试）::

    .venv\Scripts\python.exe LLM/vision_mcp/see_server.py

挂到后端（在 `LLM/conf.py` 的 `MCP_SERVERS` 加一条）—— 三个坑，逐条都有原因：
  1) `"command"` 必须是**后端同一个解释器**（Windows 上如 `.venv\Scripts\python.exe`）：
     换别的 python 会缺 openai/mcp，子进程一起来就退出；
  2) `"env": {"DASHSCOPE_API_KEY": ""}` —— 空串表示"运行时从后端环境继承"
     （mcp_client 的懒加载机制；.env 是后端启动后才加载的）；
  3) `"roles"` **只是其中一道闸门**：还得把 `"see_what"` 加进 `LLM/agent/policy.py`
     对应角色的 `allowed_tools`，否则白名单（天花板）会把工具挡掉、模型看不见它。

降级（AGENTS.md「系统稳健性」）：python-mcp 或云端依赖缺失 → 记日志 + 写 stderr +
**以退出码 2 收场**（mcp_client 会把它记成 connect_error，后端照常启动，只是没有
这个工具）。**摄像头不可用不算启动失败** —— 摄像头服务可能稍后才起，那种情况留给
每次调用返回 ok:false。
"""
import json
import os
import sys
import time

# 脚本方式启动时，本目录不在 sys.path（sys.path[0] 是本文件所在目录，实际已在，
# 但用 -m 或其它方式启动时不一定），显式补一次以保证 import vision_client 稳定。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if __package__:
    from . import vision_client                         # 包方式导入，避免模块双实例
else:
    import vision_client                                # 直接执行脚本时的回退路径

# --------------------------------------------------------------------------
# python-mcp 依赖层（可选能力降级）
try:
    from mcp.server.mcpserver import MCPServer
except Exception as e:                                  # 未装 / 版本不兼容 → 降级
    MCPServer = None
    _INIT_ERR = str(e)
else:
    _INIT_ERR = None

# 日志落盘（MCP 走 stdio，**绝不能 print 到 stdout** 污染协议）
_LOG_PATH = os.environ.get("VISION_MCP_LOG", os.path.join(_HERE, "see_mcp.log"))


def _log(msg: str):
    """写日志：文件 + stderr。stdout 留给 MCP 协议，一个字都不能多。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 工具实现
# ⚠️ 必须返回 **字符串**：LLM/mcp_client.call_tool 只读 MCP 结果的"文本块"
#    (content[*].text) 并拼成字符串回给工具循环；return dict 会变成无 .text 的
#    结构化块，上游只会看到"（MCP 工具无文本返回）"。故一律 json.dumps。
def _see_what(image: str, prompt: str, channel: int = 1) -> str:
    try:
        return json.dumps(
            vision_client.see_what(image, prompt, channel), ensure_ascii=False
        )
    except Exception as e:                     # 理论上到不了这（see_what 自己兜底）
        error_type = e.__class__.__name__
        return json.dumps(
            {"ok": False, "error": f"see_what 异常（{error_type}）"},
            ensure_ascii=False,
        )


def build_server():
    """注册工具并返回 MCPServer；python-mcp 不可用时返回 None。"""
    if MCPServer is None:
        return None
    server = MCPServer("robot-vision",
                       description="摄像头识图：把当前画面连同问题交给云端视觉模型回答")

    @server.tool(name="see_what")
    def see_what(image: str, prompt: str, channel: int = 1) -> str:
        """看一张图片并回答一个具体问题，返回 JSON 字符串。

        `image` 是必填项：传 `camera` 表示读取摄像头共享服务的当前帧，其他值表示
        本地图片路径。使用本地路径时，只能读取 `VISION_SEE_IMAGE_DIRS` 白名单目录
        内受支持的图片文件；不要尝试读取白名单之外的文件。

        `prompt` 是必填项，应具体描述想从画面确认的问题。`channel` 是摄像头通道，
        默认值为 1；使用本地文件时会被忽略。一次调用通常需要等待云端视觉模型返回。

        图片会上传到阿里云百炼视觉模型，视觉回答可能有误，重要结论必须人工核实。
        这是通用看图问答工具，不承担跌倒检测、告警触发或医疗判断，也不会直接触发
        机器人动作。摄像头未启动、密钥缺失或云端超时都会返回 `ok: false`，不要编造
        看不到的画面内容。
        """
        return _see_what(image, prompt, channel)

    return server


def main():
    # 降级 1：MCP 框架缺失 → 让 mcp_client 记 connect_error，后端照常起
    if MCPServer is None:
        _log(f"[fatal] python-mcp 不可用: {_INIT_ERR}")
        sys.stderr.write("python-mcp 未安装，robot-vision MCP 服务无法启动 "
                         f"(缺失: {_INIT_ERR})；请在后端环境安装 mcp。\n")
        sys.exit(2)
    # 降级 2：云端识图不可用（缺 openai 或缺 DASHSCOPE_API_KEY）→ 起了也没用
    if not vision_client.cloud_available():
        reason = "；".join(vision_client.missing()) or "缺少 DASHSCOPE_API_KEY（.env）"
        _log(f"[fatal] 云端识图不可用: {reason}")
        sys.stderr.write(f"云端识图不可用（{reason}），robot-vision 退出。\n")
        sys.exit(2)

    server = build_server()
    if server is None:
        sys.exit(2)
    host, port = vision_client.vision_target()
    _log(f"robot-vision MCP server 就绪（模型 {vision_client.MODEL}，"
         f"摄像头 {host}:{port}，通道 {vision_client.CHANNEL}）")
    try:
        server.run()                           # transport="stdio" 默认
    finally:
        _log("robot-vision MCP server 退出")


if __name__ == "__main__":
    main()
