"""手动熔断的管理命令（ADR 0001 承诺的「手动触发」入口，2026-10-12 兑现）。

这份设计里人工维护一律走 slash 命令（CONTEXT.md 第 152 条，不引入 Django admin），但那是
聊天通路的纪律；**停止机制的兜底入口不能也押在聊天通路上**——「聊天不回消息」恰恰是人们
想按下停止的时刻之一。`event_breaker` 命令为什么存在的同一个道理（①e：机制必须有依赖
外网之外的一个入口），对手动熔断重一分都不夸张：它就是那个「按下去的门闩」。

所以这里做的事：用与将来任何别的入口**完全相同**的三个函数
（`manual_halt.trip` / `manual_halt.lift` / `manual_halt.overview`）做同一件事，落点是
一行 ``HaltTrigger.MANUAL`` 声明——由 `halt.py` 现行读进停止判定，**不经 Redis、也不接回
``pre_trade_check``**（ADR 0001 已否决后者）。本模块自己不查表、不渲染、不做判断，免得
「命令行的说法」与「别的入口的说法」分家。

    python manage.py manual_halt                       # 只看当前活着的手动熔断，什么都不改
    python manage.py manual_halt --trip --reason "连环假信号"          # 全市场 24 小时
    python manage.py manual_halt --trip --symbol DOGE/USDT --reason "…"
    python manage.py manual_halt --trip --symbol X --hours 0 --reason "…"   # 到人工解除为止
    python manage.py manual_halt --lift --symbol DOGE/USDT            # 人工解除
    python manage.py manual_halt --trip --actor 张三 --reason "…"

不带任何动作参数时**只读**（与 `event_breaker`、`manage_deactivation_exemptions` 同一
纪律）：跑完动作也总是把当前状态打出来。`--trip` 缺省作用域是全市场——按下停止的人罕见
想「只停一半」，缺省取最宽的那档并回显写清楚；加重音的例外是 `--symbol / --strategy`
给了就按那条走。

命名：命令词取的就是动作本身（trip / lift 各自是 `--trip` / `--lift`），宾语「手动熔断」
与触发源的展示名（`HaltTrigger.MANUAL.display` = 「手动熔断」）和模块名（`manual_halt`）
是同一个词——敲这条命令的人、读 `query_halt` 的人与翻表的人，看到的是同一个名字。
"""

from __future__ import annotations

import getpass

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.regime import halt, manual_halt
from apps.regime.models import ActorKind


class Command(BaseCommand):
    help = "手动熔断：落一行 MANUAL 停止声明（trip），或人工解除（lift）"

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--trip", action="store_true", help="按下手动熔断（默认 24 小时失效）")
        action.add_argument("--lift", action="store_true", help="人工解除该作用域上的手动熔断")
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument("--symbol", default=None, metavar="SYM", help="作用域：只拦这个品种")
        scope.add_argument(
            "--strategy", default=None, metavar="ID", help="作用域：只拦这个策略（uuid）"
        )
        scope.add_argument(
            "--global", dest="global_scope", action="store_true", help="作用域：全市场（trip 缺省）"
        )
        parser.add_argument("--reason", default="", metavar="TEXT", help="trip 必填：为什么")
        parser.add_argument(
            "--hours",
            default=None,
            type=int,
            metavar="N",
            help="到点失效的时长（小时）；0 = 到人工解除为止。缺省 24",
        )
        parser.add_argument(
            "--actor", default="", metavar="NAME", help="触发方（默认取系统用户名）"
        )

    # ------------------------------------------------------------------ #
    # 句子拼装。**渲染全在这里**，库里的事 `manual_halt` 管——两个入口各自渲染的
    # 结果要靠「取数走同一个 overview」来对齐，而不是靠两处写得小心。
    # ------------------------------------------------------------------ #

    def _lines(self, *, now) -> list[str]:
        views = manual_halt.overview(now=now)
        lines = [f"手动熔断（此刻活着的行，共 {len(views)} 条）："]
        if not views:
            lines.append("  （无——没有任何作用域被手动熔断过且未解除）")
        for view in views:
            row = view.row
            lines.append(
                f"  [{row.id}] {halt.scope_display(row.scope)}"
                f"｜{view.expiry_line}"
                f"｜起点 {row.opened_at.isoformat()}"
                f"｜{'✅ 在拦' if view.in_force else '⏹ 已失效'}"
            )
            lines.append(f"     原因：{row.reason}（触发方 {row.actor_name}）")
        return lines

    def _scope_flags(self, options) -> tuple[str, str | None]:
        """三个作用域旗标 → (档, 值)；一个都没给时缺省全市场（命令 docstring 的那条理由）。"""
        if options["symbol"]:
            return "symbol", options["symbol"]
        if options["strategy"]:
            return "strategy", options["strategy"]
        return "global", None

    def handle(self, *args, **options):
        actor = options["actor"] or getpass.getuser()
        # 与 `manage_deactivation_exemptions` / `event_breaker` 同一取法：CLI 通路的触发方
        # 是**这台机器上的人**；留痕四件事里的「谁」必须指得到一个真人。
        kind, value = self._scope_flags(options)
        now = timezone.now()

        if options["trip"]:
            if options["hours"] is None:
                hours = manual_halt.DEFAULT_TRIP_HOURS
            elif options["hours"] < 0:
                raise CommandError("--hours 不能是负数；0 = 到人工解除为止")
            elif options["hours"] == 0:
                hours = None
            else:
                hours = options["hours"]
            try:
                scope = manual_halt.resolve_scope(kind, value)
                outcome = manual_halt.trip(
                    scope,
                    options["reason"],
                    actor_kind=ActorKind.CLI,
                    actor_name=actor,
                    hours=hours,
                    now=now,
                )
            except manual_halt.ManualHaltError as exc:
                # 「你给的东西不对」是命令的正常回复，不是故障（与 /event 同一条纪律）。
                raise CommandError(
                    f"{exc}\n\n用法：python manage.py help manual_halt"
                )
            row = outcome.row
            expiry = (
                "到人工解除为止"
                if row.expires_at is None
                else f"{row.expires_at.isoformat()} 失效"
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ 已{'改写' if outcome.rewritten else '落下'}手动熔断："
                    f"{halt.scope_display(scope)}｜{expiry}"
                )
            )
            if row.expires_at is None:
                self.stdout.write(
                    "   ⚠️ 未带时长：这条熔断会活到人工解除为止（--lift），别忘了解除。"
                )
        elif options["lift"]:
            try:
                scope = manual_halt.resolve_scope(kind, value)
                row = manual_halt.lift(
                    scope, actor_kind=ActorKind.CLI, actor_name=actor, now=now
                )
            except manual_halt.ManualHaltError as exc:
                raise CommandError(str(exc))
            if row is None:
                self.stdout.write(f"ℹ️ {halt.scope_display(scope)} 上没有活着的手动熔断可解除。")
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"🔓 已解除：{halt.scope_display(scope)}（人工解除）")
                )
        else:
            self.stdout.write("（只读——要改东西请加 --trip 或 --lift）")

        for line in self._lines(now=now):
            self.stdout.write(line)
