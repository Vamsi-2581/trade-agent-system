"""切片全量重算入口（第①段单元 6ii）。

CONTEXT.md 要求切片「必须有全量重算入口」。这个命令就是它：判定算法调整、日线回填、
证据门槛改了之后，用它把存量回测的切片补齐/刷新。

**「全量」读作「把全部结果过一遍」，不是「无条件全部重写」。** 默认只重算三种结果：
没有切片的、标签指纹变了的、门槛变了的（判据是 `slicing.is_stale`，三条一起看）。
原因：`computed_at` 天天在变，而「算得比现在早」不是陈旧——拿它当陈旧会让这个入口
每次都是全表重写，而它存在的意义恰恰是「只补该补的那些」。需要无条件重写时用 `--force`。

标签在本命令里**只算一次**（全市场共用一份），批量重算的全部收益就在这：逐个结果各调
一次 `load_tags()` 会把一次 `label_series` 变成一千次。

它的定位是**同步**入口：在管理机上跑完就完了，不投递任务。所以长循环里的连接纪律
（`close_old_connections()`）必须自带——见 CLAUDE.md。

**自带的方式必须在函数内导入它。** 一行模块级的 `from django.db import close_old_connections`
会把名字绑死在本模块的命名空间上，于是测试里打桩 `django.db.close_old_connections` 只改了
定义处、改不到这里——真跑一次就掐掉 `TestCase` 那条测试事务的连接（`CONN_MAX_AGE=0` ⇒
`close_at` 就是连接建立的时刻，测试事务里 autocommit 恒为 False），之后每一条 ORM 都是
`InterfaceError: connection already closed`。这个坑的隐蔽之处在于它**取决于导入顺序**：
哪个测试模块先碰到本命令，决定了这次绑定拿到的是真函数还是桩。

幂等：反复运行不产生副作用，第二次跑会把上一次的全判成「不陈旧」并跳过。

## 池化表重算并入这里（单元 7）

切片跑完接着重建池化表，不另开命令（CONTEXT.md）。两段的关系是**串联而不是并列**：
池化读的就是切片落下的载荷，所以它必须排在后面，而且在同一个进程里排在这里就不必
反序列化第二遍。

池化的范围参数一律**不适用**——它是全表的（`pool_rebuild` 收全部回测结果），
`--result` / `--strategy` / `--limit` 只收窄切片那一段。`--dry-run` 下两段都不写。

切片失败**不阻止**池化：样本收集天生容忍缺口（没有切片的计数进 `exclusions`，落在那
一代上），而让一条坏数据否决整张适用性表，比让那张表说明「这一轮少看了几个回测」更坏。
失败仍然以非零退出码收场，并且会额外说一句这次的一代是按**部分刷新**的输入建的。

## 重算差异重放挂在池化之后（单元 7 收尾）

新的一代翻成 `ready` 之后，对每一条生效中的停用决策重放一遍新表，并把「依据已随重算
失效」摆到人面前（CONTEXT.md 第 118 条）。它排在这里的理由与池化一样是**串联**：重放
读的就是刚翻出来的那一代。

复用与重复触发那两种收场都**不重放**——没有新表可重放（见 `replay_run` 的模块
docstring）。`--dry-run` 更不会：上面那道门已经把它挡在池化之前了。

**告警由本命令发**（`_notify_invalidated`），不由重放那一层发。

## 资金效率重算并进同一条命令（spec #30 D8；子票 #35）

存量回测的 `metrics["capital_efficiency"]` 也在这一条命令里刷新，**串行**排在切片同一个
循环里（同 P2 池化重算先例：一个入口、一个进程、不另开命令）。它不改上面任何一段的
顺序与语义：

- **排在切片之后、池化之前**。理由与切片→池化同源：池化读的是切片载荷，而资金效率
  不是它的输入，所以它插在中间既不提前也不推后任何依赖。放在循环里与切片并排跑，
  是为了**共用同一次 `.defer()` 的扫描**——`HEAVY_COLUMNS` 那四列是同一个坑
  （每行 600 KB，`--all` 4067 行 ≈ 2.4 GB），分成两趟扫就是把这个坑踩两遍。
- **同一套 `.defer(*HEAVY_COLUMNS)` 与 `--limit` / `--result` / `--strategy` 收窄**：
  范围参数对它是适用的（它逐结果算，与切片同粒度），与池化那段不同。
- **失败互相独立**：切片失败与资金效率失败各自计数、各自进 stderr，任一条都不中断整批，
  批末统一以非零退出码收场。一条口径坏了不该让另一条也停摆——它们读的是同一张表，
  但算的是两个毫不相干的附加产物（用户故事 22 要求两套数字**能**分别修）。
- **`--dry-run` 两段都不写**：资金效率那一段也只报「会重算什么」。
- **陈旧判据是 `capital_efficiency_slicing.is_stale`**（版本 + 门槛 + 窗口），
  **不比 `computed_at`**——理由与切片那一条逐字相同，见上面「全量」那一节。
- `--force` 同时管两段：它说的是一句关于「写不写」的话，不是关于某一个载荷的话。

**它为什么读本命令的 `--actor`**：不读。资金效率不落任何记录表（D9：零新增表/列），
没有「谁让它重算的」这个问题可答；`--actor` 继续只作用于池化那一代。
"""

