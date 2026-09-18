# 车控最终审查修复报告

## 修复范围

- 导航目标解析对 tags、地图元数据和点校验的 `stale` 信号统一 fail-closed。
- 动作发送后若传输失败或超时，任务进入 uncertain 状态并继续占用 `current`；只有权威 active 后的 idle/error 或急停清理任务。
- `robot_status` 在基础车体状态上补充当前地图和最小包含区域；地图链路异常只降级地图字段。
- 任务登记保留 `move_forward/back/left/right`、`turn`、`goto_point/zone/place` 用户动作身份，ROS 调用独立使用 `move`、`turn`、`navigate_to`。
- 所有新增或更新的 `last` 记录包含统一时间字段 `at`，并保留 `finished_at` 兼容字段。

## TDD 证据

实现前新增回归测试得到 8 个预期失败，分别覆盖 stale 输入、动作身份、uncertain 占用、状态地图上下文和最小区域选择；完成最小实现后相关测试转绿。

## 最终验证

```text
python -m pytest LLM/tests/test_car_mcp.py LLM/tests/test_maptags_goal.py LLM/tests/test_policy_roles.py LLM/tests/test_policy_tools.py -q
146 passed
```
