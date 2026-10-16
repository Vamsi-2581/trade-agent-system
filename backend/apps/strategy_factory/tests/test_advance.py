"""漏斗编排 `advance()` 的接线用例（spec #19 D9 / D10 / D12 / D14 / D18 / D20；子票 #22）。

这一份钉住的是票面的六条验收：

1. **接线**：注入假回测执行与假会话创建，断言「状态落库 + 转移记录 + 建了/没建会话 +
   通知发了」四件事（`test_happy_path_wires_state_record_session_and_notice` 逐层走完）。
2. **幂等**：同层推进两次 → 一组 fold 回测、一条 paper 会话（`TestIdempotency`）。
3. **等账户**：未指定 `paper_account` → 停 `paper_pending` 且通知发出；指定后自动前进。
4. **排队**：同 (账户, 品种) 已有活跃会话 → 停 `paper_pending`、原因写明排队（沿用
   `apps/trading/tests/test_live_session_conflict.py` 的四段式断言）。
5. **下限**：判定不通过时**不产生任何会话**（漏斗下限 = 什么都不发生）。
6. **参数继承**：paper 会话的 `config` 与候选回测参数逐键一致（这里用**真实**的
   `effects.create_paper_session`，只假余额与回测执行）。

两处刻意的取舍：

* 会话创建走**真实实现**，只把余额读取 patch 掉。若连它一起假掉，「参数逐键一致」这条
  验收就变成在断言假实现自己写的东西——那正是 #29 复核暴露过的**假绿**。
* 通知都经 `django_capture_on_commit_callbacks(execute=True)` 取：`advance` 把通知挂
  `on_commit`（一次回滚的推进不该告诉用户它发生了），所以用例必须把它跑出来才看得到。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from apps.backtest.models import BacktestResult
from apps.exchange.models import ExchangeAccount
from apps.strategy_factory import effects
from apps.strategy_factory.config import CONFIG, FactoryConfig
from apps.strategy_factory.evaluate import (
    InSampleEvidence,
    OosEvidence,
    ReasonCode,
)
from apps.strategy_factory.models import (
    StrategyCandidate,
    StrategyCandidateTransition,
)
from apps.strategy_factory.orchestrator import (
    AdvanceDeps,
    MissingRunContextError,
    RunContext,
    advance,
)
from apps.strategy_factory.status import (
    StrategyCandidateLayer,
    StrategyCandidateOrigin,
    StrategyCandidateStatus,
)
from apps.strategy_factory.walk_forward import plan_folds
from apps.trading.models import LiveSession, Strategy

pytestmark = pytest.mark.django_db

SYMBOL = "BTC/USDT:USDT"
TF = "1h"
START = date(2026, 1, 1)
END = date(2026, 4, 11)  # 100 天 → 样本内区右界 2026-03-22（与 #29 同一组几何）
FIXED_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
BALANCE = Decimal("1234.56")
PARAMS = {"exit_at_mid": False, "position_pct": 0.2}


def _ctx(**overrides) -> RunContext:
    base = dict(
        symbol=SYMBOL,
        timeframe=TF,
        evaluation_start=START,
        evaluation_end=END,
        initial_capital=Decimal("10000"),
        parameters=dict(PARAMS),
    )
    base.update(overrides)
    return RunContext(**base)


def _user(username: str):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _strategy(name: str = "ema_crossover_v9") -> Strategy:
    return Strategy.objects.create(name=name, code_path=f"strategies/{name}.py")


def _account(label: str = "paper-testnet", *, testnet: bool = True) -> ExchangeAccount:
    return ExchangeAccount.objects.create(
        exchange="binance",
        label=label,
        api_key_enc=b"k",
        api_secret_enc=b"s",
        testnet=testnet,
    )


def _candidate(
    strategy: Strategy,
    status: StrategyCandidateStatus = StrategyCandidateStatus.REGISTERED,
    *,
    name: str | None = None,
    **kwargs,
) -> StrategyCandidate:
    return StrategyCandidate.objects.create(
        name=name or strategy.name,
        hypothesis="快线上穿慢线时做多，趋势行情里赚取中间段",
        code_path=f"~/.tradelogx/strategies/{name or strategy.name}.py",
        origin=StrategyCandidateOrigin.AGENT.value,
        source_ref={"agent_session": "sess-1"},
        strategy=strategy,
        status=status.value,
        **kwargs,
    )


def _l1_row(
    strategy: Strategy,
    candidate_id: Any,
    *,
    sharpe: float | None = 2.0,
    trades: int = 42,
    parameters: dict | None = None,
    review_status: str = "pending",
    symbol: str = SYMBOL,
    user=None,
) -> BacktestResult:
    return BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol=symbol,
        timeframe=TF,
        start_date=START,
        end_date=date(2026, 3, 22),
        initial_capital=Decimal("10000"),
        final_capital=Decimal("11000"),
        total_return_pct=10.0,
        sharpe_ratio=sharpe,
        total_trades=trades,
        parameters=dict(parameters or PARAMS),
        # 传「不属于任何候选」的行时（比如挡路的会话用的那一条）留空：分组键是 UUID 列，
        # `str(None)` 会变成一个不合法的 UUID 字面量。
        **({"candidate_id": str(candidate_id)} if candidate_id else {}),
        review_status=review_status,
    )


class FakePipeline:
    """假的「跑回测」与「发通知」；会话创建与判定都走真实实现。"""

    def __init__(
        self,
        context: RunContext | None = None,
        *,
        config: FactoryConfig = CONFIG,
        strategy: Strategy | None = None,
    ) -> None:
        self.context = context if context is not None else _ctx()
        self.config = config
        self.strategy = strategy
        self.started_in_sample: list[dict[str, Any]] = []
        self.started_oos: list[dict[str, Any]] = []
        self.notifications: list[str] = []
        #: 默认「尚无结论」——模拟回测/分折还在跑。
        self.in_sample_evidence: InSampleEvidence | None = None
        self.oos_evidence: OosEvidence | None = None
        self.resolved_contexts = 0

    # -- deps 成员 ---------------------------------------------------------- #

    def resolve_run_context(self, candidate):
        self.resolved_contexts += 1
        return self.context

    def start_in_sample(self, candidate, context, plan):
        self.started_in_sample.append({"context": context, "plan": plan})
        row = _l1_row(
            self.strategy or candidate.strategy,
            candidate.pk,
            parameters=dict(context.parameters),
            user=candidate.proposed_by,
        )
        return {"result_id": str(row.pk)}

    def read_in_sample(self, candidate):
        return self.in_sample_evidence

    def start_oos(self, candidate, context, plan, in_sample_result_id):
        self.started_oos.append(
            {
                "context": context,
                "plan": plan,
                "in_sample_result_id": str(in_sample_result_id),
            }
        )
        return {"data_sufficient": True, "decay_pct": 5.0, "fold_count": 5}

    def read_oos(self, candidate):
        return self.oos_evidence

    def notify(self, candidate, message):
        self.notifications.append(message)
        return True

    def now(self):
        return FIXED_NOW

    def deps(self) -> AdvanceDeps:
        return AdvanceDeps(
            resolve_run_context=self.resolve_run_context,
            start_in_sample=self.start_in_sample,
            read_in_sample=self.read_in_sample,
            start_oos=self.start_oos,
            read_oos=self.read_oos,
            # 真实实现：只把余额读取 patch 掉（见 `_patch_balance`）。
            create_paper_session=effects.create_paper_session,
            notify=self.notify,
            now=self.now,
            config=self.config,
        )


@pytest.fixture
def _patch_balance(monkeypatch):
    """把「读账户余额」换成一个确定值（网络与适配器都不该进单元用例）。"""
    monkeypatch.setattr(effects, "fetch_account_balance", lambda account: BALANCE)
    return BALANCE


def _records(candidate: StrategyCandidate) -> list[StrategyCandidateTransition]:
    """按**落库顺序**取记录。

    不用 `at` 排序：注入的时钟在一次推进里是同一个值，「哪条在前」只能靠 `created_at`
    与主键。这条纪律本身也是被测对象（见 `TestTransitionRecords`）。
    """
    return list(
        StrategyCandidateTransition.objects.filter(candidate=candidate).order_by(
            "created_at", "id"
        )
    )


def _reload(candidate: StrategyCandidate) -> StrategyCandidate:
    candidate.refresh_from_db()
    return candidate


# --------------------------------------------------------------------------- #
# 验收 1：接线（四件事）
# --------------------------------------------------------------------------- #


class TestWiring:
    def test_happy_path_wires_state_record_session_and_notice(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """注册 → 样本内 → 样本外 → paper：每层断言四件事，最后建出会话。"""
        user = _user("pipe-happy")
        strategy = _strategy()
        candidate = _candidate(strategy, name="pipeline_happy", proposed_by=user)
        account = _account()
        fake = FakePipeline(strategy=strategy)

        # --- L1 入口：registered → in_sample_running（起样本内回测） ---------- #
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.from_status is StrategyCandidateStatus.REGISTERED
        assert out.to_status is StrategyCandidateStatus.IN_SAMPLE_RUNNING
        assert out.reason_code is ReasonCode.IN_SAMPLE_RUN_STARTED
        assert out.changed and out.recorded and not out.created_paper_session
        assert _reload(candidate).status == "in_sample_running"
        assert len(_records(candidate)) == 1
        assert fake.notifications, "每层结论都要通知提出人"
        assert "pipeline_happy" in fake.notifications[0]
        assert LiveSession.objects.count() == 0

        # --- L1 的那一步：起跑 + 读了结论（假执行器同步给出 2.0 夏普 / 42 笔） --- #
        assert fake.started_in_sample == []
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.started_run, "这一层还没起跑，这一步就该起跑"
        assert len(fake.started_in_sample) == 1
        # 起跑的那一次只跑样本内区：末界是 plan_folds 推出来的样本内右界。
        plan = fake.started_in_sample[0]["plan"]
        assert plan.in_sample_end == plan_folds(START, END, config=CONFIG).in_sample_end
        assert out.to_status is StrategyCandidateStatus.IN_SAMPLE_RUNNING, (
            "起跑后仍无结论（假执行器不写 evidence）→ 停在样本内"
        )

        # 回测跑完了：给出结论，再推一次（这一次只落结论，不起跑下一层）。
        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=2.0, total_trades=42)
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.to_status is StrategyCandidateStatus.OOS_RUNNING
        assert out.reason_code is ReasonCode.IN_SAMPLE_PASSED
        assert out.changed and not out.started_run, (
            "一步一层：起跑下一层是下一次推进的事"
        )
        assert len(fake.started_in_sample) == 1, "同层不得重复起跑"
        assert fake.started_oos == []
        # 交付 2：样本内通过后由**漏斗**写审核字段（T0 已把该职责从回测保存路径移出）。
        row = BacktestResult.objects.get(
            id=_reload(candidate).evidence["layers"]["in_sample"]["result_id"]
        )
        assert row.review_status == "approved"
        assert row.reviewed_at == FIXED_NOW
        assert LiveSession.objects.count() == 0

        # --- L2 起跑：分折开跑，尚无结论 --------------------------------------- #
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.layer is StrategyCandidateLayer.OOS
        assert out.started_run and out.reason_code is ReasonCode.OOS_PENDING
        assert len(fake.started_oos) == 1

        # --- L2：分折给出结论 → paper_pending ---------------------------------- #
        fake.oos_evidence = OosEvidence(decay_pct=5.0, fold_count=5)
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.to_status is StrategyCandidateStatus.PAPER_PENDING
        assert out.reason_code is ReasonCode.OOS_PASSED
        # 分折拿到的是**完整评估窗口**（#29 交接：不许从样本内行反推几何）。
        call = fake.started_oos[0]
        assert call["context"].evaluation_start == START
        assert call["context"].evaluation_end == END
        assert call["plan"].end_date == END
        assert LiveSession.objects.count() == 0

        # --- L3：没指定账户 → 停 paper_pending 并通知（验收 3） ---------------- #
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.from_status is out.to_status is StrategyCandidateStatus.PAPER_PENDING
        assert out.reason_code is ReasonCode.PAPER_ACCOUNT_MISSING
        assert not out.changed and not out.created_paper_session
        assert out.detail["account_problem"] == "未指定 paper 账户"
        assert "指定一个 testnet 账户" in fake.notifications[-1]
        assert LiveSession.objects.count() == 0

        # --- 指定账户后下一轮自动前进（验收 3 的后半） ------------------------- #
        candidate.paper_account = account
        candidate.save(update_fields=["paper_account"])
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.to_status is StrategyCandidateStatus.PAPER_RUNNING
        assert out.reason_code is ReasonCode.PAPER_OBSERVATION_STARTED
        assert out.changed and out.created_paper_session
        assert out.started_run is False, "建会话不是「起跑一层回测」"

        fresh = _reload(candidate)
        assert fresh.status == "paper_running"
        session = LiveSession.objects.get()
        assert fresh.paper_session_id == session.pk
        assert out.paper_session_id == str(session.pk)

        # 验收 6：参数**逐键**继承候选回测那一份（真实实现，不是假会话创建）。
        assert session.config == PARAMS
        assert session.mode == "paper"
        assert session.symbol == SYMBOL
        assert session.initial_capital == BALANCE
        assert session.current_equity == BALANCE
        assert session.exchange_account_id == account.pk
        assert (
            session.backtest_result_id
            == BacktestResult.objects.get(
                id=fresh.evidence["layers"]["in_sample"]["result_id"]
            ).pk
        )

        # 每一层都留下记录：入口（起跑）、结论、以及四层走完的最后一次转移。
        records = _records(candidate)
        assert [r.reason_code for r in records] == [
            ReasonCode.IN_SAMPLE_RUN_STARTED.value,
            ReasonCode.IN_SAMPLE_PENDING.value,
            ReasonCode.IN_SAMPLE_PASSED.value,
            ReasonCode.OOS_PENDING.value,
            ReasonCode.OOS_PASSED.value,
            ReasonCode.PAPER_ACCOUNT_MISSING.value,
            ReasonCode.PAPER_OBSERVATION_STARTED.value,
        ]
        assert all(r.at == FIXED_NOW for r in records)
        assert records[-1].layer == StrategyCandidateLayer.PAPER.value
        # 四层各自的原因码都有落点，统计「哪一层刷掉最多」靠 layer 列。
        assert {r.layer for r in records} == {"in_sample", "oos", "paper"}

    def test_run_context_is_pinned_after_first_use(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """跑批判据第一次用到就钉进 `evidence`，此后不再回读 deps。

        钉住是为了「样本内那一行」与「分折几何」永远来自同一段窗口：中途改了 deps
        却让两层跑在不同窗口上，衰减照样算得出来，只是没有意义。
        """
        strategy = _strategy("pinning")
        candidate = _candidate(strategy, name="pinning")
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            advance(candidate, deps=fake.deps())  # registered → in_sample_running
        with django_capture_on_commit_callbacks(execute=True):
            advance(candidate, deps=fake.deps())  # 起跑（用到跑批判据）
        pinned = _reload(candidate).evidence["run_context"]
        assert pinned["symbol"] == SYMBOL
        assert pinned["evaluation_start"] == START.isoformat()
        assert pinned["initial_capital"] == "10000"

        # 换一份 deps 给别的窗口：候选必须继续用钉住的那一份。
        other = FakePipeline(
            _ctx(
                symbol="ETH/USDT",
                evaluation_start=date(2025, 1, 1),
                evaluation_end=date(2025, 6, 1),
            ),
            strategy=strategy,
        )
        with django_capture_on_commit_callbacks(execute=True):
            advance(candidate, deps=other.deps())
        assert other.resolved_contexts == 0, "钉住之后不该再去问 deps 要跑批判据"

    def test_approval_lands_when_start_and_verdict_share_one_call(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """生产形状：同步起跑 → 同一次推进里就拿到结论 → 审核字段必须当场写。

        这个形状曾被用例漏掉（走的两步式），而它恰好是**默认接线唯一的形状**：真实实现
        的 `start_in_sample` 返回时结果已落库。漏掉它的代价是审核字段静默不写、候选照样
        前进，直到建 paper 会话那一步才炸在「主行没 approved」上。
        """
        strategy = _strategy("approve_same_call")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.IN_SAMPLE_RUNNING,
            name="approve_same_call",
        )
        fake = FakePipeline(strategy=strategy)
        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=2.6, total_trades=77)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.started_run, "同一次推进里先起跑"
        assert out.to_status is StrategyCandidateStatus.OOS_RUNNING, "再直接下结论"
        row = BacktestResult.objects.get(
            id=_reload(candidate).evidence["layers"]["in_sample"]["result_id"]
        )
        assert row.review_status == "approved", "起跑与结论同一次推进时，审核字段也要写"
        assert row.reviewed_at == FIXED_NOW

    def test_review_fields_only_promote_pending(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """人工已经给过结论的行**不被覆盖**（T0 纪律的镜像）。"""
        strategy = _strategy("review_keep")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="review_keep"
        )
        human_at = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        BacktestResult.objects.filter(id=row.pk).update(reviewed_at=human_at)
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])

        fake = FakePipeline(strategy=strategy)
        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=3.0, total_trades=50)
        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.to_status is StrategyCandidateStatus.OOS_RUNNING
        row.refresh_from_db()
        assert row.review_status == "approved"
        assert row.reviewed_at == human_at, "漏斗不得改写人工审核的时刻"


# --------------------------------------------------------------------------- #
# 验收 2：幂等
# --------------------------------------------------------------------------- #


class TestIdempotency:
    def test_same_layer_twice_starts_one_in_sample_run(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """同层推进两次 → 一次样本内起跑、一条同结论转移记录、一次通知。"""
        strategy = _strategy("idem_l1")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="idem_l1"
        )
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            first = advance(candidate, deps=fake.deps())
        with django_capture_on_commit_callbacks(execute=True):
            second = advance(candidate, deps=fake.deps())

        assert first.started_run and first.recorded
        assert not second.started_run, "层记录已在 → 不得重复起跑"
        assert not second.recorded, "同一条结论连来时不重复落记录"
        assert len(fake.started_in_sample) == 1
        assert len(_records(candidate)) == 1
        assert len(fake.notifications) == 0, "「尚无结果」不是结论，不打扰提出人"

    def test_same_layer_twice_starts_one_oos_run(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """分折同理：两次推进只跑一组折。"""
        strategy = _strategy("idem_l2")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="idem_l2"
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "run_context": _ctx().as_payload(),
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            },
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            advance(candidate, deps=fake.deps())
        with django_capture_on_commit_callbacks(execute=True):
            advance(candidate, deps=fake.deps())

        assert len(fake.started_oos) == 1
        assert len(_records(candidate)) == 1

    def test_same_layer_twice_creates_one_paper_session(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """paper 会话同理：同层推进两次只有一条会话。"""
        strategy = _strategy("idem_l3")
        account = _account("idem-account")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="idem_l3",
            paper_account=account,
            proposed_by=_user("idem-l3-owner"),
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "run_context": _ctx().as_payload(),
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            },
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            first = advance(candidate, deps=fake.deps())
        with django_capture_on_commit_callbacks(execute=True):
            second = advance(candidate, deps=fake.deps())

        assert first.created_paper_session
        assert not second.created_paper_session
        assert LiveSession.objects.count() == 1
        assert second.from_status is StrategyCandidateStatus.PAPER_RUNNING
        assert second.reason_code is ReasonCode.PAPER_OBSERVING


# --------------------------------------------------------------------------- #
# 「改参数重跑」这条回边必须真的能重跑（D3 的合法边）
# --------------------------------------------------------------------------- #


class TestLayerRerun:
    """反例审查找出来的一条**真缺陷**，用例把它钉住。

    状态机给 `*_failed` 留了回边（`in_sample_failed → in_sample_running`，D3 写的
    「改参数重跑」）。但起跑记录（`layers.<层>`）若在失败后仍留着，重进同一层时编排层
    会认为「这一层已经起跑过了」，于是连回测都不跑、直接把上一次的失败结论再念一遍——
    那条边在代码上等于不存在。所以落 `*_failed` 时**作废该层的起跑记录**。
    """

    def test_failed_in_sample_can_be_rerun(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        strategy = _strategy("rerun_l1")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="rerun_l1"
        )
        fake = FakePipeline(strategy=strategy)
        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=0.4, total_trades=88)

        with django_capture_on_commit_callbacks(execute=True):
            first = advance(candidate, deps=fake.deps())
        assert first.to_status is StrategyCandidateStatus.IN_SAMPLE_FAILED
        assert len(fake.started_in_sample) == 1
        assert "in_sample" not in _reload(candidate).evidence["layers"], (
            "失败后该层的起跑记录必须作废，否则回边是一句空话"
        )

        # 人/agent 来动：in_sample_failed → in_sample_running（改参数重跑）
        fresh = _reload(candidate)
        fresh.transition_to(StrategyCandidateStatus.IN_SAMPLE_RUNNING)
        fresh.save(update_fields=["status"])

        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=2.9, total_trades=90)
        with django_capture_on_commit_callbacks(execute=True):
            second = advance(fresh, deps=fake.deps())

        assert len(fake.started_in_sample) == 2, "重跑必须真的再跑一次回测"
        assert second.to_status is StrategyCandidateStatus.OOS_RUNNING
        assert second.reason_code is ReasonCode.IN_SAMPLE_PASSED
        # 重跑落库的那一行才该被批准（新记录指向新行）。
        row = BacktestResult.objects.get(
            id=_reload(candidate).evidence["layers"]["in_sample"]["result_id"]
        )
        assert row.review_status == "approved"

    def test_failed_oos_can_be_rerun(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        strategy = _strategy("rerun_l2")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="rerun_l2"
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "run_context": _ctx().as_payload(),
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            },
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)
        fake.oos_evidence = OosEvidence(decay_pct=44.0, fold_count=5)

        with django_capture_on_commit_callbacks(execute=True):
            first = advance(candidate, deps=fake.deps())
        assert first.to_status is StrategyCandidateStatus.OOS_FAILED
        assert len(fake.started_oos) == 1
        layers = _reload(candidate).evidence["layers"]
        assert "oos" not in layers, "失败后该层的起跑记录必须作废"
        assert "in_sample" in layers, "样本内那一层没失败，它的主行必须留着"

        fresh = _reload(candidate)
        fresh.transition_to(StrategyCandidateStatus.OOS_RUNNING)
        fresh.save(update_fields=["status"])
        fake.oos_evidence = OosEvidence(decay_pct=8.0, fold_count=5)

        with django_capture_on_commit_callbacks(execute=True):
            second = advance(fresh, deps=fake.deps())

        assert len(fake.started_oos) == 2, "分折重跑必须真的再跑一组折"
        assert second.to_status is StrategyCandidateStatus.PAPER_PENDING

    def test_insufficient_evidence_also_clears_the_layer(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """「证据不足」与「不合格」落同一个 `*_failed`，重跑规则必须一致。"""
        strategy = _strategy("rerun_thin")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="rerun_thin"
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)
        fake.oos_evidence = OosEvidence(
            decay_pct=None, fold_count=0, data_sufficient=False
        )

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.to_status is StrategyCandidateStatus.OOS_FAILED
        assert out.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
        assert "oos" not in _reload(candidate).evidence["layers"]


# --------------------------------------------------------------------------- #
# 验收 4：串行排队
# --------------------------------------------------------------------------- #


class TestPaperQueueing:
    def test_queued_behind_active_session_holds_with_reason(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """同 (账户, 品种) 已有活跃会话 → 停 `paper_pending`，原因写明排队中。

        四段式断言照 `apps/trading/tests/test_live_session_conflict.py`：状态 + 挡路的
        策略名 + 冲突原因 + **没有多建会话、也没动既有会话**。
        """
        strategy = _strategy("queue_blocked")
        other_strategy = _strategy("donchian_strategy")
        account = _account("queued-account")
        blocker_row = _l1_row(
            other_strategy, None, review_status="approved", symbol=SYMBOL
        )
        blocker = LiveSession.objects.create(
            user=_user("queue-owner"),
            backtest_result=blocker_row,
            strategy=other_strategy,
            symbol=SYMBOL,
            mode="paper",
            status="running",
            exchange_account=account,
            initial_capital=Decimal("1000"),
            current_equity=Decimal("1000"),
            config={},
        )
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="queue_blocked",
            paper_account=account,
            proposed_by=_user("queue-blocked-owner"),
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "run_context": _ctx().as_payload(),
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            },
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.from_status is out.to_status is StrategyCandidateStatus.PAPER_PENDING
        assert out.reason_code is ReasonCode.PAPER_QUEUED
        assert not out.changed and not out.created_paper_session
        conflict = out.detail["conflict"]
        assert "donchian_strategy" in conflict, "拒绝理由必须点出挡路的那支会话"
        assert "账户级持仓" in conflict, "拒绝理由必须说明为什么冲突"
        assert LiveSession.objects.count() == 1, "排队时不得多建会话"
        blocker.refresh_from_db()
        assert blocker.status == "running", "排队时不得改动既有会话"
        assert "排队" in fake.notifications[-1], "卡在排队也必须可见（D20）"

    def test_queue_clears_and_next_round_advances(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """挡路的会话停下后，下一轮自动建会话前进（D9 的「下一轮推进重试」）。"""
        strategy = _strategy("queue_clears")
        other_strategy = _strategy("blocker")
        account = _account("clears-account")
        blocker_row = _l1_row(other_strategy, None, review_status="approved")
        blocker = LiveSession.objects.create(
            user=_user("queue-owner-2"),
            backtest_result=blocker_row,
            strategy=other_strategy,
            symbol=SYMBOL,
            mode="paper",
            status="running",
            exchange_account=account,
            initial_capital=Decimal("1000"),
            config={},
        )
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="queue_clears",
            paper_account=account,
            proposed_by=_user("queue-clears-owner"),
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            held = advance(candidate, deps=fake.deps())
        assert held.reason_code is ReasonCode.PAPER_QUEUED

        blocker.status = "stopped"
        blocker.save(update_fields=["status"])

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())
        assert out.to_status is StrategyCandidateStatus.PAPER_RUNNING
        assert out.created_paper_session
        assert LiveSession.objects.count() == 2


# --------------------------------------------------------------------------- #
# 验收 5：漏斗下限 = 什么都不发生
# --------------------------------------------------------------------------- #


class TestFunnelFloor:
    def test_in_sample_failure_creates_no_session(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """样本内不通过 → `in_sample_failed`，且没有任何会话、没有分折。"""
        strategy = _strategy("fails_l1")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="fails_l1"
        )
        fake = FakePipeline(strategy=strategy)
        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=0.4, total_trades=88)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.to_status is StrategyCandidateStatus.IN_SAMPLE_FAILED
        assert out.reason_code is ReasonCode.IN_SAMPLE_SHARPE_BELOW_MIN
        assert out.changed and out.recorded
        assert fake.started_in_sample, "这一层还没起跑，同一次推进里先起跑再判"
        assert fake.started_oos == [], "样本内没过就不该起跑分折"
        assert LiveSession.objects.count() == 0
        assert _reload(candidate).paper_session_id is None
        assert out.detail["sharpe_ratio"] == 0.4

    def test_oos_failure_creates_no_session(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """样本外衰减超标 → `oos_failed`，同样什么都不发生。"""
        strategy = _strategy("fails_l2")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="fails_l2"
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)
        fake.oos_evidence = OosEvidence(decay_pct=44.0, fold_count=5)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.to_status is StrategyCandidateStatus.OOS_FAILED
        assert out.reason_code is ReasonCode.OOS_DECAY_ABOVE_MAX
        assert LiveSession.objects.count() == 0

    def test_oos_insufficient_evidence_does_not_release(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """数据不足判「证据不足」且不放行（D6）——原因码与「衰减超标」分得开。"""
        strategy = _strategy("oos_thin")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="oos_thin"
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)
        fake.oos_evidence = OosEvidence(
            decay_pct=None, fold_count=3, data_sufficient=False
        )

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.to_status is StrategyCandidateStatus.OOS_FAILED
        assert out.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
        assert out.detail["data_sufficient"] is False
        assert LiveSession.objects.count() == 0

    def test_insufficient_geometry_does_not_waste_a_backtest(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """窗口切不出折 → 不跑样本内回测，直接判「证据不足」并写出几何原因。"""
        strategy = _strategy("thin_window")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="thin_window"
        )
        fake = FakePipeline(
            _ctx(evaluation_start=date(2026, 1, 1), evaluation_end=date(2026, 1, 4)),
            strategy=strategy,
        )

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.to_status is StrategyCandidateStatus.IN_SAMPLE_FAILED
        assert out.reason_code is ReasonCode.INSUFFICIENT_EVIDENCE
        assert fake.started_in_sample == [], "切不出折就不该浪费一次回测"
        assert out.detail["insufficient_reason"], "几何原因要落到转移记录里"
        assert out.detail["geometry"]["data_sufficient"] is False
        assert LiveSession.objects.count() == 0


# --------------------------------------------------------------------------- #
# 总开关 / 终态 / 接线错误
# --------------------------------------------------------------------------- #


class TestGatesAndWiring:
    def test_auto_advance_disabled_changes_nothing(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """总开关关掉 → 状态不动、不建会话、不通知（只留一条「开关关闭」的记录）。"""
        strategy = _strategy("switch_off")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.PAPER_PENDING, name="switch_off"
        )
        fake = FakePipeline(strategy=strategy)
        fake.config = FactoryConfig(auto_advance_enabled=False)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.from_status is out.to_status is StrategyCandidateStatus.PAPER_PENDING
        assert out.reason_code is ReasonCode.AUTO_ADVANCE_DISABLED
        assert not out.changed and not out.created_paper_session
        assert fake.notifications == []
        assert LiveSession.objects.count() == 0

    def test_terminal_candidate_is_left_alone(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """终态不再推进：连记录都不写（轮询会把终态候选项一遍遍扫到）。"""
        strategy = _strategy("terminal")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.CANCELLED, name="terminal"
        )
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.reason_code is ReasonCode.TERMINAL
        assert out.from_status is out.to_status is StrategyCandidateStatus.CANCELLED
        assert not out.recorded
        assert _records(candidate) == []
        assert fake.notifications == []

    def test_missing_run_context_fails_loud(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """没有跑批判据 → Fail Loud，而不是落一条「候选不合格」的判定。"""
        strategy = _strategy("no_context")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="no_context"
        )
        fake = FakePipeline(strategy=strategy)
        fake.context = None

        with pytest.raises(MissingRunContextError) as excinfo:
            advance(candidate, deps=fake.deps())

        assert "跑批判据" in str(excinfo.value)
        assert _reload(candidate).status == "in_sample_running"
        assert _records(candidate) == []
        assert LiveSession.objects.count() == 0

    def test_paper_account_must_be_testnet(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """绑了实盘账户 → 停 `paper_pending` 等人工改绑（不是抛接线错误、更不是放行）。"""
        strategy = _strategy("live_account")
        account = _account("live-account", testnet=False)
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="live_account",
            paper_account=account,
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.reason_code is ReasonCode.PAPER_ACCOUNT_MISSING
        assert not out.changed
        assert "testnet" in out.detail["account_problem"]
        assert LiveSession.objects.count() == 0
        assert "testnet" in fake.notifications[-1]

    def test_paper_session_requires_approved_backtest(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """主行没 approved → 建会话这一步 Fail Loud（不允许凭未审核的回测建会话）。"""
        from apps.strategy_factory.orchestrator import AdvanceWiringError

        strategy = _strategy("unapproved")
        account = _account("unapproved-account")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="unapproved",
            paper_account=account,
            proposed_by=_user("unapproved-owner"),
        )
        row = _l1_row(strategy, candidate.pk, review_status="pending")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)

        with pytest.raises(AdvanceWiringError):
            advance(candidate, deps=fake.deps())

        assert LiveSession.objects.count() == 0
        assert _reload(candidate).status == "paper_pending"

    def test_running_paper_layer_is_left_to_t5(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """`paper_running` 的判定归 #24：本票只保证它不会被推去别处。"""
        strategy = _strategy("observing")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.PAPER_RUNNING, name="observing"
        )
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.from_status is out.to_status is StrategyCandidateStatus.PAPER_RUNNING
        assert out.reason_code is ReasonCode.PAPER_OBSERVING
        assert not out.changed


