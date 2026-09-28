"""`recompute_realized_pnl` 管理命令（空头回放的存量纠正入口）。

这个命令是**不可逆的写库工具**，所以这里钉的不是「算得对不对」（那是
`test_realized_pnl.py` 的 `compute_realized_pnl` 纯函数的事），而是**它在什么情况下
会安静地写错**：

1. **默认一行不写**。dry-run 只打印清单，`realized_pnl` 与 `current_equity` 都不动。
2. **只改写「与库中不同」的行**，相同的行不落笔——这是它幂等的根据。
3. **历史只取本单之前的成交**。线上是即时算的、本单当时还没落库；事后重算若不补这层
   时间窗，会把本单自己和之后的成交拉进回放。实测这一条会让 14 笔卖单里 12 笔得出与
   线上不同的数，所以它是命令与线上口径一致的**唯一**差别，必须有用例钉住。
4. **权益按本次改动的 Δ 调整，不做全量重设**。重设公式 `initial_capital + Σ(pnl)` 会
   因为「权益 2 位小数逐笔舍入」而差一分，也会污染 `initial_capital` 本身口径不同的
   会话（真实库里 `caa3c4aa` 就是）。用例里放一条「Σ 与权益本来就对不上」的会话，
   断言命令**不碰**它——除了自己那笔 Δ。
5. **`--session` 收窄改写范围**：别的会话的行一动不动。
6. **`realized_pnl` 由 `NULL` 变成数字的行也改写**（修复前返回 `None` 的情形比现在多，
   那类行此前根本没进过任何权益）。
7. **结算口径是账户+品种、不按会话过滤**：同账户同品种上另一条会话的成交也参与成本链。
   这不是命令的缺陷（线上同一条规则），但它意味着清单里会出现「本会话历史里看不到的
   成交」把成本拉低的行——用例钉住它，免得将来有人「顺手」加一层会话过滤把口径改掉。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.exchange.models import ExchangeAccount
from apps.trading.models import LiveSession, Order, Strategy

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


class _CommandTestCase(TestCase):
    """共享夹具：一个用户、一个账户、一条会话、一段真实形状的成交历史。

    长循环里的 `close_old_connections()` 在这里被换成假的。它在 `TestCase` 里**必然**
    把测试事务的连接关掉（Django 5 的 `close_if_unusable_or_obsolete` 第一条检查就是
    「autocommit 状态与设置不符就关」，而测试事务里 autocommit 恒为 False），之后的
    ORM 全炸 `InterfaceError: connection already closed`。打的是**定义处**
    `django.db.close_old_connections`——命令在函数体内 import，模块命名空间里没有第二个
    名字可打；`TestBatchLoopResetsConnections` 单独断言它确实被调到。
    """

    def setUp(self):
        super().setUp()
        patcher = patch("django.db.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)

        handle = uuid.uuid4().hex[:8]
        user = get_user_model().objects.create_user(
            email=f"{handle}@example.com",
            username=f"u-{handle}",
            password="x",
        )
        self.account = ExchangeAccount.objects.create(
            user=user,
            exchange="binance",
            label="t",
            api_key_enc=b"enc",
            api_secret_enc=b"enc",
        )
        self.strategy = Strategy.objects.create(
            name=f"S-{uuid.uuid4().hex[:8]}",
            code_path="strategies/t.py",
        )
        self.session = LiveSession.objects.create(
            user=user,
            strategy=self.strategy,
            symbol="DOGE/USDT",
            mode="live",
            status="running",
            exchange_account=self.account,
            initial_capital=Decimal("10000.00"),
            current_equity=Decimal("10000.00"),
        )

    def _order(self, minutes, side, qty, price, pnl=None, session=True):
        return Order.objects.create(
            user=self.account.user,
            exchange_account=self.account,
            symbol="DOGE/USDT",
            side=side,
            order_type="market",
            quantity=Decimal(qty),
            status="filled",
            filled_quantity=Decimal(qty),
            avg_fill_price=Decimal(price),
            realized_pnl=None if pnl is None else Decimal(pnl),
            live_session=self.session if session else None,
            created_at=NOW + timedelta(minutes=minutes),
        )

    def _run(self, *args):
        out = StringIO()
        call_command("recompute_realized_pnl", *args, stdout=out)
        return out.getvalue()


class TestDryRunWritesNothing(_CommandTestCase):
    """默认空跑：清单照打，一行不写。"""

    def test_dry_run_leaves_db_untouched(self):
        self._order(0, "buy", "100", "0.10")
        sell = self._order(10, "sell", "100", "0.11", pnl="99.0")  # 坏值

        out = self._run()

        sell.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(sell.realized_pnl, Decimal("99.0"))
        self.assertEqual(self.session.current_equity, Decimal("10000.00"))
        self.assertIn("空跑", out)
        self.assertIn("99.00000000", out)


class TestOnlyChangedRowsAreWritten(_CommandTestCase):
    """判据是「与库中不同」，不是「看起来不对」——所以它幂等。"""

    def test_matching_value_is_skipped(self):
        self._order(0, "buy", "100", "0.10")
        # 正确值 1.00（(0.11-0.10)*100），库中也是 1.00 → 不该落笔
        self._order(10, "sell", "100", "0.11", pnl="1.0")

        out = self._run()

        self.assertIn("没有需要改写的行", out)

    def test_wrong_value_is_rewritten(self):
        self._order(0, "buy", "100", "0.10")
        sell = self._order(10, "sell", "100", "0.11", pnl="99.0")

        self._run("--apply")

        sell.refresh_from_db()
        self.assertEqual(sell.realized_pnl, Decimal("1.0"))

    def test_second_run_is_noop(self):
        """改完再跑是空操作——幂等，反复运行无副作用。"""
        self._order(0, "buy", "100", "0.10")
        self._order(10, "sell", "100", "0.11", pnl="99.0")
        self._run("--apply")

        out = self._run("--apply")

        self.assertIn("没有需要改写的行", out)


class TestHistoryWindowStopsAtTheOrder(_CommandTestCase):
    """历史只取本单之前的成交——这是与线上口径一致的唯一差别。"""

    def test_later_fills_do_not_enter_the_replay(self):
        self._order(0, "buy", "100", "0.10")
        sell = self._order(10, "sell", "100", "0.11", pnl="99.0")
        # 本单之后的成交：若被拉进回放，会改变成本基准
        self._order(20, "buy", "100", "0.90")

        self._run("--apply")

        sell.refresh_from_db()
        # 只回放 0.10 那笔 → (0.11-0.10)*100 = 1.00；含之后那笔则会是别的数
        self.assertEqual(sell.realized_pnl, Decimal("1.0"))

    def test_other_session_same_account_symbol_shares_cost_chain(self):
        """同账户同品种的**另一条会话**的成交参与成本链（线上同规则，不是缺陷）。

        钉住它，是为了让「顺手加一层会话过滤」变成一个必须显式辩论的改动。
        """
        other = LiveSession.objects.create(
            user=self.account.user,
            strategy=self.strategy,
            symbol="DOGE/USDT",
            mode="live",
            status="running",
            exchange_account=self.account,
            initial_capital=Decimal("5000.00"),
            current_equity=Decimal("5000.00"),
        )
        self._order(0, "buy", "100", "0.90", session=False)  # 本会话看不到的历史
        self._order(5, "buy", "100", "0.10", session=False)
        sell = self._order(10, "sell", "100", "0.11", pnl="99.0")

        self._run("--apply")

        sell.refresh_from_db()
        # 回放含全部 200 的买入 → 成本 (0.90+0.10)/2 = 0.50 → (0.11-0.50)*100 = -39
        self.assertEqual(sell.realized_pnl, Decimal("-39.0"))
        other.refresh_from_db()
        self.assertEqual(other.current_equity, Decimal("5000.00"))


class TestEquityUsesDeltaNotFullReset(_CommandTestCase):
    """权益按本次改动的 Δ 调整；Σ 与权益本来就对不上的会话不被「修正」。"""

    def test_equity_moves_only_by_the_delta(self):
        self._order(0, "buy", "100", "0.10")
        self._order(10, "sell", "100", "0.11", pnl="99.0")

        self._run("--apply")

        self.session.refresh_from_db()
        # Δ = 1.00 - 99.00 = -98.00
        self.assertEqual(self.session.current_equity, Decimal("9902.00"))

    def test_session_with_off_balance_sheet_is_not_reset(self):
        """`initial_capital + Σ(pnl)` 不成立的会话不被拉平（真实库里 `caa3c4aa` 就是）。

        这里把会话权益刻意设成与 Σ 无关的数，再让命令跑一趟**没有改动**的清单：
        权益必须一动不动。若哪天有人把 Δ 实现改成「重设成 initial + Σ」，这条会红。
        """
        self.session.current_equity = Decimal("7.77")
        self.session.save(update_fields=["current_equity"])
        self._order(0, "buy", "100", "0.10")
        self._order(10, "sell", "100", "0.11", pnl="1.0")  # 已正确 → 无改动

        self._run("--apply")

        self.session.refresh_from_db()
        self.assertEqual(self.session.current_equity, Decimal("7.77"))


class TestNullBecomesValue(_CommandTestCase):
    """`realized_pnl` 由 NULL 变成数字的行也改写，并算进 Δ。"""

    def test_null_row_gets_a_value(self):
        self._order(0, "buy", "100", "0.10")
        sell = self._order(10, "sell", "100", "0.11", pnl=None)

        self._run("--apply")

        sell.refresh_from_db()
        self.assertEqual(sell.realized_pnl, Decimal("1.0"))
        self.session.refresh_from_db()
        self.assertEqual(self.session.current_equity, Decimal("10001.00"))


class TestBatchLoopResetsConnections(_CommandTestCase):
    """长循环的连接纪律（CLAUDE.md）：上面所有用例都把它换成了假的，删掉那行也不会红。

    所以这里单独断言「确实调了」。条数是 `ceil(N / BATCH_SIZE)`，N 是**卖单**条数
    （循环只遍历卖单）——取"1"是为了让它与 BATCH_SIZE 的取值解耦。
    """

    def test_the_batch_loop_resets_the_connection(self):
        self._order(0, "buy", "100", "0.10")
        self._order(10, "sell", "100", "0.11", pnl="99.0")

        with patch("django.db.close_old_connections") as reset:
            self._run()

        self.assertEqual(reset.call_count, 1)


class TestSessionFilter(_CommandTestCase):
    """`--session` 只收窄改写范围，别的会话一动不动。"""

    def test_other_sessions_are_untouched(self):
        other = LiveSession.objects.create(
            user=self.account.user,
            strategy=self.strategy,
            symbol="BTC/USDT",
            mode="live",
            status="running",
            exchange_account=self.account,
            initial_capital=Decimal("5000.00"),
            current_equity=Decimal("5000.00"),
        )
        self._order(0, "buy", "100", "0.10")
        mine = self._order(10, "sell", "100", "0.11", pnl="99.0")
        theirs = Order.objects.create(
            user=self.account.user,
            exchange_account=self.account,
            symbol="BTC/USDT",
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            status="filled",
            filled_quantity=Decimal("1"),
            avg_fill_price=Decimal("5.00"),
            realized_pnl=Decimal("88.0"),
            live_session=other,
            created_at=NOW + timedelta(minutes=10),
        )

        self._run("--apply", "--session", str(self.session.id))

        mine.refresh_from_db()
        theirs.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(mine.realized_pnl, Decimal("1.0"))
        self.assertEqual(theirs.realized_pnl, Decimal("88.0"))
        self.assertEqual(other.current_equity, Decimal("5000.00"))
