"""给 `Strategy.is_active` 补上语义，并把存量策略一次性回填为 `True`。

## 为什么这一次回填**必须**存在

`is_active` 从建表起就存在，但一直是个默认全 `False` 的裸字段。CONTEXT.md 第 106 条给它
分配了真语义：**人工总开关，`False` 即「已退役」**（永不被自动停用，也永不被自动恢复）。
这两件事凑在一起就是个只在迁移瞬间成立的陷阱——给一个默认全 `False` 的字段赋予
「`False` 即退役」的语义，等于**一次性退役全库**。所以 CONTEXT.md 同一条明令
「必须同时回填现有真实策略为 `True`」，并明确「回填 `is_active=True` 那部分可以走迁移
（幂等、安全）」。

回填之前，下游是**安静地**少做一件事：`replay_run` 的选取叠了 `strategy__is_active=True`
（重放不该去问一个已经被关掉的策略），所以在存量策略全 `False` 的库上，它一条决策都选
不到——而「选不到」和「本来就没有要重放的决策」长得一模一样。

## 回填的是**全部**存量行，不是「真实策略」

CONTEXT.md 说的是「现有**真实**策略」，而这一版迁移回填全表。这不是偷懒，是因为**迁移
里判不出「真实」**：

- 判据是「能在策略注册表解析到实现类」（CONTEXT.md 第 105 条），而注册表是**运行期**
  状态——它要在 `apps.ready()` 里 discover `~/.tradelogx/strategies` 那个目录。目录当时
  不存在就静默跳过（`pool_rebuild.strategy_raw_texts` 的 docstring 专门写过这个形状）。
  于是「在迁移里问注册表」会把**目录在不在**变成**策略退没退役**，而部署机上那个目录
  恰好缺席是完全正常的。
- `code_path` 也分不出来：回测自动建出来的幽灵行与真实策略行写的是同一个形状
  （`strategies/{name}.py`）。

两个方向的失败代价也不对称。全回填、再让幽灵清理命令去退役该退役的：幽灵会**多活跃**
一阵子，看得见。只回填解析得到的那几条：一旦注册表在这台机器上没加载起来，**真实策略
被安静退役**，要等下一次有人问「为什么日报一直是空的」才会发现。

所以这里只做一件确定性的事——**当前在册的每一行都还没被人做过决定，于是都按「未退役」
计**。幽灵那两类（无引用 → 删、有引用 → 保留但退役）交给带 dry-run 的管理命令
（CONTEXT.md 第 105 条：删除不可逆，所以不走迁移）。

幂等：`filter(is_active=False).update(is_active=True)` 再跑一次改 0 行。反向是 no-op
——把回填倒回去就是重新制造那个「一次性退役全库」的陷阱。
"""

from django.db import migrations, models


def backfill(apps, schema_editor):
    Strategy = apps.get_model("trading", "Strategy")
    Strategy.objects.filter(is_active=False).update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [
        ("trading", "0005_alter_order_status"),
    ]

    operations = [
        # 先把语义写进字段本身。CONTEXT.md 第 106 条的最后一句话是「删掉字段只会让下一个
        # 人再造一个」——不写下来的语义，下一个人只会再发明一遍，而这次发明出来的默认值
        # 很可能还是 `False`。
        migrations.AlterField(
            model_name="strategy",
            name="is_active",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "人工总开关：False = 已退役，永不被自动停用、也永不被自动恢复"
                    "（CONTEXT.md 第 106 条）。但它**不是**被管策略集合的依据"
                    "（第 105 条），集合只看注册表能不能解析到实现类。"
                    "回测自动建出的策略行默认 False，需由迁移/管理命令回填。"
                ),
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
