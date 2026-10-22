"""本仓第一个角色门槛（P5 T1，#43；`docs/adr/0005-web-write-requires-staff.md`）。

这份测试钉三件事，每件坏了都是**静默**的：

1. **`IsStaffUser` 真的拦人**：`is_admin=False` 的登录用户拿 403、`is_admin=True` 放行、
   匿名 401。只钉「staff 能过」是没用的——那种用例在一个恒返回 `True` 的实现下照样绿，
   而这正是要防的形态（权限类写错方向时，功能看起来完全正常）。
2. **`/api/auth/me/` 透出 `is_staff`**：前端要靠它决定按钮显示与否。少了这个字段，
   页面上不会报错，只会**永远不显示**写按钮，或者（更糟）永远显示——两种都不会红。
3. **它不透出 `is_admin`**：两者是同一个布尔（`is_staff` 是 `is_admin` 的 property 别名），
   并排出现会让人以为有粒度。这条断言的是**键集**，不是「有 is_staff 就行」——加回
   `is_admin` 不会让第 2 条变红，所以必须单独钉。

`is_admin=False` 那一条是**反向用例**：删掉权限类、或把它写成 `return True`，它会红。

探针视图与 `urlpatterns` 定义在本模块里，`ROOT_URLCONF` 指回本模块（`__name__`）——不往
生产 `core/urls.py` 里塞一条只为测试存在的路由，也不去挑一个恰好用了本权限类的既有视图
（那会让这条测试跟着那个视图一起漂走）。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import AccessToken

from apps.authentication.permissions import IsStaffUser

User = get_user_model()

PROBE = "/probe/"
ME = "/api/auth/me/"


@api_view(["GET"])
@permission_classes([IsStaffUser])
def _staff_probe(request):
    """只回答「过了权限类没有」，不带任何业务。"""
    return Response({"ok": True, "user": request.user.username})


urlpatterns = [path("probe/", _staff_probe)]


@pytest.fixture
def probe_urlconf(settings):
    """把 `ROOT_URLCONF` 指到本模块，探针路由才存在。

    只装在 `TestIsStaffUser` 上——`/api/auth/me/` 那组要的是真的 `core.urls`。
    """
    settings.ROOT_URLCONF = __name__


def _client(user=None) -> Client:
    """带 JWT 的客户端；`user=None` 时是匿名。写法照
    `apps/strategy_factory/tests/test_api_contract.py` 的 `_client`。"""
    client = Client()
    if user is not None:
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {AccessToken.for_user(user)}"
    return client


def _user(username: str, *, is_admin: bool = False):
    """`UserManager.create_user` 不接 `is_admin`（它只认 email/username/password），
    而 `is_admin` 是 `is_staff` 背后那个布尔——必须单独写一行。"""
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="pw12345",
    )
    if is_admin:
        user.is_admin = True
        user.save(update_fields=["is_admin"])
    return user


@pytest.mark.django_db
@pytest.mark.usefixtures("probe_urlconf")
class TestIsStaffUser:
    """`IsStaffUser` 的三条路：匿名 401、普通用户 403、staff 放行。"""

    def test_staff_user_passes(self):
        assert _client(_user("staff-ok", is_admin=True)).get(PROBE).status_code == 200

    def test_non_staff_user_is_forbidden(self):
        response = _client(_user("staff-no", is_admin=False)).get(PROBE)
        assert response.status_code == 403

    def test_anonymous_is_unauthenticated_not_forbidden(self):
        """匿名是 401 不是 403：`JWTAuthentication` 声明了 `authenticate_header`，
        所以 DRF 走 `NotAuthenticated`。401 与 403 对前端是两件事——前者要重新登录，
        后者是「你没有这个权限」，混成 403 会让页面把用户踢去登录页重试。"""
        assert _client().get(PROBE).status_code == 401

    def test_frozen_user_is_not_special_cased(self):
        """冻结是登录闸门（`LoginSerializer`），不是权限。冻结的 staff 仍然是 staff——
        本类只回答「是不是 staff」这一个问题，多答一个就成了第二套权限模型。"""
        user = _user("staff-frozen", is_admin=True)
        user.is_frozen = True
        user.save(update_fields=["is_frozen"])
        assert _client(user).get(PROBE).status_code == 200

    def test_inactive_user_is_not_special_cased(self):
        """`is_active=False` 同样不在本类的判据里：JWT 认证已经拦下了它（`JWTAuthentication`
        在 `get_user` 里校验 `is_active`），这里只确认不会**双重**判断出矛盾结果。"""
        user = _user("staff-inactive", is_admin=True)
        user.is_active = False
        user.save(update_fields=["is_active"])
        assert _client(user).get(PROBE).status_code == 401


@pytest.mark.django_db
class TestMeExposesIsStaff:
    """`GET /api/auth/me/` 的形状——前端判断按钮显隐的唯一来源。"""

    def test_me_includes_is_staff_true_for_admin(self):
        data = _client(_user("me-admin", is_admin=True)).get(ME).json()
        assert data["is_staff"] is True

    def test_me_includes_is_staff_false_for_normal_user(self):
        data = _client(_user("me-normal", is_admin=False)).get(ME).json()
        assert data["is_staff"] is False

    def test_me_does_not_expose_is_admin(self):
        """两个字段并排 = 暗示有粒度，而 `has_perm` 恒返回 `True`、全仓无 Django admin。
        断言键集而不是「没有某个值」：`is_admin` 即使是 `False` 也不该出现。"""
        data = _client(_user("me-keys", is_admin=True)).get(ME).json()
        assert "is_admin" not in data
        assert set(data) == {
            "id",
            "email",
            "username",
            "is_active",
            "is_frozen",
            "is_staff",
            "created_at",
            "last_login_at",
        }

    def test_me_requires_auth(self):
        assert _client().get(ME).status_code == 401


@pytest.mark.django_db
class TestIsStaffIsReadOnly:
    """`is_staff` 是 `is_admin` 的 property，不是列——写进来落不到任何地方。"""

    def test_serializer_ignores_is_staff_in_input(self):
        from apps.authentication.serializers import UserSerializer

        user = _user("ser-ro", is_admin=False)
        serializer = UserSerializer(
            instance=user, data={"is_staff": True}, partial=True
        )
        assert serializer.is_valid(), serializer.errors
        serializer.save()
        user.refresh_from_db()
        assert user.is_admin is False
        assert UserSerializer(instance=user).data["is_staff"] is False
