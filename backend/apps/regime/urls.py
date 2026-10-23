"""机制 Web 读面与写入口的路由（P5 子票 #41、#48、#42）。

前缀在 `core/urls.py` 挂成 `api/regime/`。**这是 `apps/regime` 的第一个 REST 前缀**——
它此前刻意没有（`apps/strategy_factory/urls.py:4` 把那写成了它的选择：机制只走 agent
工具与 slash 命令）。本 map 是第一次给它开 Web 面，理由与范围见 `CONTEXT.md` 的
「机制的 Web 读面与写入口」一节。

## 路由顺序

`pool/generations/` 写在 `pool/` **之前**。Django 按序匹配，`pool/` 是一条精确路径
（`^pool/$`），本不会吃掉 `pool/generations/`；把更具体的写在前面只是让阅读顺序与匹配
顺序一致——将来若有人把 `pool/` 改成前缀式匹配，这个顺序仍然是对的。

`briefing/<kind>/` 是一条**带参数**的路由，与上面三条无参数路由不互相遮蔽：`kind` 是
一个路径段，所以 `briefing/mechanism/` 只会落到它身上，不会有人误以为它是「briefing/ 这个
目录的索引页」。未知 kind 由视图回 400（枚举取值错不是「资源不存在」），所以这里没有
一条「兜底的 404」路由——那会把它变成 404，而两者要求人做的动作完全不同。

## 读与写

前四条是**读**（`IsAuthenticated`：所有登录用户都能看），最后一条 `switch/` 是**写**
（`IsStaffUser`：`is_staff` 才放行）。两者不是「同一件事的两个精度」，而是两套权限
（ADR 0005）——所以它们各占一条路由，而不是在一条路由里按方法分权：那样 `GET` 与 `POST`
的权限会写在同一处，改动其中一个时很容易连带另一个。
"""

from django.urls import path

from . import views

urlpatterns = [
    path("overview/", views.overview, name="regime-overview"),
    path("pool/generations/", views.pool_generations, name="regime-pool-generations"),
    # `pool/rebuild/<task_id>/` 与 `pool/rebuild/` 是**两条**路由而不是一条带可选段：
    # 前者的 `<task_id>` 是 Celery 的 uuid，后者是「投递」这个动作。写成一个可选段的
    # 正则，`pool/rebuild/` 与 `pool/rebuild/x/y/` 的匹配就变成一个需要读正则才说得清的
    # 问题——而它们一个是 202、一个是 404，差别够大到值得两条路径。
    #
    # 顺序：带 `<task_id>` 的那条写在 `pool/rebuild/` **之前**，理由与
    # `pool/generations/` 排 `pool/` 之前同源（见模块 docstring）——让阅读顺序与匹配顺序
    # 一致，将来有人把某条改成前缀式匹配时这个顺序仍然是对的。
    path(
        "pool/rebuild/<str:task_id>/",
        views.pool_rebuild_status,
        name="regime-pool-rebuild-status",
    ),
    path("pool/rebuild/", views.pool_rebuild_dispatch, name="regime-pool-rebuild"),
    path("pool/", views.pool, name="regime-pool"),
    path("briefing/<str:kind>/", views.briefing, name="regime-briefing"),
    path("switch/", views.switch, name="regime-switch"),
]
