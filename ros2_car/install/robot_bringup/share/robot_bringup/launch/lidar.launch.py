#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动 YDLidar Tmini Plus 激光雷达驱动（可选带上去噪节点）。

用法:
    ros2 launch robot_bringup lidar.launch.py
    ros2 launch robot_bringup lidar.launch.py use_scan_filter:=false

去噪默认开启（use_scan_filter:=true）：只是多起一个 scan_filter 节点、
多发一路 /scan_filtered，**不改变任何现有消费者**（slam/nav2/amcl 仍吃 /scan），
所以开了也不影响现状；要让它真正生效需要单独切消费者，见
`robot_bringup/robot_bringup/scan_filter.py` 头部注释。
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    share_dir = get_package_share_directory('robot_bringup')
    params_file = LaunchConfiguration('params_file')
    use_scan_filter = LaunchConfiguration('use_scan_filter')
    params_declare = DeclareLaunchArgument(
        'params_file',
        default_value=os.path.join(share_dir, 'config', 'lidar_tmini_plus.yaml'),
        description='YDLidar 驱动参数文件路径')
    filter_declare = DeclareLaunchArgument(
        'use_scan_filter', default_value='true',
        description='是否随雷达一起起 scan_filter 去噪（/scan → /scan_filtered）')

    driver_node = Node(
        package='ydlidar_ros2_driver',
        executable='ydlidar_ros2_driver_node',
        name='ydlidar_ros2_driver_node',
        output='screen',
        emulate_tty=True,
        parameters=[params_file],
    )

    scan_filter = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(share_dir, 'launch', 'scan_filter.launch.py')),
        condition=IfCondition(use_scan_filter),
    )

    return LaunchDescription([
        params_declare,
        filter_declare,
        driver_node,
        scan_filter,
    ])
