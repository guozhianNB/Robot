# -*- coding: utf-8 -*-
r"""LLM/tests 的公共 fixture（本目录原先没有 conftest.py，2026-09-15 新建）。"""
import pytest


@pytest.fixture()
def app_paths():
    """列出一个 FastAPI app 的全部路由路径（含 ``include_router`` 进来的）。

    为什么不能直接遍历 ``app.routes``：fastapi 0.141 + starlette 1.6 的
    ``include_router()`` 是**惰性**的 —— ``app.routes`` 里只多一个
    ``_IncludedRouter`` 占位对象（``path=None``），子路由不在里面。不展开它，
    断言会空过或误判。

    展开方式随版本而异，这里两条路都走（结果收进 set，重复无副作用）：
      * ``effective_route_contexts()``：fastapi 0.141.1 实测提供的**唯一**入口
        —— 该版本的占位对象**没有** ``.routes`` / ``.router`` 属性
        （实测 ``vars()`` 只有 ``original_router`` / ``include_context`` /
        ``_effective_*``，照抄旧写法会展开出空集）；
      * ``.routes`` / ``.router.routes``：旧版 fastapi 当场拷贝子路由的形态，
        以及 ``Mount`` 等其它容器。

    注意：``effective_route_contexts()`` 里取的是 **context 自身的 ``path``**
    （如 ``include_router(sub, prefix="/pre")`` 时为 ``/pre/a``），
    ``original_route.path`` 是**未加 prefix** 的原始子路由路径（``/a``），只作兜底 ——
    取错了会让「带前缀的 include」展开成未加前缀的路径，否定断言照样空过。
    """
    def _expand(routes, out):
        for r in routes:
            p = getattr(r, "path", None)
            if isinstance(p, str) and p:
                out.add(p)
            ctxs = getattr(r, "effective_route_contexts", None)
            if callable(ctxs):
                for c in ctxs():
                    cp = getattr(c, "path", None) or getattr(
                        getattr(c, "original_route", None), "path", None)
                    if isinstance(cp, str) and cp:
                        out.add(cp)
            sub = getattr(r, "routes", None)
            if sub is None:
                sub = getattr(getattr(r, "router", None), "routes", None)
            if sub:
                _expand(sub, out)
        return out

    def _paths(app):
        return _expand(app.routes, set())

    return _paths
