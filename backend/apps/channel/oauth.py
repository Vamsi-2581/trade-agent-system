"""Feishu (Lark) OAuth 2.0 客户端。"""

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class FeishuOAuthClient:
    """飞书用户 OAuth 客户端。

    基于飞书 v2 OAuth API：
    - URL Redirect: authorization_code grant_type
    - Token 刷新: refresh_token grant_type
    - 获取用户信息: 需额外调用 /authen/v1/user_info
    """

    AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
    USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def build_authorize_url(self, state: str, scope: str = "") -> str:
        """拼接飞书授权页 URL。

        注意：飞书用户 OAuth 不传 scope 参数，使用应用已开通的用户权限。
        """
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "state": state,
            "response_type": "code",
        }
        # scope 仅在明确需要时传入（用户 OAuth 不需要）
        if scope:
            params["scope"] = scope
        qs = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
        return f"{self.AUTHORIZE_URL}?{qs}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        """用 authorization_code 换取 user_access_token。

        返回 dict 包含: access_token, refresh_token, expires_in, open_id, name 等。
        飞书 v2 端点将数据嵌套在 data.data 下。
        """
        resp = requests.post(
            self.TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
            },
            timeout=10,
        )
        data = resp.json()
        # 打印完整响应结构（WARNING 级别确保可见）
        logger.warning(
            "[FeishuOAuth] exchange_code response: code=%s, msg=%s, data_keys=%s, data=%s",
            data.get("code"),
            data.get("msg"),
            list(data.get("data", {}).keys()) if "data" in data else [],
            data,
        )
        code_val = data.get("code")
        if code_val != 0:
            raise RuntimeError(f"Feishu OAuth exchange failed: {data}")

        # v2 API: fields at root level (access_token, expires_in, etc.)
        # v1 API had data.data nesting; v2 returns flat
        inner = data
        if "data" in inner and "access_token" not in inner:
            inner = inner.get("data", {})
            if "data" in inner:
                inner = inner["data"]

        return {
            "access_token": inner.get("access_token", ""),
            "refresh_token": inner.get("refresh_token", ""),
            "expires_in": inner.get(
                "expires_in", inner.get("refresh_expires_in", 7200)
            ),
            "open_id": inner.get("open_id", inner.get("openid", "")),
            "name": inner.get("name", ""),
            "avatar_url": inner.get("avatar_url", ""),
        }

    def get_user_info(self, access_token: str) -> dict[str, Any]:
        """用 user_access_token 获取用户信息。

        飞书 v2 token 端点不返回 open_id/name 等用户信息，需单独调用。
        """
        resp = requests.get(
            self.USER_INFO_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        data = resp.json()
        code_val = data.get("code")
        if code_val != 0:
            logger.warning(
                "[FeishuOAuth] get_user_info failed: code=%s, msg=%s, data=%s",
                code_val,
                data.get("msg"),
                data,
            )
            raise RuntimeError(f"Feishu get_user_info failed: {data}")

        user_data = data.get("data", {})
        return {
            "open_id": user_data.get("open_id", user_data.get("openid", "")),
            "union_id": user_data.get("union_id", ""),
            "name": user_data.get("name", ""),
            "en_name": user_data.get("en_name", ""),
            "avatar_url": user_data.get("avatar_url", ""),
            "email": user_data.get("email", ""),
        }

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """用 refresh_token 获取新的 access_token。

        注意：飞书的 refresh_token 是单次使用的，成功后会返回新的 refresh_token。
        返回 dict 结构同 exchange_code。
        """
        resp = requests.post(
            self.TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=10,
        )
        data = resp.json()
        code_val = data.get("code")
        if code_val != 0:
            raise RuntimeError(f"Feishu OAuth refresh failed: {data}")

        inner = data
        if "data" in inner and "access_token" not in inner:
            inner = inner.get("data", {})
            if "data" in inner:
                inner = inner["data"]

        return {
            "access_token": inner.get("access_token", ""),
            "refresh_token": inner.get("refresh_token", ""),
            "expires_in": inner.get("expires_in", 7200),
            "open_id": inner.get("open_id", ""),
        }
