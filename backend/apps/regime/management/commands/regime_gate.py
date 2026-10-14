"""行情阶段 gate 开关的管理命令（第③段单元 ③b 的运维兜底入口）。

`/regime gate` 是主入口（CONTEXT.md 第152 条：人工维护一律走 slash 命令，不引入 Django
admin），但那条路要经过 Telegram。**打开这个开关是「机制开始按行情阶段停策略」的闸门**，
它不能只有一个依赖外网的入口——聊天通路不回消息的时候，人仍然要能看一眼确认页、并且
亲手把它打开或关掉。

所以这里只做一件事：用与 `/regime gate` **完全相同**的两个函数
（`gate_switch.page` 与 `gate_switch.flip_regime_gate`）做同一件事。本模块自己不查库、
不渲染、不做判断，免得「命令行的说法」与「聊天里的说法」分家——那正是第②f 段 Q8 要求
同源的道理，本段一字不差地沿用。

    python manage.py regime_gate                # 只看确认页，什么都不改
    python manage.py regime_gate --on           # 打开（先回显确认页，再落流水 + 对账）
    python manage.py regime_gate --off          # 关掉
    python manage.py regime_gate --on --actor 张三

不带任何动作参数时是**只读**的（与 `event_breaker`、`manage_deactivation_exemptions` 一样，
跑完动作总是把当前状态打出来）。

命名：本命令**不带 `manage_` 前缀**，与 `event_breaker` 同一取法。带前缀的那条
（`manage_deactivation_exemptions`）宾语是「在期豁免」这张表，名字需要说清它管的是哪张表；
这条命令的宾语就是机制本身，命令词与 `MechanismKind.REGIME_GATE` 的那个值同名，敲
`/regime gate` 的人与敲 CLI 的人看到的是同一个词。

**与 `event_breaker` 有一处刻意的不同**：关掉之后多一句警告。`Confirmation` 里没有
`open_now` 那样的「这一步正压在什么东西上」的字段，因为 gate 的敞口不在「窗口」上，而在
**声明表没被对干净**上——阶段说不清时 `gate.derive` 的 blocked 分支排在 Shadow 分支之前，
活行一条都不会被解除。那时流水已经写下去了（档位真的翻了），所以这句警告必须按**翻完之后
的事实**说，而不是把关闭页里那句「它们仍然在拦人」再抄一遍：开关一关，它们就不拦人了，
留在表里的是一批**不再生效、但也没被清掉**的行。

这句话本身写在渲染层（`gate_switch.reconcile_warning`）而不是这里：`/regime gate` 说的是
同一件事，两处各写一句就是给「此刻表干不干净」造两个说法。本命令只负责把它打出来。
"""

from __future__ import annotations

import getpass

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.regime import events, gate_switch
from apps.regime.models import ActorKind, MechanismMode


class Command(BaseCommand):
    help = "行情阶段 gate 开关：上线确认页与人工切换（第③段 ③b）"

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group()
        action.add_argument(
            "--on", action="store_true", help="打开行情阶段 gate（切到执行态）"
        )
        action.add_argument(
            "--off", action="store_true", help="关掉行情阶段 gate（回到 Shadow）"
        )
        parser.add_argument(
            "--actor",
            default="",
            metavar="NAME",
            help="触发方（默认取系统用户名）",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        # 与 `manage_deactivation_exemptions` 同一取法：CLI 通路的触发方是**这台机器上
        # 的人**，不是任务名——留痕四件事里的「谁」必须指得到一个真人。
        actor = options["actor"] or getpass.getuser()

        # 先回显确认页再动手：与 `/regime gate on` 同序。反过来打的话，页面上那句「这一步
        # 本身不改变任何东西」会印在一次已经发生的改动之后，读的人分不出前后。
        # 方向按动作取：关闭方向问的是另一件事（「关掉的那一刻放掉了什么」）。
        closing = bool(options["off"])
        briefing = gate_switch.page(closing=closing, now=now)
        self.stdout.write(briefing.body)
        self.stdout.write("")

        if not options["on"] and not options["off"]:
            self.stdout.write(
                "（没有动作参数：以上是当前状态。--on 打开 / --off 关掉）"
            )
            return

        # `reason` 与上面那一页出自**同一次求值**（`Briefing.summary`），不在这里另拼一句：
        # 事后读流水的人只有这一句话，它与用户当时看到的那一页必须是同一份。
        to_mode = MechanismMode.EXECUTING if options["on"] else MechanismMode.SHADOW
        reason = briefing.summary

        flip = gate_switch.flip_regime_gate(
            to_mode,
            actor_kind=ActorKind.CLI,
            actor_name=actor,
            reason=reason,
            now=now,
        )
        if flip.row is None:
            # 「本来就是这一档」不是失败：`flip_regime_gate` 仍然对了一次账，那正是再敲
            # 一次同一条命令的用处（阶段说不清时关掉 gate 不会解除活行，后来阶段说清楚了
            # 就得靠再敲一次补上）。所以这里要说清「没写流水，但对过账了」。
            self.stdout.write(
                self.style.WARNING(
                    f"行情阶段 gate 本来就是{to_mode.display}，没有写第二条流水"
                    "（仍然对了一次账）。"
                )
            )
            self._warn_if_not_reconciled(briefing, flip)
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"行情阶段 gate 已切换：{MechanismMode(flip.row.from_mode).display} → "
                f"{MechanismMode(flip.row.to_mode).display}"
                f"（{events.format_moment(flip.row.at)}，触发方 {flip.row.actor_name}）"
            )
        )
        self._warn_if_not_reconciled(briefing, flip)

    def _warn_if_not_reconciled(self, briefing, flip) -> None:
        """档位翻了，但表没对上——这个组合必须说出来。

        「已切换」是**流水行**这件事的成功，它读起来像「关掉之后就没有拦人的东西了」，
        而阶段说不清时 `gate_run.sync` 走的是 blocked 那一支：本轮连对账都不发起，活行
        一条都没被解除。这句话本身在 `gate_switch.reconcile_warning`——`/regime gate`
        说的是同一件事，两处各写一句就是给「此刻表干不干净」造两个说法。
        """
        warning = gate_switch.reconcile_warning(briefing.data, flip.sync)
        if warning is not None:
            self.stdout.write(self.style.WARNING(warning))
