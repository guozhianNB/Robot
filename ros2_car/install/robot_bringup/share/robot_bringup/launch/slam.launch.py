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
from launch.substitutions import LaunchConfiguration, PythonExpression
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
            # 坐标系必须内联指定：params 文件不一定被加载，
            # 若不指定，slam_toolbox 会默认 base_frame=base_footprint，
            # 导致查不到 base_link→odom 而一直报 "Failed to compute odom pose"。
            'base_frame': 'base_link',
            'odom_frame': 'odom',
            'map_frame': 'map',
            'scan_topic': '/scan',
            # —— map→odom TF 稳定性（params 文件不一定被加载，故内联）——
            # restamp_tf=true：map→odom 用 now() 而非扫描时间戳打时间戳，
            #   减少 "No transform from X to map" 的瞬时卡顿。
            'restamp_tf': True,
            'transform_publish_period': 0.05,   # 20Hz 发布 map→odom
            'transform_timeout': 0.2,
            'tf_buffer_duration': 30.0,
            # 仅在 localization 模式传入地图文件；mapping 模式必须留空，
            # 否则 slam_toolbox 会因为 map_file_name 非空而误入 localization 模式，
            # 报 "Map starting pose not specified" 且不发布 /map。
            'map_file_name': PythonExpression(
                ["'", map_file, "' if '", mode, "' == 'localization' else ''"]),
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
