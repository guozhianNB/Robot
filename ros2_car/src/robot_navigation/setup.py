from setuptools import find_packages, setup

package_name = 'robot_navigation'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='sunrise',
    maintainer_email='sunrise@localhost',
    description='导航辅助节点：命令行发 Nav2 目标、robot/cmd_stop 急停、move/turn/navigate 服务（对接大模型端契约）',
    license='MIT',
    entry_points={
        'console_scripts': [
            'navigate_to_pose = robot_navigation.navigate_to_pose:main',
            'cmd_stop = robot_navigation.cmd_stop:main',
            'robot_actions = robot_navigation.robot_actions:main',
        ],
    },
)
