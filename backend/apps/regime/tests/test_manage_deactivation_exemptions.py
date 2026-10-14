"""人工恢复豁免管理命令（第①段单元 7 收尾，Q7 的写方）。

这条命令写的是**本机制唯一一个「让人绕过机制」的开关**：一条在期豁免会让（策略 × 阶段）
在 10 个自然日里跳过自动停用（CONTEXT.md 第 120 条）。它做的是可逆的两个动作，所以这里
钉的不是「发得对不对」这种跑一次就知道的事，而是**它在什么情况下会安静地写错**。

五条性质，每一条坏了都不报警、只出错：

1. **判据与推导逐字相同**。命令说「在期」与 `deactivation_run.in_force_exemptions` 说
   「在期」必须是同一件事——两处各判一次判据迟早会漂，而漂的表现是「命令说在期、推导
   说不在期」，这是最难查的一类不一致。所以这里除了直接测 `state_of` 的四档互斥，还
   拿命令的判定去对推导的判定（`test_the_command_and_the_derivation_agree`）。
2. **生效期在写入那一刻换算**。`expires_at = granted_at + exemption_days`，存下来的是
   绝对时刻。所以**改配置不追溯**：已经发出的那条豁免不会因为 `exemption_days` 变了而
   延长或缩短。这条与 `RegimeJudgement` 存 `attribute_date` 是同一条纪律。
3. **不猜阶段**。`--grant` 不给 `--regime` 时取当前生效阶段；**冷启动与保命档期间都
   拒绝执行**，而不是回落成某个默认档——豁免有 10 天实际效力，拿一个猜出来的阶段落库是
   最难被发现的那种「替人做决定」。保命档那一档尤其隐蔽：`high_vol` 期间推导的 `targets`
   恒空，落下来的是一条 10 天里什么都没挡住的记录，而屏幕上写的是「已发出豁免」。
4. **收回先到者为准**。`--revoke` 只关还没关的：已经关掉的（阶段离开 / 早先撤过）保持
   原样，连 `closed_reason` 都不覆盖。两个方向不对称——多写一笔会抹掉「它是怎么失效的」
   这条信息，少写一笔只是等下一轮。
5. **绝不碰决策表**。`DeactivationDecision` 的唯一写方是 `deactivation_run.run_deactivation`
   （它冻的是「那一刻的依据」）。手工改决策等于伪造一份不存在过的依据。

夹具一律用真 `Strategy` 行（`Strategy.id` 是 UUID 主键，拿整数当主键写死会在真库上静默
错位）。本命令**不经过策略注册表**（它只按 `Strategy.name` / `id` 找行），所以注册表替身
与 `ensure_strategies_discovered` 的补丁都不需要——这正是它比幽灵清理命令好测的地方。
"""

from __future__ import annotations

import getpass
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone as django_timezone

from apps.regime import config, deactivation_run, judgement
from apps.regime.management.commands import manage_deactivation_exemptions as manage
from apps.regime.models import (
    DeactivationDecision,
    DeactivationExemption,
    RegimeJudgement,
)
from apps.regime.quant import BaseRegime
from apps.trading.models import Strategy

#: 夹具的时间锚点，**取真实时钟**而不是写死一个过去时刻。
#:
#: 命令内部的 `handle` 读的是 `timezone.now()`，注入不进去。写死一个过去的时刻（比如
#: 2026-06-01）会让每一条夹具行在真机上都变成「已过期」——四档状态全塌成 `expired`，
#: 用例照样绿，而它声称钉住的那件事已经没有被测到了。要断言「在期」就必须活在当下。
NOW = django_timezone.now()

IN_FORCE = manage.STATE_IN_FORCE
PENDING = manage.STATE_PENDING
EXPIRED = manage.STATE_EXPIRED
CLOSED = manage.STATE_CLOSED