# --------------------------------------------------------------------------- #
# 通知策略
# --------------------------------------------------------------------------- #


class TestNotificationPolicy:
    def test_stuck_notice_is_sent_once_per_conclusion(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """「等账户」这类停住态重复轮询不刷屏：同一条结论只通知一次。"""
        strategy = _strategy("no_spam")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.PAPER_PENDING, name="no_spam"
        )
        row = _l1_row(strategy, candidate.pk, review_status="approved")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": str(row.pk),
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        fake = FakePipeline(strategy=strategy)

        for _ in range(3):
            with django_capture_on_commit_callbacks(execute=True):
                advance(candidate, deps=fake.deps())

        assert len(fake.notifications) == 1
        assert len(_records(candidate)) == 1

    def test_layer_start_does_not_notify(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """「还在跑」不是结论：起跑本身不打扰提出人。"""
        strategy = _strategy("quiet_start")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="quiet_start"
        )
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            out = advance(candidate, deps=fake.deps())

        assert out.started_run and out.recorded
        assert out.notify_message is None
        assert fake.notifications == []

    def test_notice_is_only_sent_after_commit(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """通知挂 `on_commit`：不捕获就一条都不该发（回滚的推进不许告诉用户它发生了）。"""
        strategy = _strategy("commit_only")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="commit_only"
        )
        fake = FakePipeline(strategy=strategy)
        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=9.9, total_trades=99)

        out = advance(candidate, deps=fake.deps())
        assert out.to_status is StrategyCandidateStatus.OOS_RUNNING, "状态照常落库"
        assert fake.notifications == [], "未提交（未捕获）时通知不得发出"

        with django_capture_on_commit_callbacks(execute=True):
            advance(_reload(candidate), deps=fake.deps())
        assert fake.notifications == [], "同一条结论不重复通知"


