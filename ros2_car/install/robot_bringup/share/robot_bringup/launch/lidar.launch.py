#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动 YDLidar Tmini Plus 激光雷达驱动。

用法:
    ros2 launch robot_bringup lidar.launch.py
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
        default_value=os.path.join(share_dir, 'config', 'lidar_tmini_plus.yaml'),
        description='YDLidar 驱动参数文件路径')

    driver_node = Node(
        package='ydlidar_ros2_driver',
        executable='ydlidar_ros2_driver_node',
        name='ydlidar_ros2_driver_node',
        output='screen',
        emulate_tty=True,
        parameters=[params_file],
    )

    return LaunchDescription([
        params_declare,
        driver_node,
    ])