class _Fixture(TestCase):
    def setUp(self):
        self.alpha = Strategy.objects.create(name="AlphaStem", code_path="/t/a.py")
        self.beta = Strategy.objects.create(name="BetaStem", code_path="/t/b.py")
        # 同名两行：命令必须拒绝猜，并把人指向 --strategy-id
        self.twin_a = Strategy.objects.create(name="TwinStem", code_path="/t/t1.py")
        self.twin_b = Strategy.objects.create(name="TwinStem", code_path="/t/t2.py")

    # --- 帮手 -------------------------------------------------------------- #

    def exemption(
        self,
        strategy=None,
        *,
        regime=BaseRegime.UPTREND.value,
        granted_at: datetime | None = None,
        days: int = 10,
        closed_at: datetime | None = None,
        closed_reason: str = "",
        granted_by: str = "测试",
        note: str = "",
    ) -> DeactivationExemption:
        granted_at = granted_at or NOW
        return DeactivationExemption.objects.create(
            strategy=strategy or self.alpha,
            regime=regime,
            granted_at=granted_at,
            expires_at=granted_at + timedelta(days=days),
            granted_by=granted_by,
            note=note,
            closed_at=closed_at,
            closed_reason=closed_reason,
        )

    def judge(self, regime=BaseRegime.UPTREND.value, *, days_ago=1) -> RegimeJudgement:
        """一条**生效中**的判定：`effective_at` 在真实时钟之前，命令才认它。"""
        effective_at = django_timezone.now() - timedelta(days=days_ago)
        return RegimeJudgement.objects.create(
            symbol=judgement.SYMBOL,
            attribute_date=(effective_at - timedelta(days=2)).date(),
            effective_at=effective_at,
            base_regime=regime,
            escalation="",
            effective_regime=regime,
            evidence={},
            config_snapshot={},
        )

    def manage(self, *args, expect_error: bool = False) -> tuple[str, str]:
        """跑命令，返回 `(stdout, stderr)`。

        **不叫 `run`**：`unittest.TestCase.run` 是框架自己的入口，覆盖掉它会让每条用例
        在框架调用 `self.run(result)` 时炸在参数个数上。
        """
        out, err = StringIO(), StringIO()
        kwargs = {"stdout": out, "stderr": err}
        if expect_error:
            with self.assertRaises(CommandError):
                call_command("manage_deactivation_exemptions", *args, **kwargs)
        else:
            call_command("manage_deactivation_exemptions", *args, **kwargs)
        return out.getvalue(), err.getvalue()

    def manage_error(self, *args) -> str:
        """跑命令并**返回**那句 `CommandError` 的原文（要断言措辞时用它）。

        与 `manage(..., expect_error=True)` 分开：那个只关心「有没有拦住」，把异常吞在
        断言里；这里关心的是「拦住之后说了什么」——给人看的命令行，措辞就是功能。
        """
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "manage_deactivation_exemptions",
                *args,
                stdout=StringIO(),
                stderr=StringIO(),
            )
        return str(ctx.exception)

    #: 覆盖提示的开头（发送侧的 `_grant` 与列出的清单都可能带 `#id`，所以断言必须
    #: 落在**那句话**上，不能只断言 id 出现过）。
    SUPERSEDE_WARNING = "注意：同一格原有在期豁免"


# --------------------------------------------------------------------------- #
# 性质 1：四种状态互斥，且与推导同一口径
# --------------------------------------------------------------------------- #


