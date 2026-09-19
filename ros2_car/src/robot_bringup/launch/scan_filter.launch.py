#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""激光扫描去噪：额外发一路 `/scan_filtered`。

用法:
    ros2 launch robot_bringup scan_filter.launch.py
    ros2 launch robot_bringup scan_filter.launch.py params_file:=<...>.yaml

⚠️ 本 launch 只负责"多发一路话题"，**不改任何现有消费者**。
   对照验证（rviz 里同时勾 /scan 与 /scan_filtered）通过后，再按
   `robot_bringup/robot_bringup/scan_filter.py` 头部注释里的三处清单切换消费者。
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    share_dir = get_package_share_directory('robot_bringup')
    params_file = LaunchConfiguration('params_file')
    params_declare = DeclareLaunchArgument(
        'params_file',
        default_value=os.path.join(share_dir, 'config', 'scan_filter.yaml'),
        description='scan_filter 参数文件路径')

    # 显式 remap 到绝对话题：即使将来放到命名空间下，也仍然进 /scan。
    filter_node = Node(
        package='robot_bringup',
        executable='scan_filter',
        name='scan_filter',
        output='screen',
        emulate_tty=True,
        parameters=[params_file],
        remappings=[('scan', '/scan'), ('scan_filtered', '/scan_filtered')],
    )

    return LaunchDescription([
        params_declare,
        filter_node,
    ])
