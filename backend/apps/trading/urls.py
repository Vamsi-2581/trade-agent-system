from django.urls import path
from . import views

urlpatterns = [
    path("orders/", views.order_list),
    path("orders/<uuid:pk>/", views.order_detail),
    path("klines/", views.klines),
    path("strategies/", views.strategy_list),
    # 某策略声明的参数表（部署会话时可覆盖的键）——与 `sessions/<pk>/` 详情里的
    # `params` 是两件事：这里说"策略允许设什么"，那里说"这条会话实际在跑什么"。
    path("strategies/<str:name>/params/", views.strategy_params),
    path("summary/", views.trading_summary),
    path("positions/", views.position_list),
    path("accounts/", views.account_list),
    # Live Sessions
    path("sessions/", views.live_session_list),
    path("sessions/<uuid:pk>/", views.live_session_detail),
    path("sessions/create/", views.live_session_create),
    path("sessions/<uuid:pk>/start/", views.live_session_start),
    path("sessions/<uuid:pk>/pause/", views.live_session_pause),
    path("sessions/<uuid:pk>/resume/", views.live_session_resume),
    path("sessions/<uuid:pk>/stop/", views.live_session_stop),
    path("sessions/<uuid:pk>/promote/", views.live_session_promote),
    path("sessions/<uuid:pk>/delete/", views.live_session_delete),
]
