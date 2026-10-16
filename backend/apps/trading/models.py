import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class Order(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("submitted", "Submitted"),
        ("partial", "Partial"),
        ("filled", "Filled"),
        ("cancelled", "Cancelled"),
        ("failed", "Failed"),
        # 「未知」= 按 clientOrderId 反查**没有得出结论**（不可达 / 意外响应）。
        # 它是一个**非终态**，语义是「这张单可能仍在交易所活着」：
        #   * 所以它必须出现在每一处「活跃订单」枚举里（漏掉一处等于把它当成
        #     不存在，而它恰恰是唯一可能变成真实敞口的那一档）；
        #   * 所以它不能自动降级成 failed——若单子其实在交易所，本地记 failed 会
        #     让账面与实际分叉，而分叉的账面比空白更危险。
        ("unknown", "未知"),
    ]
    # 处于这些状态的订单仍可能变成真实敞口，**每一处「活跃订单」枚举都必须用这个
    # 常量**，不许再手抄一份字面量：`unknown` 是唯一可能活着的单，漏掉它的那一处
    # 会把这张单当成不存在（2026-09-22 之前 views / pending_reconcile 各抄了一份）。
    ACTIVE_STATUSES = ("pending", "submitted", "partial", "unknown")

    SIDE_CHOICES = [("buy", "Buy"), ("sell", "Sell")]
    TYPE_CHOICES = [("market", "Market"), ("limit", "Limit"), ("stop", "Stop")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(unique=True, default=uuid.uuid4)  # 幂等键
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True
    )
    exchange_account = models.ForeignKey(
        "exchange.ExchangeAccount", on_delete=models.PROTECT
    )
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=4, choices=SIDE_CHOICES)
    order_type = models.CharField(max_length=8, choices=TYPE_CHOICES)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    price = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    exchange_order_id = models.CharField(max_length=128, blank=True)
    # 下单时给交易所的那个 clientOrderId。`request_id` 是本地 UUID 幂等键，而
    # clientOrderId 是**交易所侧**的锚：减仓子单的重发去重（`reduce.py` 拿同一个
    # clientOrderId 重发）与对账反查都靠它。它是字符串（`cl<8hex><10hex>`），进不去
    # `request_id` 那个 UUIDField，所以单开这一列。空串 = 这条路径没给过单号。
    client_order_id = models.CharField(max_length=128, blank=True, default="")
    filled_quantity = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    avg_fill_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    realized_pnl = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )  # 已实现盈亏
    error_message = models.TextField(blank=True)

    # 关联实盘会话和策略
    live_session = models.ForeignKey(
        "trading.LiveSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
        help_text="订单所属的实盘会话",
    )
    # 触发该订单的策略/信号名称（下单时快照）。
    # 独立于 live_session 外键，即使实盘会话被删除，交易记录详情仍能追溯触发来源。
    triggered_strategy = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="触发该订单的策略/信号名称（下单时快照，会话删除后仍保留）",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "orders"
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["status"]),
        ]


