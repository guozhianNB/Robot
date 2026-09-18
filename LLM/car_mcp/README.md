# 车控 MCP

本目录提供七个车控工具：`robot_move`、`robot_turn`、`robot_goto_point`、
`robot_goto_zone`、`robot_goto_place`、`robot_stop`、`robot_status`。

## 生产路径

生产环境的后端运行在 PC，链路固定为：

```text
LLM 工具循环
  -> PC: car_server.py（stdio MCP，无 rclpy）
  -> PC: car_link.py（websocket-client，四条独立连接）
  -> 板卡 rosbridge :9090
  -> 板卡 robot_actions
  -> Nav2 或 chassis_driver -> STM32
```

`LLM/conf.py` 已登记 `MCP_SERVERS["car"]`，默认 `enabled=True`，但只有设置
`mcp_enabled=True` 时 MCP 管理器才会拉起子进程。子进程使用后端当前 Python 解释器，避免
另一个解释器缺少 `mcp` 或 `websocket-client`。

板卡需要提供 `/robot/readiness`、三个动作服务、`/robot/exec_state` 1 Hz 心跳、
`/robot/arrived`、`/amcl_pose` 和 `/robot/cmd_stop`。`car_link.py` 使用四条独立 websocket：

| 连接 | 用途 |
|---|---|
| `ctrl` | 串行调用动作服务，允许长时间阻塞 |
| `stop` | 独立发布急停，绕过 busy/readiness |
| `state` | 后台接收执行状态、到达状态和 AMCL 位姿 |
| `probe` | 快速调用 readiness，不与 `ctrl` 共用读循环 |

五个动作工具在参数、安全校验和 readiness 通过后立即返回 `status="started"`，后台线程继续
等待板卡结果。`started` 只代表已受理，不代表到达；完成情况用 `robot_status` 查询。

## 配置

主后端通过 `_car_mcp_env()` 显式传递下列环境变量：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `ROSBRIDGE_URL` | `ws://<MAPS_SSH_HOST>:9090` | 板卡 rosbridge 地址，始终传递 |
| `CAR_PROBE_TIMEOUT_S` | `3` | readiness 调用超时 |
| `CAR_ACTION_TIMEOUT_S` | `190` | 动作服务最长等待时间 |
| `CAR_HEARTBEAT_TTL_S` | `3` | 执行状态心跳新鲜度 |
| `CAR_MCP_LOG` | `LLM/car_mcp/car_mcp.log` | MCP 子进程日志文件 |

后三个超时项与日志路径只在环境中实际设置时覆盖默认值。所有时间值必须为正有限秒数，非法值
退回默认值。

## 降级行为

- 可选依赖缺失、rosbridge 断开、readiness 不存在或 Nav2 未启动时，后端继续运行；
- 动作工具返回 `ok:false` 和真实原因，不登记任务，也不假装已发车；
- `robot_status` 在通道不可用时返回 `ok:true, status:"unavailable"`；
- 地图或 tags 无法安全校验时，三个 goto 工具 fail-closed；move/turn/stop/status 不受影响；
- `python-mcp` 缺失时子进程向 stderr 写原因并以退出码 2 退出。

MCP 使用 stdio 协议，禁止向 stdout 打印业务日志。`car_server.py` 只写日志文件或 stderr，所有工具
都返回 JSON 字符串。

## PC 无板卡测试

在仓库根目录运行：

```powershell
.venv\Scripts\python.exe -m pytest LLM/tests/test_car_mcp.py -q
.venv\Scripts\python.exe -m pytest LLM/tests/test_maptags_goal.py -q
```

测试使用假 websocket 和假地图存储，不访问真实板卡。

## 板卡或 VM 自测路径

`car_controller.py`、`odom_sim_driver.py`、`car_cli_test.py` 是板卡本地或 ROS2 VM 自测工具，
依赖 `rclpy`，不进入生产 MCP 导入链，也不要与板卡 `robot_actions` 同时争用 `/cmd_vel`。

```bash
source /opt/ros/jazzy/setup.bash
python3 LLM/car_mcp/odom_sim_driver.py

# 另一个已 source ROS2 的终端
python3 LLM/car_mcp/car_cli_test.py status
python3 LLM/car_mcp/car_cli_test.py move forward 0.5
python3 LLM/car_mcp/car_cli_test.py turn 90
python3 LLM/car_mcp/car_cli_test.py stop
```

这条路径只验证本地控制器与模拟 `/odom`，不等价于生产的 rosbridge/readiness/Nav2 链路。

## 真机验收

真机测试前必须清空车周围空间，按顺序验证 readiness、0.3 m 前进、30 度转向、开阔点导航、
区域停靠点导航、导航中急停，以及断开 rosbridge 后的诚实失败。任一步失败应立即停止，不继续后续
运动测试。仓库内自动化测试不代表这些现场项目已通过。
