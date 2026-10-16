"""候选策略模型（spec #19 D1）。

它是「一个想法 / 一段生成的代码 / 一组网格变体」在系统里的**身份**：来源、假设、
代码落点、提出人、现在卡在哪一层、每一层的判定依据、以及它后来绑了哪个 paper 会话。

两条纪律：

1. **不碰 `Strategy.is_active`（D2）**。`Strategy.is_active` 是人工总开关（`False` =
   已退役，永不被自动停用、也永不被自动恢复）。候选的自动状态全部住在这张表上，
   谁都不能推导谁——所以这个模块里**没有任何一处**写 `Strategy`。

2. **只持久化，不判定**。判定在 `evaluate.py` 的纯函数里；本模型只提供「这条边合法吗」
   的守门方法（`transition_to`），把非法转移变成一次 Fail Loud 的异常。把判定逻辑塞进
   模型方法，等于让判定变成「需要 DB 才能跑」的东西，D14 的测试缝就没了。
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.strategy_factory.status import (
    InvalidTransitionError,
    StrategyCandidateLayer,
    StrategyCandidateOrigin,
    StrategyCandidateStatus,
    allowed_next_statuses,
    can_transition,
    coerce_status,
)


class StrategyCandidate(models.Model):
    """候选策略（D1 的字段形状）。

    **命名避让**：本对象叫 `StrategyCandidate`，不叫裸 `Candidate`——`apps/regime` 的
    `CandidateEvent` / `CandidateOrigin` / `CandidateStatus` 已占用「候选**事件**」
    （资讯判定的产物，另一个域）。同理来源字段叫 `origin` 而不是 `source`
    （`GridSearchJob.source` 已有 `agent/cron/api` 三态，同名不同义）。

    `paper_session` / `live_session` 两个外键刻意放在**候选**上，而不是给 `LiveSession`
    加列：`LiveSession` 是热模型（订单执行链路每根 bar 都在读它），少动一次少一次风险。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # -- 身份 -------------------------------------------------------------- #
    name = models.CharField(
        max_length=128,
        db_index=True,
        help_text="策略注册键（候选声明的名字；登记时校验它唯一且能解析到实现类，D17）",
    )
    hypothesis = models.TextField(
        blank=True, help_text="一句话假设：这条候选认为市场会怎样，靠什么赚钱"
    )
    code_path = models.CharField(
        max_length=256,
        blank=True,
        help_text="策略文件落点（~/.tradelogx/strategies/{name}.py）",
    )

    # -- 来源（Q3 三来源） -------------------------------------------------- #
    origin = models.CharField(
        max_length=16,
        choices=StrategyCandidateOrigin.choices(),
        help_text="agent 生成 / 网格变体 / 人工投喂",
    )
    source_ref = models.JSONField(
        default=dict,
        blank=True,
        help_text="来源引用：agent 会话 id / GridSearchJob id + 组合参数 / 外部研究链接",
    )

    # -- 人 ----------------------------------------------------------------- #
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="strategy_candidates",
        help_text="提出人（删用户不删候选：失败过的候选要能回答「为什么当初没上」，D26）",
    )

    # -- 代码 --------------------------------------------------------------- #
    strategy = models.ForeignKey(
        "trading.Strategy",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="strategy_candidates",
        help_text="解析到的策略行；登记失败时为空。**只读**，绝不写它的 is_active（D2）",
    )

    # -- 状态与证据 --------------------------------------------------------- #
    status = models.CharField(
        max_length=32,
        choices=StrategyCandidateStatus.choices(),
        default=StrategyCandidateStatus.REGISTERED.value,
        db_index=True,
    )
    evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text="各层判定结论 + 原因码 + 数字 + 时间区间（判定纯函数的 Verdict 落点）",
    )

    # -- 会话（paper / live） ----------------------------------------------- #
    paper_session = models.ForeignKey(
        "trading.LiveSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paper_strategy_candidates",
        help_text="paper 观察会话（mode=paper，绑 testnet 账户）",
    )
    live_session = models.ForeignKey(
        "trading.LiveSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="live_strategy_candidates",
        help_text="升实盘后的会话（mode=live，绑 testnet=False 账户）",
    )

    # -- 人工输入 ----------------------------------------------------------- #
    paper_account = models.ForeignKey(
        "exchange.ExchangeAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paper_strategy_candidates",
        help_text="指定绑哪个 testnet 账户（D10：整条流水线唯一的人工输入）",
    )
    live_account = models.ForeignKey(
        "exchange.ExchangeAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="live_strategy_candidates",
        help_text="人工卡点升实盘时显式指定的目标实盘账户（testnet=False，D11）",
    )

    # -- 时间 --------------------------------------------------------------- #
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    decided_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="人工决策时刻（升实盘 / 否决 / 取消），由人工卡点那一步写入",
    )

    class Meta:
        db_table = "strategy_candidates"
        verbose_name = "候选策略"
        verbose_name_plural = "候选策略"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["origin"]),
        ]

    def __str__(self) -> str:
        return f"[{self.status}] {self.name}"

    # -- 状态机守门（D3） --------------------------------------------------- #

    @property
    def status_enum(self) -> StrategyCandidateStatus:
        """`status` 列取回后的枚举视图；未登记的值直接报错。"""
        return coerce_status(self.status)

    def allowed_next_statuses(self) -> frozenset[StrategyCandidateStatus]:
        return allowed_next_statuses(self.status)

    def can_transition_to(self, target: StrategyCandidateStatus | str) -> bool:
        return can_transition(self.status, target)

    def transition_to(
        self, target: StrategyCandidateStatus | str
    ) -> StrategyCandidateStatus:
        """把候选推进到 `target`，非法转移抛 `InvalidTransitionError`。

        **只改内存里的字段，不 save**：编排层（#22）要在一次事务里落状态 + 写证据 +
        建会话，`save()` 由它决定。这里若顺手 save，重试路径就会多出一次半个状态已
        落库的窗口。

        也**不自动写 `decided_at`**：那三个终态（live/rejected/cancelled）各有自己的
        决策时刻，而且「人工什么时候点的」不该由状态机替人回答。
        """
        coerced = coerce_status(target)
        if not self.can_transition_to(coerced):
            raise InvalidTransitionError(self.status_enum, coerced)
        self.status = coerced.value
        return coerced


