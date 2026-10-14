"""事件熔断开关的管理命令（第②段单元 ②f 的运维兜底入口）。

`/regime` 是主入口（CONTEXT.md 第152 条：人工维护一律走 slash 命令，不引入 Django
admin），但那条路要经过 Telegram。**打开这个开关是「机制开始自动对市场动手」的闸门**，
它不能只有一个依赖外网的入口——聊天通路不回消息的时候，人仍然要能看一眼确认页、并且
亲手把它打开或关掉。

所以这里只做一件事：用与 `/regime` **完全相同**的两个函数（`breaker_switch.page` 与
`breaker_switch.flip_event_breaker`）做同一件事。本模块自己不查库、不渲染、不做判断，
免得「命令行的说法」与「聊天里的说法」分家——那正是第②f 段 Q8 要求同源的道理。

    python manage.py event_breaker                # 只看确认页，什么都不改
    python manage.py event_breaker --on           # 打开（先回显确认页，再落流水）
    python manage.py event_breaker --off          # 关掉
    python manage.py event_breaker --on --actor 张三

不带任何动作参数时是**只读**的（与 `manage_deactivation_exemptions` 一样，跑完动作总是
把当前状态打出来）。

命名：本命令**不带 `manage_` 前缀**（区别于 `manage_deactivation_exemptions`）。那条命令
的宾语是「在期豁免」这张表，名字需要说清它管的是哪张表；这条命令的宾语就是机制本身，
命令词与 `MechanismKind.EVENT_BREAKER` 的那个值同名，敲 `/regime` 的人与敲 CLI 的人看到
的是同一个词。
"""

from __future__ import annotations

import getpass

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.regime import breaker_switch, events
from apps.regime.models import ActorKind, MechanismMode


class Command(BaseCommand):
    help = "事件熔断开关：上线确认页与人工切换（第②段 ②f）"

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group()
        action.add_argument(
            "--on", action="store_true", help="打开事件熔断（切到执行态）"
        )
        action.add_argument(
            "--off", action="store_true", help="关掉事件熔断（回到 Shadow）"
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

        # 先回显确认页再动手：与 `/regime on` 同序。反过来打的话，页面上那句「这一步本身
        # 不改变任何东西」会印在一次已经发生的改动之后，读的人分不出前后。
        briefing = breaker_switch.page(now=now)
        self.stdout.write(briefing.body)
        self.stdout.write("")

        if not options["on"] and not options["off"]:
            self.stdout.write(
                "（没有动作参数：以上是当前状态。--on 打开 / --off 关掉）"
            )
            return

        # 关闭方向的 `reason` 问的是另一件事（「关掉的那一刻放掉了什么」），所以换一份；
        # 但快照仍是上面那一份——`closing_summary` 收的正是这个 `data`。
        to_mode = MechanismMode.EXECUTING if options["on"] else MechanismMode.SHADOW
        reason = (
            briefing.summary
            if to_mode is MechanismMode.EXECUTING
            else breaker_switch.closing_summary(briefing.data)
        )

        row = breaker_switch.flip_event_breaker(
            to_mode,
            actor_kind=ActorKind.CLI,
            actor_name=actor,
            reason=reason,
            now=now,
        )
        if row is None:
            self.stdout.write(
                self.style.WARNING(
                    f"事件熔断本来就是{to_mode.display}，没有写第二条流水。"
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"事件熔断已切换：{MechanismMode(row.from_mode).display} → "
                f"{MechanismMode(row.to_mode).display}"
                f"（{events.format_moment(row.at)}，触发方 {row.actor_name}）"
            )
        )
        if to_mode is MechanismMode.SHADOW and briefing.data.open_now is not None:
            self.stdout.write(
                self.style.WARNING("⚠️ 关掉的这一刻正压在窗口里：这个窗口不再拦人。")
            )
