from setuptools import find_packages, setup

package_name = 'robot_chassis'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/config', ['config/chassis_params.yaml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='sunrise',
    maintainer_email='sunrise@localhost',
    description='STM32 麦轮底盘 ROS2 驱动',
    license='MIT',
    entry_points={
        'console_scripts': [
            'chassis_driver = robot_chassis.chassis_driver:main',
        ],
    },
)
