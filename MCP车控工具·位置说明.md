# 📍 「LLM 控制小车移动」MCP 车控 —— 所处位置标注

> 需要修改/查看时，从这里往前走。全部为**新增文件，未改动任何既有源**。

## 新代码都在这里
- **主目录：`LLM/car_mcp/`**（高层车控 MCP 模块）
  - `car_controller.py` —— ★ 高层控制核心（纯 rclpy：move/turn/stop/status，到位自动停）
  - `car_server.py` —— MCP 2.0 服务端（stdio 子进程，包成 LLM 可见的工具）
  - `odom_sim_driver.py` —— VM 模拟底盘（镜像真机底盘语义，产 `/odom`）
  - `car_cli_test.py` —— VM 端到端自测（无需 python-mcp）
  - `README.md` —— 详细说明（跑法/接入/注意）
- **冒烟脚本：`LLM/tool/_car_mcp_demo.py`**（走真实 MCP 管道调用）

## 唯一改动的既有文件
- `LLM/conf.py::MCP_SERVERS` —— 需加一条 `"car"` 配置才能真正接入后端
  （当前**尚未添加**，属"待你决定后的一处配置"，默认关、不加也行；其余后端全部自动兼容）。

## 目标部署
- **RDK X5 真机**：起真 `chassis_driver`（README §2），跑 `car_server.py` 驱动真车。
- **VM 模拟**：terminal A 起 `odom_sim_driver.py`，terminal B 跑 `car_cli_test.py` 用底盘数据自测。

## 快速上手（一句话）
```bash
source /opt/ros/jazzy/setup.bash
python3 LLM/car_mcp/odom_sim_driver.py      # 终端 A：模拟底盘
python3 LLM/car_mcp/car_cli_test.py move forward 0.5   # 终端 B：前移0.5m自动停
```
详见 `LLM/car_mcp/README.md`。
