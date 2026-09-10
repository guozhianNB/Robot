#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仅起机器人基础（不含 slam/nav）：robot_state_publisher + lidar + chassis odom。

用法(与 bringup 兼容的可复用基底):
    ros2 launch robot_bringup robot_base.launch.py odom_source:=chassis
    ros2 launch robot_bringup robot_base.launch.py odom_source:=fused   # 轮速+激光 EKF 双源融合
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
    use_ekf = LaunchConfiguration('use_ekf')
    use_sim_time = LaunchConfiguration('use_sim_time')
    declare = [
        DeclareLaunchArgument('odom_source', default_value='rf2o',
                              description='chassis | rf2o | fused（fused=轮速+激光 EKF 双源融合）'),
        # 原来这里把 use_ekf 写死成 false 传给 odom.launch.py，导致从 robot_base
        # 起永远开不了 EKF；现在提成启动参数透传（默认 false 保持原行为）
        DeclareLaunchArgument('use_ekf', default_value='false',
                              description='chassis 模式下是否启用 EKF 融合（fused 模式自带 EKF）'),
        DeclareLaunchArgument('use_sim_time', default_value='false'),
    ]
    rsp = Node(
        package='robot_state_publisher', executable='robot_state_publisher',
        name='robot_state_publisher', output='screen', parameters=[{
            'robot_description': open(os.path.join(share_dir, 'urdf', 'car.urdf')).read(),
            'use_sim_time': use_sim_time}])
    lidar = IncludeLaunchDescription(PythonLaunchDescriptionSource(
        os.path.join(share_dir, 'launch', 'lidar.launch.py')))
    odom = IncludeLaunchDescription(PythonLaunchDescriptionSource(
        os.path.join(share_dir, 'launch', 'odom.launch.py')),
        launch_arguments={'odom_source': odom_source, 'use_ekf': use_ekf}.items())
    return LaunchDescription(declare + [rsp, lidar, odom])