from __future__ import annotations

import getpass

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.backtest.models import BacktestResult
from apps.regime import pool_rebuild, replay, replay_run, slicing
from apps.regime.models import ActorKind, ReviewVerdict
from apps.strategy_factory import capital_efficiency_slicing
from apps.trading import alerts

#: 每多少条断一次旧连接。与 `run_grid_search_task` 的逐组合清理同一个意图。
BATCH_SIZE = 50

#: `BacktestResult` 上四个又大又用不到的列。切片只读 `metrics` / `start_date` /
#: `end_date` / `initial_capital` / `strategy`，以及另表的 `trades`——这四列一个都不读。
#:
#: **`--all` 跑不跑得动就压在这一行上**：实测每行带上它们的开销约 600 KB，4067 行一次
#: 拉进内存就是 ~2.4 GB，超过容器 2 GiB 的上限，进程卡在 cgroup 回收里不动（现象是
#: 「挂了」，实测 283 秒才吐出第一行，整轮永远跑不完）。defer 之后同一批 4067 行的
#: `is_stale` 扫描是 0.3 秒、峰值 167 MB。
#:
#: 想加列进来之前先确认没人读它——`save_slice` 是另起一条 `.only("id", "metrics")` 的
#: 查询写的，所以 defer 不影响落库。
HEAVY_COLUMNS = ("equity_curve", "drawdown_curve", "ohlcv_data", "indicator_data")


