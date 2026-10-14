import uuid
from django.conf import settings
from django.db import models


class FeishuUserToken(models.Model):
    """用户级别飞书 OAuth token 存储，使用 Fernet 加密。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="feishu_tokens"
    )
    open_id = models.CharField(
        max_length=128, unique=True, verbose_name="Feishu Open ID"
    )
    access_token_enc = models.BinaryField()
    refresh_token_enc = models.BinaryField()
    expires_at = models.DateTimeField(verbose_name="Access Token 过期时间")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feishu_user_tokens"
        verbose_name = "Feishu 用户 Token"

    def __str__(self):
        return f"{self.user.username} - {self.open_id[:12]}"

    def _get_fernet(self):
        from cryptography.fernet import Fernet

        key = getattr(settings, "FERNET_KEY", "")
        if not key:
            raise ValueError("FERNET_KEY not configured")
        return Fernet(key.encode())

    def encrypt_access_token(self, value: str) -> bytes:
        return self._get_fernet().encrypt(value.encode())

    def encrypt_refresh_token(self, value: str) -> bytes:
        return self._get_fernet().encrypt(value.encode())

    def decrypt_access_token(self) -> str:
        return self._get_fernet().decrypt(bytes(self.access_token_enc)).decode()

    def decrypt_refresh_token(self) -> str:
        return self._get_fernet().decrypt(bytes(self.refresh_token_enc)).decode()


class FeishuAuthSession(models.Model):
    """OAuth 授权握手期间的临时状态，授权完成后失效。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="feishu_auth_sessions",
    )
    state = models.CharField(
        max_length=256, unique=True, verbose_name="CSRF State Token"
    )
    flow_type = models.CharField(
        max_length=4,
        choices=[("qr", "QR Code"), ("url", "URL Redirect")],
        default="qr",
    )
    status = models.CharField(
        max_length=16,
        choices=[
            ("pending", "待扫码"),
            ("completed", "已完成"),
            ("expired", "已过期"),
            ("failed", "授权失败"),
        ],
        default="pending",
    )
    qr_url = models.TextField(blank=True, verbose_name="飞书授权链接")
    qr_expires_at = models.DateTimeField(
        null=True, blank=True, verbose_name="二维码过期时间"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "feishu_auth_sessions"
        verbose_name = "Feishu 授权会话"

    def __str__(self):
        return f"{self.user.username} - {self.flow_type} [{self.status}]"
