# LLM/car_mcp —— 高层「小车移动」MCP 车控

让 **LLM 能控制小车移动** 的可选能力模块。目标是 **RDK X5 真机**，VM 用模拟底盘数据自测，
**高层语义动作（move/turn，到位自动停）实现在本层**。全部为**新增文件**，不改动任何既有源。

```
LLM/car_mcp/
  car_controller.py     ★ 高层控制核心（纯 rclpy，不含 MCP；move/turn/stop/status）
  car_server.py          MCP 2.0 服务端（stdio 子进程）—— 把控制器包成 MCP 工具
  odom_sim_driver.py     VM 用模拟底盘（镜像真机 chassis_driver 语义，出 /odom）
  car_cli_test.py        VM 端到端自测（无需 python-mcp，只需 rclpy + 模拟底盘）
  README.md             本文件
LLM/tool/_car_mcp_demo.py   冒烟：走真·MCP 管道调用车控工具（需 python-mcp）
```

---

## 一、它解决什么 & 为什么"控制器在这个目录"

真机 `ros2_car/.../chassis_driver.py` 把 `/cmd_vel` 当**持续速度保持**（自身没有
"走 x 米后停"）。所以"走 1m / 转 90°"这种**有终点**的语义动作，判定与停止逻辑必须
额外做——本目录的 `car_controller.py` 就是这个控制器：循环 publish `/cmd_vel`、读
`/odom` 判到位、到位自动发全零/急停。**这是"在高层 MCP 侧做工具"的落点。**

## 二、通信/话题（真机与模拟同一套，代码零改动）

| 话题 | 类型 | 方向 | 用途 |
|---|---|---|---|
| `/cmd_vel` | `geometry_msgs/Twist` | 发布↓ | 下发运动速度（持续） |
| `/robot/cmd_stop` | `std_msgs/Bool` | 发布↓ | 急停（一收即停） |
| `/odom` | `nav_msgs/Odometry` | 订阅↑ | 读实际位姿/位移，判到位 |

- **真机 RDK X5**：单独起真 `chassis_driver`（串口→STM32）→ 喂 `/cmd_vel`、产 `/odom`。
- **VM 模拟**：起本目录 `odom_sim_driver.py` 当"假底盘"产 `/odom` → 同一份控制器自测。

## 三、工具（LLM 可见）

| 工具 | 参数 | 说明 | 护栏 |
|---|---|---|---|
| `robot_move` | `direction`(forward/back/left/right) + `distance_m` | 直线/横移，到位自动停 | 单次 ≤5m |
| `robot_turn` | `angle_deg`(正=左, 负=右) | 原地转向，到位自动停 | 单次 ≤360° |
| `robot_stop` | — | 立即急停（老人喊停/异常必调） | — |
| `robot_status` | — | 读位姿/是否在动（先查再动） | — |

默认运动参数在 `car_controller.py` 顶部常量（直行 0.2 m/s、横移 0.15 m/s、转弯 20°/s）。

## 四、VM 自测（用底盘数据，无需 python-mcp）

要先在本机 source ROS2：`source /opt/ros/jazzy/setup.bash`

```bash
# 终端 A：起模拟底盘（产生 /odom 底盘数据）
python3 LLM/car_mcp/odom_sim_driver.py
# 另一终端 B：验证底盘数据
ros2 topic echo /odom

# 终端 C：驱动控制器（高层动作，到位自动停）
python3 LLM/car_mcp/car_cli_test.py status
python3 LLM/car_mcp/car_cli_test.py move forward 0.5     # 前移 0.5m 自动停，moved_m≈0.5
python3 LLM/car_mcp/car_cli_test.py turn 90               # 原地左转 90°，turned_deg≈90
# 运动中想急停：再开一个终端
python3 LLM/car_mcp/car_cli_test.py stop
```

## 五、走真·MCP 管道的冒烟（若后端环境装了 python-mcp）

```bash
python3 -m LLM.tool._car_mcp_demo      # Linux（需模拟/真底盘已在跑）
```
Windows / RDK X5 用 `.venv\Scripts\python.exe -m LLM.tool._car_mcp_demo`。

## 六、接入 LLM 后端（正式启用）

在 `LLM/conf.py::MCP_SERVERS` 加一条（**唯一要改的既有文件，仅加一个 dict、默认关**）：
```python
MCP_SERVERS = {
    # ...现有 fetch / tavily ...
    "car": {
        "command": "<目标机装有 mcp+rclpy 的解释器，如 python3>",
        "args": ["/home/aa/Robot/LLM/car_mcp/car_server.py"],   # 绝对路径
        "enabled": False,          # 默认关；需真车/模拟底盘就绪且想启用时再开
    },
}
```
然后后端设置页打开 `mcp_enabled`（改后重启后端生效）→ 前端 `/api/tools` 自动出现
robot_move / robot_turn / robot_stop / robot_status。

> 别的都不用改：`mcp_client` 会自动拉起、转 schema、`tools.run_tool` 自动分发、前端自动显示。

## 七、注意

- MCP 服务走 **stdio**，`car_server.py` 业务日志写到 `LLM/car_mcp/car_mcp.log`，**绝不 print 到 stdout**。
- 每个工具返回 **JSON 字符串**（`mcp_client.call_tool` 只读文本块，dict 会读不到）。
- `car_controller` 内 rclpy 节点跑在**独立后台线程**（MultiThreadedExecutor），move/turn 在调用线程
  循环 publish + 读后台刷新的 `/odom`，`robot_stop` 可随时从别的线程打断。
- 真机接入前请核对真 `chassis_driver` 的串口/限速/看门狗参数；本模块已按真机保守限速。
