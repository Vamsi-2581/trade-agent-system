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
目录的索引页」。

**未知 kind 由视图回 400**（枚举取值错不是「资源不存在」），而 Django 的路由匹配另有一组
形状会先命中、走不到视图——它们各是各的状态码，不是「404 一条兜底」：

| 请求形状 | 结果 | 为什么 |
| --- | --- | --- |
| `briefing/bogus/` | 400 `{"error"}` | 一个合法路径段、但不是枚举取值 → 视图判 |
| `briefing/mechanism` | 301 | `APPEND_SLASH` 把它跳到带斜杠那条 |
| `briefing/` `briefing//` | 404 | 没有 `kind` 段，路由根本没匹配上 |
| `briefing/a/b/` | 404 | 多一段，`<str:kind>` 不跨斜杠 |

所以「未知 kind 一律 400」只在**有 kind 段**时成立；上面后三种是路由层的「没有这一页」，
不是「kind 拼错了」。不为它们加兜底路由：把「没有这个 URL」改写成 400 会让客户端以为
自己该改参数，而实际该改的是路径结构。

## 读与写

前四条是**读**（`IsAuthenticated`：所有登录用户都能看），`switch/` 与两条
`strategies/<id>/{retire,restore}/` 是**写**（`IsStaffUser`：`is_staff` 才放行）。两者不是
「同一件事的两个精度」，而是两套权限（ADR 0005）——所以它们各占一条路由，而不是在一条
路由里按方法分权：那样 `GET` 与 `POST` 的权限会写在同一处，改动其中一个时很容易连带
另一个。

`strategies/<id>/`（读，确认页）与 `strategies/<id>/retire/`（写）因此也是两条：前者是
「这一页说的会发生什么」，后者是「动手」。**这不是同一条路由的 GET/POST**——ADR 0005 的
那句话要求读对所有人开放、写只对 `is_staff`，而按方法分权正好把这两套权限塞回同一处。

**`retire/` 不是 `DELETE`**：它一行都不删，翻的是 `Strategy.is_active`（退役）。用 POST
是因为它是一次动作（翻开关 + 落一条启停流水），不是对某个资源的删除；叫 DELETE 会让
下一个人以为库里那行没了。
"""

from django.urls import path

from . import views

urlpatterns = [
    path("overview/", views.overview, name="regime-overview"),
    # `halt/` 与 `overview/` 是**两条**读面，不是一条的两个精度：前者答「此刻在拦什么」
    # （halt 状态机的求值），后者答「机制整体是什么档」。合成一条会让交易页的横幅与
    # `/regime` 的首屏被迫同频——而停止判定比开关档位变得快得多（解除也要立刻看见）。
    path("halt/", views.halt_state, name="regime-halt-state"),
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
    # `pool/cell/` 排在 `pool/` 之前，理由与 `pool/generations/` 同源（见模块 docstring）：
    # 让阅读顺序与匹配顺序一致。它是 `pool/?evidence=0` 的配套读面——列表只发矩阵，
    # 一格的依据按需拉（详见 `views.pool_cell` 的 docstring）。
    path("pool/cell/", views.pool_cell, name="regime-pool-cell"),
    path("pool/", views.pool, name="regime-pool"),
    # 策略的「机制在册」总开关（`/regime` 页面上的「删除策略」）。三条路由而不是一条：
    # 读（确认页，所有登录用户）与两个写（退役 / 恢复启用，`is_staff`）各有各的权限，
    # 与 `pool/rebuild/<task_id>/` + `pool/rebuild/` 同源的理由——权限不同的事不写在同一
    # 条路由里按方法分权，改其中一个时就不会连带另一个。
    #
    # 两个写分别一条而不是一条带 `to_active` 参数：`<uuid>` 后那段是**动作**，动作的名字
    # 应当出现在 URL 里（与 `sessions/<pk>/stop/`、`/pause/` 同形），而不是藏进请求体——
    # 否则排障时一条 POST 记录答不出它到底翻了哪个方向。
    #
    # 顺序：三条都带 `<uuid:strategy_id>`，读那条更短（`strategies/<id>/`），写在后面。
    # Django 按序匹配，短的不会吃掉长的（两条写各自多一段路径），把读排前面只是让阅读
    # 顺序与匹配顺序一致——与 `pool/generations/` 排 `pool/` 之前同源。
    path(
        "strategies/<uuid:strategy_id>/",
        views.strategy_lifecycle_page,
        name="regime-strategy-lifecycle",
    ),
    path(
        "strategies/<uuid:strategy_id>/retire/",
        views.retire_strategy,
        name="regime-strategy-retire",
    ),
    path(
        "strategies/<uuid:strategy_id>/restore/",
        views.restore_strategy,
        name="regime-strategy-restore",
    ),
    path("briefing/<str:kind>/", views.briefing, name="regime-briefing"),
    path("switch/", views.switch, name="regime-switch"),
]
