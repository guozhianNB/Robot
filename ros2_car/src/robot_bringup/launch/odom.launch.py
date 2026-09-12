#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动里程计来源。

odom_source:=chassis → STM32 底盘编码器里程计（robot_chassis/chassis_driver）
odom_source:=rf2o    → rf2o 激光里程计（无底盘时的兜底，官方示例同款）
odom_source:=fused   → 双源融合：底盘编码器速度 + rf2o 激光位姿，odom→base_link TF
                       由 robot_localization EKF 统一发布（麦轮打滑场景推荐）

节点启停与 TF 归属由 robot_bringup/odom_fusion.py::plan_odom_sources 决定，
保证 odom→base_link 每段 TF 只有一个发布者（REP-105）。
详见 docs/superpowers/specs/2026-09-10-rf2o-ekf-odom-fusion-design.md

用法:
    ros2 launch robot_bringup odom.launch.py odom_source:=chassis
    ros2 launch robot_bringup odom.launch.py odom_source:=rf2o
    ros2 launch robot_bringup odom.launch.py odom_source:=fused
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from robot_bringup.odom_fusion import (
    LASER_ODOM,
    LASER_ODOM_RAW,
    WHEEL_ODOM,
    plan_odom_sources,
)


def _build_nodes(context, *args, **kwargs):
    chassis_share = get_package_share_directory('robot_chassis')
    bringup_share = get_package_share_directory('robot_bringup')

    odom_source = LaunchConfiguration('odom_source').perform(context)
    use_ekf = LaunchConfiguration('use_ekf').perform(context)
    plan = plan_odom_sources(odom_source, use_ekf)

    nodes = []

    if plan['chassis']:
        nodes.append(Node(
            package='robot_chassis',
            executable='chassis_driver',
            name='chassis_driver',
            output='screen',
            emulate_tty=True,
            parameters=[
                os.path.join(chassis_share, 'config', 'chassis_params.yaml'),
                {'publish_tf': plan['chassis_publish_tf']},
            ]))

    if plan['rf2o']:
        # fused 模式下 rf2o 输出到 raw 话题，由 odom_relay 补协方差后再给 EKF
        nodes.append(Node(
            package='rf2o_laser_odometry',
            executable='rf2o_laser_odometry_node',
            name='rf2o_laser_odometry',
            output='screen',
            parameters=[{
                'laser_scan_topic': '/scan',
                'odom_topic': LASER_ODOM_RAW if plan['relay'] else WHEEL_ODOM,
                # rf2o 用激光扫描时间戳发 TF，会让 slam_toolbox 查不到（Failed to
                # compute odom pose），故它自己一律不发 TF，改由 EKF 或 odom_to_tf 发
                'publish_tf': plan['rf2o_publish_tf'],
                'base_frame_id': 'base_link',
                'odom_frame_id': 'odom',
                'init_pose_from_topic': '',
                'freq': 10.0,
            }]))

    if plan['relay']:
        nodes.append(Node(
            package='robot_bringup',
            executable='odom_relay',
            name='odom_relay',
            output='screen',
            emulate_tty=True,
            parameters=[{
                'input_topic': LASER_ODOM_RAW,
                'output_topic': LASER_ODOM,
                'restamp': True,
            }]))

    if plan['ekf']:
        nodes.append(Node(
            package='robot_localization',
            executable='ekf_node',
            name='ekf_filter_node',
            output='screen',
            emulate_tty=True,
            parameters=[os.path.join(bringup_share, 'config', 'ekf_params.yaml')],
            remappings=[('odometry/filtered', '/odom_filtered')]))

    if plan['odom_to_tf']:
        # rf2o 兜底模式：用当前 ROS 时间把 /odom 转发成 odom→base_link TF
        nodes.append(Node(
            package='robot_bringup',
            executable='odom_to_tf',
            name='odom_to_tf',
            output='screen',
            parameters=[{
                'odom_frame': 'odom',
                'base_frame': 'base_link',
                'odom_topic': WHEEL_ODOM,
            }]))

    return nodes


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument(
            'odom_source', default_value='rf2o',
            description='里程计来源: chassis(STM32编码器) | rf2o(激光里程计兜底) | fused(双源融合)'),
        DeclareLaunchArgument(
            'use_ekf', default_value='false',
            description='chassis 模式下是否启用 EKF（fused 模式自带 EKF，忽略此项）'),
        OpaqueFunction(function=_build_nodes),
    ])
