#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仅起机器人基础（不含 slam/nav）：robot_state_publisher + lidar + chassis odom。

用法(与 bringup 兼容的可复用基底):
    ros2 launch robot_bringup robot_base.launch.py odom_source:=chassis
"""
import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    share_dir = get_package_share_directory('robot_bringup')
    odom_source = LaunchConfiguration('odom_source')
    use_sim_time = LaunchConfiguration('use_sim_time')
    declare = [
        DeclareLaunchArgument('odom_source', default_value='rf2o'),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
    ]
    rsp = Node(
        package='robot_state_publisher', executable='robot_state_publisher',
        name='robot_state_publisher', output='screen', parameters=[{
            'robot_description': open(os.path.join(share_dir, 'urdf', 'car.urdf')).read(),
            'use_sim_time': use_sim_time}]),
    lidar = IncludeLaunchDescription(PythonLaunchDescriptionSource(
        os.path.join(share_dir, 'launch', 'lidar.launch.py')))
    odom = IncludeLaunchDescription(PythonLaunchDescriptionSource(
        os.path.join(share_dir, 'launch', 'odom.launch.py')),
        launch_arguments={'odom_source': odom_source, 'use_ekf': 'false'}.items())
    return LaunchDescription(declare + [rsp, lidar, odom])
