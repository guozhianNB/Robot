# -*- coding: utf-8 -*-
r"""基础设施层：配置以外的横切能力，全部**零业务、零外部依赖**。

- :mod:`LLM.conf`          集中配置（仍在包根，作为全包共享的顶层契约）
- :mod:`LLM.core.log`      审计日志（JSONL 落盘）
- :mod:`LLM.core.bus`      SSE 事件总线（任意线程 publish → asyncio 扇出）
- :mod:`LLM.core.vectors`  零依赖轻量向量检索（字符 n-gram 哈希）
- :mod:`LLM.core.zonegeo`  点在区域内判定（纯几何）

本层**不许** import 上层（store / agent / maps / voice），否则破坏依赖方向。
"""
