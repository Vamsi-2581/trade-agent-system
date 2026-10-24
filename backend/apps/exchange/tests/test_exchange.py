"""Tests for exchange models and utils."""

from __future__ import annotations


from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.exchange.models import ExchangeAccount
from apps.exchange.utils import get_exchange_account


class TestExchangeAccountModel(TestCase):
    """测试 ExchangeAccount 模型"""

    def test_create_exchange_account(self):
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="test_account",
            api_key_enc=b"encrypted_key_data",
            api_secret_enc=b"encrypted_secret_data",
            is_active=True,
        )
        self.assertEqual(account.exchange, "binance")
        self.assertEqual(account.label, "test_account")
        self.assertTrue(account.is_active)
        self.assertIsNotNone(account.id)
        self.assertIsNotNone(account.created_at)

    def test_string_representation(self):
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="my_binance",
            api_key_enc=b"key",
            api_secret_enc=b"secret",
        )
        self.assertIn("binance", str(account))
        self.assertIn("my_binance", str(account))

    def test_inactive_account(self):
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="frozen",
            is_active=False,
        )
        self.assertFalse(account.is_active)


class TestGetExchangeAccount(TestCase):
    """测试获取交易所账户"""

    def test_returns_none_for_nonexistent_account(self):
        result = get_exchange_account("00000000-0000-0000-0000-000000000000")
        self.assertIsNone(result)

    def test_returns_none_for_inactive_account(self):
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="test",
            is_active=False,
        )
        result = get_exchange_account(str(account.id))
        self.assertIsNone(result)

    def test_returns_account_when_active(self):
        account = ExchangeAccount.objects.create(
            exchange="binance",
            label="test_active",
            api_key_enc=b"encrypted_key",
            api_secret_enc=b"encrypted_secret",
            is_active=True,
        )
        result = get_exchange_account(str(account.id))
        self.assertIsNotNone(result)
        self.assertEqual(result.exchange, "binance")


class TestExchangeAccountReadContract(APITestCase):
    """读面必须把「实盘 / 模拟」这一位发给客户端。

    症状（2026-10-24「binance 实盘没有同步现有资产」）：交易页上每一张账户卡片都被
    标成「实盘」，于是用户盯着一个**模拟**账户等它的实盘资产出现；真正的实盘账户
    （在库里是 `is_active=False`）反而从不出现。

    成因是读面一个字段都不发：`ExchangeAccountSerializer` 把 `testnet` 声明成
    `write_only=True`（写入可用、读出不发），而前端读的 `testnet_status` 在 backend 里
    从来不存在——读到的永远是 `undefined`，于是 `!testnet_status` 恒为真，全部账户
    被判成实盘。这一条锁的就是「账户分类这一位必须出现在响应里」。
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="t_testnet_flag",
            email="t_testnet_flag@example.com",
            password="x",
        )
        self.client.force_authenticate(self.user)

    def test_testnet_flag_is_in_the_response(self):
        ExchangeAccount.objects.create(
            exchange="binance",
            label="模拟账户",
            testnet=True,
            is_active=True,
            api_key_enc=b"k",
            api_secret_enc=b"s",
        )
        ExchangeAccount.objects.create(
            exchange="binance",
            label="实盘账户",
            testnet=False,
            is_active=True,
            api_key_enc=b"k",
            api_secret_enc=b"s",
        )

        resp = self.client.get("/api/exchange/accounts/")

        self.assertEqual(resp.status_code, 200)
        by_label = {a["label"]: a for a in resp.json()}
        self.assertIs(by_label["模拟账户"]["testnet"], True)
        self.assertIs(by_label["实盘账户"]["testnet"], False)
