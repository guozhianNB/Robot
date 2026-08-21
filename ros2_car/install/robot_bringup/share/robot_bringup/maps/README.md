# maps/

存放 SLAM 建图保存的地图文件（my_map.pgm + my_map.yaml）。

## 保存地图（建图完成后，另开终端）

```bash
source ~/ros2/car_ws/install/setup.bash
ros2 run nav2_map_server map_saver_cli -f ~/ros2/car_ws/maps/my_map
```

生成 `my_map.pgm`（图片）+ `my_map.yaml`（配置）。

## 使用地图

- slam_toolbox 定位模式：`ros2 launch robot_bringup slam.launch.py mode:=localization map:=~/ros2/car_ws/maps/my_map.yaml`
- Nav2 导航：`ros2 launch robot_bringup bringup.launch.py mode:=navigation map:=~/ros2/car_ws/maps/my_map.yaml`

> 注意：.pgm/.yaml 为运行时产物，不入库；如需保留示例地图请另行归档。
