from django.urls import path
from . import views

urlpatterns = [
    path("status/", views.channel_status),
    path("webhook/lark/", views.lark_webhook, name="lark-webhook"),
    # Feishu OAuth
    path("auth/lark/url/", views.feishu_auth_url, name="feishu-auth-url"),
    path("auth/lark/qr/", views.feishu_auth_qr, name="feishu-auth-qr"),
    path(
        "auth/lark/qr/<str:session_id>/status/",
        views.feishu_auth_qr_status,
        name="feishu-auth-qr-status",
    ),
    path(
        "auth/lark/callback/", views.feishu_oauth_callback, name="feishu-oauth-callback"
    ),
    path("auth/lark/refresh/", views.feishu_refresh_token, name="feishu-refresh-token"),
    path("auth/lark/status/", views.feishu_auth_status, name="feishu-auth-status"),
    path("auth/lark/revoke/", views.feishu_revoke, name="feishu-revoke"),
]
