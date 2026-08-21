#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小车一键启动。

用法:
    建图:   ros2 launch robot_bringup bringup.launch.py mode:=mapping
    导航:   ros2 launch robot_bringup bringup.launch.py mode:=navigation map:=~/ros2/car_ws/maps/my_map.yaml

可选参数:
    odom_source:=rf2o|chassis  里程计来源（默认 rf2o：无底盘时激光里程计兜底；底盘接上后改 chassis）
    use_ekf:=false|true        是否启用 EKF 里程计融合（需 odom_source:=chassis）
    rviz:=true|false           是否启动 rviz

建图时键盘遥控请另开终端:
    ros2 run teleop_twist_keyboard teleop_twist_keyboard
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    share_dir = get_package_share_directory('robot_bringup')

    mode = LaunchConfiguration('mode')
    odom_source = LaunchConfiguration('odom_source')
    use_ekf = LaunchConfiguration('use_ekf')
    rviz = LaunchConfiguration('rviz')
    map_file = LaunchConfiguration('map')
    use_sim_time = LaunchConfiguration('use_sim_time')

    declare_args = [
        DeclareLaunchArgument('mode', default_value='mapping',
                              description='mapping(建图) | navigation(导航)'),
        DeclareLaunchArgument('odom_source', default_value='rf2o',
                              description='里程计来源: rf2o | chassis'),
        DeclareLaunchArgument('use_ekf', default_value='false'),
        DeclareLaunchArgument('rviz', default_value='true'),
        DeclareLaunchArgument('map', default_value=os.path.join(share_dir, 'maps', 'my_map.yaml')),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
    ]

    # 机器人模型 + TF（base_link → laser_link 等）
    robot_state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        name='robot_state_publisher',
        output='screen',
        parameters=[{
            'robot_description': open(
                os.path.join(share_dir, 'urdf', 'car.urdf'), 'r').read(),
            'use_sim_time': use_sim_time,
        }],
    )

    lidar = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(share_dir, 'launch', 'lidar.launch.py')),
    )

    odom = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(share_dir, 'launch', 'odom.launch.py')),
        launch_arguments={
            'odom_source': odom_source,
            'use_ekf': use_ekf,
        }.items(),
    )

    slam = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(share_dir, 'launch', 'slam.launch.py')),
        launch_arguments={
            'mode': 'mapping',
            'rviz': rviz,
            'use_sim_time': use_sim_time,
        }.items(),
        condition=IfCondition(
            PythonExpression(["'", mode, "' == 'mapping'"])),
    )

    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(share_dir, 'launch', 'navigation.launch.py')),
        launch_arguments={
            'map': map_file,
            'rviz': rviz,
            'use_sim_time': use_sim_time,
        }.items(),
        condition=IfCondition(
            PythonExpression(["'", mode, "' == 'navigation'"])),
    )

    return LaunchDescription(
        declare_args
        + [robot_state_publisher, lidar, odom, slam, navigation]
    )
