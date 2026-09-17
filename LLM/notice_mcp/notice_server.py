#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""护士传达 MCP 服务端（子进程, stdio 传输）。

它把"把一条话传给护士"的能力包成 MCP 工具 `notify_nurse`，供 LLM 后端
（`LLM/agent/mcp_client.py`）在启动时以 stdio 子进程拉起，工具自动并入 OpenAI
function-calling 工具循环 —— 大模型据此可以在老人说不舒服 / 要找护士 / 问到它答不了的
用药问题时，**真的**把话传到护士台（人设红线 `base.md` 早就写了"我这就去通知护士"，
在此之前它没有工具可调，只有一句话）。

工具一览（业务实现见 notice_client.py）：
  notify_nurse(message, level=info|warning|critical, uid="")   把一条要传达的话推给护士台

用法（本地直接起，便于单独调试）::

    .venv\Scripts\python.exe LLM/notice_mcp/notice_server.py

挂到后端（`LLM/conf.py` 的 `MCP_SERVERS["notice"]` 已配好）—— 三个坑，逐条都有原因：
  1) `"command"` 必须是**后端同一个解释器**（Windows 上如 `.venv\Scripts\python.exe`）：
     换别的 python 会缺 mcp，子进程一起来就退出；
  2) `"env": {"NOTICE_BACKEND_URL": …}` —— 跨机部署只改这一个值（见下方降级说明）；
  3) `"roles"` **只是其中一道闸门**：还得把 `"notify_nurse"` 加进 `LLM/agent/policy.py`
     对应角色的 `allowed_tools`，否则白名单（天花板）会把工具挡掉、模型看不见它。

降级（AGENTS.md「系统稳健性」）：
  - python-mcp 缺失 → 记日志 + 写 stderr + **退出码 2**（mcp_client 记 connect_error，
    后端照常启动，只是没这个工具）；
  - **后端不可达不算启动失败**（后端可能稍后才起 / 正在重启）→ 启动**不做任何网络探测**，
    留到每次调用时由 notice_client 返回 `{"ok": false, ...}`。
"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if __package__:
    from . import notice_client                        # 包方式导入，避免模块双实例
else:
    import notice_client                               # 直接执行脚本时的回退路径

# --------------------------------------------------------------------------
# python-mcp 依赖层（可选能力降级）
try:
    from mcp.server.mcpserver import MCPServer
except Exception as e:                                 # 未装 / 版本不兼容 → 降级
    MCPServer = None
    _INIT_ERR = str(e)
else:
    _INIT_ERR = None

# 日志落盘（MCP 走 stdio，**绝不能 print 到 stdout** 污染协议）
_LOG_PATH = os.environ.get("NOTICE_MCP_LOG", os.path.join(_HERE, "notice_mcp.log"))


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
def _notify_nurse(message: str, level: str = "info", uid: str = "") -> str:
    """调业务层并落一条调用日志（**不记 message 原文** —— 与脱敏口径一致：那是老人的私聊内容）。"""
    try:
        out = notice_client.push(message, level=level, uid=uid)
    except Exception as e:                     # 理论上到不了这（push 自己兜底）
        error_type = e.__class__.__name__
        # 异常也要留痕：只回给模型一个类名的话，日志里什么都没有，线上没法排查。
        _log(f"notify_nurse 异常 {error_type}: {str(e)[:200]}")
        return json.dumps({"ok": False, "error": f"notify_nurse 异常（{error_type}）"},
                          ensure_ascii=False)
    _log(f"notify_nurse level={out.get('level') or notice_client.normalize_level(level)} "
         f"has_uid={bool((uid or '').strip())} ok={out.get('ok')} "
         f"deduped={out.get('deduped')} id={out.get('id')}")
    return json.dumps(out, ensure_ascii=False)


def build_server():
    """注册工具并返回 MCPServer；python-mcp 不可用时返回 None。"""
    if MCPServer is None:
        return None
    server = MCPServer(
        "robot-notice",
        description="护士传达：把一条要传达给护士的话推给护士台（护士在她的屏幕上立刻看到）",
    )

    @server.tool(name="notify_nurse")
    def notify_nurse(message: str, level: str = "info", uid: str = "") -> str:
        """把一条要传达的话推给护士台，护士在自己的屏幕上会立刻看到。返回 JSON 字符串。

        **什么时候用**：老人说身体不舒服、想找护士、问到用药/病情你答不上来（红线是医疗
        只读——说完"这个我不懂，我帮您问护士"就应该调它把问题转达）；或者现场有需要护士
        知道的情况（老人摔倒、长时间没动静、房间里有异常）。**不要拿它代替急停**：需要
        立刻停车请调 robot_stop。

        `message` 必填：一条通知只说一件事，用一句话说清「谁、什么事、在哪儿」——护士看到
        的就是这句话。**把老人的称呼写进这句话里**（例如"张爷爷说胸口疼，想找护士"），
        因为你通常拿不到他的 uid。

        `level` 是紧急程度，默认 info：摔倒、呼救、胸口剧痛、意识不清用 critical（护士台
        会置顶并响提示音）；一般身体状况、想让护士来看看用 warning；日常转达、事务性留言
        用 info。（大小写不敏感；确切的词只有这三个。）

        `uid` 是老人 uid，能确定就填、不确定就留空（留空不影响送达，只是护士卡上看不到
        「姓名 · 床号」）。

        限制：这是单向通知，护士不会用这个工具回话；**同样的内容** 60 秒内重复上报会被合并
        成一条（内容不同则各自成条，不会被吞）；后台连不上时返回 `ok: false`，此时**不要**
        对老人说"已经通知护士了"，改说"我这就想办法联系护士"。
        """
        return _notify_nurse(message, level, uid)

    return server


def main():
    # 降级：MCP 框架缺失 → 让 mcp_client 记 connect_error，后端照常起
    if MCPServer is None:
        _log(f"[fatal] python-mcp 不可用: {_INIT_ERR}")
        sys.stderr.write("python-mcp 未安装，robot-notice MCP 服务无法启动 "
                         f"(缺失: {_INIT_ERR})；请在后端环境安装 mcp。\n")
        sys.exit(2)

    server = build_server()
    if server is None:                         # 与上面同族的降级出口（build_server 只在缺 mcp 时返回 None）
        _log("[fatal] build_server 返回 None")
        sys.stderr.write("robot-notice MCP 服务构建失败，退出。\n")
        sys.exit(2)
    # 注意：**不在这里探活后端** —— 后端可能稍后才起/正在重启，探活失败会让整个工具失踪。
    _log(f"robot-notice MCP server 就绪（护士后台 {notice_client.backend_url()}，"
         f"超时 {notice_client.TIMEOUT:g}s）")
    try:
        server.run()                           # transport="stdio" 默认
    finally:
        _log("robot-notice MCP server 退出")


if __name__ == "__main__":
    main()
