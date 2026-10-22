"""机制 Web 读面的路由（P5 子票 #41）。

前缀在 `core/urls.py` 挂成 `api/regime/`。**这是 `apps/regime` 的第一个 REST 前缀**——
它此前刻意没有（`apps/strategy_factory/urls.py:4` 把那写成了它的选择：机制只走 agent
工具与 slash 命令）。本 map 是第一次给它开 Web 面，理由与范围见 `CONTEXT.md` 的
「机制的 Web 读面与写入口」一节。

## 路由顺序

`pool/generations/` 写在 `pool/` **之前**。Django 按序匹配，`pool/` 是一条精确路径
（`^pool/$`），本不会吃掉 `pool/generations/`；把更具体的写在前面只是让阅读顺序与匹配
顺序一致——将来若有人把 `pool/` 改成前缀式匹配，这个顺序仍然是对的。
"""

from django.urls import path

from . import views

urlpatterns = [
    path("overview/", views.overview, name="regime-overview"),
    path("pool/generations/", views.pool_generations, name="regime-pool-generations"),
    path("pool/", views.pool, name="regime-pool"),
]