# --------------------------------------------------------------------------- #
# 转移记录
# --------------------------------------------------------------------------- #


class TestTransitionRecords:
    def test_record_carries_reason_layer_and_evidence(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """D20：每次推进写一条记录（从哪层到哪层、原因码、依据、时刻）。"""
        strategy = _strategy("record_shape")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="record_shape"
        )
        fake = FakePipeline(strategy=strategy)
        fake.in_sample_evidence = InSampleEvidence(sharpe_ratio=2.4, total_trades=64)

        with django_capture_on_commit_callbacks(execute=True):
            advance(candidate, deps=fake.deps())

        record = _records(candidate)[0]
        assert record.layer == StrategyCandidateLayer.IN_SAMPLE.value
        assert record.from_status == "in_sample_running"
        assert record.to_status == "oos_running"
        assert record.reason_code == ReasonCode.IN_SAMPLE_PASSED.value
        assert record.at == FIXED_NOW
        assert record.detail["sharpe_ratio"] == 2.4
        assert record.detail["total_trades"] == 64
        assert record.detail["next_status"] == "oos_running"

    def test_layer_records_survive_for_audit(
        self, django_capture_on_commit_callbacks, _patch_balance
    ):
        """层记录（起跑过的那一行/那一条会话）留在 `evidence` 里，事后可复核。"""
        strategy = _strategy("layers_kept")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name="layers_kept"
        )
        fake = FakePipeline(strategy=strategy)

        with django_capture_on_commit_callbacks(execute=True):
            advance(candidate, deps=fake.deps())

        evidence = _reload(candidate).evidence
        assert evidence["layers"]["in_sample"]["result_id"]
        assert (
            evidence["layers"]["in_sample"]["in_sample_end"]
            == date(2026, 3, 22).isoformat()
        )
        assert evidence["verdict"]["reason_code"] == ReasonCode.IN_SAMPLE_PENDING.value
        assert evidence["run_context"]["symbol"] == SYMBOL


