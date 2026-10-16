"""候选策略流水线的路由（spec #19 D21，子票 #23）。

前缀在 `core/urls.py` 挂成 `api/strategy-factory/`，与 app 边界一致
（`apps/regime` 只走 agent 工具、没有 REST 前缀——那是它的选择，不照抄）。
"""

from django.urls import path

from . import views

urlpatterns = [
    # 登记在 <uuid> 之前：Django 按序匹配，`register` 不是 uuid，不会撞 <uuid:pk>；
    # 显式写在前面让阅读顺序与调用顺序一致（先有登记，后有单条操作）。
    path("candidates/", views.candidate_list, name="strategy-factory-candidate-list"),
    path(
        "candidates/register/",
        views.candidate_register,
        name="strategy-factory-candidate-register",
    ),
    path(
        "candidates/<uuid:pk>/",
        views.candidate_detail,
        name="strategy-factory-candidate-detail",
    ),
    path(
        "candidates/<uuid:pk>/promote/",
        views.candidate_promote,
        name="strategy-factory-candidate-promote",
    ),
    path(
        "candidates/<uuid:pk>/reject/",
        views.candidate_reject,
        name="strategy-factory-candidate-reject",
    ),
    path(
        "candidates/<uuid:pk>/cancel/",
        views.candidate_cancel,
        name="strategy-factory-candidate-cancel",
    ),
]