class TestStateOf(_Fixture):
    def test_the_four_states_are_mutually_exclusive(self):
        rows = {
            IN_FORCE: self.exemption(),
            PENDING: self.exemption(granted_at=NOW + timedelta(days=1)),
            EXPIRED: self.exemption(granted_at=NOW - timedelta(days=20)),
            CLOSED: self.exemption(closed_at=NOW - timedelta(days=1)),
        }
        for expected, row in rows.items():
            with self.subTest(state=expected):
                self.assertEqual(manage.state_of(row, now=NOW), expected)

    def test_closed_beats_expired_and_pending(self):
        """已关闭的判定排在最前：一条「关掉之后又到期了」的记录不该显示成「已过期」。

        顺序错了不会漏掉任何一行，只会让那一行的原因变成另一档——而「阶段离开 / 人工
        收回」正是事后复盘要看的东西。
        """
        row = self.exemption(
            granted_at=NOW - timedelta(days=20),
            closed_at=NOW - timedelta(days=15),
            closed_reason=deactivation_run.CLOSE_REASON_REGIME_LEFT,
        )
        self.assertEqual(manage.state_of(row, now=NOW), CLOSED)

    def test_the_boundary_instants_are_not_in_force(self):
        """两个端点都不算在期，且与 `in_force_exemptions` 的 `__gt`/`__lte` 逐字同向。

        `expires_at == now` 若被判成在期，豁免会多活一刻；`granted_at == now` 若被判成
        在期，一条还没生效的豁免会立刻开始挡停用。两端各偏一点，恰好是「命令说在期、
        推导说不在期」的第二个入口。
        """
        self.assertEqual(manage.state_of(self.exemption(days=0), now=NOW), EXPIRED)
        self.assertEqual(
            manage.state_of(
                self.exemption(granted_at=NOW + timedelta(seconds=1)), now=NOW
            ),
            PENDING,
        )

    def test_the_command_and_the_derivation_agree(self):
        """命令的「在期」与推导的「在期」是同一件事——**逐条对**。

        这条是本文件里最重要的一条：两处各判一次判据迟早会漂，而漂的表现是「命令说在期、
        推导说不在期」。四档各造一条，命令说在期的集合必须**恰好**等于推导认的集合。
        """
        rows = [
            self.exemption(regime=BaseRegime.UPTREND.value),
            self.exemption(
                regime=BaseRegime.RANGE.value, granted_at=NOW + timedelta(days=1)
            ),
            self.exemption(
                regime=BaseRegime.DOWNTREND.value, granted_at=NOW - timedelta(days=20)
            ),
            self.exemption(
                regime=BaseRegime.HIGH_VOL.value, closed_at=NOW - timedelta(days=1)
            ),
        ]
        by_command = {
            (row.strategy_id, row.regime)
            for row in rows
            if manage.state_of(row, now=NOW) == IN_FORCE
        }
        self.assertEqual(set(deactivation_run.in_force_exemptions(now=NOW)), by_command)
        self.assertEqual(by_command, {(self.alpha.id, BaseRegime.UPTREND.value)})


class TestRegimeDisplay(_Fixture):
    def test_a_known_regime_renders_its_chinese_name(self):
        self.assertEqual(
            manage.regime_display(BaseRegime.HIGH_VOL.value),
            BaseRegime.HIGH_VOL.display,
        )

    def test_a_dirty_row_is_shown_as_itself(self):
        """认不出的取值原样显示，**不回落成某一档**。

        模型上的 `choices` 只在表单校验里管用，手工写进去的行绕得过它。一条脏行不该让
        清单命令炸掉，但也绝不能被显示成「下行趋势」——那会让人以为豁免覆盖的是另一档。
        """
        self.assertEqual(manage.regime_display("moon"), "moon（认不出的阶段）")


# --------------------------------------------------------------------------- #
# 性质 2：生效期在写入那一刻换算
# --------------------------------------------------------------------------- #


class TestGrantExpiry(_Fixture):
    def test_expiry_comes_from_the_config_at_write_time(self):
        self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", BaseRegime.RANGE.value
        )
        row = DeactivationExemption.objects.get()
        self.assertEqual(row.regime, BaseRegime.RANGE.value)
        self.assertEqual(
            row.expires_at - row.granted_at,
            timedelta(days=config.DEACTIVATION.exemption_days),
            "生效期必须是配置面那个数，而不是某个抄在命令里的字面量",
        )

    def test_a_config_change_is_not_retroactive(self):
        """改了 `exemption_days` 之后：**新发的**用新值，**已经发出的**一动不动。

        这是「存绝对时刻」而不是「查询时按配置算」的全部意义。反过来的话，某天有人把
        生效期从 10 天调成 30 天，所有在期豁免会**悄悄延长三周**——而那正是唯一一个让
        人跳过自动停用的开关，错了没人会发现。
        """
        self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", BaseRegime.RANGE.value
        )
        first = DeactivationExemption.objects.get()

        with patch.object(
            config, "DEACTIVATION", replace(config.DEACTIVATION, exemption_days=3)
        ):
            self.manage(
                "--grant", "--strategy", "BetaStem", "--regime", BaseRegime.RANGE.value
            )

        first.refresh_from_db()
        second = DeactivationExemption.objects.get(strategy=self.beta)
        self.assertEqual(first.expires_at - first.granted_at, timedelta(days=10))
        self.assertEqual(second.expires_at - second.granted_at, timedelta(days=3))

    def test_the_grant_is_immediately_in_force_for_the_derivation(self):
        """发完就能被推导读到。命令写、推导读，中间没有第二份状态。"""
        self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", BaseRegime.RANGE.value
        )
        self.assertIn(
            (self.alpha.id, BaseRegime.RANGE.value),
            deactivation_run.in_force_exemptions(),
        )