class StrategyCandidateTransition(models.Model):
    """候选的一次推进记录（spec #19 D20 的审计面）。

    用户故事 36：「流水线的推进动作有审计记录（什么时候把哪条候选从哪层推到哪层、
    依据是什么），这样机制本身可归因」。所以每次推进落一行，**包括没推进的那些**
    ——`from_status == to_status` 的行正是「卡在 `paper_pending` 等账户 / 排队」的
    落点（D20：卡住也要可见，否则就是静默消失）。

    **为什么 `at` 不写 `auto_now_add`**：时刻是编排层注入的外部效应之一（D14 的
    `deps` 里有「取现时刻」），而 `auto_now_add` 会绕过注入、用数据库那一刻的钟。
    判定依据里的时间区间要与真实时刻可比，测试也要能钉住一个确定的时刻。

    **为什么 `detail` 是 JSON 而不是列**：它装的是「依据」——判定数字（夏普、笔数、
    衰减、折数）、钉住的行/会话 id、以及被拒绝时的原文原因。这些形状会随层演化
    （#24 的 paper 统计就在长），而它们是**证据**、不是查询维度。要查询的维度
    （候选、状态、原因码、时刻）都已经是列。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        StrategyCandidate,
        on_delete=models.CASCADE,
        related_name="transitions",
        help_text="被推进的候选",
    )
    layer = models.CharField(
        max_length=16,
        choices=StrategyCandidateLayer.choices(),
        help_text="这一步落在哪一层（统计「哪一层刷掉最多」靠它，别从原因码推）",
    )
    from_status = models.CharField(
        max_length=32,
        choices=StrategyCandidateStatus.choices(),
        help_text="推进前的状态；与 to_status 相同表示「停住」（等账户 / 排队 / 观察中）",
    )
    to_status = models.CharField(
        max_length=32,
        choices=StrategyCandidateStatus.choices(),
        help_text="推进后的状态；与 from_status 相同时这一行是「停住」的证据",
    )
    reason_code = models.CharField(
        max_length=48,
        db_index=True,
        help_text="判定纯函数给出的原因码（`evaluate.ReasonCode` 的值）",
    )
    detail = models.JSONField(
        default=dict,
        blank=True,
        help_text="依据：判定数字 + 钉住的行 / 会话 id + 被拒绝时的原文原因",
    )
    at = models.DateTimeField(
        db_index=True, help_text="推进时刻（由编排层注入的时钟给出，不走 auto_now）"
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=(
            "这一行落库的时刻，只用来给同一 `at` 的多行定序（注入的时钟在一次推进里"
            "是同一个值，光按 `at` 排不出先后，「从哪层到哪层」的顺序就丢了）"
        ),
    )

    class Meta:
        db_table = "strategy_candidate_transitions"
        verbose_name = "候选推进记录"
        verbose_name_plural = "候选推进记录"
        ordering = ["-at", "-created_at", "-id"]
        indexes = [
            models.Index(fields=["candidate", "-at", "-created_at"]),
            models.Index(fields=["reason_code"]),
        ]

    def __str__(self) -> str:
        return (
            f"[{self.layer}] {self.from_status} → {self.to_status} ({self.reason_code})"
        )
