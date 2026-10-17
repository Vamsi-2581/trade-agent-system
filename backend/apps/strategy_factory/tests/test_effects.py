"""默认接线（`effects.py`）的用例（spec #19 D10 / D12 / D18 / D19；子票 #22）。

编排层（`test_advance.py`）用假实现跑；这一份跑**真实实现**，钉住三件事：

1. **样本内那一次真的只跑样本内区**（#29 的交接）。用真实取数 + 真实引擎跑一遍，断言
   落库那行的区间不越过 `plan_folds(...).in_sample_end`——越过了衰减就会被系统性低估
   （真值 30% 算成 ~25.5% 从而错误放行），而 `_validate_wiring` 又是到分折那一步才拦。
   在这里就地拦，错就错在近处。
2. **审核字段的写入职责**（T0 的另一半）：回测保存路径落 `pending`，`approved` 只能由
   漏斗在样本内通过时写。
3. **paper 会话的参数继承与不变式**（用户故事 19 / D10）：`config` 逐键来自候选主行；
   非 testnet 账户、未通过审核的主行、同账户同品种冲突都必须 Fail Loud。
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from apps.backtest.models import BacktestResult
from apps.datasource.models import KlineRecord
from apps.exchange.models import ExchangeAccount
from apps.regime import deactivation_run as deactivation_run_module
from apps.regime import pool_rebuild as pool_rebuild_module
from apps.regime.deactivation import RegimeState
from apps.regime.models import (
    DeactivationExemption,
    RebuildStatus,
    RegimePoolCell,
    RegimePoolRebuild,
)
from apps.strategy_engine.base import BaseStrategy
from apps.strategy_engine.registry import StrategyRegistry
from apps.strategy_factory import effects
from apps.strategy_factory.evaluate import (
    InSampleEvidence,
    OosEvidence,
    RegimeEvidence,
    RegimePoolCellEvidence,
)
from apps.strategy_factory.models import StrategyCandidate
from apps.strategy_factory.orchestrator import (
    AdvanceDeps,
    AdvanceWiringError,
    RunContext,
)
from apps.strategy_factory.status import (
    StrategyCandidateOrigin,
    StrategyCandidateStatus,
)
from apps.strategy_factory.walk_forward import WALK_FORWARD_METRICS_KEY, plan_folds
from apps.trading.models import LiveSession, Strategy

SYMBOL = "BTC/USDT:USDT"
TF = "1h"
START = date(2026, 1, 1)
END = date(2026, 4, 11)
IN_SAMPLE_END = date(2026, 3, 22)
PROBE_NAME = "factory_probe"
PARAMS = {"exit_at_mid": False, "position_pct": 0.25}
FIXED_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

#: 就绪代的指纹计数器（模型上「就绪代的指纹唯一」的部分唯一约束要求每次造代换一个）。
_fingerprints = itertools.count(1)


class _ProbeStrategy(BaseStrategy):
    """最小探针策略：每根 bar 交替买卖，产出真实的权益曲线与夏普。"""

    name = PROBE_NAME
    description = (
        "策略类型：趋势跟踪\n"
        "核心指标：无\n"
        "适用场景：漏斗接线测试探针\n"
        "入场逻辑：每根 bar 交替买卖"
    )

    def on_bar(self, kline, history):
        if len(history) % 4 == 0:
            return self.ctx.buy(Decimal("1"))
        if len(history) % 4 == 2 and self.ctx.position > 0:
            return self.ctx.sell(Decimal("1"))
        return None


def _register_probe() -> None:
    if PROBE_NAME not in StrategyRegistry.list_registered():
        StrategyRegistry.register(_ProbeStrategy, name=PROBE_NAME)


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


def _user(username: str = "effects-owner"):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345"
    )


def _strategy(name: str = PROBE_NAME) -> Strategy:
    return Strategy.objects.create(name=name, code_path=f"strategies/{name}.py")


def _account(
    label: str = "effects-testnet", *, testnet: bool = True, active: bool = True
):
    return ExchangeAccount.objects.create(
        exchange="binance",
        label=label,
        api_key_enc=b"k",
        api_secret_enc=b"s",
        testnet=testnet,
        is_active=active,
    )


def _candidate(
    strategy: Strategy,
    status: StrategyCandidateStatus = StrategyCandidateStatus.REGISTERED,
    *,
    name: str = PROBE_NAME,
    **kwargs,
) -> StrategyCandidate:
    kwargs.setdefault("source_ref", {})
    return StrategyCandidate.objects.create(
        name=name,
        hypothesis="交替买卖的探针",
        code_path=f"~/.tradelogx/strategies/{name}.py",
        origin=StrategyCandidateOrigin.AGENT.value,
        strategy=strategy,
        status=status.value,
        **kwargs,
    )


def _seed_days(start: date, days: int) -> None:
    """铺满 `[start, start+days)` 的 1h K 线（供真实取数路径消费，零交易所调用）。"""
    base = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    rows = []
    for day in range(days):
        for hour in range(24):
            ts = base + timedelta(days=day, hours=hour)
            close = 100.0 + (day * 24 + hour) * 0.05
            rows.append(
                KlineRecord(
                    exchange="binance",
                    symbol=SYMBOL,
                    timeframe=TF,
                    timestamp=ts,
                    open=close,
                    high=close + 1.0,
                    low=close - 1.0,
                    close=close,
                    volume=1.0,
                )
            )
    KlineRecord.objects.bulk_create(rows)


def _main_row(
    strategy: Strategy,
    candidate_id,
    *,
    parameters: dict | None = None,
    review_status: str = "approved",
    symbol: str = SYMBOL,
    user=None,
    metrics: dict | None = None,
) -> BacktestResult:
    return BacktestResult.objects.create(
        strategy=strategy,
        user=user,
        symbol=symbol,
        timeframe=TF,
        start_date=START,
        end_date=IN_SAMPLE_END,
        initial_capital=Decimal("10000"),
        final_capital=Decimal("12000"),
        total_return_pct=20.0,
        sharpe_ratio=2.2,
        total_trades=48,
        parameters=dict(parameters or PARAMS),
        metrics=dict(metrics or {}),
        **({"candidate_id": str(candidate_id)} if candidate_id else {}),
        review_status=review_status,
    )


def _with_in_sample_row(candidate: StrategyCandidate, row: BacktestResult):
    candidate.evidence = {
        "layers": {
            "in_sample": {"result_id": str(row.pk), "started_at": FIXED_NOW.isoformat()}
        }
    }
    candidate.save(update_fields=["evidence"])
    return candidate


# --------------------------------------------------------------------------- #
# 跑批判据
# --------------------------------------------------------------------------- #


class TestResolveRunContext:
    pytestmark = pytest.mark.django_db

    def test_source_ref_evaluation_is_accepted(self):
        strategy = _strategy("ctx_from_ref")
        candidate = _candidate(
            strategy,
            name="ctx_from_ref",
            source_ref={
                "agent_session": "s1",
                "evaluation": {
                    "symbol": SYMBOL,
                    "timeframe": TF,
                    "evaluation_start": START.isoformat(),
                    "evaluation_end": END.isoformat(),
                    "initial_capital": "5000",
                    "parameters": {"a": 1},
                    "is_perp": True,
                },
            },
        )
        ctx = effects.resolve_run_context(candidate)
        assert ctx is not None
        assert ctx.initial_capital == Decimal("5000")
        assert ctx.is_perp is True
        assert ctx.parameters == {"a": 1}

    def test_pinned_evidence_wins_over_source_ref(self):
        strategy = _strategy("ctx_pinned")
        candidate = _candidate(
            strategy,
            name="ctx_pinned",
            source_ref={
                "evaluation": {
                    "symbol": "ETH/USDT",
                    "timeframe": TF,
                    "evaluation_start": START.isoformat(),
                    "evaluation_end": END.isoformat(),
                    "initial_capital": "1",
                }
            },
        )
        candidate.evidence = {"run_context": _ctx(symbol=SYMBOL).as_payload()}
        candidate.save(update_fields=["evidence"])
        assert effects.resolve_run_context(candidate).symbol == SYMBOL

    def test_nothing_available_returns_none(self):
        strategy = _strategy("ctx_none")
        candidate = _candidate(strategy, name="ctx_none")
        assert effects.resolve_run_context(candidate) is None

    def test_malformed_source_ref_fails_loud(self):
        strategy = _strategy("ctx_bad")
        candidate = _candidate(
            strategy,
            name="ctx_bad",
            source_ref={"evaluation": "2026-01-01..2026-04-11"},
        )
        with pytest.raises(AdvanceWiringError):
            effects.resolve_run_context(candidate)

    def test_incomplete_window_fails_loud(self):
        strategy = _strategy("ctx_half")
        candidate = _candidate(
            strategy,
            name="ctx_half",
            source_ref={"evaluation": {"symbol": SYMBOL, "timeframe": TF}},
        )
        with pytest.raises(AdvanceWiringError):
            effects.resolve_run_context(candidate)


# --------------------------------------------------------------------------- #
# 读证据
# --------------------------------------------------------------------------- #


class TestReadEvidence:
    pytestmark = pytest.mark.django_db

    def test_no_layer_record_is_an_error_not_a_pass(self):
        strategy = _strategy("read_none")
        candidate = _candidate(strategy, name="read_none")
        evidence = effects.read_in_sample(candidate)
        assert evidence is not None and evidence.error

    def test_placeholder_row_reads_as_not_produced(self):
        """占位行（还没有引擎产物）判「这次起跑没产出」，不是「还没跑完」。"""
        strategy = _strategy("read_placeholder")
        candidate = _candidate(strategy, name="read_placeholder")
        row = BacktestResult.objects.create(
            strategy=strategy,
            symbol=SYMBOL,
            timeframe=TF,
            start_date=START,
            end_date=IN_SAMPLE_END,
            initial_capital=Decimal("10000"),
            final_capital=Decimal("10000"),
            total_return_pct=0.0,
        )
        _with_in_sample_row(candidate, row)
        evidence = effects.read_in_sample(candidate)
        assert evidence is not None and evidence.error
        assert "权益曲线为空" in evidence.error

    def test_missing_row_reads_as_error(self):
        strategy = _strategy("read_missing")
        candidate = _candidate(strategy, name="read_missing")
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": "00000000-0000-0000-0000-000000000000",
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        evidence = effects.read_in_sample(candidate)
        assert evidence is not None and evidence.error
        assert "不存在" in evidence.error

    def test_concluded_row_yields_metrics(self):
        strategy = _strategy("read_done")
        candidate = _candidate(strategy, name="read_done")
        row = _main_row(strategy, candidate.pk)
        BacktestResult.objects.filter(id=row.pk).update(
            equity_curve=[{"timestamp": "t"}]
        )
        _with_in_sample_row(candidate, row)
        evidence = effects.read_in_sample(candidate)
        assert evidence == InSampleEvidence(sharpe_ratio=2.2, total_trades=48)

    def test_oos_payload_is_translated(self):
        strategy = _strategy("read_oos")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="read_oos"
        )
        row = _main_row(
            strategy,
            candidate.pk,
            metrics={
                WALK_FORWARD_METRICS_KEY: {
                    "data_sufficient": True,
                    "decay_pct": 12.5,
                    "fold_count": 5,
                }
            },
        )
        _with_in_sample_row(candidate, row)
        assert effects.read_oos(candidate) == OosEvidence(
            decay_pct=12.5, fold_count=5, data_sufficient=True
        )

    def test_oos_missing_payload_is_an_error(self):
        strategy = _strategy("read_oos_missing")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="read_oos_missing"
        )
        row = _main_row(strategy, candidate.pk)
        _with_in_sample_row(candidate, row)
        evidence = effects.read_oos(candidate)
        assert evidence is not None and evidence.error
        assert WALK_FORWARD_METRICS_KEY in evidence.error

    def test_oos_insufficient_payload_is_not_an_error(self):
        """「数据不足」是**结论**，不是接线错误：载荷在、`data_sufficient=False`。"""
        strategy = _strategy("read_oos_thin")
        candidate = _candidate(
            strategy, StrategyCandidateStatus.OOS_RUNNING, name="read_oos_thin"
        )
        row = _main_row(
            strategy,
            candidate.pk,
            metrics={
                WALK_FORWARD_METRICS_KEY: {
                    "data_sufficient": False,
                    "decay_pct": None,
                    "fold_count": 0,
                }
            },
        )
        _with_in_sample_row(candidate, row)
        evidence = effects.read_oos(candidate)
        assert evidence is not None
        assert evidence.error is None
        assert evidence.data_sufficient is False


# --------------------------------------------------------------------------- #
# 建 paper 会话
# --------------------------------------------------------------------------- #


class TestCreatePaperSession:
    pytestmark = pytest.mark.django_db

    @pytest.fixture
    def _balance(self, monkeypatch):
        """把余额读成确定值（只在需要真建会话的用例里显式请求）。"""
        monkeypatch.setattr(
            effects, "fetch_account_balance", lambda account: Decimal("2500.50")
        )

    def test_inherits_config_key_by_key_and_uses_account_balance(self, _balance):
        strategy = _strategy("paper_ok")
        account = _account()
        user = _user("paper-owner")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="paper_ok",
            paper_account=account,
            proposed_by=user,
        )
        row = _main_row(strategy, candidate.pk, parameters=dict(PARAMS))
        _with_in_sample_row(candidate, row)

        session = effects.create_paper_session(candidate)

        assert session.config == PARAMS, "参数必须与候选回测那一份逐键一致"
        assert session.config is not PARAMS, "给的是副本，不是同一个 dict 对象"
        assert session.mode == "paper"
        assert session.status == "pending", "创建与启动分开（启动走既有入口）"
        assert session.symbol == SYMBOL
        assert session.initial_capital == Decimal("2500.50")
        assert session.current_equity == Decimal("2500.50")
        assert session.exchange_account_id == account.pk
        assert session.backtest_result_id == row.pk
        assert session.user_id == user.pk

    def test_user_falls_back_to_main_row_owner(self, _balance):
        strategy = _strategy("paper_user_fallback")
        account = _account("fallback-account")
        owner = _user("main-row-owner")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="paper_user_fallback",
            paper_account=account,
        )
        row = _main_row(strategy, candidate.pk, user=owner)
        _with_in_sample_row(candidate, row)

        session = effects.create_paper_session(candidate)
        assert session.user_id == owner.pk

    def test_live_account_is_rejected(self):
        strategy = _strategy("paper_live_account")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="paper_live_account",
            paper_account=_account("live-acct", testnet=False),
            proposed_by=_user("paper-live-owner"),
        )
        _with_in_sample_row(candidate, _main_row(strategy, candidate.pk))
        with pytest.raises(AdvanceWiringError) as excinfo:
            effects.create_paper_session(candidate)
        assert "testnet" in str(excinfo.value)
        assert LiveSession.objects.count() == 0

    def test_unapproved_main_row_is_rejected(self):
        strategy = _strategy("paper_unapproved")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="paper_unapproved",
            paper_account=_account("unapproved-acct"),
            proposed_by=_user("paper-unapproved-owner"),
        )
        _with_in_sample_row(
            candidate, _main_row(strategy, candidate.pk, review_status="pending")
        )
        with pytest.raises(AdvanceWiringError) as excinfo:
            effects.create_paper_session(candidate)
        assert "review_status" in str(excinfo.value)
        assert LiveSession.objects.count() == 0

    def test_conflicting_active_session_is_rejected(self):
        """既有不变式在**建会话这一步**再挡一次（编排层的证据不是唯一防线）。"""
        strategy = _strategy("paper_conflict")
        blocker_strategy = _strategy("paper_blocker")
        account = _account("conflict-acct")
        LiveSession.objects.create(
            user=_user("conflict-blocker-owner"),
            backtest_result=_main_row(blocker_strategy, None),
            strategy=blocker_strategy,
            symbol=SYMBOL,
            mode="paper",
            status="running",
            exchange_account=account,
            initial_capital=Decimal("1000"),
        )
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="paper_conflict",
            paper_account=account,
            proposed_by=_user("paper-conflict-owner"),
        )
        _with_in_sample_row(candidate, _main_row(strategy, candidate.pk))

        with pytest.raises(AdvanceWiringError) as excinfo:
            effects.create_paper_session(candidate)
        assert "账户级持仓" in str(excinfo.value)
        assert LiveSession.objects.count() == 1, "冲突时不得多建会话"

    def test_missing_account_is_rejected(self):
        strategy = _strategy("paper_no_account")
        candidate = _candidate(
            strategy,
            StrategyCandidateStatus.PAPER_PENDING,
            name="paper_no_account",
        )
        _with_in_sample_row(candidate, _main_row(strategy, candidate.pk))
        with pytest.raises(AdvanceWiringError):
            effects.create_paper_session(candidate)

    def test_balance_comes_from_account_scoped_adapter(self):
        """余额经既有的账户适配器读（`testnet` 由账户带，所以 paper 读模拟盘）。"""
        account = _account("balance-acct")

        class _FakeAdapter:
            def __init__(self):
                self.disconnected = False

            async def get_balance(self):
                return {"USDT": Decimal("777.00")}

            async def disconnect(self):
                self.disconnected = True

        adapter = _FakeAdapter()
        with patch(
            "apps.trading.pending_reconcile.build_account_adapter",
            AsyncMock(return_value=adapter),
        ):
            assert effects.fetch_account_balance(account) == Decimal("777.00")
        assert adapter.disconnected, "读完成该断开，别把连接挂在那里"

    def test_missing_adapter_fails_loud(self):
        account = _account("no-adapter")
        with patch(
            "apps.trading.pending_reconcile.build_account_adapter",
            AsyncMock(return_value=None),
        ):
            with pytest.raises(AdvanceWiringError):
                effects.fetch_account_balance(account)


# --------------------------------------------------------------------------- #
# 跨层：行情证据（#34，spec #30 D2 / D3 / D5）
# --------------------------------------------------------------------------- #


class TestReadRegime:
    """`effects.read_regime` 的三个读口与两条「拿不到」。

    这一份钉的是票面上最贵的两件事：

    1. **冷启动不抛、给 `None`**：没有当代池化表、或当前没有生效阶段，都是「适用性问题
       现在问不出来」，不是「结论是中性」。合成一份中性证据会让闸门在证据缺失处自己编
       一个结论——fail-closed 要防的正是这件事。
    2. **当代配对**：`current_cells` 必须收到**显式取过的**那一代，不能让它自己再问一次。
       分两次读会落成「依据写的是第 N 代、引用的却是第 N+1 代那一格」，而这种错在库上
       看起来完全正常。所以这里 patch `current_cells` 直接断言它收到的 `generation`
       是不是 `current_generation()` 返回的那一个。

    格子与阶段一律**真造库**（`Strategy` 是 UUID 主键，拿整数当主键写死会在真库上静默
    错位），只把两个读函数本身换成看得见的替身。
    """

    pytestmark = pytest.mark.django_db

    @pytest.fixture
    def _strategy_row(self):
        return Strategy.objects.create(
            name="regime_probe", code_path="strategies/regime_probe.py"
        )

    def _candidate(self, strategy: Strategy) -> StrategyCandidate:
        return StrategyCandidate.objects.create(
            name="regime_probe",
            hypothesis="行情证据采集探针",
            code_path="~/.tradelogx/strategies/regime_probe.py",
            origin=StrategyCandidateOrigin.AGENT.value,
            strategy=strategy,
            status=StrategyCandidateStatus.OOS_RUNNING.value,
        )

    def _generation(self, **kwargs) -> RegimePoolRebuild:
        kwargs.setdefault("status", RebuildStatus.READY.value)
        kwargs.setdefault("finished_at", FIXED_NOW)
        # 就绪代的指纹唯一（模型上有一条部分唯一约束）——同一个类里的用例每次造代都得
        # 给一个新指纹，让计数器兜底，省得每个用例都去编一个字符串。
        kwargs.setdefault("input_fingerprint", f"f{next(_fingerprints)}")
        return RegimePoolRebuild.objects.create(
            actor_kind="task",
            actor_name="任务",
            pool_version=1,
            started_at=FIXED_NOW,
            **kwargs,
        )

    def _cell(self, generation, strategy, regime, **kwargs) -> RegimePoolCell:
        kwargs.setdefault("source", "strategy")
        return RegimePoolCell.objects.create(
            rebuild=generation,
            strategy=strategy,
            regime=regime,
            state=kwargs.pop("state", "fit"),
            reason=kwargs.pop("reason", ""),
            **kwargs,
        )

    # -- 冷启动 ------------------------------------------------------------- #

    def test_no_generation_returns_none_without_raising(self, _strategy_row):
        """当代一张表都没有（从没重算过）→ None，不抛。"""
        candidate = self._candidate(_strategy_row)
        assert effects.read_regime(candidate) is None

    def test_no_effective_regime_returns_none(self, _strategy_row):
        """当代在、当前却没有生效阶段（冷启动）→ None。

        阶段说不清时，那一格即便存在也无从引用——查「在下行趋势里行不行」得先知道现在
        是不是下行趋势。
        """
        candidate = self._candidate(_strategy_row)
        generation = self._generation()
        self._cell(generation, _strategy_row, "downtrend", state="unfit")

        with patch.object(
            deactivation_run_module, "current_regime_state", return_value=RegimeState()
        ):
            assert effects.read_regime(candidate) is None

    def test_cold_start_is_not_a_fabricated_neutral(self, _strategy_row):
        """冷启动必须停在 `None`，不能落成一份合成的中性证据（fail-closed）。

        这条与上两条的分工：上面钉「不抛」，这条钉「形状对」——合成 `regime=None,
        cell=state='neutral'` 也能跑，但那样调用方再也分不开「机制没算过」与「算了、
        结论是中性」，而闸门对这两件事的处置相反。
        """
        candidate = self._candidate(_strategy_row)
        assert effects.read_regime(candidate) is None
        assert RegimeEvidence().cell is None, "空证据里没有格子（不是中性格子）"
        assert RegimeEvidence().regime is None, "空证据里没有阶段（不是箱体震荡）"

    # -- 当代配对（本票最高价值的一条） -------------------------------------- #

    def test_current_cells_receives_the_fetched_generation(self, _strategy_row):
        """`current_cells` 必须收到**显式取过的**那一代，不许它自己再问一次。"""
        candidate = self._candidate(_strategy_row)
        generation = self._generation()
        self._cell(generation, _strategy_row, "downtrend", state="unfit")

        seen: list[object] = []
        real = pool_rebuild_module.current_cells

        def spy(gen=None):
            seen.append(gen)
            return real(gen)

        with (
            patch.object(pool_rebuild_module, "current_cells", spy),
            patch.object(
                deactivation_run_module,
                "current_regime_state",
                return_value=RegimeState(regime="downtrend", effective_at=FIXED_NOW),
            ),
        ):
            evidence = effects.read_regime(candidate)

        assert len(seen) == 1, "一次 read_regime 只该查一次格子"
        assert seen[0] == generation, (
            "current_cells 收到的是 None（它自己会再问一次当前代）→ "
            "「手里的代」与「拿到的格子」可能来自两代（依据第 N 代、格子第 N+1 代）"
        )
        assert seen[0] is not None, "配对读取必须把那一代传进去，不能让它自己再问一次"
        assert seen[0].pk == generation.pk
        assert evidence is not None
        assert evidence.regime == "downtrend"
        assert evidence.effective_at == FIXED_NOW
        assert evidence.cell is not None
        assert evidence.cell.state == "unfit"

    def test_generation_is_fetched_before_cells(self, _strategy_row):
        """两代并存时，配对的是**最近 ready 的那一代**（`finished_at` 倒序，不是 start）。"""
        candidate = self._candidate(_strategy_row)
        old = self._generation(finished_at=FIXED_NOW - timedelta(days=1))
        new = self._generation(finished_at=FIXED_NOW)
        # 两代各有这一格，但结论不同——配对错了读到的就是另一份结论。
        self._cell(old, _strategy_row, "downtrend", state="fit")
        self._cell(new, _strategy_row, "downtrend", state="unfit")

        with patch.object(
            deactivation_run_module,
            "current_regime_state",
            return_value=RegimeState(regime="downtrend", effective_at=FIXED_NOW),
        ):
            evidence = effects.read_regime(candidate)

        assert evidence is not None and evidence.cell is not None
        assert evidence.cell.state == "unfit", "配对的是最近 ready 的那一代"

    # -- 三样东西都进证据 ---------------------------------------------------- #

    def test_cell_fields_are_carried_verbatim(self, _strategy_row):
        """格子的四个取值原样进证据，采集侧不重写一句可能与之不一致的话。"""
        candidate = self._candidate(_strategy_row)
        generation = self._generation()
        self._cell(
            generation,
            _strategy_row,
            "range",
            state="unfit",
            reason="direction_conflict",
            source="archetype",
            needs_review=True,
        )
        with patch.object(
            deactivation_run_module,
            "current_regime_state",
            return_value=RegimeState(regime="range", effective_at=FIXED_NOW),
        ):
            evidence = effects.read_regime(candidate)

        assert evidence == RegimeEvidence(
            regime="range",
            effective_at=FIXED_NOW,
            cell=RegimePoolCellEvidence(
                state="unfit",
                reason="direction_conflict",
                source="archetype",
                needs_review=True,
            ),
        )

    def test_in_force_exemptions_are_carried(self, _strategy_row):
        """在期豁免原样进证据（D5）。"""
        candidate = self._candidate(_strategy_row)
        generation = self._generation()
        self._cell(generation, _strategy_row, "downtrend", state="unfit")
        exemption = DeactivationExemption.objects.create(
            strategy=_strategy_row,
            regime="downtrend",
            granted_at=FIXED_NOW - timedelta(days=1),
            expires_at=FIXED_NOW + timedelta(days=9),
            granted_by="ticket34-probe",
            note="人工复核后放行",
        )

        with patch.object(
            deactivation_run_module,
            "current_regime_state",
            return_value=RegimeState(regime="downtrend", effective_at=FIXED_NOW),
        ):
            # `now` 与造库用的 FIXED_NOW 是同一个时刻：豁免的「在期」是相对现在的量，
            # 两处各读一次时钟就会让「恰在到期那一刻」这条边界随运行时刻漂移。
            evidence = effects.read_regime(candidate, now=FIXED_NOW)

        assert evidence is not None
        assert evidence.exemptions == {(_strategy_row.pk, "downtrend"): exemption.pk}

    def test_regime_known_but_no_cell_is_not_a_fabricated_neutral(self, _strategy_row):
        """阶段说得清、当代没有这一格 → `cell is None`，而不是一份合成中性。

        多品种 / 窗口未覆盖（spec 用户故事 10）是第三种状态：既不是冷启动，也不是
        「算了、中性」。它必须能在结构上与中性结论分开，否则依据栏会把「没覆盖」读成
        「覆盖过了、不适用风险」。
        """
        candidate = self._candidate(_strategy_row)
        generation = self._generation()
        # 当代存在，但只有别的策略 / 别的阶段的格子——这一格确实没有。
        other = Strategy.objects.create(
            name="regime_other", code_path="strategies/regime_other.py"
        )
        self._cell(generation, other, "downtrend", state="unfit")
        self._cell(generation, _strategy_row, "uptrend", state="fit")

        with patch.object(
            deactivation_run_module,
            "current_regime_state",
            return_value=RegimeState(regime="downtrend", effective_at=FIXED_NOW),
        ):
            evidence = effects.read_regime(candidate)

        assert evidence is not None
        assert evidence.regime == "downtrend", "阶段说得清"
        assert evidence.cell is None, "当代没有这一格——不是合成一份中性"
        assert evidence.covered is False

    # -- 注入替身 ----------------------------------------------------------- #

    def test_injected_double_drives_the_reader_with_zero_db(self):
        """`AdvanceDeps.read_regime` 可以整个换掉：判定层因此能在零 DB 下跑。

        这一条是 D2「证据经 deps 注入」的落地证明——造不出一份当代池化表时（单测环境、
        或单独验闸门判定时），一个 lambda 就能把证据递进去。
        """
        expected = RegimeEvidence(
            regime="downtrend",
            cell=RegimePoolCellEvidence(state="unfit", reason="direction_conflict"),
        )
        deps = AdvanceDeps(
            resolve_run_context=lambda candidate: None,
            start_in_sample=lambda candidate, ctx, plan: {},
            read_in_sample=lambda candidate: None,
            start_oos=lambda candidate, ctx, plan, rid: {},
            read_oos=lambda candidate: None,
            create_paper_session=lambda candidate: None,
            read_paper=lambda candidate: None,
            read_regime=lambda candidate: expected,
            notify=lambda candidate, message: True,
            now=lambda: FIXED_NOW,
        )
        assert deps.read_regime(object()) is expected

    def test_default_deps_wires_the_real_reader(self):
        deps = effects.default_deps()
        assert deps.read_regime is effects.read_regime


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #


class TestNotify:
    pytestmark = pytest.mark.django_db

    def test_notifies_the_proposer_through_the_single_notice_port(self):
        strategy = _strategy("notify_probe")
        user = _user("notify-owner")
        candidate = _candidate(strategy, name="notify_probe", proposed_by=user)
        with patch(
            "apps.trading.alerts.notify_user", AsyncMock(return_value=True)
        ) as fake:
            assert effects.notify(candidate, "hello") is True
        fake.assert_awaited_once_with(user.pk, "hello")


# --------------------------------------------------------------------------- #
# 默认接线自检
# --------------------------------------------------------------------------- #


class TestDefaultDeps:
    pytestmark = pytest.mark.django_db

    def test_default_deps_exposes_every_effect(self):
        deps = effects.default_deps()
        assert deps.start_in_sample is effects.start_in_sample
        assert deps.read_in_sample is effects.read_in_sample
        assert deps.start_oos is effects.start_oos
        assert deps.read_oos is effects.read_oos
        assert deps.create_paper_session is effects.create_paper_session
        assert deps.resolve_run_context is effects.resolve_run_context
        assert deps.notify is effects.notify

    def test_default_deps_accepts_derived_config(self):
        from dataclasses import replace

        from apps.strategy_factory.config import CONFIG

        deps = effects.default_deps(replace(CONFIG, sharpe_min=2.5))
        assert deps.config.sharpe_min == 2.5
        assert CONFIG.sharpe_min == 1.5, "派生不改全局单例"


# --------------------------------------------------------------------------- #
# 样本内那一次真的跑起来（集成）
# --------------------------------------------------------------------------- #


class TestStartInSampleIntegration:
    """真实取数 + 真实引擎：钉住「只跑样本内区」与「审核字段落 pending」。"""

    pytestmark = pytest.mark.django_db(transaction=True)

    def test_in_sample_run_stays_inside_the_in_sample_region(self):
        _register_probe()
        _seed_days(START, 100)
        strategy = Strategy.objects.create(
            name=PROBE_NAME, code_path=f"strategies/{PROBE_NAME}.py"
        )
        candidate = _candidate(
            strategy, StrategyCandidateStatus.IN_SAMPLE_RUNNING, name=PROBE_NAME
        )
        plan = plan_folds(START, END)

        started = effects.start_in_sample(candidate, _ctx(), plan)

        assert started.get("result_id")
        row = BacktestResult.objects.get(id=started["result_id"])
        assert row.candidate_id == candidate.pk
        assert row.end_date <= plan.in_sample_end, (
            "样本内行越过了样本内区右界 → 衰减分母被拉向样本外（#29 的交接）"
        )
        assert row.start_date >= START
        # T0 的另一半：回测保存路径不再写 approved，「自动通过」归漏斗判定。
        assert row.review_status == "pending"
        assert row.reviewed_at is None
        assert row.timeframe == TF
        assert row.symbol == SYMBOL
        assert row.parameters == PARAMS

        # 跑完之后立刻可读，且是真结论（不是占位行）。层记录由编排层写——这里照它的
        # 形状补上，验证「层记录 + 落库行」这一对在真实数据上读得通。
        candidate.evidence = {
            "layers": {
                "in_sample": {
                    "result_id": started["result_id"],
                    "started_at": FIXED_NOW.isoformat(),
                }
            }
        }
        candidate.save(update_fields=["evidence"])
        evidence = effects.read_in_sample(candidate)
        assert evidence is not None
        assert evidence.error is None
        assert evidence.total_trades is not None
