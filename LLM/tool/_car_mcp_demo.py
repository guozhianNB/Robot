# -*- coding: utf-8 -*-
r"""MCP 车控工具冒烟自测（独立脚本，下划线前缀 → 不会被 tools.py 自动加载）。

用法（项目根目录，后端环境需装有 python-mcp；ROS2 已 source）:
    .venv\Scripts\python.exe -m LLM.tool._car_mcp_demo        # Windows/RDK
    python3 -m LLM.tool._car_mcp_demo                         # Linux VM

它会：
  1. 临时向 conf.MCP_SERVERS 注册 "car"（指向 LLM/car_mcp/car_server.py）；
  2. 走 mcp_client.start() 拉起并注册车控工具；
  3. 依次调用 robot_status / robot_move(forward,0.5) 打印结果（需模拟底盘或真底盘在跑）；
  4. 清理（stop）。

真实部署请把该服务器配置写进 conf.py 的 MCP_SERVERS（而不是这里临时注入），
并把 command/args 指向目标机装有 mcp+rclpy 的解释器。
"""
from .. import conf
from .. import mcp_client
import sys

_SCRIPT = str(conf.BASE_DIR / "LLM" / "car_mcp" / "car_server.py")
_PY = "python" if sys.platform == "win32" else "python3"   # 平台自适应解释器名


def main():
    # 1. 临时注册 car MCP 服务器（仅本脚本进程内生效）
    conf.MCP_SERVERS["car"] = {
        "command": _PY,          # Windows: python；Linux: python3
        "args": [_SCRIPT],
        "enabled": True,
    }

    print("mcp 依赖可用:", mcp_client.available())
    schemas = mcp_client.start({"mcp_enabled": True})
    if not schemas:
        print("启动失败/无工具，状态:", mcp_client.status())
        return
    print("已注册 MCP 工具:", [s["function"]["name"] for s in schemas])

    # 2. 逐个调用
    for name, args in [
        ("robot_status", {}),
        ("robot_move", {"direction": "forward", "distance_m": 0.5}),
        ("robot_status", {}),
    ]:
        if name not in {s["function"]["name"] for s in schemas}:
            print(f"[skip] 工具 {name} 未注册")
            continue
        print(f"\n调用 {name}{args if args else ''} ...")
        r = mcp_client.call_tool(name, args)
        print("->", r.get("result") or r.get("message") or r)

    # 3. 清理
    mcp_client.stop()
    print("\n已关闭 MCP 会话")


if __name__ == "__main__":
    main()
