# ROS2 基础教程

> 面向零基础新手，目标：理解 ROS2 的核心概念，能搭建开发环境，编写节点进行通信，最终让仿真机器人动起来。

---

## 目录

1. [ROS2 是什么？](#1-ros2-是什么)
2. [ROS2 安装](#2-ros2-安装)
3. [核心概念速览](#3-核心概念速览)
4. [工作空间与包](#4-工作空间与包)
5. [第一个节点 —— 发布者与订阅者](#5-第一个节点--发布者与订阅者)
6. [第一个服务 —— 客户端与服务器](#6-第一个服务--客户端与服务器)
7. [自定义接口（msg / srv）](#7-自定义接口msg--srv)
8. [Launch 文件](#8-launch-文件)
9. [参数](#9-参数)
10. [Rviz 可视化](#10-rviz-可视化)
11. [TF 坐标变换](#11-tf-坐标变换)
12. [URDF 机器人模型](#12-urdf-机器人模型)
13. [Gazebo 仿真入门](#13-gazebo-仿真入门)
14. [SLAM 与导航栈速览](#14-slam-与导航栈速览)
15. [常见报错与排坑](#15-常见报错与排坑)

---

## 1. ROS2 是什么？

### 1.1 一句话理解

> **ROS2（Robot Operating System 2）** 不是真正的"操作系统"，而是一个**机器人分布式通信框架**——它让机器人的各个部件（摄像头、激光雷达、电机、AI 算法）能互相收发消息、协同工作。

### 1.2 为什么需要 ROS2？

假设你要做一个巡逻机器人：

| 部件 | 功能 | 用什么语言写 |
|------|------|-------------|
| 激光雷达驱动 | 读取雷达数据 | C++ |
| 导航算法 | 路径规划 | C++ |
| 视觉模块 | YOLO 检测 | Python |
| 语音模块 | 对话交互 | Python |
| 底盘控制 | 电机驱动 | C++ |

如果没有 ROS2，你需要自己写一大堆代码让这些不同语言写的程序互相通信。ROS2 帮你把这件事标准化了——**每个程序变成一个"节点(Node)"，通过"话题(Topic)"或"服务(Service)"互相收发数据**。

### 1.3 ROS2 vs ROS1

| 对比项 | ROS1 | ROS2 |
|--------|------|------|
| 底层通信 | 自定义协议（TCPROS/UDPROS） | **DDS**（工业级通信标准） |
| 实时性 | ❌ 不支持 | ✅ 支持硬实时 |
| 多机器人 | ❌ 麻烦 | ✅ 原生支持 |
| 跨平台 | Linux 为主 | Linux / Windows / macOS |
| 安全性 | ❌ 无 | ✅ DDS 安全机制 |
| 寿命 | 维护模式（2025 年 EOL） | **当前和未来** |

> **💡 建议：** 直接学 ROS2，不要再碰 ROS1。你的项目用 ROS2 Humble 或 Jazzy 版本。

### 1.4 发行版选择

| 发行版 | Ubuntu 版本 | 推荐？ |
|--------|------------|--------|
| **Humble Hawksbill** | Ubuntu 22.04 | ✅ **最稳定，推荐新手** |
| Iron Irwini | Ubuntu 22.04 | ⚠️ 已停止维护 |
| **Jazzy Jalisco** | Ubuntu 24.04 | ✅ 较新，长期支持 |
| Rolling Ridley | 滚动更新 | ❌ 不稳定 |

> 如果你用 Ubuntu 22.04 → 装 **Humble**（教程全部以 Humble 为例）
> 如果你用 Ubuntu 24.04 → 装 **Jazzy**

---

## 2. ROS2 安装

### 2.1 安装 Ubuntu（重要）

> ROS2 **最好在 Linux 上运行**。如果你只有 Windows，有两种方案：
> - **方案 A（推荐）：** 装双系统 / 虚拟机（VMware/VirtualBox）安装 Ubuntu 22.04
> - **方案 B：** Windows 下装 WSL2（适用于 Linux 的 Windows 子系统）

本教程假设你在 **Ubuntu 22.04 + ROS2 Humble** 环境下操作。

### 2.2 安装 ROS2 Humble

打开终端，逐条执行以下命令：

```bash
# 1. 设置编码
sudo apt update && sudo apt install locales
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8

# 2. 添加 ROS2 源
sudo apt install software-properties-common
sudo add-apt-repository universe
sudo apt update && sudo apt install curl -y
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /usr/share/keyrings/ros-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" | sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null

# 3. 安装 ROS2
sudo apt update
sudo apt upgrade

# 桌面版安装（含可视化工具，推荐）
sudo apt install ros-humble-desktop

# 4. 安装 colcon 构建工具
sudo apt install python3-colcon-common-extensions

# 5. 环境配置（加到 ~/.bashrc 就不用每次都 source）
echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc
source ~/.bashrc
```

### 2.3 验证安装

```bash
# 打开一个新终端，输入：
ros2 --version

# 能看到版本号，说明安装成功
# 例如：ros2 humble
```

### 2.4 测试小乌龟

```bash
# 终端1：启动 turtlesim 仿真器
ros2 run turtlesim turtlesim_node

# 终端2：用键盘控制小乌龟
ros2 run turtlesim turtle_teleop_key
```

> 如果你看到一个蓝色窗口里有一只小乌龟，可以用键盘方向键控制它移动——恭喜，ROS2 装好了！

---

## 3. 核心概念速览

在动手写代码之前，先理解 ROS2 的四个核心概念。

### 3.1 节点（Node）

> **一个节点就是一个独立的可执行程序**，负责完成某一项具体任务。

- 比如：`camera_node` 负责读取摄像头，`lidar_node` 负责读取雷达，`nav_node` 负责路径规划
- 一个机器人系统通常由几十个节点组成
- 节点之间通过**话题**或**服务**通信

### 3.2 话题（Topic）

> **话题是"发布-订阅"式的单向数据流**，适合连续数据流。

```
发布者 ──(发布数据)──→ 话题名称 ──(订阅)──→ 订阅者
```

| 现实类比 | 电台广播 |
|---------|---------|
| 发布者 = 电台主播 | 一直在说话，不管有没有人听 |
| 订阅者 = 收音机 | 想听就打开，可以有很多台 |
| 话题 = 调频频率（FM 103.7） | 主播和听众约好一个频率 |

**适合场景：** 激光雷达数据、摄像头画面、里程计数据——都是连续不断发送的。

### 3.3 服务（Service）

> **服务是"请求-响应"式的双向通信**，适合一次性任务。

```
客户端 ──(发送请求)──→ 服务端
客户端 ←──(返回响应)── 服务端
```

| 现实类比 | 打电话问客服 |
|---------|------------|
| 客户端 = 打电话的人 | 问了问题，等回答 |
| 服务端 = 客服 | 收到问题，处理，回答 |
| 服务 = "查询余额" | 一个特定的功能 |

**适合场景：** 查询某个状态、触发某个动作（如"拍照保存"、"开始建图"）。

### 3.4 动作（Action）

> **动作是"带反馈的服务"**，适合耗时的任务，执行过程中能报告进度。

```
客户端 ──(发送目标)──→ 动作服务器
客户端 ←──(进度反馈)── 动作服务器（持续发送）
客户端 ←──(最终结果)── 动作服务器（完成时发送）
```

**适合场景：** 导航到某个点（需要一路报告"走到哪了"）、机械臂抓取。

### 3.5 概念对比表

| | 话题（Topic） | 服务（Service） | 动作（Action） |
|--|-------------|----------------|---------------|
| 通信方式 | 发布-订阅 | 请求-响应 | 目标-反馈-结果 |
| 数据流向 | 单向 | 双向一次性 | 双向持续 |
| 是否阻塞 | 非阻塞 | 阻塞等待 | 非阻塞，可取消 |
| 典型场景 | 传感器数据 | 查询/触发 | 导航/抓取 |
| 类比 | 电台广播 | 打电话问客服 | 叫外卖（实时跟踪） |

### 3.6 命令行工具速查

```bash
# 查看所有正在运行的节点
ros2 node list

# 查看节点信息
ros2 node info /节点名

# 查看所有话题
ros2 topic list

# 查看话题类型
ros2 topic type /话题名

# 实时查看话题数据
ros2 topic echo /话题名

# 手动发布消息到话题
ros2 topic pub /话题名 消息类型 "{data: 值}"

# 查看所有服务
ros2 service list

# 手动调用服务
ros2 service call /服务名 服务类型 "{参数}"
```

---

## 4. 工作空间与包

### 4.1 工作空间结构

ROS2 的代码组织方式是：**工作空间（Workspace） → 包（Package）**

```
ros2_ws/                    ← 工作空间根目录（可以随便取名）
├── src/                    ← 源码目录（放你写的代码）
│   ├── my_package/         ← 一个功能包
│   │   ├── package.xml     ← 包的元信息（名称、依赖）
│   │   ├── setup.py        ← Python 包的构建配置
│   │   ├── my_package/     ← Python 模块目录
│   │   │   └── node.py
│   │   └── launch/         ← 启动文件
│   └── another_package/    ← 另一个功能包
├── build/                  ← 编译中间文件（自动生成）
├── install/                ← 安装文件（自动生成）
└── log/                    ← 日志文件（自动生成）
```

### 4.2 创建工作空间

```bash
# 创建一个工作空间
mkdir -p ~/ros2_ws/src
cd ~/ros2_ws

# 编译（现在还是空的，只是初始化 build/install/log 目录）
colcon build
```

### 4.3 创建包

有两种方式写 ROS2 节点：**Python**（简单，适合新手）和 **C++**（性能好）。

本教程**全部用 Python**，因为：
- 你的项目主要用 Python（YOLO、语音、大模型）
- Python 代码量更少，更适合学习概念
- 和 C++ 节点可以混用，不影响

```bash
# 进入工作空间的 src 目录
cd ~/ros2_ws/src

# 创建 Python 包
ros2 pkg create my_robot_pkg \
  --build-type ament_python \
  --dependencies rclpy std_msgs geometry_msgs

# 参数说明：
#   my_robot_pkg          ← 包名（全部小写+下划线）
#   --build-type          ← 构建类型（Python 用 ament_python）
#   --dependencies        ← 依赖的库
#     rclpy                = ROS2 Python 客户端库（必选）
#     std_msgs             = 标准消息类型
#     geometry_msgs        = 几何消息类型（位置、速度等）
```

### 4.4 编译与运行

```bash
# 每次写了新代码都要编译
cd ~/ros2_ws
colcon build

# 如果只想编译某个包，加快速度
colcon build --packages-select my_robot_pkg

# 激活工作空间环境（让系统能找到你的包）
source install/setup.bash

# 运行节点
ros2 run my_robot_pkg my_node
```

> **💡 小技巧：** 把 `source ~/ros2_ws/install/setup.bash` 也加到 `~/.bashrc`，这样每次打开终端自动加载工作空间。

---

## 5. 第一个节点 —— 发布者与订阅者

我们写一个**发布者节点**，每秒发送一次"Hello ROS2"消息；再写一个**订阅者节点**，收到后打印出来。

### 5.1 发布者（Publisher）

创建文件 `~/ros2_ws/src/my_robot_pkg/my_robot_pkg/publisher_node.py`：

```python
#!/usr/bin/env python3
import rclpy                        # ROS2 Python 客户端库
from rclpy.node import Node         # 节点基类
from std_msgs.msg import String     # 字符串消息类型


class MyPublisher(Node):
    """一个简单的发布者节点，每秒发布一次 "Hello ROS2" 消息"""

    def __init__(self):
        # 节点名称（ros2 node list 中看到的）
        super().__init__('my_publisher')

        # 创建发布者
        #   参数1: 消息类型     → String
        #   参数2: 话题名称     → 'my_topic'
        #   参数3: 队列大小     → 10（暂存 10 条消息，满了丢弃最老的）
        self.publisher_ = self.create_publisher(String, 'my_topic', 10)

        # 创建定时器，每秒调用一次 timer_callback
        self.timer = self.create_timer(1.0, self.timer_callback)

        self.get_logger().info('🚀 发布者节点已启动！')

    def timer_callback(self):
        """定时器回调函数——每秒执行一次"""
        msg = String()
        msg.data = 'Hello ROS2!'
        self.publisher_.publish(msg)
        self.get_logger().info(f'发布: {msg.data}')


def main(args=None):
    rclpy.init(args=args)           # 初始化 ROS2
    node = MyPublisher()            # 创建节点
    rclpy.spin(node)                # 保持节点运行（一直阻塞，直到按 Ctrl+C）
    node.destroy_node()             # 销毁节点
    rclpy.shutdown()                # 关闭 ROS2


if __name__ == '__main__':
    main()
```

### 5.2 订阅者（Subscriber）

创建文件 `~/ros2_ws/src/my_robot_pkg/my_robot_pkg/subscriber_node.py`：

```python
#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class MySubscriber(Node):
    """一个简单的订阅者节点，收到消息就打印"""

    def __init__(self):
        super().__init__('my_subscriber')

        # 创建订阅者
        #   参数1: 消息类型   → String
        #   参数2: 话题名称   → 'my_topic'（必须和发布者一致）
        #   参数3: 回调函数   → 收到消息时自动调用
        #   参数4: 队列大小   → 10
        self.subscription = self.create_subscription(
            String,
            'my_topic',
            self.listener_callback,
            10
        )

        self.get_logger().info('👂 订阅者节点已启动！')

    def listener_callback(self, msg):
        """收到消息时自动调用"""
        self.get_logger().info(f'收到: {msg.data}')


def main(args=None):
    rclpy.init(args=args)
    node = MySubscriber()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()
```

### 5.3 配置 setup.py

编辑 `~/ros2_ws/src/my_robot_pkg/setup.py`，在 `entry_points` 中添加：

```python
entry_points={
    'console_scripts': [
        'publisher = my_robot_pkg.publisher_node:main',
        'subscriber = my_robot_pkg.subscriber_node:main',
    ],
},
```

### 5.4 运行测试

```bash
cd ~/ros2_ws
colcon build --packages-select my_robot_pkg
source install/setup.bash

# 终端1：运行发布者
ros2 run my_robot_pkg publisher

# 终端2：运行订阅者
ros2 run my_robot_pkg subscriber
```

**预期效果：** 发布者终端每秒打印一次"发布: Hello ROS2!"，订阅者终端同步打印"收到: Hello ROS2!"。

```bash
# 也可以直接用命令行验证
ros2 topic list                    # 应该看到 /my_topic
ros2 topic echo /my_topic          # 实时显示消息内容
ros2 topic info /my_topic          # 查看发布者和订阅者数量
ros2 node list                     # 查看所有节点
```

---

## 6. 第一个服务 —— 客户端与服务器

### 6.1 服务端（Service Server）

创建一个**加法计算器**：客户端发两个数字，服务端返回它们的和。

创建文件 `~/ros2_ws/src/my_robot_pkg/my_robot_pkg/service_server.py`：

```python
#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from example_interfaces.srv import AddTwoInts   # ROS2 自带的加法服务接口


class AddService(Node):
    """加法服务端：收到两个整数，返回它们的和"""

    def __init__(self):
        super().__init__('add_service')

        # 创建服务
        #   参数1: 服务类型 → AddTwoInts
        #   参数2: 服务名称 → 'add_two_ints'
        #   参数3: 回调函数 → 收到请求时调用
        self.srv = self.create_service(AddTwoInts, 'add_two_ints', self.add_callback)

        self.get_logger().info('➕ 加法服务已启动，等待请求...')

    def add_callback(self, request, response):
        """收到客户端请求时调用"""
        response.sum = request.a + request.b
        self.get_logger().info(f'收到请求: {request.a} + {request.b} = {response.sum}')
        return response


def main(args=None):
    rclpy.init(args=args)
    node = AddService()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()
```

### 6.2 客户端（Service Client）

创建文件 `~/ros2_ws/src/my_robot_pkg/my_robot_pkg/service_client.py`：

```python
#!/usr/bin/env python3
import sys
import rclpy
from rclpy.node import Node
from example_interfaces.srv import AddTwoInts


class AddClient(Node):
    """加法客户端：发送两个数字，等待计算结果"""

    def __init__(self, a, b):
        super().__init__('add_client')

        # 创建客户端
        self.client = self.create_client(AddTwoInts, 'add_two_ints')

        # 等待服务端就绪（最多等 1 秒）
        while not self.client.wait_for_service(timeout_sec=1.0):
            self.get_logger().warn('等待服务端启动...')

        # 构造请求
        request = AddTwoInts.Request()
        request.a = a
        request.b = b

        # 异步调用（不阻塞），并设置一个回调处理结果
        self.future = self.client.call_async(request)
        self.future.add_done_callback(self.response_callback)

    def response_callback(self, future):
        """收到服务端响应时调用"""
        try:
            response = future.result()
            self.get_logger().info(f'计算结果: {response.sum}')
        except Exception as e:
            self.get_logger().error(f'服务调用失败: {e}')
        rclpy.shutdown()  # 收到响应后退出


def main(args=None):
    # 从命令行参数读取 a 和 b，默认 2 和 3
    a = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    b = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    rclpy.init()
    node = AddClient(a, b)
    rclpy.spin(node)
```

### 6.3 配置 setup.py

编辑 `setup.py`，在 `entry_points` 中添加：

```python
entry_points={
    'console_scripts': [
        'publisher = my_robot_pkg.publisher_node:main',
        'subscriber = my_robot_pkg.subscriber_node:main',
        'add_server = my_robot_pkg.service_server:main',
        'add_client = my_robot_pkg.service_client:main',
    ],
},
```

### 6.4 运行测试

```bash
cd ~/ros2_ws
colcon build --packages-select my_robot_pkg
source install/setup.bash

# 终端1：启动服务端
ros2 run my_robot_pkg add_server

# 终端2：启动客户端（计算 10 + 20）
ros2 run my_robot_pkg add_client 10 20

# 也可以用命令行调用
ros2 service call /add_two_ints example_interfaces/srv/AddTwoInts "{a: 5, b: 7}"
```

---

## 7. 自定义接口（msg / srv）

ROS2 自带的接口不够用时，你可以**自己定义消息类型**。

### 7.1 创建接口包

> 最佳实践：把接口放在一个单独的包中，可以被多个包共用。

```bash
cd ~/ros2_ws/src
ros2 pkg create my_interfaces --build-type ament_cmake --dependencies std_msgs
```

### 7.2 自定义消息（.msg）

创建文件 `~/ros2_ws/src/my_interfaces/msg/RobotStatus.msg`：

```msg
# 机器人状态消息
string robot_name           # 机器人名字
float32 battery_percent     # 电量百分比（0~100）
float32 x                   # 当前位置 x（米）
float32 y                   # 当前位置 y（米）
float32 linear_velocity     # 线速度（米/秒）
float32 angular_velocity    # 角速度（弧度/秒）
bool is_charging            # 是否在充电
uint8 mode                  # 0=手动, 1=自动, 2=夜间巡逻
```

### 7.3 自定义服务（.srv）

创建目录 `srv`：

```bash
mkdir ~/ros2_ws/src/my_interfaces/srv
```

创建文件 `~/ros2_ws/src/my_interfaces/srv/NavigateTo.srv`：

```
# 请求：目标位置
float32 target_x
float32 target_y
---
# 响应：是否成功
bool success
string message
```

### 7.4 配置 CMakeLists.txt

编辑 `~/ros2_ws/src/my_interfaces/CMakeLists.txt`：

```cmake
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

# 注册消息
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/RobotStatus.msg"
  "srv/NavigateTo.srv"
  DEPENDENCIES std_msgs
)
```

### 7.5 编译接口

```bash
cd ~/ros2_ws
colcon build --packages-select my_interfaces
source install/setup.bash

# 验证接口是否生成
ros2 interface show my_interfaces/msg/RobotStatus
ros2 interface show my_interfaces/srv/NavigateTo
```

### 7.6 在其他包中使用自定义接口

在包的 `package.xml` 中添加依赖：

```xml
<depend>my_interfaces</depend>
```

然后在代码中使用：

```python
from my_interfaces.msg import RobotStatus
from my_interfaces.srv import NavigateTo

# 用自定义消息类型创建发布者
publisher = self.create_publisher(RobotStatus, 'robot_status', 10)
```

---

## 8. Launch 文件

如果你有多个节点要启动，一个一个 `ros2 run` 太麻烦。**Launch 文件**可以一次性启动多个节点。

### 8.1 创建 launch 目录

```bash
mkdir -p ~/ros2_ws/src/my_robot_pkg/launch
```

### 8.2 编写 Launch 文件

创建文件 `~/ros2_ws/src/my_robot_pkg/launch/demo.launch.py`：

```python
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    """同时启动发布者和订阅者"""
    return LaunchDescription([
        Node(
            package='my_robot_pkg',
            executable='publisher',
            name='my_publisher_node',        # 覆盖节点名称
            output='screen',                  # 输出到终端
        ),
        Node(
            package='my_robot_pkg',
            executable='subscriber',
            name='my_subscriber_node',
            output='screen',
        ),
    ])
```

### 8.3 配置 setup.py

在 `setup.py` 中添加 launch 文件路径：

```python
from glob import glob
# ...

data_files=[
    # ... 其他配置 ...
    (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
],
```

别忘了加 `import os` 和 `from glob import glob` 在文件开头。

### 8.4 运行 Launch

```bash
cd ~/ros2_ws
colcon build --packages-select my_robot_pkg
source install/setup.bash

ros2 launch my_robot_pkg demo.launch.py
```

> 这样发布者和订阅者就**同时启动**了，再也不用手动开两个终端。

---

## 9. 参数

参数是节点的**可配置选项**，可以在启动时修改，也可以在运行时动态修改。

### 9.1 在代码中使用参数

修改 `publisher_node.py`，让发布频率可配置：

```python
class MyPublisher(Node):
    def __init__(self):
        super().__init__('my_publisher')

        # 声明参数：名字、默认值、描述
        self.declare_parameter('publish_rate', 1.0)
        self.declare_parameter('message', 'Hello ROS2!')

        # 读取参数
        rate = self.get_parameter('publish_rate').value
        msg_text = self.get_parameter('message').value

        self.publisher_ = self.create_publisher(String, 'my_topic', 10)
        # 用参数值设置定时器频率
        self.timer = self.create_timer(1.0 / rate, self.timer_callback)
        self.msg_text = msg_text

        self.get_logger().info(f'🚀 发布者启动：频率={rate}Hz, 消息="{msg_text}"')

    def timer_callback(self):
        msg = String()
        msg.data = self.msg_text
        self.publisher_.publish(msg)
```

### 9.2 设置参数

```bash
# 方式1：命令行传参
ros2 run my_robot_pkg publisher --ros-args -p publish_rate:=5.0 -p message:="你好ROS2"

# 方式2：在 launch 文件中设置
Node(
    package='my_robot_pkg',
    executable='publisher',
    parameters=[
        {'publish_rate': 5.0},
        {'message': '你好ROS2'}
    ],
)
```

### 9.3 运行时动态修改

```bash
# 运行时修改参数（先找到节点名）
ros2 param list                    # 列出所有参数
ros2 param get /my_publisher publish_rate     # 查看参数值
ros2 param set /my_publisher publish_rate 10.0  # 动态修改
```

---

## 10. Rviz 可视化

**Rviz** 是 ROS2 的 3D 可视化工具——可以看激光雷达数据、摄像头画面、机器人模型、导航路径等。

### 10.1 启动 Rviz

```bash
# 直接启动
rviz2

# 或者通过 ROS2 命令启动
ros2 run rviz2 rviz2
```

### 10.2 显示数据

Rviz 默认是空白界面，需要**添加显示项**（左下角 "Add" 按钮）：

| 要显示的数据 | 选择类型 | 设置话题 |
|-------------|---------|---------|
| 激光雷达点云 | LaserScan | /scan |
| 摄像头画面 | Image | /camera/image_raw |
| 机器人模型 | RobotModel | 设置 URDF |
| 导航路径 | Path | /plan |
| 坐标轴 | TF | /tf（自动显示所有坐标系） |

### 10.3 发布测试数据

```bash
# 发布一个虚拟的激光雷达数据（用于测试 Rviz 显示）
ros2 topic pub /scan sensor_msgs/msg/LaserScan "{angle_min: -3.14, angle_max: 3.14, range_min: 0.1, range_max: 10.0, ranges: [1.0, 2.0, 3.0]}"
```

> **💡 提示：** Rviz 是你调试机器人时最好的朋友。导航出问题了，第一件事就是打开 Rviz 看数据流是否正常。

---

## 11. TF 坐标变换

机器人有多个部件：底盘、激光雷达、摄像头、机械臂……每个部件都有自己的**坐标系**。**TF（Transform）** 就是管理这些坐标系之间关系的系统。

### 11.1 常见坐标系

```
map ──→ odom ──→ base_link ──→ laser_frame
                          └──→ camera_frame
                          └──→ left_wheel
                          └──→ right_wheel
```

| 坐标系 | 含义 |
|--------|------|
| `map` | 地图坐标系（固定，世界坐标） |
| `odom` | 里程计坐标系（相对起点） |
| `base_link` | 机器人中心坐标系 |
| `laser_frame` | 激光雷达坐标系 |
| `camera_frame` | 摄像头坐标系 |

### 11.2 发布静态 TF

激光雷达相对于机器人中心的位置通常是固定的，用"静态 TF"发布：

```python
from tf2_ros import StaticTransformBroadcaster
from geometry_msgs.msg import TransformStamped
import tf_transformations

class TFBroadcaster(Node):
    def __init__(self):
        super().__init__('tf_broadcaster')
        self.broadcaster = StaticTransformBroadcaster(self)

        t = TransformStamped()
        t.header.frame_id = 'base_link'        # 父坐标系
        t.child_frame_id = 'laser_frame'       # 子坐标系
        t.transform.translation.x = 0.1        # 激光雷达在机器人前方 10cm
        t.transform.translation.y = 0.0
        t.transform.translation.z = 0.2        # 高出中心 20cm
        # 旋转：四元数（x, y, z, w），这里无旋转
        t.transform.rotation.x = 0.0
        t.transform.rotation.y = 0.0
        t.transform.rotation.z = 0.0
        t.transform.rotation.w = 1.0

        self.broadcaster.send_transform(t)
        self.get_logger().info('📐 TF 已发布')
```

### 11.3 查看 TF

```bash
# 查看所有 TF 关系
ros2 run tf2_tools view_frames.py

# 查看两个坐标系之间的变换
ros2 run tf2_ros tf2_echo base_link laser_frame

# 在 Rviz 中添加 TF 显示，可以直观看到坐标系
```

---

## 12. URDF 机器人模型

**URDF（Unified Robot Description Format）** 用 XML 描述机器人的结构——有几个轮子、尺寸多少、颜色什么样。

### 12.1 一个简单的两轮机器人 URDF

创建文件 `~/ros2_ws/src/my_robot_pkg/urdf/simple_robot.urdf`：

```xml
<?xml version="1.0"?>
<robot name="simple_robot">

  <!-- 底盘 -->
  <link name="base_link">
    <visual>
      <geometry>
        <box size="0.3 0.2 0.1"/>      <!-- 长宽高：30cm x 20cm x 10cm -->
      </geometry>
      <origin xyz="0 0 0" rpy="0 0 0"/>
      <material name="blue">
        <color rgba="0 0 1 1"/>         <!-- 蓝色 -->
      </material>
    </visual>
  </link>

  <!-- 左轮 -->
  <link name="left_wheel">
    <visual>
      <geometry>
        <cylinder radius="0.05" length="0.03"/>  <!-- 半径5cm，厚3cm -->
      </geometry>
      <origin xyz="0 0 0" rpy="1.5708 0 0"/>    <!-- 旋转使其竖起来 -->
      <material name="black">
        <color rgba="0 0 0 1"/>
      </material>
    </visual>
  </link>

  <!-- 右轮 -->
  <link name="right_wheel">
    <visual>
      <geometry>
        <cylinder radius="0.05" length="0.03"/>
      </geometry>
      <origin xyz="0 0 0" rpy="1.5708 0 0"/>
      <material name="black">
        <color rgba="0 0 0 1"/>
      </material>
    </visual>
  </link>

  <!-- 关节：将左轮连接到底盘 -->
  <joint name="left_wheel_joint" type="continuous">
    <parent link="base_link"/>
    <child link="left_wheel"/>
    <origin xyz="-0.1 -0.12 0"/>    <!-- 相对底盘的位置 -->
    <axis xyz="0 1 0"/>             <!-- 绕 Y 轴旋转（前进方向） -->
  </joint>

  <!-- 关节：将右轮连接到底盘 -->
  <joint name="right_wheel_joint" type="continuous">
    <parent link="base_link"/>
    <child link="right_wheel"/>
    <origin xyz="-0.1 0.12 0"/>
    <axis xyz="0 1 0"/>
  </joint>

</robot>
```

### 12.2 在 Rviz 中显示 URDF

```bash
# 安装 urdf_tutorial
sudo apt install ros-humble-urdf-tutorial

# 用 Rviz 显示你的 URDF 模型
ros2 run urdf_tutorial display simple_robot.urdf
```

### 12.3 发布机器人状态（让轮子动起来）

要让轮子在 Rviz 里真正转动，需要发布关节状态：

```bash
# 安装 robot_state_publisher
sudo apt install ros-humble-robot-state-publisher

# 创建 launch 文件加载 URDF 并发布状态
```

---

## 13. Gazebo 仿真入门

**Gazebo** 是一个 3D 机器人仿真器——你可以在里面跑机器人、加障碍物，完全模拟真实的物理环境。

### 13.1 安装 Gazebo

```bash
# ROS2 Humble 自带 Gazebo（已改名为 Ignition/Gazebo Fortress）
sudo apt install ros-humble-gazebo-ros2-pkgs
```

### 13.2 启动一个空世界

```bash
# 启动 Gazebo，加载一个空世界
ros2 launch gazebo_ros gazebo.launch.py
```

### 13.3 在 Gazebo 中生成机器人

```python
# spawn_robot.py —— 将 URDF 机器人放到 Gazebo 世界里
from launch import LaunchDescription
from launch.actions import ExecuteProcess
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        # 启动 Gazebo
        ExecuteProcess(
            cmd=['gazebo', '--verbose', '-s', 'libgazebo_ros_factory.so'],
            output='screen'
        ),
        # 生成机器人（从 URDF 文件）
        Node(
            package='gazebo_ros',
            executable='spawn_entity.py',
            arguments=[
                '-entity', 'my_robot',
                '-file', '$(find my_robot_pkg)/urdf/simple_robot.urdf',
                '-x', '0', '-y', '0', '-z', '0.1'
            ],
            output='screen'
        ),
    ])
```

### 13.4 添加差速驱动

要让机器人能在 Gazebo 里移动，需要添加差速驱动插件到 URDF：

```xml
<!-- 在 URDF 末尾添加 -->
<gazebo>
  <plugin name="diff_drive" filename="libgazebo_ros_diff_drive.so">
    <ros>
      <namespace>/</namespace>
    </ros>
    <update_rate>100</update_rate>
    <left_joint>left_wheel_joint</left_joint>
    <right_joint>right_wheel_joint</right_joint>
    <wheel_separation>0.24</wheel_separation>  <!-- 轮距 -->
    <wheel_diameter>0.1</wheel_diameter>       <!-- 轮径 -->
    <command_topic>/cmd_vel</command_topic>     <!-- 速度指令话题 -->
    <odom_topic>/odom</odom_topic>             <!-- 里程计话题 -->
  </plugin>
</gazebo>
```

发布速度指令让机器人动起来：

```bash
# 让机器人以 0.2 m/s 前进
ros2 topic pub /cmd_vel geometry_msgs/msg/Twist "{linear: {x: 0.2}, angular: {z: 0.0}}"

# 让机器人旋转
ros2 topic pub /cmd_vel geometry_msgs/msg/Twist "{linear: {x: 0.0}, angular: {z: 0.5}}"
```

---

## 14. SLAM 与导航栈速览

这是你的项目中 ROS2 的**核心用途**——让机器人自主导航。

### 14.1 导航栈架构

```
传感器数据 ──→ SLAM（建图定位） ──→ 全局路径规划 ──→ 局部路径规划 ──→ 底盘控制
(LiDAR)         (map→odom)          (A* / Dijkstra)   (DWA / TEB)       (cmd_vel)
```

### 14.2 什么是 SLAM？

**SLAM（Simultaneous Localization and Mapping）** = 同时定位与建图。

- 机器人不知道自己在哪，也没有地图
- 它一边走，一边用激光雷达扫描周围环境
- 同时构建地图并确定自己在图中的位置

### 14.3 最简导航流程

```bash
# 安装导航2（Navigation2）——ROS2 的官方导航框架
sudo apt install ros-humble-navigation2
sudo apt install ros-humble-nav2-bringup

# 安装 SLAM 工具
sudo apt install ros-humble-slam-toolbox
```

**步骤：**

```
1. 启动机器人（真车或仿真） ──→ 确保 /scan 和 /odom 有数据
2. 启动 SLAM Toolbox    ──→ 在建图模式下运行
3. 键盘控制机器人走一圈  ──→ 地图逐渐构建完整
4. 保存地图            ──→ 得到一张 .pgm 图片+ .yaml 配置
5. 启动 AMCL 定位       ──→ 让机器人在已有地图中定位
6. 设置导航目标点       ──→ 机器人自动规划路径并走过去
```

### 14.4 在仿真中测试导航

如果你的项目中有 TurtleBot4 或类似底盘，可以快速体验：

```bash
# 安装 TurtleBot4 仿真
sudo apt install ros-humble-turtlebot4-simulator

# 启动 TurtleBot4 在 Gazebo 中
ros2 launch turtlebot4_gz turtlebot4_gz.launch.py

# 启动 SLAM 建图
ros2 launch turtlebot4_navigation slam.launch.py

# 启动键盘控制
ros2 run turtlebot4_teleop teleop_keyboard

# 保存地图
ros2 run nav2_map_server map_saver_cli -f ~/my_map
```

---

## 15. 常见报错与排坑

### 15.1 找不到包

```
错误：Package 'my_package' not found
```

**原因：** 没有 source 工作空间。

```bash
# 解决方法
source ~/ros2_ws/install/setup.bash
# 或者加到 ~/.bashrc
echo "source ~/ros2_ws/install/setup.bash" >> ~/.bashrc
```

### 15.2 编译错误

```bash
# 只编译你的包，加快速度
colcon build --packages-select my_robot_pkg

# 如果编译缓存有问题，清理重来
colcon build --packages-select my_robot_pkg --cmake-clean-cache
rm -rf build/ install/ log/
colcon build
```

### 15.3 话题收不到数据

```bash
# 排查步骤：
# 1. 列出所有话题，看你订阅的话题是否存在
ros2 topic list

# 2. 查看话题类型是否匹配
ros2 topic type /your_topic

# 3. 手动发布测试数据
ros2 topic pub /your_topic std_msgs/msg/String "{data: 'test'}"

# 4. 查看话题带宽
ros2 topic hz /your_topic
```

### 15.4 端口被占用

```bash
# 如果报 DDS 相关错误，重启 ROS2 环境
pkill -9 "ros2"
source ~/.bashrc

# 或者设置不同的 DDS 域名
export ROS_DOMAIN_ID=42
```

### 15.5 权限问题

```bash
# 如果插了 USB 设备（雷达、摄像头）提示没权限
sudo usermod -a -G dialout $USER
# 然后注销重新登录
```

### 15.6 性能问题

```bash
# 如果发现 CPU 占用过高，降低话题发布频率
# 在代码中设置合适的 publish_rate

# 或者在 launch 文件中用 QoS 设置降低带宽
from rclpy.qos import QoSProfile, ReliabilityPolicy
qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)
self.create_subscription(..., qos_profile=qos)
```

---

## 附录：和你项目的关系

你的项目《智能养老陪护巡逻机器人》中，ROS2 将负责：

```
┌──────────────────────────────────────────────────┐
│                   ROS2 系统                       │
│                                                   │
│  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │
│  │ 导航模块  │  │ 视觉模块  │  │  语音/大模型模块 │  │
│  │          │  │          │  │                │  │
│  │  SLAM    │  │ YOLOv8   │  │ ASR → LLM     │  │
│  │  路径规划 │  │ 跌倒检测  │  │      → TTS    │  │
│  │  避障    │  │ 人脸识别  │  │                │  │
│  └────┬─────┘  └────┬─────┘  └──────┬─────────┘  │
│       │             │               │             │
│       └──────┬──────┴───────┬───────┘             │
│              │              │                     │
│         ┌────▼────┐   ┌────▼────┐                │
│         │ LiDAR   │   │ 摄像头   │                │
│         │ /scan   │   │ /image   │                │
│         └─────────┘   └─────────┘                │
│                                                   │
│              ┌──────────────┐                     │
│              │ /cmd_vel     │                     │
│              │ 底盘控制     │                     │
│              └──────┬───────┘                     │
│                     │                             │
│              ┌──────▼───────┐                     │
│              │ STM32 电机驱动 │                    │
│              └──────────────┘                     │
└──────────────────────────────────────────────────┘
```

**你接下来的学习路线建议：**

1. ✅ **这篇教程看完** → 理解核心概念，装好环境
2. 🟡 **动手写代码** → 把发布者/订阅者/服务的例子自己敲一遍
3. 🟠 **跑仿真 TurtleBot** → 体验 SLAM 建图和导航
4. 🔴 **结合你的项目** → 把 YOLO 检测结果通过 ROS2 话题发出来
5. 🔵 **买底盘+激光雷达** → 在真车上跑导航

---

> **💡 最后的建议：** ROS2 内容非常多，不可能一次全学会。先从 **Publisher/Subscriber** 开始，跑通小乌龟，看懂 Rviz 数据流——当你真的需要导航时，再深入 Navigation2。**循序渐进，动手为主！**