class Strategy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128)
    code_path = models.CharField(max_length=256)  # strategies/{id}.py
    git_commit_hash = models.CharField(max_length=40, blank=True)
    # 人工总开关：`False` = 已退役，永不被自动停用、也永不被自动恢复（CONTEXT.md 第 106
    # 条）。注意它**不是**被管策略集合的依据（第 105 条）——集合只看注册表能不能解析到
    # 实现类。回测自动建出的策略行也走这个默认值，靠迁移 `0006` 与幽灵清理命令回填。
    is_active = models.BooleanField(
        default=False,
        help_text=(
            "人工总开关：False = 已退役，永不被自动停用、也永不被自动恢复"
            "（CONTEXT.md 第 106 条）。但它**不是**被管策略集合的依据"
            "（第 105 条），集合只看注册表能不能解析到实现类。"
            "回测自动建出的策略行默认 False，需由迁移/管理命令回填。"
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "strategies"


class DailySnapshot(models.Model):
    """
    每日账户净值快照。
    用于风控回撤计算，OrderExecutor 每日开盘前记录。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField()
    total_equity = models.DecimalField(
        max_digits=20, decimal_places=8
    )  # 期初总权益(USDT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "daily_snapshots"
        unique_together = [["user", "date"]]
        indexes = [
            models.Index(fields=["user", "-date"]),
        ]


class LiveSession(models.Model):
    """
    实盘交易会话。
    从回测审核通过后部署，记录策略运行状态和收益。
    """

    STATUS_CHOICES = [
        ("pending", "待启动"),
        ("running", "运行中"),
        ("paused", "已暂停"),
        ("stopped", "已停止"),
        ("error", "异常"),
    ]
    MODE_CHOICES = [("live", "实盘"), ("paper", "模拟")]
    #: 仍算「活跃」的会话状态（`Order.ACTIVE_STATUSES` 的同一条纪律：每一处「活跃会话」
    #: 枚举都用这个常量，不许再手抄一份字面量）。口径是「这个会话还在，将来还可能持仓」：
    #: `pending` 还没启动但会启动，`paused` 只是暂停止损／止盈照样管着仓位，`stopped` /
    #: `error` 则已经结束了。被管策略集合的第二条判据（CONTEXT.md 第 105 条）读的是它。
    ACTIVE_STATUSES = ("pending", "running", "paused")
    #: 冲突判定用：「正占用该品种账户级持仓」的状态。口径**不同于** ACTIVE_STATUSES——
    #: `pending` 还没 runner、不占持仓（它自己的 start 会走同一道检查），`stopped` /
    #: `error` 已结束；所以只有 `running`（runner 在跑）与 `paused`（可能残留持仓，
    #: resume 后立刻又占）与另一会话冲突。
    CONFLICT_STATUSES = ("running", "paused")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    backtest_result = models.ForeignKey(
        "backtest.BacktestResult",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="live_sessions",
        help_text="来源回测",
    )
    strategy = models.ForeignKey(Strategy, on_delete=models.PROTECT)
    symbol = models.CharField(max_length=32)
    mode = models.CharField(max_length=8, choices=MODE_CHOICES, default="paper")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    exchange_account = models.ForeignKey(
        "exchange.ExchangeAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        help_text="绑定的交易所账户",
    )
    initial_capital = models.DecimalField(max_digits=20, decimal_places=2)
    current_equity = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True
    )
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    config = models.JSONField(default=dict, help_text="运行参数（仓位比例、止损等）")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "live_sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["user", "mode"]),
            models.Index(fields=["backtest_result"]),
        ]

    def mode_account_mismatch(self) -> str | None:
        """mode 与绑定账户的 testnet 属性是否自洽；自洽返回 None，否则返回原因。

        不变式：`paper ⇒ testnet=True`、`live ⇒ testnet=False`。字段名承诺隔离而
        实现不隔离，是这份代码里代价最高的一类错误（用户以为在演练、实际在动钱）。
        本方法只把**静默错配变成启动失败**，不新增任何能力。
        """
        if self.mode not in ("paper", "live"):
            return f"未知的会话模式 {self.mode!r}（只接受 paper / live）"
        account = self.exchange_account
        if account is None:
            return "会话未绑定交易所账户"
        expected_testnet = self.mode == "paper"
        if account.testnet != expected_testnet:
            return (
                f"mode={self.mode} 要求账户 testnet={expected_testnet}，"
                f"但绑定的账户「{account.label}」testnet={account.testnet}"
            )
        return None

    @classmethod
    def conflicting_active_session(
        cls, exchange_account, symbol, exclude_pk=None
    ) -> str | None:
        """同交易所账户 + 同品种上是否还有活跃会话（running/paused）；无则 None，有则拒绝原因。

        `exchange_account` 接受 ExchangeAccount 实例或 pk：视图层手里有实例，
        runner（`live_mode._shared_account_conflict`）手里只有账户 id 字符串。
        账户行不存在（已删除）时查询自然为空 → None。

        为什么算冲突：runner 每根 bar 把**账户级**该品种持仓同步进 `ctx.position`
        （`live_mode._sync_position_from_exchange`），`close_position` 平到账户级 0——
        没有「本会话仓位」的概念。两个策略同账户同品种必然互相平仓（2026-10-07：
        box 会话平掉 donchian 开的 10401 DOGE/USDT，实现亏损 -79.78）。

        `exclude_pk` 排除会话自身：对 running 会话点 start 是「重启自己」，不算冲突。
        """
        qs = cls.objects.filter(
            exchange_account=exchange_account,
            symbol=symbol,
            status__in=cls.CONFLICT_STATUSES,
        ).select_related("strategy", "exchange_account")
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        other = qs.first()
        if other is None:
            return None
        account = other.exchange_account
        return (
            f"交易所账户「{account.label}」已有 {symbol} 的活跃会话："
            f"{other.strategy.name}（id {str(other.id)[:8]}…，status={other.status}）。"
            f"runner 共享该品种的账户级持仓，同账户同品种多策略会互相平仓，请先停止另一会话"
        )

    def __str__(self):
        return f"LiveSession({self.mode}/{self.status}) - {self.strategy.name} {self.symbol}"


class LiveSessionStatusChange(models.Model):
    """会话状态变更时间线（spec #19 D8，子票 #24）。

    ## 为什么要有这张表

    D8 的观察期口径是「墙钟 ≥ N 天**且期间 `running` 时长 ≥ N 天**」——`paused` 是
    「暂停止损/止盈」，**不计入观察**。但 `LiveSession` 上只有一个 `status` 标量
    （**只有当前值，没有历史**）加 `started_at` / `stopped_at` 两个时刻，于是
    「running 累计多少天」这个数**根本算不出来**：一条 7 天 running → 3 天 paused →
    7 天 running 的会话，墙钟是 17 天，而其中 3 天不该算——库里没有任何东西记得那 3 天。

    同一处的第二个坑：`live_session_start` 每次启动都无条件
    `started_at = timezone.now()`，而 `live_session_resume` **完全不碰** `started_at`。
    所以「跑 7 天 → 暂停 3 天 → 恢复 7 天」读到的墙钟是 17 天，**把 paused 算了进去**
    ——误差方向恰好是**偏乐观**的那一侧，也就是 D8 明令禁止的那一侧。

    补上这条时间线之后，`running` 时长才第一次成为**可复核的真值**：它由「进入
    running 的时刻」与「离开 running 的时刻」逐段求和得出，与 `status` 当前是什么无关。

    ## 落库纪律

    * **每一次状态变更写一行**，四个入口（start / pause / resume / stop）各一处，
      且与 `session.save()` 在**同一个事务**里——时间线与会话状态分家的话，
      这张表就成了「另一种说法」，而算观察期时没人知道该信哪一个。
    * `from_status` / `to_status` 都存字面值（不是外键、不复用 `STATUS_CHOICES` 的
      元组对象）：这是一份**审计流水**，它记录的是当时发生的事实。将来 `STATUS_CHOICES`
      增删一档，历史行必须保持它当时的原样，不能被新枚举重新解释。
    * `changed_at` 由调用方给（不 `auto_now_add`）：测试要注入固定时刻来钉「恰 14 天」
      边界，而 `auto_now_add` 会把注入的时刻悄悄丢掉。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    live_session = models.ForeignKey(
        LiveSession,
        on_delete=models.CASCADE,
        related_name="status_changes",
        help_text="发生变更的会话；会话行没了，它的时间线也无从谈起",
    )
    from_status = models.CharField(
        max_length=12, blank=True, default="", help_text="变更前状态；空串 = 会话刚建立"
    )
    to_status = models.CharField(max_length=12)
    changed_at = models.DateTimeField(help_text="变更时刻（由调用方给，UTC aware）")

    class Meta:
        db_table = "live_session_status_changes"
        ordering = ["changed_at", "id"]
        indexes = [
            # 算 running 时长时永远是「按会话取一段、按时间排序」，这是那条查询的形状。
            models.Index(fields=["live_session", "changed_at"]),
        ]

    def __str__(self):
        return (
            f"LiveSessionStatusChange({self.from_status or '∅'} → {self.to_status}"
            f" @ {self.changed_at:%Y-%m-%d %H:%M})"
        )


def running_duration(session, *, until):
    """`session` 在 `until` 之前的 **running 累计时长**（`timedelta`）。

    **这是全仓关于「running 时长」的唯一算法**（spec #19 D8 的口径）：

    * 时间线按 `changed_at` 升序走一遍，维护「当前状态」与「这一段从什么时候开始」；
    * 只有状态是 `running` 的段才累加；
    * 最后一段若仍处于 `running`，累加到 `until` 为止。

    ## 三条边界，每条都是刻意选的

    1. **没有时间线 → 返回 `None`，不是 `0`。** 「算不出来」与「跑了 0 秒」是两件
       不同的事：前者该判「证据不足」（不放行），后者是一个**看起来合规**的数字，
       会让一条 `started_at` 有值但时间线缺失的会话被判成「还没跑满」并一直等下去。
       返回 `None` 让调用方必须显式处理这个缺口。

    2. **会话尚未进入过 `running` → 返回 `timedelta(0)`，不是 `None`。** 这与第 1 条
       不矛盾：时间线存在、且**明确记录着**「它从来没跑过」，那是一种**已知**的零，
       不是未知。它的判定后果与「跑了 0 天」一致（观察不满），而那是正确结论。

    3. **`until` 早于某一段的起点时不计入该段**（`max(0)` 保证不出现负时长）。
       注入一个早于会话启动的时刻会得到 0，而不是一个负数把总额吃掉。

    ## 为什么是自由函数而不是 `session` 的方法

    它是一个**纯查询 + 纯算术**，不读 `self.status`（第 1 条的口径恰恰是「不看当前
    状态」——当前状态答不出历史）。挂成方法会让人以为它读会话自身的字段；这里显式
    要求调用方把「截止到什么时候」传进来，因为那个时刻是**判定的一部分**（测试要钉
    边界），不该由被查对象自己决定。
    """
    rows = list(
        session.status_changes.order_by("changed_at", "id").values_list(
            "to_status", "changed_at"
        )
    )
    if not rows:
        return None

    total = timezone.timedelta(0)
    current_status: str | None = None
    segment_start = None

    for to_status, changed_at in rows:
        if segment_start is not None and current_status == "running":
            total += max(changed_at - segment_start, timezone.timedelta(0))
        current_status = to_status
        segment_start = changed_at

    if current_status == "running" and segment_start is not None:
        total += max(until - segment_start, timezone.timedelta(0))

    return total


def record_status_change(session, *, to_status, changed_at) -> None:
    """写一行时间线（四个入口共用；`session.save()` 由调用方负责）。

    **调用方必须与 `session.save()` 包在同一个事务里**：时间线是「另一种说法」的
    防线，分家之后没有人知道该信哪一个（见 `LiveSessionStatusChange` 的 docstring）。
    本函数只管写行，不管事务——事务的边界属于发起状态变更的那个入口，它知道
    自己的成功条件是什么。

    `from_status` 从**传入的会话对象**读而不是从库里重读：调用方手里这一份就是
    「变更前」的那一份（它马上要被 save 覆盖），再读一次库反而可能读到别人的写入。
    """
    LiveSessionStatusChange.objects.create(
        live_session=session,
        from_status=session.status or "",
        to_status=to_status,
        changed_at=changed_at,
    )