def test_deps_default_is_lazy_and_wired():
    """`advance` 不传 deps 时用生产接线；八个效应成员一个都不能缺。"""
    from apps.strategy_factory import effects as effects_module

    deps = effects_module.default_deps()
    assert deps.notify is effects_module.notify
    assert deps.create_paper_session is effects_module.create_paper_session
    assert deps.read_oos is effects_module.read_oos
    assert callable(deps.now)
    assert isinstance(deps.now(), datetime)
    assert deps.config is CONFIG


def test_advance_outcome_is_serialisable():
    """任务返回值（`tasks._outcome_payload` 吃的那份）都是 JSON-safe 字段。"""
    from apps.strategy_factory.tasks import _outcome_payload
    from apps.strategy_factory.orchestrator import AdvanceOutcome

    payload = _outcome_payload(
        AdvanceOutcome(
            candidate_id="c1",
            layer=StrategyCandidateLayer.OOS,
            from_status=StrategyCandidateStatus.OOS_RUNNING,
            to_status=StrategyCandidateStatus.PAPER_PENDING,
            reason_code=ReasonCode.OOS_PASSED,
            changed=True,
            started_run=True,
            recorded=True,
        )
    )
    assert payload == {
        "candidate_id": "c1",
        "layer": "oos",
        "from_status": "oos_running",
        "to_status": "paper_pending",
        "reason_code": "oos_passed",
        "changed": True,
        "started_run": True,
        "created_paper_session": False,
        "paper_session_id": None,
        "recorded": True,
        "notified": False,
    }


def test_plan_geometry_matches_reference_window():
    """几何自检：本文件用的 100 天窗口与 #29 用例同一组（样本内右界 2026-03-22）。"""
    plan = plan_folds(START, END, config=CONFIG)
    assert plan.data_sufficient is True
    assert plan.in_sample_end == date(2026, 3, 22)
    assert plan.folds == 5
    assert sum(w.days for w in plan.windows) == 20
    assert timedelta(0) <= (END - START)
