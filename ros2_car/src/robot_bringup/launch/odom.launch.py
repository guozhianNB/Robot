#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动里程计来源。

odom_source:=chassis → STM32 底盘编码器里程计（robot_chassis/chassis_driver）
odom_source:=rf2o    → rf2o 激光里程计（无底盘时的兜底，官方示例同款）

两者都发布 /odom + odom→base_link tf，二选一，不要同时开。

用法:
    ros2 launch robot_bringup odom.launch.py odom_source:=chassis
    ros2 launch robot_bringup odom.launch.py odom_source:=rf2o
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    chassis_share = get_package_share_directory('robot_chassis')

    odom_source = LaunchConfiguration('odom_source')
    use_ekf = LaunchConfiguration('use_ekf')

    source_declare = DeclareLaunchArgument(
        'odom_source', default_value='rf2o',
        description='里程计来源: chassis(STM32编码器) | rf2o(激光里程计兜底)')
    ekf_declare = DeclareLaunchArgument(
        'use_ekf', default_value='false',
        description='是否启用 robot_localization EKF 融合（需 odom_source:=chassis，见 ekf_params.yaml）')

    chassis_node = Node(
        package='robot_chassis',
        executable='chassis_driver',
        name='chassis_driver',
        output='screen',
        emulate_tty=True,
        parameters=[os.path.join(chassis_share, 'config', 'chassis_params.yaml')],
        condition=IfCondition(PythonExpression(['odom_source == "chassis"'])),
    )

    rf2o_node = Node(
        package='rf2o_laser_odometry',
        executable='rf2o_laser_odometry_node',
        name='rf2o_laser_odometry',
        output='screen',
        parameters=[{
            'laser_scan_topic': '/scan',
            'odom_topic': '/odom',
            'publish_tf': True,
            'base_frame_id': 'base_link',
            'odom_frame_id': 'odom',
            'init_pose_from_topic': '',
            'freq': 10.0,
        }],
        condition=IfCondition(PythonExpression(['odom_source == "rf2o"'])),
    )

    return LaunchDescription([
        source_declare,
        ekf_declare,
        chassis_node,
        rf2o_node,
    ])
