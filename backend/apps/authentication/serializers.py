from rest_framework import serializers
from django.contrib.auth import authenticate
from .models import User


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("email", "username", "password")

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()

    def validate(self, data):
        user = authenticate(email=data["email"], password=data["password"])
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        if not user.is_active:
            raise serializers.ValidationError("Account is disabled")
        if user.is_frozen:
            raise serializers.ValidationError("Account is frozen by risk control")
        data["user"] = user
        return data


class UserSerializer(serializers.ModelSerializer):
    """当前用户的可读档案。

    `is_staff` 是 P5 加的（`docs/adr/0005-web-write-requires-staff.md`）：前端要隐藏
    「翻开关 / 触发重算」这些按钮，就得先知道自己是不是 staff，而 `AuthContext` 此前只
    拿到 `{username, email}`。

    **刻意不加 `is_admin`**：它与 `is_staff` 是同一个布尔（`is_staff` 就是 `is_admin`
    的 property 别名），两个字段并排出现会让人以为有粒度——而 `has_perm` 恒返回 `True`、
    全仓无 Django admin，粒度并不存在。要判「是不是管理员」只有 `is_staff` 这一个说法。

    `read_only_fields = fields` 一并覆盖 `is_staff`：它是 `is_admin` 的 property，写进来
    也落不到任何列上，静默丢弃比 400 更糟。
    """

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "username",
            "is_active",
            "is_frozen",
            "is_staff",
            "created_at",
            "last_login_at",
        )
        read_only_fields = fields