# --------------------------------------------------------------------------- #
# 性质 3：不猜阶段
# --------------------------------------------------------------------------- #


class TestGrantRegime(_Fixture):
    def test_it_takes_the_current_effective_regime_when_omitted(self):
        self.judge(BaseRegime.DOWNTREND.value)
        out, _ = self.manage("--grant", "--strategy", "AlphaStem")
        row = DeactivationExemption.objects.get()
        self.assertEqual(row.regime, BaseRegime.DOWNTREND.value)
        self.assertIn(BaseRegime.DOWNTREND.display, out)

    def test_a_pending_judgement_is_not_taken(self):
        """待生效的那条不算「当前生效阶段」——它明天才咬人。

        `current_judgement` 的语义就是 `effective_at <= now`，命令复用它而不是自己写
        一遍「最近一条」。这里钉的是命令确实走的是它：给一条只在未来生效的判定、又不给
        `--regime`，命令必须当成冷启动拒绝，而不是把那条约上。
        """
        self.judge(BaseRegime.DOWNTREND.value, days_ago=-1)  # 生效时刻在未来
        self.manage("--grant", "--strategy", "AlphaStem", expect_error=True)
        self.assertFalse(DeactivationExemption.objects.exists())

    def test_cold_start_refuses_instead_of_guessing(self):
        """一条生效中的判定都没有 → `CommandError`，且**一行都不写**。

        回落成某个默认档会让一条猜出来的阶段获得 10 天的实际效力。冷启动时人本来就该
        自己说是哪个阶段——这也是这条命令唯一一次要求人多打几个字。
        """
        self.assertFalse(RegimeJudgement.objects.exists())
        message = self.manage_error("--grant", "--strategy", "AlphaStem")
        self.assertIn("冷启动", message)
        self.assertFalse(DeactivationExemption.objects.exists())

    def test_a_blanket_phase_refuses_to_be_inferred(self):
        """**保命档期间不猜**（第③段 Q4），与上一条同一条取向。

        高波动是叠加层、不是可以登记豁免的基础阶段：保命档期间每个策略的结论都是
        `blanket`、`targets` 恒空，所以一条 `high_vol` 的豁免既不计入推导的 `exempt`、
        也不挡任何东西——它是一张 10 天的空转记录。而人看到「已发出豁免」会以为自己
        办成了一次恢复（这正是这条命令过去的形态：`_current_regime` 直接取
        `effective_regime`，保命档期间它就等于 `high_vol`）。
        """
        self.judge(BaseRegime.HIGH_VOL.value)
        message = self.manage_error("--grant", "--strategy", "AlphaStem")
        self.assertIn("保命档", message)
        self.assertFalse(DeactivationExemption.objects.exists())

    def test_an_explicitly_named_blanket_is_allowed_with_a_warning(self):
        """**显式点名** `--regime high_vol` 仍然照落——显式就是知情。

        机制不该替人否决一个明确的选择；但它必须把「这条豁免在保命档期间不起作用」说出来。
        """
        out, _ = self.manage(
            "--grant",
            "--strategy",
            "AlphaStem",
            "--regime",
            BaseRegime.HIGH_VOL.value,
        )
        self.assertEqual(
            DeactivationExemption.objects.get().regime, BaseRegime.HIGH_VOL.value
        )
        self.assertIn("不是可以登记豁免的基础阶段", out)


# --------------------------------------------------------------------------- #
# 性质 3 续：策略解析
# --------------------------------------------------------------------------- #


