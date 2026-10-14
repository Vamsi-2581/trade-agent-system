"""幽灵策略清理（第①段单元 7 收尾）。

回测路径会 `get_or_create` 策略行（`apps/backtest/tasks.py`、`apps/strategy_engine/
backtest_mode.py`），于是库里攒下一批**没有实现文件**的行。CONTEXT.md 第 105 条要求先清理
它们：「带着幽灵行会让日报变成几十条「未知」」。同一条还定死了两个动作与一条纪律：

- 无实现文件 **且** 从未被会话/回测引用 → **删除**；
- 有历史引用 → **保留但停用**（`is_active=False`）；
- 删除必须走**带 dry-run 的管理命令**（默认只打印待删清单，`--apply` 才真删），**不走数据
  迁移**——迁移会在部署时自动执行、无人复核，而这是不可逆地删掉几十行。

## 本命令只做 `True → False` 这一个方向

`is_active` 是人工总开关，`False` 即「已退役」（CONTEXT.md 第 106 条），语义里明写「永不被
自动恢复」——所以本命令**永不**把任何一行置回 `True`。存量行的 `True` 由迁移
`trading/0006_backfill_strategy_is_active` 一次性回填；两者是配套的：迁移负责「在册的都还没
被人做过决定」，本命令负责「幽灵该退役」。哪天有人想在清理里顺手把「看起来还活着」的策略
激活，那是在替人做决定。

## 「有引用吗」不抄清单，走模型的反射

判据写的是「会话/回测」，但**删除的安全性取决于有没有任何一张表指着它**，而这两者不是一回事：

- `LiveSession.strategy` 是 `PROTECT`——有引用时 `delete()` 直接抛 `ProtectedError`，那还算是
  一个响亮的失败；
- `BacktestResult.strategy`、`GridSearchJob.strategy`、`RegimePoolCell.strategy` 都是
  `CASCADE`——有引用时 `delete()` **静默带走**那些行。回测数据是这个仓库里更贵的资产
  （CONTEXT.md 语），池化格是池化表的正文。

所以这里用 `Strategy._meta.related_objects` 现算全部反向关系，而不是手写一张「LiveSession +
BacktestResult」的清单：手写的那张在写下它的当天就漏掉了 `GridSearchJob` 与 `RegimePoolCell`，
而漏掉的代价是静默级联删除。**新增一张指向 `Strategy` 的表，本命令自动把它算进来**——这正是
反射唯一要买的东西。

反射本身也有一个坑，就写在 `reference_counts` 里：`OneToOneField` 的 `many_to_one` 是
`False`（Django 的 `OneToOneRel` 就是这么定义的），只按 `many_to_one` 过滤会漏掉
`ArchetypeOverride` 那张表——它同样是 `CASCADE`，同样会静默带走。

于是判据落成「**零引用**才删」：比 CONTEXT.md 那两句的并集更保守一档，而保守的方向是安全的
（多留一行 ≠ 多删一行）。

## 注册表为空时必须挡住 `--apply`

`unresolved` 的判据来自策略注册表，而注册表要在 `apps.ready()` 里 discover
`~/.tradelogx/strategies`——那个目录当时不存在就**静默跳过**（`pool_rebuild.strategy_raw_texts`
的 docstring 专门写过这个形状）。这种情况下**每一行**都会被判成幽灵，`--apply` 会把全库删掉。
所以：一行都没解析到时，dry-run 照打（那是你要看的东西），但 `--apply` 直接以 `CommandError`
收场，并把「先确认那个目录在不在」写在错误里。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.regime import pool_rebuild
from apps.strategy_engine.registry import StrategyRegistry
from apps.trading.models import Strategy


def reference_counts(strategy_ids) -> dict:
    """`{策略 id: 引用它的行数}`。零引用 = 可以删。

    反射 `Strategy._meta.related_objects` 而不是列一张表名清单——理由见模块 docstring：
    清单会漏，漏了就是静默级联删除。
    """
    ids = list(strategy_ids)
    counts = {strategy_id: 0 for strategy_id in ids}
    if not ids:
        return counts

    for relation in Strategy._meta.related_objects:
        # 只看「别的表指向我」的 FK/O2O。多对多反向对策略表不存在，真出现了也说明设计变了，
        # 那时 `--apply` 会在 PROTECT/CASCADE 上如实暴露，而不是被这里悄悄跳过。
        field = relation.field
        # 两类都要收：`ForeignKey`（`many_to_one`）与 `OneToOneField`。后者**不是**
        # `many_to_one`——Django 的 `OneToOneRel` 恰恰把它置成 `False`——但它一样的
        # `CASCADE`。`ArchetypeOverride` 就是这一档：只看 `many_to_one` 会让它被静默跳过，
        # 于是「删掉零引用的策略」顺手删掉一条人工纠正。
        if not (
            getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False)
        ):
            continue
        column = f"{field.name}_id"
        rows = relation.related_model.objects.filter(
            **{f"{field.name}__in": ids}
        ).values_list(column, flat=True)
        for strategy_id in rows:
            if strategy_id in counts:
                counts[strategy_id] += 1
    return counts


def classify(rows) -> tuple[list, list]:
    """`(策略 id, 名称)` 序列 → `(待删, 待退役)` 两份 `(策略 id, 名称, 引用数)`。

    传入顺序就是输出顺序（调用方按 `created_at, id` 排好），因为两份清单要给人看，顺序不稳
    会让两次 dry-run 的 diff 多出一堆假差异。
    """
    rows = list(rows)
    counts = reference_counts([strategy_id for strategy_id, _ in rows])
    doomed, retiring = [], []
    for strategy_id, name in rows:
        (doomed if counts[strategy_id] == 0 else retiring).append(
            (strategy_id, name, counts[strategy_id])
        )
    return doomed, retiring


class Command(BaseCommand):
    help = "清理没有实现文件的幽灵策略：零引用的删除，有引用的保留并停用（默认只打印）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="真的执行（删除 + 停用）。不加则只打印清单，一行不写",
        )

    def handle(self, *args, **options):
        # 注册表是「有没有实现文件」的唯一出处，而 `apps.ready()` 只在目录当时存在时才
        # discover。不补一次，整库都会被判成幽灵。
        pool_rebuild.ensure_strategies_discovered()

        rows = list(
            Strategy.objects.order_by("created_at", "id").values_list("id", "name")
        )
        if not rows:
            self.stdout.write("库里没有任何策略行，无需清理")
            return

        # `strategy_raw_texts` 的第二个返回值就是「解析不到实现类」的那批——判据一。复用
        # 它而不是自己问一遍注册表：两处各问一次，迟早会漂。
        _, unresolved = pool_rebuild.strategy_raw_texts([row[0] for row in rows])
        unresolved_set = set(unresolved)

        if not unresolved_set:
            self.stdout.write(f"策略共 {len(rows)} 条，全部能解析到实现类，没有幽灵行")
            return

        # 保持 `rows` 的顺序，只留下幽灵那一批。
        ghosts = [row for row in rows if row[0] in unresolved_set]
        doomed, retiring = classify(ghosts)

        self.stdout.write(
            f"策略共 {len(rows)} 条，其中没有实现文件（幽灵）{len(ghosts)} 条："
            f"零引用待删 {len(doomed)}，有引用待退役 {len(retiring)}"
        )

        # 注册表**一个实现类都没发现** = 它没加载起来（`~/.tradelogx/strategies` 不在），不是
        # 「全库都是幽灵」。这两种情况的清单长得一模一样，所以必须吼出来，而且下面还要挡住
        # `--apply`。判据取注册表自身、不取「本库里一条都没解析到」——后者在「库里的行恰好都
        # 是幽灵、注册表其实好好的」时也成立，那是一个合法且该被执行的清单。
        registry_empty = not StrategyRegistry.list_registered()
        if registry_empty:
            self.stderr.write(
                self.style.ERROR(
                    "一个实现类都没解析到——这不是「全库都是幽灵」，"
                    "而是策略注册表没加载起来（多半是 ~/.tradelogx/strategies 不在）。"
                    "下面的清单不可信。"
                )
            )

        self._print_list("待删（零引用，删除不可逆）", doomed)
        self._print_list("待退役（有历史引用，保留并置 is_active=False）", retiring)

        if not options["apply"]:
            self.stdout.write("以上是 dry-run，一行未写。确认无误后加 --apply 执行。")
            return

        if registry_empty:
            raise CommandError(
                "注册表一行都没解析到，拒绝执行删除。先确认 ~/.tradelogx/strategies 存在"
                "（容器内路径），再重跑；清单本身由 dry-run 打印，没有丢。"
            )

        self._apply(doomed, retiring)

    # -- 输出 -------------------------------------------------------------- #

    def _print_list(self, title: str, items: list) -> None:
        if not items:
            return
        self.stdout.write(f"{title}：{len(items)} 条")
        for strategy_id, name, refs in items:
            suffix = f"（引用 {refs} 行）" if refs else ""
            self.stdout.write(f"  {strategy_id}  {name}{suffix}")

    # -- 执行 -------------------------------------------------------------- #

    def _apply(self, doomed: list, retiring: list) -> None:
        """两个动作**一个事务**：清单是同一次读出来的，半个清单生效会让下一次 dry-run 与
        这一次对不上。退役只是 `update`，删除走 `delete()`——零引用已经保证不会级联带走
        任何东西（`reference_counts`）。
        """
        with transaction.atomic():
            if retiring:
                # 只碰幽灵那批 id，不写 `True`（见模块 docstring）。
                Strategy.objects.filter(
                    id__in=[strategy_id for strategy_id, _, _ in retiring]
                ).update(is_active=False)
            deleted = 0
            if doomed:
                deleted, _ = Strategy.objects.filter(
                    id__in=[strategy_id for strategy_id, _, _ in doomed]
                ).delete()

        if retiring:
            self.stdout.write(f"已退役 {len(retiring)} 条（is_active=False）")
        if doomed:
            # `delete()` 的返回值是 `(总行数, {模型: 行数})`；这里零引用，所以总行数应当等于
            # 待删条数。不等就是有级联发生——那意味着 `reference_counts` 漏了东西，必须看见。
            self.stdout.write(f"已删除 {len(doomed)} 条（实际影响 {deleted} 行）")
            if deleted != len(doomed):
                self.stderr.write(
                    self.style.WARNING(
                        f"删除影响行数 {deleted} ≠ 待删条数 {len(doomed)}："
                        "有级联删除发生，请检查是否有新表指向 Strategy 而 reference_counts 漏了"
                    )
                )
