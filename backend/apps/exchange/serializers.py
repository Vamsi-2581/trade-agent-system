from rest_framework import serializers
from .models import ExchangeAccount


class ExchangeAccountSerializer(serializers.ModelSerializer):
    api_key = serializers.CharField(write_only=True, required=True)
    api_secret = serializers.CharField(write_only=True, required=True)
    # `testnet` 必须**双向**：写入时用来指定「这是模拟账户」，读面上它更是客户端区分
    # 实盘/模拟的**唯一**依据（交易页的账户徽标与「实盘/模拟」页签都只看这一位）。
    # 它曾经是 `write_only=True`，于是 GET 的响应里根本没有这个键——客户端读到的永远
    # 是 `undefined`，`!undefined` 恒为真，全部账户被判成实盘。2026-10-24 那句
    # 「binance 实盘没有同步现有资产」就是这么来的：用户盯着一张标着「实盘」的卡片，
    # 而那张卡片其实是一个模拟账户（真正的实盘账户当时 `is_active=False`，压根没被列出）。
    testnet = serializers.BooleanField(default=False)
    leverage = serializers.IntegerField(write_only=True, default=5)

    class Meta:
        model = ExchangeAccount
        fields = [
            "id",
            "exchange",
            "label",
            "is_active",
            "testnet",
            "created_at",
            "api_key",
            "api_secret",
            "leverage",
        ]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        testnet = validated_data.pop("testnet", False)
        validated_data.pop("leverage", None)
        api_key = validated_data.pop("api_key")
        api_secret = validated_data.pop("api_secret")
        account = ExchangeAccount(testnet=testnet, **validated_data)
        account.api_key_enc = account.encrypt_api_key(api_key)
        account.api_secret_enc = account.encrypt_api_secret(api_secret)
        account.save()
        return account