class TestStrategyResolution(_Fixture):
    def test_by_name(self):
        self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", BaseRegime.RANGE.value
        )
        self.assertEqual(DeactivationExemption.objects.get().strategy_id, self.alpha.id)

    def test_by_id(self):
        self.manage(
            "--grant",
            "--strategy-id",
            str(self.beta.id),
            "--regime",
            BaseRegime.RANGE.value,
        )
        self.assertEqual(DeactivationExemption.objects.get().strategy_id, self.beta.id)

    def test_an_ambiguous_name_is_refused_with_the_ids(self):
        """同名两行 → 报错并把人指向 `--strategy-id`，而不是随便挑一行。

        随便挑一行的后果是给**另一条**策略发了豁免，而两条的名字一模一样——从输出上
        完全看不出来，只能看 id。
        """
        message = self.manage_error(
            "--grant", "--strategy", "TwinStem", "--regime", BaseRegime.RANGE.value
        )
        self.assertFalse(DeactivationExemption.objects.exists())
        for row in (self.twin_a, self.twin_b):
            self.assertIn(str(row.id), message)

    def test_an_unknown_name_is_refused(self):
        self.manage("--grant", "--strategy", "NoSuchStem", expect_error=True)
        self.assertFalse(DeactivationExemption.objects.exists())

    def test_an_unknown_id_is_refused(self):
        self.manage("--grant", "--strategy-id", str(uuid.uuid4()), expect_error=True)
        self.assertFalse(DeactivationExemption.objects.exists())

    def test_a_malformed_id_says_so_in_its_own_words(self):
        """格式错误要说「策略 id 必须是 UUID」，不能说成「豁免 id 必须是整数」。

        两处 id 类型不同（策略是 UUID、豁免是自增整数），共用一个解析器会把「格式不对」
        报成另一种东西——而这是给人看的命令行，提示必须能照着改。
        """
        message = self.manage_error(
            "--grant", "--strategy-id", "not-a-uuid", "--regime", BaseRegime.RANGE.value
        )
        self.assertIn("UUID", message)

    def test_it_needs_exactly_one_of_name_and_id(self):
        self.manage("--grant", "--regime", BaseRegime.RANGE.value, expect_error=True)
        self.manage(
            "--grant",
            "--strategy",
            "AlphaStem",
            "--strategy-id",
            str(self.alpha.id),
            "--regime",
            BaseRegime.RANGE.value,
            expect_error=True,
        )
        self.assertFalse(DeactivationExemption.objects.exists())


# --------------------------------------------------------------------------- #
# 性质 4（发出侧）：覆盖在期豁免必须说出来
# --------------------------------------------------------------------------- #


class TestGrantSupersedes(_Fixture):
    def test_it_warns_when_an_in_force_exemption_is_superseded(self):
        """同一格再发一条是「覆盖」语义，而推导只认最近发出的那条。

        两条都在表里（表上刻意没有唯一约束），但**生效期不会被叠加**。不说出来的话，
        人会以为豁免延长到了 20 天。
        """
        old = self.exemption(days=10)
        out, _ = self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", old.regime
        )
        new = DeactivationExemption.objects.exclude(id=old.id).get()
        self.assertIn(f"{self.SUPERSEDE_WARNING} #{old.id}", out)
        # 覆盖之后生效的确实是新的那条：推导的回答必须跟着命令的清单走
        self.assertEqual(
            deactivation_run.in_force_exemptions()[(self.alpha.id, old.regime)], new.id
        )

    def test_it_does_not_warn_about_rows_that_are_not_in_force(self):
        """已过期 / 已关闭的都不算「被覆盖」，不该出现在提示里。

        误报的代价是发出人对这句提示脱敏，而它正是唯一会通知「生效期没有叠加」的地方。
        """
        expired = self.exemption(
            regime=BaseRegime.RANGE.value, granted_at=NOW - timedelta(days=20)
        )
        out, _ = self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", expired.regime
        )
        self.assertNotIn(self.SUPERSEDE_WARNING, out)

        closed = self.exemption(
            regime=BaseRegime.DOWNTREND.value, closed_at=NOW - timedelta(days=1)
        )
        out, _ = self.manage(
            "--grant", "--strategy", "BetaStem", "--regime", closed.regime
        )
        self.assertNotIn(self.SUPERSEDE_WARNING, out)