class Command(BaseCommand):
    help = (
        "重算存量回测的行情阶段切片（metrics.regime_slice）与资金效率"
        "（metrics.capital_efficiency），并重建池化表；幂等，按需重算"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="全部回测结果（与 --result / --strategy 至少给一个）",
        )
        parser.add_argument(
            "--result",
            action="append",
            default=[],
            metavar="ID",
            help="指定回测结果 id，可重复",
        )
        parser.add_argument(
            "--strategy",
            action="append",
            default=[],
            metavar="NAME",
            help="指定策略名（`Strategy.name` 精确匹配），可重复",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="最多处理多少个结果，0 = 不限",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="无条件重算，不做陈旧判定",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只报会重算哪些，不写库（池化那一段也不写）",
        )
        parser.add_argument(
            "--actor",
            default="",
            metavar="NAME",
            help="记进重算记录的触发人，默认取当前系统用户（Q8）",
        )

    def handle(self, *args, **options):
        if not (options["all"] or options["result"] or options["strategy"]):
            raise CommandError("至少给一个范围：--all / --result / --strategy")

        queryset = self._select(options)
        limit = options["limit"]
        if limit > 0:
            queryset = queryset[:limit]

        # 标签算一次，全批共用。空标签（日线还没回填）不是错误：切片会照落、四格全
        # unknown，那是可读的结论；但要吼一声，否则「全 unknown」看起来像算法坏了。
        tags = slicing.load_tags()
        if not tags:
            self.stderr.write(
                self.style.WARNING("当前没有任何已判定日线，切片将全部为 unknown")
            )

        force = options["force"]
        dry_run = options["dry_run"]
        recomputed = 0
        fresh = 0
        failed: list[str] = []
        eff_recomputed = 0
        eff_fresh = 0
        eff_failed: list[str] = []

        # 函数内导入，**不是**模块顶部（与 `apps/regime/tasks.py` 同一写法）：模块级
        # `from django.db import close_old_connections` 会把名字绑死在本模块上，此后
        # 打桩 `django.db.close_old_connections` 就到不了这里——而真跑一次会掐掉
        # `TestCase` 事务那条连接。理由详见模块 docstring。
        from django.db import close_old_connections

        for index, result in enumerate(queryset.iterator(chunk_size=BATCH_SIZE)):
            if index % BATCH_SIZE == 0:
                close_old_connections()

            # -- 切片 -------------------------------------------------------- #
            if not force and not slicing.is_stale(result, tags):
                fresh += 1
            elif dry_run:
                self.stdout.write(
                    f"[dry-run] 会重算切片 {result.id} {self._label(result)}"
                )
                recomputed += 1
            else:
                try:
                    slicing.compute_slice(result, tags)
                    recomputed += 1
                except Exception as e:
                    # 单条失败不中断整批：坏的那一条不该挡住其余几百条。批末以非零退出码结束。
                    failed.append(str(result.id))
                    self.stderr.write(
                        self.style.ERROR(
                            f"切片失败 {result.id} {self._label(result)}：{e}"
                        )
                    )

            # -- 资金效率（#35） --------------------------------------------- #
            # 与切片并排而不是另起一圈：两段共用同一次 `.defer()` 扫描（见模块 docstring
            # 「资金效率重算并进同一条命令」）。两条各自 try/except、各自计数——一条口径
            # 坏了不该让另一条也停摆。
            if not force and not capital_efficiency_slicing.is_stale(result):
                eff_fresh += 1
            elif dry_run:
                self.stdout.write(
                    f"[dry-run] 会重算资金效率 {result.id} {self._label(result)}"
                )
                eff_recomputed += 1
            else:
                try:
                    capital_efficiency_slicing.compute_capital_efficiency_for(result)
                    eff_recomputed += 1
                except Exception as e:
                    eff_failed.append(str(result.id))
                    self.stderr.write(
                        self.style.ERROR(
                            f"资金效率失败 {result.id} {self._label(result)}：{e}"
                        )
                    )

        self.stdout.write(
            f"切片重算：{'待' if dry_run else ''}重算 {recomputed}，"
            f"跳过（不陈旧）{fresh}，失败 {len(failed)}"
        )
        self.stdout.write(
            f"资金效率重算：{'待' if dry_run else ''}重算 {eff_recomputed}，"
            f"跳过（不陈旧）{eff_fresh}，失败 {len(eff_failed)}"
        )

        self._rebuild_pool(options, failed)

        if failed or eff_failed:
            # 一条退出码管两段：两段都是「这一轮该刷的没刷完」，而它们的补跑方式是同一条
            # 命令。分开报在 stdout/stderr 上，退出码只有一个。
            raise CommandError(
                f"{len(failed)} 条切片失败、{len(eff_failed)} 条资金效率失败，详见上面的 stderr"
            )

    # -- 池化那一段 ------------------------------------------------------ #

    def _rebuild_pool(self, options, failed: list[str]) -> None:
        """切片跑完接着重建池化表。**同步**，与切片同一段连接纪律之外（一次查询 + 几批写入）。

        `failed` 只数**切片**那一档：池化读的是切片载荷，「这一代是按部分刷新的输入建的」
        这句话说的只是切片。#35 的资金效率不是池化的输入，它的失败有自己的一行报告。
        """
        if options["dry_run"]:
            self.stdout.write(
                "[dry-run] 会全表重建池化表（范围参数只作用于切片那一段）"
            )
            return

        # 注册表是原型归属的唯一出处，而 `apps.ready()` 只在目录当时存在时才 discover。
        # 这里补一次，否则整批策略会安静地全落到「未归类」。
        pool_rebuild.ensure_strategies_discovered()

        # 范围参数对这一段**不适用**（见模块 docstring）。无条件说出来，不按「这次有没有
        # 给范围参数」来判：一个有条件的提示会让人以为「没说就是收窄了」。
        self.stdout.write("池化：全表重建（范围参数只作用于切片那一段）")

        actor = options["actor"] or getpass.getuser()
        summary = pool_rebuild.rebuild_pool(
            actor_kind=ActorKind.CLI.value, actor_name=actor
        )

        if summary["reused"]:
            # 复用的代价是这一轮没有记录，所以这句必须说出来（见 pool_rebuild 的模块 docstring）。
            self.stdout.write(
                f"池化：输入指纹未变，复用第 {summary['rebuild_id']} 代，未落新记录"
            )
        elif summary["duplicate"]:
            # 不是故障：同一指纹已有一代就绪，那条部分唯一约束按设计拦下了这一次。
            self.stdout.write(
                self.style.WARNING(
                    f"池化：同一输入指纹已有一代就绪（另一进程先一步完成），"
                    f"本次未替换当前代；这一次留下了 #{summary['rebuild_id']} 这条失败记录"
                )
            )
        else:
            self.stdout.write(
                f"池化：新一代 #{summary['rebuild_id']}，候选 {summary['candidates']}，"
                f"采用 {summary['results_used']}，格子 {summary['cells_total']}，"
                f"变化 {summary['cells_changed']}"
            )

        # 排除按切片数、按原因分开报：不报的话「采用 < 候选」这个差就没有出口。
        excluded = {k: v for k, v in summary["exclusions"].items() if v}
        if excluded:
            self.stdout.write(
                "池化排除："
                + "，".join(f"{k} {v}" for k, v in sorted(excluded.items()))
            )

        if summary["unclassified"]:
            self.stdout.write(
                self.style.WARNING(
                    f"未归类策略 {len(summary['unclassified'])} 条"
                    f"（{summary['unclassified'][:5]}）：走的是原型兜底，"
                    "原因多半是描述里没有可识别的原型词，或注册表没发现实现类"
                )
            )
        if summary["needs_review"]:
            self.stdout.write(
                self.style.WARNING(
                    f"待人工复核的格子 {summary['needs_review']} 个（方向冲突，不产生停用建议）"
                )
            )
        if failed:
            self.stdout.write(
                self.style.WARNING(
                    f"注意：{len(failed)} 条切片失败，这一代池化表是按部分刷新的输入建的"
                )
            )

        if summary["reused"] or summary["duplicate"]:
            # 两道门都不重放，理由同一个：**没有新表可重放**。复用那一轮指纹没变 ⇒ 结论
            # 逐格相同 ⇒ 重放出来的结论必然与上次逐条相同，落下去只是噪声；重复触发那一轮
            # 没替换当前代，也就没有新表。见 replay_run 的模块 docstring。
            return

        self._replay_decisions()

    # -- 重算差异重放（单元 7 收尾） ---------------------------------------- #

    def _replay_decisions(self) -> None:
        """新一代翻完之后，对生效中的停用决策重放一遍新表（CONTEXT.md 第 118 条）。

        「重算不自动恢复任何停用决策」是这份设计的地基，所以这里的产出**只有告知**：
        一条差异记录 + 一句给人看的话。恢复要走人工豁免命令。
        """
        summary = replay_run.run_replay()

        if summary["skipped"]:
            self.stdout.write(f"重放：{summary['note']}")
            return
        if not summary["written"]:
            self.stdout.write(
                f"重放：第 {summary['generation_id']} 代，生效中的决策 "
                f"{summary['decisions']} 条（其中 {summary['already']} 条这一代已评过），"
                "本轮没有新的重放"
            )
            return

        verdicts = summary["verdicts"]
        self.stdout.write(
            f"重放：第 {summary['generation_id']} 代，重放 {summary['written']} 条，"
            f"仍判不适用 {verdicts[ReviewVerdict.STILL_UNFIT.value]}，"
            f"仍判中性 {verdicts[ReviewVerdict.STILL_NEUTRAL.value]}，"
            f"依据失效 {verdicts[ReviewVerdict.BECAME_FIT.value]}，"
            f"与上次结论不同 {summary['changed']}"
        )
        self._notify_invalidated(summary["invalidated"])

    def _notify_invalidated(self, invalidated: list[dict]) -> None:
        """把「依据已随重算失效」告诉受影响的人。

        **告警在这里发，不在 `replay_run` 里发**：`apps/regime/tasks.py` 的纪律是「任务
        自己不写告警，失败往上抛」，重放那一层同理——它只算差异，投递属于入口。

        触发者就站在 stdout 前面，所以**没有收件人不等于没人看见**——但这句提示必须打
        出来，否则一次没送出去的告警与一次送出去的告警长得一模一样。CONTEXT.md 说的是
        「告知触发者与告知受影响用户是同一件事」，这里两句都做到。
        """
        if not invalidated:
            return

        message = replay.format_alert(invalidated)
        self.stdout.write(message)

        recipients = replay_run.alert_recipients(
            [item["strategy_id"] for item in invalidated]
        )
        if not recipients:
            self.stdout.write(
                self.style.WARNING(
                    f"重放：{len(invalidated)} 条依据失效，但没有活跃实盘会话引用这些策略，"
                    "没有可通知的收件人（上面那条清单即全部告知）"
                )
            )
            return

        delivered = 0
        for user_id in recipients:
            try:
                # `notify_user` 是 async，这里是同步的管理命令——用 `async_to_sync` 过桥，
                # 与 `apps/trading/views.py` 的用法一致。
                if async_to_sync(alerts.notify_user)(user_id, message):
                    delivered += 1
                else:
                    self.stderr.write(
                        self.style.ERROR(f"重放告警未送达 user={user_id}（无接收人）")
                    )
            except Exception as e:  # noqa: BLE001 - 通知失败不得影响重算结果本身
                self.stderr.write(
                    self.style.ERROR(f"重放告警投递异常 user={user_id}：{e}")
                )

        # 一次重放**只发一次**，不做每日节流：重算是人触发的低频动作（CONTEXT.md），
        # `daily_snapshot` 那套「同一人同一天只告警一次」是给 5 分钟一轮的循环用的。
        if delivered:
            self.stdout.write(f"重放：已通知 {delivered} 人")

    def _select(self, options):
        """按范围参数拼查询集。三个参数是**并集**，不互相收窄。

        一律 defer 掉 `HEAVY_COLUMNS`（见那份常量的注释）：两条分支都要，所以加在
        `objects` 这一层，而不是各自补一次。

        切片与资金效率**共用这一份查询集**（同一次扫描跑两段）。资金效率读
        `start_date` / `end_date` / `initial_capital` 与另表的 `trades`——那四列一个都
        不在 `HEAVY_COLUMNS` 里，所以它的加入不改变这一行的必要性。想往
        `HEAVY_COLUMNS` 里加列之前先确认没人读它，想从扫描里**去掉**这几列之前先确认
        这两段都不读它。
        """
        queryset = BacktestResult.objects.defer(*HEAVY_COLUMNS)
        if options["all"]:
            return queryset.order_by("created_at")

        condition = Q()
        if options["result"]:
            condition |= Q(id__in=options["result"])
        if options["strategy"]:
            condition |= Q(strategy__name__in=options["strategy"])
        return queryset.filter(condition).order_by("created_at")

    @staticmethod
    def _label(result) -> str:
        return (
            f"{result.symbol} {result.timeframe} {result.start_date}~{result.end_date}"
        )
