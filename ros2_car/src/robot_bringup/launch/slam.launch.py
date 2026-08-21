#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SLAM 建图 / 定位（slam_toolbox）。

用法:
    建图:   ros2 launch robot_bringup slam.launch.py mode:=mapping
    定位:   ros2 launch robot_bringup slam.launch.py mode:=localization map:=<map.yaml>
    不弹 rviz:  rviz:=false
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    share_dir = get_package_share_directory('robot_bringup')

    mode = LaunchConfiguration('mode')
    map_file = LaunchConfiguration('map')
    params_file = LaunchConfiguration('params_file')
    rviz = LaunchConfiguration('rviz')
    use_sim_time = LaunchConfiguration('use_sim_time')

    mode_declare = DeclareLaunchArgument(
        'mode', default_value='mapping',
        description='SLAM 模式: mapping(建图) | localization(加载地图定位)')
    map_declare = DeclareLaunchArgument(
        'map', default_value=os.path.join(share_dir, 'maps', 'my_map.yaml'),
        description='localization 模式加载的地图文件')
    params_declare = DeclareLaunchArgument(
        'params_file', default_value=os.path.join(share_dir, 'config', 'slam_toolbox_params.yaml'))
    rviz_declare = DeclareLaunchArgument('rviz', default_value='true')
    sim_declare = DeclareLaunchArgument('use_sim_time', default_value='false')

    slam_node = Node(
        package='slam_toolbox',
        executable='async_slam_toolbox_node',
        name='slam_toolbox',
        output='screen',
        emulate_tty=True,
        parameters=[params_file, {
            'use_sim_time': use_sim_time,
            'mode': mode,
            'map_file_name': map_file,   # localization 模式使用
        }],
    )

    rviz_config = os.path.join(share_dir, 'rviz', 'mapping.rviz')
    rviz_node = Node(
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        arguments=['-d', rviz_config],
        condition=IfCondition(rviz),
    )

    return LaunchDescription([
        mode_declare,
        map_declare,
        params_declare,
        rviz_declare,
        sim_declare,
        slam_node,
        rviz_node,
    ])