class TestGrantActor(_Fixture):
    def test_the_actor_defaults_to_the_system_user(self):
        self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", BaseRegime.RANGE.value
        )
        self.assertEqual(
            DeactivationExemption.objects.get().granted_by, getpass.getuser()
        )

    def test_an_explicit_actor_and_note_are_kept(self):
        """`granted_by` 与 `note` 是这条豁免唯一的人话痕迹：10 天之后回头看那条决策，
        只有这里能回答「当初是谁、为什么放行的」。"""
        self.manage(
            "--grant",
            "--strategy",
            "AlphaStem",
            "--regime",
            BaseRegime.RANGE.value,
            "--actor",
            "张三",
            "--note",
            "回测中该阶段表现尚可，先观察",
        )
        row = DeactivationExemption.objects.get()
        self.assertEqual(row.granted_by, "张三")
        self.assertEqual(row.note, "回测中该阶段表现尚可，先观察")


# --------------------------------------------------------------------------- #
# 性质 4（收回侧）：先到者为准
# --------------------------------------------------------------------------- #


class TestRevoke(_Fixture):
    def test_it_closes_the_row_with_the_manual_reason(self):
        row = self.exemption()
        self.manage("--revoke", str(row.id))
        row.refresh_from_db()
        self.assertIsNotNone(row.closed_at)
        self.assertEqual(row.closed_reason, deactivation_run.CLOSE_REASON_MANUAL)
        self.assertNotIn(
            (self.alpha.id, row.regime), deactivation_run.in_force_exemptions()
        )

    def test_it_does_not_overwrite_another_writer_s_reason(self):
        """已经关掉的（阶段离开 / 早先撤过）保持原样，连 `closed_reason` 都不覆盖。

        两个方向不对称：多写一笔会抹掉「它是怎么失效的」这条信息（而「人撤的」与
        「阶段离开的」正是事后复盘要看的那一格），少写一笔只是等下一轮。
        """
        left_at = NOW - timedelta(days=3)
        row = self.exemption(
            granted_at=NOW - timedelta(days=5),
            closed_at=left_at,
            closed_reason=deactivation_run.CLOSE_REASON_REGIME_LEFT,
        )
        out, _ = self.manage("--revoke", str(row.id))
        row.refresh_from_db()
        self.assertEqual(row.closed_at, left_at)
        self.assertEqual(row.closed_reason, deactivation_run.CLOSE_REASON_REGIME_LEFT)
        self.assertIn("本来就已关闭", out)

    def test_several_ids_in_one_call(self):
        first = self.exemption(regime=BaseRegime.RANGE.value)
        second = self.exemption(regime=BaseRegime.HIGH_VOL.value)
        self.manage("--revoke", str(first.id), "--revoke", str(second.id))
        self.assertFalse(
            DeactivationExemption.objects.filter(closed_at__isnull=True).exists()
        )

    def test_an_unknown_id_is_refused_and_nothing_is_written(self):
        """整批拒绝，而不是「能关的关掉、剩下的报个错」。

        报错时人本来就已经在「我刚才写的是哪条」这件事上不确定了；这时候把其中几条
        悄悄关掉，收场是半张表被改过，而屏幕上只有一句错误。
        """
        row = self.exemption()
        self.manage("--revoke", str(row.id), "--revoke", "999999", expect_error=True)
        row.refresh_from_db()
        self.assertIsNone(row.closed_at)

    def test_a_non_integer_id_says_what_is_expected(self):
        message = self.manage_error("--revoke", "abc")
        self.assertIn("必须是整数", message)

    def test_grant_and_revoke_are_mutually_exclusive(self):
        """两个动作的方向相反，同时给出来没有任何合理解释，所以由 argparse 直接挡掉。"""
        self.manage(
            "--grant", "--revoke", "1", "--strategy", "AlphaStem", expect_error=True
        )
        self.assertFalse(DeactivationExemption.objects.exists())


# --------------------------------------------------------------------------- #
# 性质 5：绝不碰决策表
# --------------------------------------------------------------------------- #


class TestItNeverTouchesDecisions(_Fixture):
    def _decision(self) -> DeactivationDecision:
        return DeactivationDecision.objects.create(
            strategy=self.alpha,
            regime=BaseRegime.RANGE.value,
            evidence={"reason": "unfit"},
            pool_rebuild_id=1,
            first_decided_at=NOW,
            last_confirmed_at=NOW,
        )

    def test_no_decision_is_created(self):
        self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", BaseRegime.RANGE.value
        )
        self.assertFalse(DeactivationDecision.objects.exists())

    def test_an_existing_decision_is_left_alone(self):
        """手工改决策等于伪造一份不存在过的依据（它冻的是「那一刻的依据」）。

        收回豁免之后正确的收场是**等下一轮推导**——它会照常把那条策略判成 `target`，
        而 `last_confirmed_at` 由推导自己往前推，不由这条命令推。
        """
        decision = self._decision()
        row = self.exemption()
        self.manage(
            "--grant", "--strategy", "BetaStem", "--regime", BaseRegime.RANGE.value
        )
        self.manage("--revoke", str(row.id))
        decision.refresh_from_db()
        self.assertEqual(decision.last_confirmed_at, NOW)
        self.assertEqual(decision.evidence, {"reason": "unfit"})


