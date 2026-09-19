#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单独跑 lidar_loc 做定位（自带 map_server，不拉 AMCL / Nav2）。

用法:
    ros2 launch robot_bringup lidar_loc.launch.py map:=<地图.yaml>
    ros2 launch robot_bringup lidar_loc.launch.py map:=... set_initial_pose:=true \
        initial_pose_x:=1.0 initial_pose_y:=0.5 initial_pose_yaw:=0.0

为什么要自带 map_server：`navigation.launch.py` 会连 AMCL 一起拉起来，而 AMCL 与
lidar_loc **都广播 map→odom**，同时跑就是两个发布者（违反 REP-105）。所以这里只起
`map_server` + 它自己的 lifecycle_manager，把定位交给 lidar_loc。

本 launch **不会**被 `navigation.launch.py` / `bringup.launch.py` 引用 —— 要启用
lidar_loc 是显式的替换动作，不是默认行为。
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

    map_file = LaunchConfiguration('map')
    params_file = LaunchConfiguration('params_file')
    rviz = LaunchConfiguration('rviz')
    use_sim_time = LaunchConfiguration('use_sim_time')
    set_initial_pose = LaunchConfiguration('set_initial_pose')
    initial_pose_x = LaunchConfiguration('initial_pose_x')
    initial_pose_y = LaunchConfiguration('initial_pose_y')
    initial_pose_yaw = LaunchConfiguration('initial_pose_yaw')

    declare_args = [
        DeclareLaunchArgument(
            'map', default_value=os.path.join(share_dir, 'maps', 'my_map.yaml'),
            description='要加载的地图 yaml'),
        DeclareLaunchArgument(
            'params_file', default_value=os.path.join(share_dir, 'config', 'lidar_loc.yaml'),
            description='lidar_loc 参数文件'),
        DeclareLaunchArgument('rviz', default_value='false'),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('set_initial_pose', default_value='false'),
        DeclareLaunchArgument('initial_pose_x', default_value='0.0'),
        DeclareLaunchArgument('initial_pose_y', default_value='0.0'),
        DeclareLaunchArgument('initial_pose_yaw', default_value='0.0'),
    ]

    map_server_node = Node(
        package='nav2_map_server',
        executable='map_server',
        name='map_server',
        output='screen',
        emulate_tty=True,
        parameters=[{'yaml_filename': map_file, 'use_sim_time': use_sim_time}],
    )

    # map_server 是生命周期节点，必须有人 configure + activate
    lifecycle_manager = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager_localization',
        output='screen',
        emulate_tty=True,
        parameters=[{'autostart': True,
                     'use_sim_time': use_sim_time,
                     'node_names': ['map_server']}],
    )

    lidar_loc_node = Node(
        package='robot_bringup',
        executable='lidar_loc',
        name='lidar_loc',
        output='screen',
        emulate_tty=True,
        parameters=[params_file, {
            'use_sim_time': use_sim_time,
            'set_initial_pose': set_initial_pose,
            'initial_pose_x': initial_pose_x,
            'initial_pose_y': initial_pose_y,
            'initial_pose_yaw': initial_pose_yaw,
        }],
    )

    rviz_node = Node(
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        arguments=['-d', os.path.join(share_dir, 'rviz', 'navigation.rviz')],
        condition=IfCondition(rviz),
    )

    return LaunchDescription(
        declare_args + [map_server_node, lifecycle_manager, lidar_loc_node, rviz_node]
    )
