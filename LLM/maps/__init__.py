# -*- coding: utf-8 -*-
r"""地图域：地图文件存取、标记真相、位姿，以及编辑器独立服务的路由与进程管理。

- :mod:`LLM.maps.mapstore`    「地图文件在哪、怎么读写」抽象（Local / Ssh）
- :mod:`LLM.maps.mapsources`  地图源注册表（具名、可切换，替代环境变量）
- :mod:`LLM.maps.maptags`     **标记唯一真相** ``<图名>.tags.json``（地点/区域）
- :mod:`LLM.maps.mapserver`   PGM 解析 + 灰度 PNG 编码 + 标点校验（纯 stdlib）
- :mod:`LLM.maps.locator`     位姿与「车此刻在跑哪张图」（只读、默认降级可用）
- :mod:`LLM.maps.roslink`     rosbridge 连接层（连接 + 降级 + 假数据注入）
- :mod:`LLM.maps.mapapi`      编辑器专属路由 —— 只在独立进程 :8010 挂载
- :mod:`LLM.maps.mapctl`      编辑器独立服务的启停管理（主后端侧，仅 admin）

**进程边界**：``mapapi`` 只被 ``LLM.mapeditor_server``（:8010）import；
主后端只 import ``locator`` / ``maptags`` / ``mapctl``，绝不 import 编辑器业务。
"""