# --------------------------------------------------------------------------- #
# 列出
# --------------------------------------------------------------------------- #


class TestListing(_Fixture):
    def test_an_empty_table_says_so(self):
        out, _ = self.manage()
        self.assertIn("没有任何豁免记录", out)

    def test_it_counts_every_state(self):
        """四档各一条 → 汇总里四档都是 1。清单是发出人唯一能看见在期豁免的地方。"""
        self.exemption(regime=BaseRegime.UPTREND.value)
        self.exemption(
            regime=BaseRegime.RANGE.value, granted_at=NOW + timedelta(days=1)
        )
        self.exemption(
            regime=BaseRegime.DOWNTREND.value, granted_at=NOW - timedelta(days=20)
        )
        self.exemption(
            regime=BaseRegime.HIGH_VOL.value, closed_at=NOW - timedelta(days=1)
        )
        out, _ = self.manage()
        self.assertIn("豁免共 4 条", out)
        for state, label in manage.STATE_DISPLAY.items():
            with self.subTest(state=state):
                self.assertIn(f"{label} 1", out)

    def test_a_closed_row_shows_why_in_chinese(self):
        """「人撤的」与「阶段离开的」必须一眼分得开，而不是显示成原因码。"""
        self.exemption(
            regime=BaseRegime.UPTREND.value,
            closed_at=NOW,
            closed_reason=deactivation_run.CLOSE_REASON_MANUAL,
        )
        self.exemption(
            regime=BaseRegime.RANGE.value,
            closed_at=NOW,
            closed_reason=deactivation_run.CLOSE_REASON_REGIME_LEFT,
        )
        out, _ = self.manage()
        for reason in (
            deactivation_run.CLOSE_REASON_MANUAL,
            deactivation_run.CLOSE_REASON_REGIME_LEFT,
        ):
            with self.subTest(reason=reason):
                self.assertIn(deactivation_run.CLOSE_REASON_DISPLAY[reason], out)
                self.assertNotIn(reason, out, "展示层不该漏出原因码本身")

    def test_a_dirty_regime_row_does_not_break_the_listing(self):
        self.exemption(regime="moon")
        out, _ = self.manage()
        self.assertIn("moon（认不出的阶段）", out)

    def test_the_listing_runs_after_a_grant(self):
        """发完立刻把清单打出来：发出人要在同一屏里看见「现在在期的是什么」，
        否则他下一步要自己去猜一条 id。"""
        out, _ = self.manage(
            "--grant", "--strategy", "AlphaStem", "--regime", BaseRegime.RANGE.value
        )
        self.assertIn("已发出豁免", out)
        self.assertIn("豁免共 1 条", out)

    def test_the_listing_says_the_exemptions_are_ineffective_during_a_blanket(self):
        """清单里那句「此刻算不算数」：保命档在拦时，**在期豁免一条都不生效**。

        「在期」两个字本身不告诉人这件事——而人看到的形态是「我明明放行过它，它还被停着」，
        与「机制没听见我」完全分不开（CONTEXT.md 第 176 条）。这句话与 `query_halt` 的
        输出共用一处（`deactivation_run.exemption_standing`），所以它是同一句。
        """
        self.exemption()
        self.judge(BaseRegime.HIGH_VOL.value)
        out, _ = self.manage()
        self.assertIn("当前一条都不生效", out)

    def test_the_listing_says_they_are_in_force_when_nothing_blocks_them(self):
        """反方向。少了这一条，「永远说『不生效』」这种写坏了的实现也能通过上面那条。"""
        self.exemption()
        self.judge(BaseRegime.RANGE.value)
        out, _ = self.manage()
        self.assertIn("豁免照常生效", out)
