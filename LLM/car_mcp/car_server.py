#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""高层「小车移动」MCP 服务端（子进程, stdio 传输）。

它把高层车载控制器（car_controller.CarController）包成 MCP 2.0 工具，供 LLM 后端
（LLM/mcp_client.py）在启动时以 stdio 子进程拉起，工具名自动并入 OpenAI function-
calling 工具循环 —— 大模型据此可让小车"前进/后退/横移 x 米、转向 x 度、急停、查状态"。

对本机并无 python-mcp 依赖时**也能起**（降级成一行提示并退出非零）——遵循
AGENTS「系统稳健性」：可选能力缺失只降级、不污染/崩后端。

工具一览（高层语义、到位自动停，实现见 car_controller.py）：
  robot_move(direction=forward|back|left|right, distance_m)  直线/横移, 到位自动停
  robot_turn(angle_deg)                                      原地转向, 到位自动停
  robot_stop()                                               立即急停
  robot_status()                                             读位姿/是否运动中(供先查再动)

配置：本文件顶层常量即为默认（速度/上限/话题名）。在 RDK X5 上跑真底盘时，
请确认 conf.py 里 conf.MCP_SERVERS["car"] 的 command/args 指向本机已 source ROS2
且装有 python-mcp 的解释器。日志写到 stdout 之外(见下方 helper)以免污染 MCP 协议。

用法（本地直接起，便于单独调试）::

    python3 LLM/car_mcp/car_server.py
"""
import json
import os
import sys
import time

# 让后端直接 import 本目录包时路径正确（也便于从项目根直接跑）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from car_controller import CarController          # 传输无关的核心控制器

# --------------------------------------------------------------------------
# python-mcp 依赖层（可选能力降级）
_INIT_ERR = None
try:
    from mcp.server.mcpserver import MCPServer
except Exception as e:                            # 未装 / 版本不兼容 → 降级
    MCPServer = None
    _INIT_ERR = str(e)

# 日志落盘（MCP 走 stdio，绝不能 print 到 stdout 污染协议）
_LOG_PATH = os.environ.get("CAR_MCP_LOG", os.path.join(_HERE, "car_mcp.log"))


def _log(msg: str):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 单例控制器（进程内一个节点，工具调用共用）
_ctrl = CarController(
    cmd_vel_topic=os.environ.get("CAR_CMD_VEL_TOPIC", "/cmd_vel"),
    cmd_stop_topic=os.environ.get("CAR_CMD_STOP_TOPIC", "/robot/cmd_stop"),
    odom_topic=os.environ.get("CAR_ODOM_TOPIC", "/odom"),
)


# --------------------------------------------------------------------------
# 工具实现（返回 JSON **字符串**）
# ⚠️ 原因：LLM/mcp_client.call_tool 只读取 MCP 结果的"文本块"(content[*].text)，
# 并把它们拼成字符串返回给工具循环。所以 MCP server 的工具必须 return str
# （SDK 会把它做成 TextContent），而不是 dict（dict 会成为无 .text 的结构化块，
# 上游会读到"（MCP 工具无文本返回）"）。每个工具把控制器的 dict json 化后返回。
def _move(direction: str, distance_m: float) -> str:
    try:
        return json.dumps(_ctrl.robot_move(direction, distance_m), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"robot_move 异常: {e}"}, ensure_ascii=False)


def _turn(angle_deg: float) -> str:
    try:
        return json.dumps(_ctrl.robot_turn(angle_deg), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"robot_turn 异常: {e}"}, ensure_ascii=False)


def _stop() -> str:
    try:
        return json.dumps(_ctrl.robot_stop(), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"robot_stop 异常: {e}"}, ensure_ascii=False)


def _status() -> str:
    try:
        return json.dumps(_ctrl.robot_status(), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"robot_status 异常: {e}"}, ensure_ascii=False)


# --------------------------------------------------------------------------
# 注册与主入口
def build_server():
    if MCPServer is None:
        return None
    server = MCPServer("robot-car")

    @server.tool()
    def robot_move(direction: str, distance_m: float) -> str:
        """让小车朝 direction(forward/back/left/right) 移动 distance_m 米，到位自动停。

        面对不确定不要瞎指挥：可先调 robot_status 看是否空闲在动。direction:
        forward=前进, back=后退, left=向左横移, right=向右横移。distance_m 必须>0，
        单次上限见安全护栏(默认5m)。返回 JSON 字符串。"""
        return _move(direction, distance_m)

    @server.tool()
    def robot_turn(angle_deg: float) -> str:
        """让小车原地旋转 angle_deg 度后自动停。正=左转，负=右转（例 90=左转90°, -90=右转）。返回 JSON 字符串。"""
        return _turn(angle_deg)

    @server.tool()
    def robot_stop() -> str:
        """立即让小车急停（发 /robot/cmd_stop + 全零速度）。老人喊停/异常时务必调用。返回 JSON 字符串。"""
        return _stop()

    @server.tool()
    def robot_status() -> str:
        """查询小车当前状态：是否正在运动、位姿(x,y,yaw)。模型下指令前建议先查。返回 JSON 字符串。"""
        return _status()

    return server


def main():
    if MCPServer is None:
        _log(f"[fatal] python-mcp 不可用: {_INIT_ERR}")
        sys.stderr.write("python-mcp 未安装，robot-car MCP 服务无法启动 "
                         f"(缺失: {_INIT_ERR})；请在后端环境安装 mcp。\n")
        sys.exit(2)                       # 明显失败码，便于 mcp_client 判别降级

    if not _ctrl.ros_available:
        _log(f"[fatal] rclpy 不可用: {_ctrl.missing}")
        sys.stderr.write("rclpy 未安装，robot-car 无法连 ROS2。降级运行：仅角色口 mock。\n")
        sys.exit(2)

    if not _ctrl.start():
        _log("[fatal] ROS 节点启动失败（未起 rclpy? 未 source ROS2?）")
        sys.stderr.write("CarController 启动失败，robot-car 退出。\n")
        sys.exit(2)

    server = build_server()
    if server is None:
        sys.exit(2)
    _log("robot-car MCP server 就绪")
    try:
        server.run()                       # transport="stdio" 默认
    finally:
        _ctrl.shutdown()
        _log("robot-car MCP server 退出")


if __name__ == "__main__":
    main()
