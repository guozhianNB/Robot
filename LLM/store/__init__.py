# -*- coding: utf-8 -*-
r"""持久化层：一切"数据落在哪、怎么读写"的封装。

- :mod:`LLM.store.db`       SQLite（brain.db，WAL + 线程锁）——档案/记忆/提醒/历史/设置
- :mod:`LLM.store.ragstore` ChromaDB 向量存储（可选依赖，缺失只降级）
- :mod:`LLM.store.graph`    Kuzu 知识图谱（可选依赖，缺失只降级）
- :mod:`LLM.store.embed`    Embedding 封装（阿里 text-embedding-v3，失败回退 vectors）
- :mod:`LLM.store.migrate`  一次性幂等迁移（旧结构 → v3 分层）

依赖方向：core → store → agent → server。``db.py`` 对 ``agent.tools`` 的
``TOOL_DEFAULTS`` 是**函数内延迟导入**（循环依赖的唯一出口），勿提到模块顶层。
"""
