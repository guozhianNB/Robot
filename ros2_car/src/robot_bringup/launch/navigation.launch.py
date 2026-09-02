#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nav2 自主导航（基于 nav2_bringup 的 localization + navigation 组合）。

用法:
    ros2 launch robot_bringup navigation.launch.py map:=~/ros2/car_ws/maps/my_map.yaml
    rviz:=false 关闭 rviz
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
    nav2_bringup_dir = get_package_share_directory('nav2_bringup')

    map_file = LaunchConfiguration('map')
    params_file = LaunchConfiguration('params_file')
    rviz = LaunchConfiguration('rviz')
    use_sim_time = LaunchConfiguration('use_sim_time')
    autostart = LaunchConfiguration('autostart')

    declare_args = [
        DeclareLaunchArgument(
            'map', default_value=os.path.join(share_dir, 'maps', 'my_map.yaml'),
            description='要加载的地图 yaml'),
        DeclareLaunchArgument(
            'params_file', default_value=os.path.join(share_dir, 'config', 'nav2_params.yaml'),
            description='Nav2 参数文件'),
        DeclareLaunchArgument('rviz', default_value='true'),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('autostart', default_value='true',
                              description='自动启动 nav2 生命周期节点'),
    ]

    # 定位：map_server + amcl
    localization = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_bringup_dir, 'launch', 'localization_launch.py')),
        launch_arguments={
            'map': map_file,
            'params_file': params_file,
            'use_sim_time': use_sim_time,
            'autostart': autostart,
        }.items(),
    )

    # 导航：controller / planner / behavior / bt_navigator / velocity_smoother
    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(nav2_bringup_dir, 'launch', 'navigation_launch.py')),
        launch_arguments={
            'params_file': params_file,
            'use_sim_time': use_sim_time,
            'autostart': autostart,
        }.items(),
    )

    rviz_node = Node(
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        arguments=['-d', os.path.join(share_dir, 'rviz', 'navigation.rviz')],
        condition=IfCondition(rviz),
    )

    # 大模型端对接（契约 docs/目标文档及说明/ROS底盘接口需求.md）：
    # robot_actions = robot/move + robot/turn + robot/navigate_to 服务 + exec_state/arrived
    # cmd_stop      = robot/cmd_stop 急停（取消 Nav2 + 零速）
    robot_actions_node = Node(
        package='robot_navigation',
        executable='robot_actions',
        name='robot_actions',
        output='screen',
        emulate_tty=True,
    )

    cmd_stop_node = Node(
        package='robot_navigation',
        executable='cmd_stop',
        name='cmd_stop',
        output='screen',
        emulate_tty=True,
    )

    return LaunchDescription(
        declare_args + [localization, navigation, rviz_node,
                        robot_actions_node, cmd_stop_node]
    )
