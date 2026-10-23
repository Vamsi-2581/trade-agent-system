"""本仓第一个角色门槛（`docs/adr/0005-web-write-requires-staff.md`）。

在此之前全仓视图只有 `IsAuthenticated`：`CONTEXT.md:163` 把「v1 没有角色模型，所有用户
平等」写成了**已知缺口**，而不是遗漏。P5 给机制开 Web 写入口之后，那批动作多了一条
通路，而网页上的大按钮比聊天里的一行命令更容易误触（其中一个还会在同一事务里顺手写
一批 `HaltDeclaration` 行），所以只对**写**加了这一道。

## 它是界面纪律，不是安全边界

slash 通路（`/regime …`）**没有**跟着收紧，管理命令也没有。任何登录用户仍然可以在
聊天里敲一行命令做同样的事。这句话必须同时出现在页面文案与 `CONTEXT.md` 里——不写，
下一个人会以为权限已经收口，而真正的后门在另一个渠道。这个类的 docstring 就是那句话
的代码侧副本。

## 判据只有一个布尔

`User.is_staff` 是 `User.is_admin` 的**别名**（`apps/authentication/models.py`），不是
Django 的 staff 概念：`has_perm` / `has_module_perms` 恒返回 `True`，全仓无 Django
admin。所以「谁是管理员」没有粒度，这次也不引入粒度——序列化器同理只透出 `is_staff`，
**不透出 `is_admin`**（两个字段会让人以为有粒度）。

## 用法

```python
@permission_classes([IsStaffUser])
def flip_switch(request): ...
```

**不要**写成 `[IsAuthenticated, IsStaffUser]`：本类已经包含了「已登录」这一半（匿名
用户没有 `is_staff`，判据为假），叠上去只是让读者以为两者可以分开满足。反过来，**读**
端点仍然只写 `[IsAuthenticated]`——本类只用在写入口。
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission


class IsStaffUser(BasePermission):
    """`request.user.is_staff` 为真才放行；匿名与普通用户一律 403/401。

    **包含认证**：未登录的请求在这里就被挡下（DRF 因为 `JWTAuthentication` 声明了
    `authenticate_header`，给出的是 401 而不是 403），所以视图不必再叠 `IsAuthenticated`。

    `message` 里刻意**不写**「联系管理员开通」这类话：本系统没有开通流程，只有数据库里
    一个布尔。写成那样等于给一个不存在的能力许愿。
    """

    message = "此操作需要管理员权限。"

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        return bool(getattr(user, "is_staff", False))
