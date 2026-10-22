"""`ActorKind.WEB`（P5 T1，#43；`docs/adr/0005-web-write-requires-staff.md`）。

这个枚举的成员是**「谁干的」这一栏的词汇**，它不在任何一张表里独存：七处
`actor_kind` / `录入方类别` / `撤回方类别` 字段各自把 `ActorKind.choices()` 冻进了自己的
`choices`，而 `choices` 只是校验面。所以加一个成员要动的东西比「加一行枚举」多，且漏掉
任何一处都**不会报错**：

- 漏掉某张表的 `choices` → 那张表里写 `web` 的行**写不进去**（`full_clean` 才拦，
  `objects.create` 不拦），而写不进去的表现是 `IntegrityError` 或静默落成脏值；
- 漏掉 `_ACTOR_KIND_DISPLAY` → `ActorKind.WEB.display` 抛 `KeyError`，只在那条流水
  第一次被渲染时才炸（确认页的「上一次退回 Shadow 是谁干的」正好是它）；
- 漏掉迁移 → 库里的 `choices` 与代码不一致，`makemigrations --check` 红。

三条都钉，且第三条钉的是**「一次迁移改完全部七处」**——分成两次迁移的话，中间那一刻
库里有「一半表认识 web、一半不认识」，而那是个没有测试会经过的状态。
"""

from __future__ import annotations

import importlib.util

from django.apps import apps
from django.test import SimpleTestCase

from apps.regime.models import (
    ActorKind,
    HaltDeclaration,
    MajorEventChange,
    RegimeMechanismSwitch,
    RegimePoolRebuild,
    RegimeReducePause,
    RegimeTruthInterval,
)

#: 带 `ActorKind` choices 的字段，`(模型, 字段名)`。**这张清单是断言的锚**：新增一处
#: `actor_kind` 字段而忘了加进来时，下面那条 `test_every_actor_kind_field_is_listed`
#: 会红——否则新字段会悄悄不在这份测试的覆盖里。
ACTOR_KIND_FIELDS = [
    (HaltDeclaration, "actor_kind"),
    (MajorEventChange, "actor_kind"),
    (RegimeMechanismSwitch, "actor_kind"),
    (RegimePoolRebuild, "actor_kind"),
    (RegimeReducePause, "actor_kind"),
    (RegimeTruthInterval, "actor_kind"),
    (RegimeTruthInterval, "retracted_by_kind"),
]


class TestWebIsAMember(SimpleTestCase):
    """枚举本身：取值、display、以及它与 `CLI` 不合并。"""

    def test_web_value_is_the_literal_web(self):
        """字面值是**库里的数据**：迁移、既有行、前端判断都按这个串走。改它等于改数据。"""
        assert ActorKind.WEB.value == "web"

    def test_web_display_is_backend_rendered(self):
        """`CONTEXT.md`「渲染分界」：枚举的 display 由后端给，前端不许自己拼一遍。"""
        assert ActorKind.WEB.display == "网页"

    def test_every_member_has_a_display(self):
        """漏一个成员，`display` 会抛 `KeyError`——而它只在**渲染那一条流水时**才炸。"""
        for member in ActorKind:
            with self.subTest(member=member.name):
                assert isinstance(member.display, str) and member.display

    def test_choices_covers_every_member_including_web(self):
        values = {value for value, _ in ActorKind.choices()}
        assert values == {member.value for member in ActorKind}
        assert "web" in values

    def test_web_is_not_merged_into_cli(self):
        """ADR 0005 与 `CONTEXT.md` 都写着「网页登录用户是第四种身份口径，塞进 `CLI`
        会让『谁干的』这一栏混进『命令行敲的』与『网页点的』两种」。合并的话这条会红。"""
        assert ActorKind.WEB is not ActorKind.CLI
        assert ActorKind.WEB.value != ActorKind.CLI.value


class TestEveryActorKindFieldKnowsWeb(SimpleTestCase):
    """七处字段的 `choices` 都必须认识 `web`。

    `choices` 是**校验面**：它不进数据库约束，所以「某张表不认识 web」不会在写入时被
    拦下（`objects.create` 绕得过），只会让 `full_clean` 与表单路径拒绝它——一个只在
    某条入口才出现的失败。
    """

    def test_every_actor_kind_field_is_listed(self):
        """锚：`ACTOR_KIND_FIELDS` 必须与模型上真实的 `choices=ActorKind.choices()`
        字段集合相等。新增一处而忘了登记时，这条红。"""
        found = set()
        for model in apps.get_app_config("regime").get_models():
            for field in model._meta.get_fields():
                choices = getattr(field, "choices", None)
                if not choices:
                    continue
                values = {value for value, _ in choices}
                # 认「这个字段的 choices 就是 ActorKind 那套词汇」：成员集完全覆盖
                # ActorKind 才算。别的枚举（`MechanismMode` 等）不会被误收。
                if {member.value for member in ActorKind} <= values:
                    found.add((model.__name__, field.name))
        expected = {(model.__name__, name) for model, name in ACTOR_KIND_FIELDS}
        assert found == expected

    def test_every_listed_field_accepts_web(self):
        for model, name in ACTOR_KIND_FIELDS:
            with self.subTest(model=model.__name__, field=name):
                choices = dict(model._meta.get_field(name).choices)
                assert choices["web"] == "网页"

    def test_the_field_width_fits_the_value(self):
        """`web` 只有 3 个字符，`max_length=16` 富余——这条钉的是「别为了省事改成
        `max_length=4`」那种顺手改动，它会在写入时把更长的既有取值截断。"""
        for model, name in ACTOR_KIND_FIELDS:
            with self.subTest(model=model.__name__, field=name):
                assert model._meta.get_field(name).max_length >= len(
                    ActorKind.WEB.value
                )


class TestTheMigrationIsOnePass(SimpleTestCase):
    """一次迁移改完全部七处（票面「**一次迁移**，不是两次」）。"""

    @staticmethod
    def _migrations_dir():
        """`apps/regime/migrations/`。

        用 `importlib.util.find_spec(...).origin` 而不是 `模块.__file__`：后者在类型上是
        `str | None`（命名空间包里没有 `__file__`），而 `Path(None)` 是运行期的 `TypeError`。
        `find_spec` 的 `origin` 同样是 `str | None`，所以断言一次——真为 `None` 时测试
        要**当场说清楚**，而不是抛一个 `TypeError` 让人去猜。
        """
        from pathlib import Path

        spec = importlib.util.find_spec("apps.regime")
        assert spec is not None and spec.origin is not None
        return Path(spec.origin).parent / "migrations"

    def test_0015_is_the_only_migration_that_knows_web(self):
        """`web` 只该在 `0015_*` 里出现。它出现在更早的迁移里说明有人重写了历史；
        出现在更晚的迁移里说明这次是分两次落的——中间那一刻库里有「一半表认识 web、
        一半不认识」，而那是个没有测试会经过的状态。"""
        knowing = sorted(
            p.name
            for p in self._migrations_dir().glob("[0-9]*.py")
            if '("web"' in p.read_text("utf-8")
        )
        assert knowing == ["0015_alter_haltdeclaration_actor_kind_and_more.py"]

    def test_0015_touches_all_seven_fields(self):
        """一条迁移里七处 `AlterField`：这是「一次迁移」的字面形态。少一处就会有一条
        路径认识 `web`、另一条不认识。"""
        source = (
            self._migrations_dir() / "0015_alter_haltdeclaration_actor_kind_and_more.py"
        ).read_text("utf-8")
        assert source.count("migrations.AlterField(") == len(ACTOR_KIND_FIELDS)
