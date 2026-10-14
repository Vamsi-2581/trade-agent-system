"""减仓执行器（第②段单元 ②d）：从「此刻该不该动手」到「消息怎么写」。

`test_halt.py` 钉的是**读**的一侧（一条声明此刻挡不挡得住），`test_halt_sync.py` 钉的是
**写**的一侧（谁该拦 → 声明行）。这个文件钉的是**动**的一侧，四件事各一组：

1. **触发源**。「窗口开着」的判据是 `halt_at <= at < resume_at`，**不是「表里有行」**
   ——`halt_sync` 会把下一段提前写进声明表，拿有行当判据的表现是「还没到窗口就把仓减了」。
   再叠一层「这个事件压在哪个作用域上」（`halt_sync.touches`），以及「哪个品种真在手上」
   （必须来自真实持仓，不能在规划期猜一份清单）。
2. **分片算术**。先减半再按步进取整、片数受 `min_qty` 与 `min_notional` 两道约束、
   问不到最小名义额就整笔一次、降片到底仍不足最小额就整笔一次（CONTEXT.md:127）。
   全是不吃库、不读时钟的纯函数，所以它们跑在 `SimpleTestCase` 上。
3. **记录与闸门**。认领幂等（唯一键在，不是这里的 `created` 判断）、基准在当天第一行冻结、
   「第 N 次」数事件不数动作、Q8 的「本日已收场就停」——**`NO_OP` 不算收场**。
4. **渲染**。滑点三档各写一句不同的话、比例取不到时写明「未判定」、撤单那一段挂在
   账户口径而不是逐品种记录上。

两半分开跑，理由与 `test_halt_sync.py` 同一个：`SimpleTestCase` **就是**「这几条不许碰库」
的执行形态，而 DB 那一半要的是真事务。**没有一条测试去 mock 适配器**——`reduce_run` 的
async 段（`_run_account` / `_execute_symbol`）刻意留白：它要一个真的交易所，而这一层的
纪律是「机械的那一半已经全部下移到 `apps/trading/reduce.py`」（那个模块有 39 条自己的
测试）。这里测的是**在什么条件下才轮到它**，以及**轮到之后留下什么痕迹**。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from apps.exchange.models import ExchangeAccount
from apps.regime import halt, halt_sync, reduce_run
from apps.regime.models import (
    ActorKind,
    EventImpact,
    EventScope,
    EventStatus,
    HaltDeclaration,
    HaltTrigger,
    MajorEvent,
    MechanismKind,
    MechanismMode,
    ReduceStatus,
    RegimeMechanismSwitch,
    RegimeReducePause,
    RegimeReduceRecord,
    SlippageBasis,
)

NOW = datetime(2026, 9, 23, 4, 0, tzinfo=dt_timezone.utc)
DAY = date(2026, 9, 23)
SYMBOL = "DOGE/USDT"
SYMBOL_NATIVE = "DOGEUSDT"

#: 密钥的 Fernet 口令。模块级生成一次：`ExchangeAccount` 的加解密都走 `settings.FERNET_KEY`，
#: 而 `override_settings` 打在类上，两者必须是同一个值。
FERNET_KEY = Fernet.generate_key().decode()

#: 比 `reduce_run.MIN_KEY_LENGTH` 长的假密钥——短的那些会被 `configured_accounts` 挡掉。
LONG_KEY = "k" * 32
LONG_SECRET = "s" * 32


# --------------------------------------------------------------------------- #
# 替身与夹具
# --------------------------------------------------------------------------- #


def _event(
    name: str = "FOMC 议息",
    *,
    halt_at: datetime | None = None,
    resume_at: datetime | None = None,
    impact: EventImpact = EventImpact.HIGH,
    status: EventStatus = EventStatus.SCHEDULED,
    scope_kind: EventScope = EventScope.MARKET,
    symbols: list[str] | None = None,
) -> MajorEvent:
    """**不落库**的 `MajorEvent`。纯函数那半段只读 `triggers_halt` 与两个时刻，不必有库。"""
    start = halt_at if halt_at is not None else NOW - timedelta(hours=1)
    return MajorEvent(
        name=name,
        scope_kind=scope_kind.value,
        symbols=symbols or [],
        event_time=start,
        impact=impact.value,
        halt_at=start,
        resume_at=resume_at if resume_at is not None else start + timedelta(hours=2),
        status=status.value,
        created_by="tester",
    )


def _event_row(**over) -> MajorEvent:
    """落库的事件。`RegimeReduceRecord.event` 是 PROTECT 外键，认领那一半要真行。"""
    fields = {
        "name": "FOMC 议息",
        "scope_kind": EventScope.MARKET.value,
        "symbols": [],
        "event_time": NOW - timedelta(hours=1),
        "impact": EventImpact.HIGH.value,
        "halt_at": NOW - timedelta(hours=1),
        "resume_at": NOW + timedelta(hours=1),
        "status": EventStatus.SCHEDULED.value,
        "created_by": "tester",
    }
    fields.update(over)
    return MajorEvent.objects.create(**fields)


class _FakeAccount:
    """只给 `shard_client_id` 用的替身：那个函数只读 `id.hex`，不碰库。"""

    def __init__(self, hex_id: str = "0123456789abcdef0123456789abcdef"):
        self.id = uuid.UUID(hex_id)


def _fernet() -> Fernet:
    return Fernet(FERNET_KEY.encode())


def _account(
    *,
    exchange: str = "binance",
    label: str = "主账户",
    key: str = LONG_KEY,
    secret: str = LONG_SECRET,
    user=None,
    active: bool = True,
) -> ExchangeAccount:
    fernet = _fernet()
    return ExchangeAccount.objects.create(
        exchange=exchange,
        label=label,
        api_key_enc=fernet.encrypt(key.encode()),
        api_secret_enc=fernet.encrypt(secret.encode()),
        is_active=active,
        user=user,
    )


def _declare(
    *,
    trigger: HaltTrigger = HaltTrigger.EVENT,
    scope: str | None = None,
    opened_at: datetime | None = None,
    expires_at: datetime | None = None,
    closed_at: datetime | None = None,
) -> HaltDeclaration:
    return HaltDeclaration.objects.create(
        trigger=trigger.value,
        scope=scope if scope is not None else halt.global_scope(),
        label="FOMC 议息",
        opened_at=opened_at if opened_at is not None else NOW - timedelta(hours=2),
        expires_at=expires_at,
        closed_at=closed_at,
        reason="高影响事件窗口",
        actor_kind=ActorKind.TASK.value,
        actor_name="regime.sync_halt_windows",
    )


def _switch(kind: MechanismKind, mode: MechanismMode) -> RegimeMechanismSwitch:
    return RegimeMechanismSwitch.objects.create(
        kind=kind.value,
        from_mode=MechanismMode.SHADOW.value,
        to_mode=mode.value,
        at=NOW - timedelta(days=1),
        actor_kind=ActorKind.CLI.value,
        actor_name="ops",
        reason="测试",
    )


def _user(email: str, *, active: bool = True):
    User = get_user_model()
    user = User.objects.create_user(
        email=email, username=email.split("@")[0], password="pw12345"
    )
    if not active:
        user.is_active = False
        user.save(update_fields=["is_active"])
    return user


def _record(account, event, **over) -> RegimeReduceRecord:
    fields = {
        "exchange_account": account,
        "event": event,
        "symbol": SYMBOL_NATIVE,
        "business_day": DAY,
        "ordinal": 1,
        "status": ReduceStatus.CLAIMED.value,
    }
    fields.update(over)
    return RegimeReduceRecord.objects.create(**fields)


def _pause(account, *, symbol: str = SYMBOL_NATIVE, closed_at: datetime | None = None):
    return RegimeReducePause.objects.create(
        exchange_account=account,
        symbol=symbol,
        reason="滑点超限",
        evidence={"slippage_pct": "0.9"},
        opened_at=NOW - timedelta(minutes=5),
        closed_at=closed_at,
        actor_kind=reduce_run.ACTOR_KIND.value,
        actor_name=reduce_run.ACTOR_NAME,
    )


# --------------------------------------------------------------------------- #
# 触发源（纯：候选事件当参数收进来）
# --------------------------------------------------------------------------- #


class TestFiringWindow(SimpleTestCase):
    """`firing_pairs` 的窗口判据：`halt_at <= at < resume_at`，两个取等号方向都要对。

    只钉「窗口开着就减」的话，把「表里有行」当判据的实现也照样通过——而那个实现会在
    `halt_sync` 提前写下下一段的那一刻就开始减仓（下面 `test_the_next_segment_is_written_
    early_but_does_not_fire_yet` 就是那条）。
    """

    def _pairs(self, event, scopes=None):
        return reduce_run.firing_pairs(
            scopes if scopes is not None else [halt.global_scope()], [event], NOW
        )

    def test_a_window_containing_now_fires(self):
        pairs = self._pairs(_event(halt_at=NOW - timedelta(minutes=1)))
        self.assertEqual(
            [(p.event_name, p.scope) for p in pairs], [("FOMC 议息", "global")]
        )

    def test_halt_at_is_inclusive(self):
        # 生效的**那一刻**就该动手：写 `halt_at < at` 会让窗口起点漏一拍。
        self.assertEqual(len(self._pairs(_event(halt_at=NOW))), 1)

    def test_resume_at_is_exclusive(self):
        # 截止那一秒不再动手：写成 `resume_at >= at` 会让窗口多出一拍，
        # 而那一拍正好是事件结果出来之后。
        self.assertEqual(
            self._pairs(_event(halt_at=NOW - timedelta(hours=1), resume_at=NOW)), []
        )

    def test_the_next_segment_is_written_early_but_does_not_fire_yet(self):
        """**表里有行 ≠ 此刻该减**。`halt_sync` 会把下一段提前写进声明表（那个模块 docstring
        的「为什么『下一段』要提前写进表里」），所以候选里有一行完全不代表它在拦。
        """
        future = _event(name="下次议息", halt_at=NOW + timedelta(hours=3))
        self.assertEqual(self._pairs(future), [])

    def test_a_cancelled_event_does_not_fire(self):
        # `triggers_halt` 是「已排期 ∧ 档位为高」的合取，取消掉的事件到这里自然就掉了。
        cancelled = _event(status=EventStatus.CANCELLED)
        self.assertEqual(self._pairs(cancelled), [])

    def test_a_medium_impact_event_does_not_fire(self):
        self.assertEqual(self._pairs(_event(impact=EventImpact.MEDIUM)), [])

    def test_firing_needs_a_declaration_on_that_scope(self):
        """事件压在哪个作用域，由 `halt_sync.touches` 答。**各写一份就是给「该不该减」
        造第二个答案**，而分歧的表现正是最难查的那种：声明写着熔断中、减仓一动不动。
        """
        market = _event()
        # 全市场事件只压在 global 上：一条品种档声明认不出它。
        self.assertEqual(self._pairs(market, [halt.symbol_scope(SYMBOL)]), [])
        self.assertEqual(len(self._pairs(market, [halt.global_scope()])), 1)

    def test_a_symbol_event_fires_only_on_its_own_scope(self):
        event = _event(scope_kind=EventScope.SYMBOLS, symbols=[SYMBOL])

        self.assertEqual(
            self._pairs(event, [halt.symbol_scope(SYMBOL)]),
            [reduce_run.FiringScope(None, "FOMC 议息", f"symbol:{SYMBOL}")],
        )
        self.assertEqual(self._pairs(event, [halt.symbol_scope("BTC/USDT")]), [])
        self.assertEqual(self._pairs(event, [halt.global_scope()]), [])

    def test_every_matching_scope_gets_its_own_pair(self):
        event = _event(scope_kind=EventScope.SYMBOLS, symbols=[SYMBOL, "BTC/USDT"])
        scopes = [halt.symbol_scope(SYMBOL), halt.symbol_scope("BTC/USDT")]

        pairs = self._pairs(event, scopes)

        self.assertEqual([p.scope for p in pairs], scopes)


class TestTargetsFor(SimpleTestCase):
    """（事件 × 作用域）→（事件 × 品种）。**这一步必须拿真实持仓来筛。**"""

    def _firing(self, scope=None, event_id: int = 1, name: str = "FOMC 议息"):
        return reduce_run.FiringScope(event_id, name, scope or halt.global_scope())

    def test_a_global_scope_covers_every_position(self):
        positions = {
            SYMBOL_NATIVE: ("long", Decimal("10")),
            "BTCUSDT": ("short", Decimal("2")),
        }

        targets = reduce_run.targets_for([self._firing()], positions)

        self.assertEqual(
            [(t.symbol, t.event_id, t.scope) for t in targets],
            [("BTCUSDT", 1, "global"), (SYMBOL_NATIVE, 1, "global")],
        )

    def test_a_symbol_scope_covers_only_that_symbol(self):
        positions = {
            SYMBOL_NATIVE: ("long", Decimal("10")),
            "BTCUSDT": ("long", Decimal("2")),
        }

        targets = reduce_run.targets_for(
            [self._firing(halt.symbol_scope(SYMBOL))], positions
        )

        self.assertEqual([t.symbol for t in targets], [SYMBOL_NATIVE])

    def test_a_symbol_scope_we_do_not_hold_produces_nothing(self):
        """事件压在某个品种上，而账户里没有这个品种——**不打无准备的减仓，也不写记录**。
        为它写一行就等于替机制断言了一次它没做过的事。
        """
        positions = {"BTCUSDT": ("long", Decimal("2"))}

        self.assertEqual(
            reduce_run.targets_for(
                [self._firing(halt.symbol_scope(SYMBOL))], positions
            ),
            [],
        )

    def test_an_empty_book_produces_nothing(self):
        self.assertEqual(reduce_run.targets_for([self._firing()], {}), [])

    def test_a_strategy_scope_never_reduces(self):
        """策略档声明拦的是「某个策略的下单」，不是「某个账户的持仓」——**减仓的作用对象是
        账户持仓**（CONTEXT.md:116），两者对不上。第③段的 gate 才会用到策略档。
        """
        positions = {SYMBOL_NATIVE: ("long", Decimal("10"))}

        self.assertEqual(
            reduce_run.targets_for(
                [self._firing(halt.strategy_scope("s-1"))], positions
            ),
            [],
        )

    def test_an_unreadable_scope_falls_back_to_the_whole_book(self):
        """读不懂的作用域当**全市场**（`halt.parse_scope` 的 fail-closed）——所以读坏的作用域
        会真的去减，而不是静默一动不动。看不懂的写法只能来自写入方的 bug，而一条写坏了的
        声明静默失效是没有任何别的信号能暴露的。
        """
        positions = {
            SYMBOL_NATIVE: ("long", Decimal("10")),
            "BTCUSDT": ("long", Decimal("2")),
        }

        targets = reduce_run.targets_for([self._firing("SYMBOL:DOGE/USDT")], positions)

        self.assertEqual([t.symbol for t in targets], ["BTCUSDT", SYMBOL_NATIVE])

    def test_the_scope_symbol_is_normalised_to_the_exchange_dialect(self):
        """作用域里是事件口径（`BASE/QUOTE`），持仓的键是交易所口径（`BASEQUOTE`），
        换算只走 `reduce.normalize_symbol` 那一个口。
        """
        positions = {SYMBOL_NATIVE: ("long", Decimal("10"))}

        targets = reduce_run.targets_for(
            [self._firing(halt.symbol_scope(SYMBOL))], positions
        )

        self.assertEqual([t.symbol for t in targets], [SYMBOL_NATIVE])

    def test_a_symbol_is_claimed_by_the_first_event_only(self):
        """**一个品种一轮只认领一次**。`firing` 已按 `halt_at` / `id` 排好，所以「第一件」
        是确定的那一件——不是随便一件，否则同一轮重跑会换一个事件，认领键也跟着换。
        """
        positions = {SYMBOL_NATIVE: ("long", Decimal("10"))}
        firing = [
            self._firing(event_id=7, name="先到的"),
            self._firing(event_id=9, name="后到的"),
        ]

        targets = reduce_run.targets_for(firing, positions)

        self.assertEqual(
            [(t.symbol, t.event_id, t.event_name) for t in targets],
            [(SYMBOL_NATIVE, 7, "先到的")],
        )

    def test_a_target_holds_exactly_one_symbol(self):
        """装成列表的话 `_run_symbol` 就得再拆一次，而拆的时候漏掉「这个 Target 其实覆盖
        两个品种」，第二个品种会静默地不减。
        """
        with self.assertRaises(RuntimeError):
            reduce_run.Target(1, "x", "global", (SYMBOL_NATIVE, "BTCUSDT")).symbol

        self.assertEqual(
            reduce_run.Target(1, "x", "global", (SYMBOL_NATIVE,)).symbol, SYMBOL_NATIVE
        )


class TestNormalizeScopeSymbol(SimpleTestCase):
    def test_only_symbol_scopes_carry_a_symbol(self):
        self.assertEqual(
            reduce_run.normalize_scope_symbol("symbol", SYMBOL), SYMBOL_NATIVE
        )
        # 全市场 = 不筛；策略档与空值同理（策略档到不了这一步，但函数自己的契约要自洽）。
        self.assertIsNone(reduce_run.normalize_scope_symbol("global", None))
        self.assertIsNone(reduce_run.normalize_scope_symbol("symbol", ""))
        self.assertIsNone(reduce_run.normalize_scope_symbol("strategy", "s-1"))


# --------------------------------------------------------------------------- #
# 分片（纯算术）
# --------------------------------------------------------------------------- #


class TestFloorToStep(SimpleTestCase):
    def test_it_never_rounds_up(self):
        """向上取整会让请求量**大于**该减的量，而减仓多减的部分没有「补回」（CONTEXT.md:124）。"""
        self.assertEqual(
            reduce_run._floor_to_step(Decimal("1.99"), Decimal("0.5")), Decimal("1.5")
        )
        self.assertEqual(
            reduce_run._floor_to_step(Decimal("0.49"), Decimal("0.5")), Decimal("0")
        )

    def test_a_missing_or_nonsense_step_changes_nothing(self):
        # 步进问不到时不在这里抛：那是 `_load_rules` 的结论（放弃下这一单），不是算术的问题。
        for step in (None, Decimal("0"), Decimal("-1")):
            self.assertEqual(
                reduce_run._floor_to_step(Decimal("1.23"), step), Decimal("1.23"), step
            )


class TestPlanShards(SimpleTestCase):
    """三个数是有序地问的，每个都改变结论（CONTEXT.md:127）。"""

    def _plan(self, qty, **over):
        fields = {
            "step_size": Decimal("0.1"),
            "min_qty": Decimal("0.1"),
            "min_notional": Decimal("5"),
            "reference": Decimal("100"),
            "shard_count": 3,
        }
        fields.update(over)
        return reduce_run.plan_shards(Decimal(qty), **fields)

    def test_half_is_floored_to_the_step_before_anything_else(self):
        plan = self._plan("10.07")  # 一半 5.035 → 5.0

        self.assertEqual(plan.planned_qty, Decimal("5.0"))
        self.assertLessEqual(plan.total, plan.planned_qty)

    def test_a_half_below_one_step_is_a_no_op_not_a_failure(self):
        """取整后为 0 是**真的没什么可减**，所以返回空 `shards` 而不是抛错。"""
        plan = self._plan("0.15", min_qty=Decimal("0.1"))  # 一半 0.075 → 0

        self.assertEqual(plan.planned_qty, Decimal("0"))
        self.assertEqual(plan.shards, ())
        self.assertFalse(plan.ok)
        self.assertIn("不足一个步进", plan.note)

    def test_the_remainder_goes_to_the_earlier_shards(self):
        """余数按**一个步进**逐片补、从第一片开始轮，不留余数在中间片里：任何一片小于
        `min_qty` 都会被交易所拒，而「补到前面的片」在总量上等价、在失败面上严格更好。
        """
        plan = self._plan("10")  # 一半 5.0，三片：1.6 × 3 + 两个步进补到前两片

        self.assertEqual(plan.shards, (Decimal("1.7"), Decimal("1.7"), Decimal("1.6")))
        self.assertEqual(plan.total, plan.planned_qty)
        self.assertIn("共 3 片", plan.note)

    def test_every_shard_is_a_multiple_of_the_step(self):
        plan = self._plan("7")
        step = Decimal("0.1")
        for shard in plan.shards:
            self.assertEqual(shard % step, 0, shard)

    def test_min_qty_caps_the_shard_count(self):
        """片数上限是 `shard_count`，但同时受 `min_qty` 约束：每片至少一个 `min_qty`，
        否则那些片会被交易所按最小起订量拒掉。一半 5.0、每片至少 2 → 只能切两片。
        """
        plan = self._plan("10", min_qty=Decimal("2"))

        self.assertEqual(len(plan.shards), 2)
        self.assertEqual(plan.shards, (Decimal("2.5"), Decimal("2.5")))
        for shard in plan.shards:
            self.assertGreaterEqual(shard, Decimal("2"))

    def test_an_unknown_min_notional_means_one_whole_order(self):
        """知道精度但不知道最小额，就没法判「每片够不够」——**不切**。按某个假设切下去是
        把一个问不到的数当成已知，而它的表现是「少切了几片」，看不出来。
        """
        plan = self._plan("10", min_notional=None)

        self.assertEqual(plan.shards, (plan.planned_qty,))
        self.assertIn("最小名义额问不到", plan.note)

    def test_an_adapter_that_declares_no_min_notional_is_reported_as_such(self):
        # `min_notional <= 0` = 适配器说它不管这件事，与「问不到」是两件事、两句话。
        plan = self._plan("10", min_notional=Decimal("0"), min_qty=Decimal("10"))

        self.assertEqual(plan.shards, (plan.planned_qty,))
        self.assertIn("不声明最小名义额约束", plan.note)

    def test_a_known_min_notional_downgrades_the_shard_count(self):
        plan = self._plan("10", min_notional=Decimal("200"))

        # 5.0/3 × 100 = 166.7 < 200，降到两片：2.5 × 100 = 250 ≥ 200。
        self.assertEqual(len(plan.shards), 2)

    def test_an_unknown_reference_price_does_not_downgrade(self):
        """参考价取不到时**不降片**：不知道名义价值却按某个假设降片，是把一个问不到的数
        当成已知——而它的表现是「少切了几片」，看不出来。
        """
        plan = self._plan("10", min_notional=Decimal("100000"), reference=None)

        self.assertEqual(len(plan.shards), 3)

    def test_a_min_notional_nothing_can_satisfy_still_sends_one_order(self):
        """降片已到底、整笔名义额仍低于最小额——**仍发这一张**（CONTEXT.md:127）。
        「减不掉才是更坏的结局」，而这句话必须写进 note，否则读的人会以为机制忘了降片。
        """
        plan = self._plan("10", min_notional=Decimal("1000"))

        self.assertEqual(plan.shards, (plan.planned_qty,))
        self.assertIn("仍低于最小额", plan.note)
        self.assertIn("CONTEXT.md:127", plan.note)

    def test_a_step_too_coarse_to_split_sends_one_order(self):
        """片数算得出 2，但**每片的量取整到步进之后是 0**（一半 0.1、步进 0.1、两片各
        0.05）。这时回退成整笔一次而不是发两张 0 量的单——`_split` 返回空是算术的结论，
        不是失败。
        """
        plan = self._plan("0.2", min_qty=Decimal("0.05"), min_notional=Decimal("0.001"))

        self.assertEqual(plan.shards, (plan.planned_qty,))
        self.assertIn("切不出多片", plan.note)

    def test_the_plain_single_order_note_says_nothing_surprising(self):
        # min_qty 大到只装得下一片，而名义额够——最常见的「整笔一次」，理由是片数。
        plan = self._plan("10", min_qty=Decimal("10"), min_notional=Decimal("5"))

        self.assertEqual(plan.shards, (plan.planned_qty,))
        self.assertEqual(plan.note, "整笔一次")

    def test_planned_quantity_is_never_more_than_half(self):
        for qty in ("10", "10.07", "3.333", "0.15", "100"):
            plan = self._plan(qty)
            self.assertLessEqual(plan.planned_qty, Decimal(qty) / 2, qty)


class TestShardClientId(SimpleTestCase):
    """确定性 + 逐片唯一 + 不超 36 字符。确定性是这条链路的一半价值（CONTEXT.md:125
    的重发要靠同一个单号走幂等）。"""

    def setUp(self):
        self.account = _FakeAccount()

    def test_it_is_a_pure_function_of_the_tuple(self):
        first = reduce_run.shard_client_id(self.account, 7, SYMBOL_NATIVE, 0)
        second = reduce_run.shard_client_id(self.account, 7, SYMBOL_NATIVE, 0)

        self.assertEqual(first, second)
        self.assertEqual(first, "rd01234567-7-DOGEUSDT-0")

    def test_each_shard_gets_its_own_id(self):
        ids = {
            reduce_run.shard_client_id(self.account, 7, SYMBOL_NATIVE, i)
            for i in range(3)
        }

        self.assertEqual(len(ids), 3)

    def test_a_long_symbol_is_truncated_not_hashed(self):
        """截断后仍然可读（排查时一眼看出是哪个品种）；碰撞面在（账户 × 事件 × 品种 × 片号）
        这个元组上早被记录表分开了。
        """
        long_symbol = "X" * 40

        cid = reduce_run.shard_client_id(self.account, 7, long_symbol, 0)

        self.assertEqual(len(cid), reduce_run.CLIENT_ID_MAX)
        self.assertTrue(cid.startswith("rd01234567-7-XX"))

    def test_a_main_key_that_eats_the_budget_raises(self):
        """事件主键长到把预算吃光——只可能是主键类型换了。**不静默截断 head**：那会让两个
        事件的单号撞在一起，而撞单号在交易所侧是**同一张单**。
        """
        with self.assertRaises(ValueError):
            reduce_run.shard_client_id(self.account, 10**30, SYMBOL_NATIVE, 0)


# --------------------------------------------------------------------------- #
# 渲染（纯函数）
# --------------------------------------------------------------------------- #


class TestBusinessDay(SimpleTestCase):
    """业务日 = 业务时区（Asia/Shanghai）的自然日，即「本日第 N 次」的那个「本日」。"""

    def test_it_follows_the_business_timezone_not_utc(self):
        # UTC 04:00 = 北京 12:00 —— 同一天；UTC 16:30 = 北京次日 00:30 —— 差一天。
        self.assertEqual(reduce_run.business_day(NOW), DAY)
        self.assertEqual(
            reduce_run.business_day(
                datetime(2026, 9, 23, 16, 30, tzinfo=dt_timezone.utc)
            ),
            date(2026, 9, 24),
        )

    def test_the_boundary_is_business_midnight(self):
        self.assertEqual(
            reduce_run.business_day(
                datetime(2026, 9, 23, 15, 59, tzinfo=dt_timezone.utc)
            ),
            DAY,
        )
        self.assertEqual(
            reduce_run.business_day(
                datetime(2026, 9, 23, 16, 0, tzinfo=dt_timezone.utc)
            ),
            date(2026, 9, 24),
        )


class TestPct(SimpleTestCase):
    def test_a_zero_or_missing_denominator_has_no_percentage(self):
        for numerator, denominator in (
            (Decimal("5"), None),
            (None, Decimal("10")),
            (Decimal("5"), Decimal("0")),
            (Decimal("5"), Decimal("-1")),
        ):
            self.assertIsNone(reduce_run._pct(numerator, denominator))

    def test_it_is_one_decimal_and_rounds_half_up(self):
        self.assertEqual(reduce_run._pct(Decimal("4"), Decimal("10")), Decimal("40.0"))
        self.assertEqual(reduce_run._pct(Decimal("4"), Decimal("30")), Decimal("13.3"))


class TestRenderAccountReport(SimpleTestCase):
    def _make_outcome(self, **over) -> reduce_run.RecordOutcome:
        # 不叫 `_outcome`：那是 `unittest.TestCase` 在 `run()` 里塞进去的内部字段，
        # 同名的话这里的方法会被它覆盖掉，报的是「'_Outcome' object is not callable」。
        fields = {
            "symbol": SYMBOL,
            "status": ReduceStatus.SUCCEEDED,
            "ordinal": 2,
            "baseline_qty": Decimal("100"),
            "qty_after": Decimal("40"),
        }
        fields.update(over)
        return reduce_run.RecordOutcome(**fields)

    def _render(self, outcomes, **over):
        fields = {"label": "binance（主账户）", "day": DAY, "outcomes": outcomes}
        fields.update(over)
        return reduce_run.render_account_report(**fields)

    def test_the_head_names_the_account_and_the_business_day(self):
        text = self._render([])

        self.assertIn("binance（主账户）", text)
        self.assertIn(f"业务日 {DAY}", text)

    def test_a_line_carries_the_status_and_the_frozen_baseline_percentage(self):
        """CONTEXT.md:151：「本日第 N 次熔断，累计已减至基准的 X%」。X 用**这一次的**
        `qty_after / baseline_qty`——基准在当天第一行冻结，所以 X 是相对这一天的起点。
        """
        text = self._render([self._make_outcome(note="共 3 片")])

        self.assertIn(f"· {SYMBOL}：{ReduceStatus.SUCCEEDED.display}（共 3 片）", text)
        self.assertIn("本日第 2 次熔断，累计已减至基准的 40.0%", text)

    def test_an_unknown_baseline_says_undetermined_instead_of_a_number(self):
        # 编一个 0% 或 100% 出来，读的人会拿它去判断这一轮减得够不够。
        text = self._render([self._make_outcome(baseline_qty=None, qty_after=None)])

        self.assertIn("本日第 2 次熔断，基准或余量取不到，累计比例未判定", text)
        self.assertNotIn("%", text.split("未判定")[0])

    def test_the_three_slippage_bases_each_get_their_own_sentence(self):
        mid = self._render(
            [
                self._make_outcome(
                    slippage_basis=SlippageBasis.MID, slippage_pct=Decimal("0.12")
                )
            ]
        )
        no_fill = self._render(
            [self._make_outcome(slippage_basis=SlippageBasis.NO_FILL)]
        )
        unavailable = self._render(
            [self._make_outcome(slippage_basis=SlippageBasis.UNAVAILABLE)]
        )

        self.assertIn("滑点：0.12（以动作发起时刻中间价为基准）", mid)
        self.assertIn("滑点：本次无成交，无从计算", no_fill)
        self.assertIn("滑点：基准价取不到，本次不判定滑点", unavailable)

    def test_a_mid_basis_without_a_number_writes_nothing(self):
        # 「以中间价为基准」后面跟不出数字时，只写前半句会变成一句没有信息的话。
        text = self._render(
            [self._make_outcome(slippage_basis=SlippageBasis.MID, slippage_pct=None)]
        )

        self.assertNotIn("滑点", text)

    def test_a_paused_symbol_says_it_went_manual(self):
        text = self._render([self._make_outcome(paused=True)])

        self.assertIn("已暂停该品种的自动减仓，转人工", text)

    def test_the_cancel_section_hangs_off_the_account_not_the_symbol(self):
        """撤单是**按作用域一次做完**的，逐品种复制一份只会让同一句话在消息里出现 N 遍。
        而它**反推不出来**：枚举失败那句话说的事情只在内存里存在过。
        """
        cancel_line = "本次未能枚举挂单，敞口在窗口内仍可能增加（超时）"

        text = self._render([self._make_outcome()], cancel_lines=[cancel_line])

        self.assertIn(cancel_line, text)
        self.assertLess(text.index("本日第 2 次熔断"), text.index(cancel_line))
        # 与逐品种的记录之间隔一个空行，否则它读起来像最后一个品种的补充说明。
        self.assertIn(f"\n\n{cancel_line}", text)

    def test_no_outcomes_and_no_cancels_is_just_the_head(self):
        self.assertEqual(self._render([]).count("\n"), 0)


class TestRenderReduceFailure(SimpleTestCase):
    def test_it_says_plainly_that_nothing_was_reduced(self):
        """一条沉默的失败与「本来就没到窗口」在收件人眼里一模一样。"""
        text = reduce_run.render_reduce_failure(
            label="binance（主账户）",
            symbol=SYMBOL,
            reason="下单被拒",
            ordinal=2,
            day=DAY,
        )

        self.assertIn("binance（主账户）", text)
        self.assertIn(SYMBOL, text)
        self.assertIn("未减仓", text)
        self.assertIn("下单被拒", text)
        self.assertIn("本日第 2 次熔断", text)
        self.assertIn(f"业务日 {DAY}", text)
        self.assertIn("不设「补回」", text)


class TestRecordOutcomeLine(SimpleTestCase):
    def test_the_note_goes_into_parentheses(self):
        outcome = reduce_run.RecordOutcome(
            symbol=SYMBOL,
            status=ReduceStatus.NO_OP,
            ordinal=1,
            baseline_qty=Decimal("1"),
            qty_after=None,
            note="减半后不足一个步进",
        )

        self.assertEqual(
            outcome.line(),
            f"{SYMBOL}：{ReduceStatus.NO_OP.display}（减半后不足一个步进）",
        )

    def test_no_note_means_no_parentheses(self):
        outcome = reduce_run.RecordOutcome(
            symbol=SYMBOL,
            status=ReduceStatus.SUCCEEDED,
            ordinal=1,
            baseline_qty=Decimal("1"),
            qty_after=Decimal("1"),
        )

        self.assertEqual(outcome.line(), f"{SYMBOL}：{ReduceStatus.SUCCEEDED.display}")


# --------------------------------------------------------------------------- #
# 记录与闸门（真库）
# --------------------------------------------------------------------------- #


@override_settings(FERNET_KEY=FERNET_KEY)
class _ReduceTestBase(TestCase):
    """真库那一半的公共夹具。**不打开任何开关**：那件事每个用例自己决定——
    `TestBuildPlans` 显式打开事件熔断，其余用例问的都不是开关。
    """

    def setUp(self):
        super().setUp()
        self.account = _account()
        self.event = _event_row()


class TestConfiguredAccounts(_ReduceTestBase):
    """可以动手的账户：`is_active` 且**像**配好了密钥。"""

    def test_a_usable_account_is_returned(self):
        self.assertEqual(
            [a.pk for a in reduce_run.configured_accounts()], [self.account.pk]
        )

    def test_a_placeholder_key_is_not_a_key(self):
        """「字段里塞了一个占位符」与「真的配好了」在 `build_account_adapter` 眼里一样
        （它只判字段有没有值），所以这一层要自己挡。
        """
        _account(label="占位符", key="TODO", secret="TODO")

        self.assertEqual(
            [a.pk for a in reduce_run.configured_accounts()], [self.account.pk]
        )

    def test_an_inactive_account_is_not_touched(self):
        _account(label="停用", active=False)

        self.assertEqual(
            [a.pk for a in reduce_run.configured_accounts()], [self.account.pk]
        )

    def test_an_undecryptable_account_is_skipped_not_fatal(self):
        # 解密失败也是一种「没配好」，不是事故——整轮不该因为一个坏账户塌掉。
        ExchangeAccount.objects.create(
            exchange="binance",
            label="坏密钥",
            api_key_enc=b"not-a-fernet-token",
            api_secret_enc=b"not-a-fernet-token",
        )

        self.assertEqual(
            [a.pk for a in reduce_run.configured_accounts()], [self.account.pk]
        )


class TestScopesInForce(_ReduceTestBase):
    def test_an_open_event_declaration_under_an_open_switch_is_in_force(self):
        _declare()
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

        self.assertEqual(reduce_run.scopes_in_force(now=NOW), [halt.global_scope()])

    def test_the_switch_alone_is_not_enough(self):
        _declare()

        self.assertEqual(reduce_run.scopes_in_force(now=NOW), [])

    def test_the_declaration_alone_is_not_enough(self):
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

        self.assertEqual(reduce_run.scopes_in_force(now=NOW), [])

    def test_only_the_event_breaker_source_counts(self):
        """保命档（高波动）没有开关、一到就生效，但它**不是事件熔断**——减仓这条线只认事件
        熔断那个源。把它混进来，保命档会顺手减一次仓，而那次减仓在告警里读起来像是有事件。
        """
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)
        _declare(trigger=HaltTrigger.BLANKET)
        _declare(trigger=HaltTrigger.DEACTIVATION, scope=halt.strategy_scope("s-1"))

        self.assertEqual(reduce_run.scopes_in_force(now=NOW), [])

    def test_a_closed_declaration_drops_out(self):
        _declare(closed_at=NOW - timedelta(minutes=1))
        _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

        self.assertEqual(reduce_run.scopes_in_force(now=NOW), [])


class TestEnableLater(_ReduceTestBase):
    """Q8 的闸门：机制做出一次动作之后停下来等人确认。**没有持久开关**——判据只看库里
    已经发生的事，所以进程重启、任务重跑都不改变结论。
    """

    def test_an_untouched_day_allows_another_round(self):
        self.assertTrue(reduce_run.enable_later(self.account, DAY).allowed)

    def test_a_claim_still_in_flight_does_not_stop_the_round(self):
        # 认领是「这一轮正在做」，不是「做过了」。把它算成收场，第二条品种会被自己的第一条
        # 挡住——而它连记录都已经写下了。
        _record(self.account, self.event, status=ReduceStatus.CLAIMED.value)

        self.assertTrue(reduce_run.enable_later(self.account, DAY).allowed)

    def test_a_no_op_does_not_stop_the_round(self):
        """**一处收窄**：`NO_OP` 是「减半后不足一个步进」这个算术事实，不是机制对市场做出的
        判断。算成收场的话，一个尘埃账户会把整个账户当天的其余品种全停掉。
        """
        _record(self.account, self.event, status=ReduceStatus.NO_OP.value)

        self.assertTrue(reduce_run.enable_later(self.account, DAY).allowed)

    def test_a_finished_reduction_stops_the_round_and_names_the_row(self):
        row = _record(
            self.account, self.event, status=ReduceStatus.SUCCEEDED.value, ordinal=1
        )

        verdict = reduce_run.enable_later(self.account, DAY)

        self.assertFalse(verdict.allowed)
        self.assertIn("已有收场的自动减仓", verdict.reason)
        self.assertIn(str(row), verdict.reason)
        self.assertIn("请人工确认", verdict.reason)

    def test_every_terminal_status_stops_the_round(self):
        for status in (
            ReduceStatus.SUCCEEDED,
            ReduceStatus.PARTIAL,
            ReduceStatus.FAILED,
            ReduceStatus.UNSETTLED,
        ):
            with self.subTest(status=status):
                RegimeReduceRecord.objects.all().delete()
                _record(self.account, self.event, status=status.value)
                self.assertFalse(reduce_run.enable_later(self.account, DAY).allowed)

    def test_another_day_does_not_stop_today(self):
        _record(
            self.account,
            self.event,
            status=ReduceStatus.SUCCEEDED.value,
            business_day=DAY - timedelta(days=1),
        )

        self.assertTrue(reduce_run.enable_later(self.account, DAY).allowed)

    def test_another_account_does_not_stop_this_one(self):
        other = _account(label="另一个")
        _record(other, self.event, status=ReduceStatus.SUCCEEDED.value)

        self.assertTrue(reduce_run.enable_later(self.account, DAY).allowed)


class TestStaleClaims(_ReduceTestBase):
    def test_a_claim_without_a_finish_is_stale(self):
        row = _record(self.account, self.event)

        self.assertEqual(
            [r.pk for r in reduce_run.stale_claims(self.account)], [row.pk]
        )

    def test_a_finished_row_is_not_stale(self):
        row = _record(self.account, self.event)
        reduce_run.finish(row, status=ReduceStatus.SUCCEEDED)

        self.assertEqual(reduce_run.stale_claims(self.account), [])

    def test_a_claim_that_was_finished_halfway_still_counts_as_finished(self):
        """判据是**两个条件都要**（`status` 与 `finished_at` 一起写）。只看 `status` 会漏掉
        「收尾写了一半」的行——而那种行的存在恰恰说明写入中断了。
        """
        _row = _record(self.account, self.event, finished_at=NOW)

        self.assertEqual(reduce_run.stale_claims(self.account), [])

    def test_another_account_has_its_own_claims(self):
        other = _account(label="另一个")
        _record(other, self.event)

        self.assertEqual(reduce_run.stale_claims(self.account), [])


class TestLivePauseSymbols(_ReduceTestBase):
    def test_an_open_pause_counts(self):
        _pause(self.account)

        self.assertEqual(reduce_run.live_pause_symbols(self.account), {SYMBOL_NATIVE})

    def test_a_lifted_pause_does_not(self):
        _pause(self.account, closed_at=NOW)

        self.assertEqual(reduce_run.live_pause_symbols(self.account), set())

    def test_the_scope_is_the_account(self):
        other = _account(label="另一个")
        _pause(other)

        self.assertEqual(reduce_run.live_pause_symbols(self.account), set())


class TestOrdinalFor(_ReduceTestBase):
    """「第 N 次」**数事件不数动作**（字段 docstring）。"""

    def test_the_first_event_of_the_day_is_one(self):
        self.assertEqual(reduce_run.ordinal_for(self.account, DAY, self.event.pk), 1)

    def test_every_symbol_of_one_event_shares_the_same_n(self):
        """不排除本事件的话，同一事件的第二个品种会拿到 N+1——而这两行在库里读起来就是
        两次不同的熔断，日报上「本日第 2 次熔断」出现两遍。
        """
        second_event = _event_row(name="另一个事件")
        _record(self.account, self.event, symbol=SYMBOL_NATIVE, ordinal=1)
        _record(self.account, self.event, symbol="BTCUSDT", ordinal=1)

        self.assertEqual(reduce_run.ordinal_for(self.account, DAY, self.event.pk), 1)
        self.assertEqual(reduce_run.ordinal_for(self.account, DAY, second_event.pk), 2)

    def test_two_rows_of_another_event_still_count_once(self):
        # 「数一数当天已有几行 + 1」数的是**动作**：另一个事件的第二个品种会把它推高。
        other = _event_row(name="别的事件")
        _record(self.account, other, symbol=SYMBOL_NATIVE)
        _record(self.account, other, symbol="BTCUSDT")

        self.assertEqual(reduce_run.ordinal_for(self.account, DAY, self.event.pk), 2)

    def test_another_account_has_its_own_count(self):
        other = _account(label="另一个")
        other_event = _event_row(name="别的事件")
        _record(other, other_event)

        self.assertEqual(reduce_run.ordinal_for(self.account, DAY, self.event.pk), 1)


class TestClaimRecord(_ReduceTestBase):
    def test_the_first_claim_freezes_the_reading_as_the_baseline(self):
        record, created = reduce_run.claim_record(
            self.account,
            event_id=self.event.pk,
            symbol=SYMBOL_NATIVE,
            day=DAY,
            qty_before=Decimal("100"),
            reason="高影响事件熔断",
        )

        self.assertTrue(created)
        self.assertEqual(record.status, ReduceStatus.CLAIMED.value)
        self.assertEqual(record.business_day, DAY)
        self.assertEqual(record.ordinal, 1)
        self.assertEqual(record.baseline_qty, Decimal("100"))
        self.assertEqual(record.qty_before, Decimal("100"))
        self.assertIsNone(record.finished_at)

    def test_a_second_claim_returns_the_same_row_untouched(self):
        """幂等靠的是**唯一键**（`uniq_regime_reduce_record`），不是调用方的判断——
        `created=False` 只是让日志能说清「为什么这一轮什么也没做」。
        """
        first, _ = reduce_run.claim_record(
            self.account,
            event_id=self.event.pk,
            symbol=SYMBOL_NATIVE,
            day=DAY,
            qty_before=Decimal("100"),
            reason="第一轮",
        )

        second, created = reduce_run.claim_record(
            self.account,
            event_id=self.event.pk,
            symbol=SYMBOL_NATIVE,
            day=DAY,
            qty_before=Decimal("80"),
            reason="第二轮",
        )

        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.qty_before, Decimal("100"))
        self.assertEqual(second.reason, "第一轮")

    def test_the_baseline_stays_frozen_across_events_in_the_same_day(self):
        """基准的定义是「这一天的第一行取到了什么」——第二次熔断时手上已经不是 100 了，
        但 X% 要相对**这一天的起点**，不是相对上一次减仓。
        """
        reduce_run.claim_record(
            self.account,
            event_id=self.event.pk,
            symbol=SYMBOL_NATIVE,
            day=DAY,
            qty_before=Decimal("100"),
            reason="第一次",
        )
        second_event = _event_row(name="别的事件")

        record, created = reduce_run.claim_record(
            self.account,
            event_id=second_event.pk,
            symbol=SYMBOL_NATIVE,
            day=DAY,
            qty_before=Decimal("50"),
            reason="第二次",
        )

        self.assertTrue(created)
        self.assertEqual(record.baseline_qty, Decimal("100"))
        self.assertEqual(record.qty_before, Decimal("50"))
        self.assertEqual(record.ordinal, 2)

    def test_another_symbol_freezes_its_own_baseline(self):
        reduce_run.claim_record(
            self.account,
            event_id=self.event.pk,
            symbol=SYMBOL_NATIVE,
            day=DAY,
            qty_before=Decimal("100"),
            reason="第一次",
        )

        record, _ = reduce_run.claim_record(
            self.account,
            event_id=self.event.pk,
            symbol="BTCUSDT",
            day=DAY,
            qty_before=Decimal("3"),
            reason="第一次",
        )

        self.assertEqual(record.baseline_qty, Decimal("3"))


class TestRecordPause(_ReduceTestBase):
    def test_a_new_pause_is_reported_as_new(self):
        opened = reduce_run.record_pause(
            self.account,
            symbol=SYMBOL_NATIVE,
            reason="滑点超限",
            evidence={"slippage_pct": "0.9"},
            at=NOW,
        )

        self.assertTrue(opened)
        row = RegimeReducePause.objects.get()
        self.assertEqual(row.actor_kind, ActorKind.TASK.value)
        self.assertEqual(row.actor_name, reduce_run.ACTOR_NAME)
        self.assertIsNone(row.closed_at)

    def test_hitting_the_live_unique_key_is_not_an_incident(self):
        """撞 `uniq_live_reduce_pause` 就是「早就暂停了」——上一轮已经告警过，这一轮不该再
        惊动一次人。用 `IntegrityError` 判而不是先查再写：先查再写在两个任务挨着跑的时候
        会双写。
        """
        for _ in range(2):
            opened = reduce_run.record_pause(
                self.account,
                symbol=SYMBOL_NATIVE,
                reason="滑点超限",
                evidence={},
                at=NOW,
            )

        self.assertFalse(opened)
        self.assertEqual(RegimeReducePause.objects.count(), 1)

    def test_a_lifted_pause_can_be_opened_again(self):
        # 唯一键只管 `closed_at IS NULL`，所以解除之后是**新的一行**——解除只走人工。
        _pause(self.account, closed_at=NOW - timedelta(minutes=1))

        opened = reduce_run.record_pause(
            self.account,
            symbol=SYMBOL_NATIVE,
            reason="又超了",
            evidence={},
            at=NOW,
        )

        self.assertTrue(opened)
        self.assertEqual(RegimeReducePause.objects.count(), 2)

    def test_the_pause_is_per_symbol(self):
        _pause(self.account, symbol=SYMBOL_NATIVE)

        opened = reduce_run.record_pause(
            self.account, symbol="BTCUSDT", reason="滑点超限", evidence={}, at=NOW
        )

        self.assertTrue(opened)


class TestRecipientsFor(_ReduceTestBase):
    def test_an_owned_account_goes_to_its_owner_alone(self):
        owner = _user("owner@test.local")
        _user("bystander@test.local")
        self.account.user = owner
        self.account.save(update_fields=["user"])

        ids, note = reduce_run.recipients_for(self.account)

        self.assertEqual(ids, [str(owner.pk)])
        self.assertEqual(note, "")

    def test_an_unowned_account_goes_to_everyone_and_says_so(self):
        """`user` 为空 = **未归属**，不是「没有人要通知」——后者是一条告警静悄悄地没有
        收件人（CONTEXT.md:66「没有接收人就不算告警」）。
        """
        mine = _user("active@test.local")
        gone = _user("inactive@test.local", active=False)

        ids, note = reduce_run.recipients_for(self.account)

        self.assertIn(str(mine.pk), ids)
        self.assertNotIn(str(gone.pk), ids)
        self.assertEqual(note, reduce_run.UNOWNED_NOTE)


class TestFinish(_ReduceTestBase):
    def test_it_writes_the_close_out_fields_it_was_given(self):
        record = _record(self.account, self.event)

        reduce_run.finish(
            record,
            status=ReduceStatus.PARTIAL,
            planned_qty=Decimal("5"),
            qty_after=Decimal("0.123456789"),
            sub_orders=[{"shard": 0, "filled": "2.5"}],
            cancel_report={"enumerated": True},
            slippage_pct=Decimal("0.12"),
            slippage_basis=SlippageBasis.MID,
            extra_reason="减仓过程中断",
        )

        record.refresh_from_db()
        self.assertEqual(record.status, ReduceStatus.PARTIAL.value)
        self.assertEqual(record.planned_qty, Decimal("5"))
        # 与 DecimalField(20, 8) 同口径，**向下取**：余量报大了看着像还剩不少。
        self.assertEqual(record.qty_after, Decimal("0.12345678"))
        self.assertEqual(record.sub_orders, [{"shard": 0, "filled": "2.5"}])
        self.assertEqual(record.cancel_report, {"enumerated": True})
        self.assertEqual(record.slippage_pct, Decimal("0.12"))
        self.assertEqual(record.slippage_basis, SlippageBasis.MID.value)
        self.assertIn("减仓过程中断", record.reason)
        self.assertIsNotNone(record.finished_at)

    def test_finished_at_and_status_are_written_together(self):
        """`finished_at` 是「这条认领还归机制处置吗」的唯一标记（`stale_claims` 的另一半），
        所以它必须与 `status` 一起写。分成两次写会让「收了一半」的行存在。
        """
        record = _record(self.account, self.event)

        reduce_run.finish(record, status=ReduceStatus.FAILED)

        self.assertEqual(reduce_run.stale_claims(self.account), [])

    def test_what_was_not_passed_is_not_clobbered(self):
        # 收尾的每一档只带自己知道的那几个字段：失败那一档不知道该减多少，
        # 把 `planned_qty` 顺手写成 None 会让「计划了多少」永久丢掉。
        record = _record(
            self.account, self.event, planned_qty=Decimal("5"), reason="原始理由"
        )

        reduce_run.finish(record, status=ReduceStatus.FAILED)

        record.refresh_from_db()
        self.assertEqual(record.planned_qty, Decimal("5"))
        self.assertEqual(record.reason, "原始理由")

    def test_an_unavailable_basis_is_written_as_such(self):
        # 「基准价取不到，本次不判定滑点」必须落进记录：日报事后要能说出当时没判。
        record = _record(self.account, self.event)

        reduce_run.finish(
            record,
            status=ReduceStatus.SUCCEEDED,
            slippage_basis=SlippageBasis.UNAVAILABLE,
        )

        record.refresh_from_db()
        self.assertEqual(record.slippage_basis, SlippageBasis.UNAVAILABLE.value)
        self.assertIsNone(record.slippage_pct)
        self.assertEqual(
            record.slippage_basis_display, SlippageBasis.UNAVAILABLE.display
        )


class TestBuildPlans(_ReduceTestBase):
    """装配（同步）→ 执行（async）之间的那一刀。这一层**只读**，且全在同步侧完成。"""

    def _arm(
        self,
        *,
        event: MajorEvent | None = None,
        declared: bool = True,
        switch: bool = True,
    ):
        """把三个前置条件一次装好。`event` 留空就用 `setUp` 那条（窗口 NOW±1h，此刻正开着）。

        传了 `event` 就把 `setUp` 那条**推出窗口**（`resume_at` 收到 `NOW`，`resume_at` 是
        开区间，所以此刻不再命中）。不移开的话它会在背后替本轮供给一个正开着的窗口，
        「下一段事件提前写进表里、但还没到点」这类用例就永远测不出来——库里有事件、有声明，
        而判定本该是空的那一次，实际仍然减了仓。
        """
        if event is not None:
            self.event.resume_at = NOW
            self.event.save(update_fields=["resume_at"])
        if declared:
            _declare(scope=halt.global_scope())
        if switch:
            _switch(MechanismKind.EVENT_BREAKER, MechanismMode.EXECUTING)

    def test_a_firing_event_arms_every_configured_account(self):
        self._arm()

        plans = reduce_run.build_plans(at=NOW)

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].account.pk, self.account.pk)
        self.assertEqual(plans[0].label, "binance（主账户）")
        self.assertEqual(
            [(f.event_name, f.scope) for f in plans[0].firing],
            [("FOMC 议息", "global")],
        )

    def test_the_plan_carries_scopes_not_symbols(self):
        """品种要拿**真实持仓**筛，而真实持仓只能走适配器读（本地没有持仓表）。规划期猜一份
        品种清单就是第二份持仓算术，而它看起来完全正常。
        """
        self._arm()

        plan = reduce_run.build_plans(at=NOW)[0]

        self.assertEqual(len(plan.firing), 1)
        self.assertFalse(hasattr(plan.firing[0], "symbols"))

    def test_no_declaration_means_no_plans(self):
        # 事件在窗口里、开关也开着，但没有任何声明行——`scopes_in_force` 是空的那道闸。
        self._arm(declared=False)

        self.assertEqual(reduce_run.build_plans(at=NOW), [])

    def test_the_switch_being_off_means_no_plans(self):
        self._arm(switch=False)

        self.assertEqual(reduce_run.build_plans(at=NOW), [])

    def test_an_event_outside_its_window_means_no_plans(self):
        """`halt_sync` 会把下一段提前写进声明表，所以此刻库里**有**一条事件声明、也有事件行
        ——而窗口还没开。装配期就要认出这一点，否则「还没到窗口就把仓减了」。

        `_event_row` 不像 `_event` 那样从 `halt_at` 推 `resume_at`（它的默认值是给「此刻
        正开着」那一种行用的），所以这里两个时刻都要给全，否则先撞 `ck_major_event_window_ordered`。
        """
        self._arm(
            event=_event_row(
                name="下次议息",
                halt_at=NOW + timedelta(hours=3),
                resume_at=NOW + timedelta(hours=5),
            )
        )

        self.assertEqual(reduce_run.build_plans(at=NOW), [])

    def test_no_usable_account_means_no_plans(self):
        self.account.is_active = False
        self.account.save(update_fields=["is_active"])
        self._arm()

        self.assertEqual(reduce_run.build_plans(at=NOW), [])

    def test_an_unowned_account_is_planned_with_the_everyone_recipients(self):
        mine = _user("active@test.local")
        self._arm()

        plan = reduce_run.build_plans(at=NOW)[0]

        self.assertIn(str(mine.pk), plan.recipients)
        self.assertEqual(plan.recipient_note, reduce_run.UNOWNED_NOTE)


class TestDispatchIdle(_ReduceTestBase):
    def test_an_idle_round_returns_the_full_summary_not_none(self):
        """窗口同步任务把 `dispatch()` 的返回值折进自己的摘要里（`fired.get("rows", 0)`），
        所以**空闲轮也必须返回一个字典**。返回 `None` 的话，那条接线会在每一个没有事件的
        五分钟里抛 `AttributeError` —— 而它恰好是最常见的那一轮。
        """
        summary = reduce_run.dispatch(now=NOW)

        self.assertEqual(summary, {"accounts": 0, "rows": 0, "skipped": 0, "stop": ""})
        self.assertEqual(reduce_run.dispatch(now=NOW).get("rows", 0), 0)


class TestHighImpactEventsIsTheSharedSource(_ReduceTestBase):
    def test_the_read_side_asks_halt_sync_for_the_candidates(self):
        """`halt_sync.high_impact_events` 是公开的**就是为了这里**：写侧（谁该拦）与读侧
        （此刻压着谁）必须是同一批事件。另写一条 `impact="high"` 的查询，分歧的表现是
        「声明写出来了、减仓不动」——两边看起来都正常。
        """
        medium = _event_row(name="中等事件", impact=EventImpact.MEDIUM.value)
        high = _event_row(name="高影响事件")

        names = [e.name for e in halt_sync.high_impact_events()]

        self.assertIn(high.name, names)
        self.assertNotIn(medium.name, names)
